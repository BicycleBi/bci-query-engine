"""Full HTTP path with disposable PostgreSQL. Put initdb and pg_ctl on PATH."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from fastapi.testclient import TestClient
import psycopg
import pytest

from app import main
from test_main_auth import _encode_token

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database(monkeypatch):
    if not shutil.which("initdb") or not shutil.which("pg_ctl") or os.getuid() == 0:
        pytest.skip("Disposable PostgreSQL requires initdb and pg_ctl under a nonroot user")
    with tempfile.TemporaryDirectory(prefix="qe-pg-", dir="/tmp") as tmp:
        data = Path(tmp) / "cluster"
        subprocess.run(
            [
                "initdb",
                "-D",
                data,
                "-A",
                "trust",
                "-U",
                "fixture",
                "--no-locale",
                "--encoding=UTF8",
            ],
            check=True,
            capture_output=True,
        )
        ctl = ["pg_ctl", "-D", data, "-w", "-t", "15"]
        subprocess.run(
            [
                *ctl,
                "-l",
                str(data / "server.log"),
                "-o",
                f"-k {tmp} -c listen_addresses='' -p 56423",
                "start",
            ],
            check=True,
        )
        try:
            for prefix in ("METADATA_DB", "DATA_DB"):
                for name, value in {
                    "HOST": tmp,
                    "PORT": "56423",
                    "USER": "fixture",
                    "NAME": prefix.lower(),
                    "PASSWORD": "fixture",
                }.items():
                    monkeypatch.setenv(f"{prefix}_{name}", value)
            params = dict(host=tmp, port=56423, user="fixture")
            with psycopg.connect(**params, dbname="postgres", autocommit=True) as admin:
                admin.execute("CREATE DATABASE metadata_db")
                admin.execute("CREATE DATABASE data_db")
            with psycopg.connect(**params, dbname="metadata_db", autocommit=True) as meta:
                meta.execute("""
                    CREATE SCHEMA app; CREATE SCHEMA log;
                    CREATE TABLE security_users(user_id text, email text, active bool);
                    CREATE TABLE security_user_roles(user_id text, client_key text, role_key text, expires_at timestamptz);
                    CREATE TABLE security_group_members(user_id text, client_key text, group_key text, expires_at timestamptz);
                    CREATE TABLE security_group_roles(client_key text, group_key text, role_key text, expires_at timestamptz);
                    CREATE TABLE security_role_permissions(role_key text, resource_key text, permission_key text);
                    INSERT INTO security_users VALUES ('alice','alice@example.invalid',true);
                    INSERT INTO security_group_members VALUES ('alice','fixture','reviewers',null);
                    INSERT INTO security_group_roles VALUES ('fixture','reviewers','viewer',null);
                    INSERT INTO security_role_permissions VALUES ('viewer','artifact:fixture:summary','artifact:read'),
                      ('viewer','artifact:fixture:summary','artifact:execute');
                    CREATE TABLE app.templates(template_id uuid PRIMARY KEY, html_content text, is_active bool);
                    CREATE TABLE app.artifacts(artifact_id uuid PRIMARY KEY, client_key text, artifact_key text,
                      view_name text, delivery_mode text, display_name text, template_id uuid, active bool);
                    INSERT INTO app.templates VALUES ('00000000-0000-0000-0000-000000000001','<h1>{{ rows[0].label }}</h1>',true);
                    INSERT INTO app.artifacts VALUES ('00000000-0000-0000-0000-000000000002','fixture','summary',
                      'public.summary','web','Summary','00000000-0000-0000-0000-000000000001',true);
                    CREATE TABLE log.artifact_runs(run_id uuid, artifact_id uuid, artifact_key text, client_key text,
                      triggered_by text,status text,delivery_mode text,row_count int,slice_count int,recipient_count int,
                      started_at timestamptz,completed_at timestamptz,error_message text);
                """)
                meta.execute((ROOT / "sql/006_artifact_outputs.sql").read_text())
                meta.execute((ROOT / "docs/artifact-metadata-queries.sql").read_text())
                meta.execute((ROOT / "docs/artifact-email-composition.sql").read_text())
                meta.execute("""
                    CREATE FUNCTION public.metadata_report(jsonb) RETURNS jsonb LANGUAGE sql AS
                      $$ SELECT jsonb_build_object('mode',current_setting('transaction_read_only'),'input',$1) $$;
                    INSERT INTO app.artifact_metadata_queries SELECT artifact_id,'usage-example',
                       'public.metadata_report','artifact' FROM app.artifacts;
                """)
                with psycopg.connect(**params, dbname="data_db") as conn:
                    conn.execute("""
                        CREATE VIEW public.summary AS SELECT 'fixture'::text AS label;
                        CREATE FUNCTION public.summary_query(jsonb) RETURNS jsonb LANGUAGE sql AS
                          $$ SELECT $1 || jsonb_build_object('label',current_setting('bci.authenticated_subject'),
                                                             'readonly',current_setting('transaction_read_only')) $$;
                    """)
                yield meta
        finally:
            subprocess.run([*ctl, "-m", "fast", "stop"], check=True)


def test_read_render_and_revoke(database, monkeypatch, tmp_path):
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setattr(main, "SECURITY_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(main, "record_request_span_async", lambda **kw: None)
    client = TestClient(main.app)
    token = _encode_token(
        {
            "iss": main.SECURITY_TOKEN_ISSUER,
            "aud": main.SECURITY_TOKEN_AUDIENCE,
            "exp": int(time.time()) + 180,
            "client_key": "fixture",
            "roles": ["viewer"],
            "sub": "alice",
        }
    )
    headers = {"Authorization": "Bearer " + token}
    url = "/artifacts/fixture/summary"
    assert client.get(url, headers=headers).text == "<h1>fixture</h1>"
    query = client.post(url + "/data", headers=headers, json={"period": "month"})
    assert query.json() == {"label": "alice", "period": "month", "readonly": "on"}
    named = client.post(url + "/metadata/usage-example", headers=headers, json={})
    assert named.json() == {"input": {}, "mode": "on"}
    request = {
        "client_key": "fixture",
        "artifact_key": "summary",
        "behavior": "display",
        "query": {},
    }
    render = client.post("/artifact-executions", headers=headers, json=request)
    assert render.status_code == 200, render.text
    assert render.json()["preview_html"] == "<h1>alice</h1>"
    run = "/artifact-executions/" + render.json()["run_id"]
    assert client.get(run, headers=headers).json()["status"] == "completed"

    # Retain the file and receipt on success. Remove only this run's files
    # when recording the output fails before commit.
    from app import outputs

    monkeypatch.setenv("ARTIFACT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(outputs, "_durable_mount", lambda root, mounts: True)
    export = {
        **request,
        "output_formats": ["csv"],
        "query": {
            "csv": {"columns": [{"key": "item", "label": "Item"}], "rows": [{"item": "sample"}]}
        },
    }
    response = client.post("/artifact-executions", headers=headers, json=export)
    assert response.status_code == 200, response.text
    receipt = response.json()["outputs"][0]
    stored = Path(receipt["storage_path"])
    assert stored.read_text(encoding="utf-8-sig").splitlines() == ["Item", "sample"]
    assert database.execute("SELECT count(*) FROM log.artifact_outputs").fetchone() == (1,)
    database.execute("ALTER TABLE log.artifact_outputs RENAME TO hidden_outputs")
    assert client.post("/artifact-executions", headers=headers, json=export).status_code == 500
    assert list(stored.parent.parent.iterdir()) == [stored.parent]
    assert database.execute("SELECT to_regclass('log.artifact_outputs')").fetchone() == (None,)

    # A wrapper reuses the same Data contract through an ordered Metadata slot.
    database.execute("""
        INSERT INTO app.templates VALUES ('00000000-0000-0000-0000-000000000003','<section>{{ slots.body }}</section>',true);
        INSERT INTO app.artifacts VALUES ('00000000-0000-0000-0000-000000000004','fixture','wrapper',null,'email',
          'Email','00000000-0000-0000-0000-000000000003',true);
        INSERT INTO app.artifact_email_composition VALUES ('00000000-0000-0000-0000-000000000004','Period {{ parameters.period }}');
        INSERT INTO app.artifact_email_content VALUES ('00000000-0000-0000-0000-000000000004','body',0,
          '00000000-0000-0000-0000-000000000002',null);
        INSERT INTO security_role_permissions VALUES ('viewer','artifact:fixture:wrapper','artifact:execute');
    """)
    wrapped = client.post(
        "/artifact-executions",
        headers=headers,
        json={**request, "artifact_key": "wrapper", "query": {"period": "month"}},
    )
    assert wrapped.status_code == 200, wrapped.text
    assert wrapped.json()["preview_html"] == "<section><h1>alice</h1></section>"
    assert wrapped.json()["email_subject"] == "Period month"
    database.execute(
        "DELETE FROM security_role_permissions WHERE resource_key='artifact:fixture:summary' AND permission_key='artifact:execute'"
    )
    assert (
        client.post(
            "/artifact-executions",
            headers=headers,
            json={**request, "artifact_key": "wrapper", "query": {"period": "month"}},
        ).status_code
        == 403
    )
    database.execute("UPDATE security_group_members SET expires_at=now()")
    for response in [
        client.get(url, headers=headers),
        client.post(url + "/data", headers=headers, json={}),
        client.get(run, headers=headers),
    ]:
        assert response.status_code == 403
    assert (
        client.post(
            "/artifact-executions", headers=headers, json={**request, "behavior": "deliver"}
        ).status_code
        == 422
    )
    assert client.post("/artifacts", headers=headers, json={}).status_code == 404
    assert client.post("/delivery-batches", headers=headers, json={}).status_code == 404
