import base64
import hashlib
import hmac
import importlib
import json
import sys
import time

from fastapi.testclient import TestClient


def _load_main(monkeypatch):
    monkeypatch.setenv("QUERY_ENGINE_SECURITY_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("QUERY_ENGINE_SECURITY_TOKEN_ISSUER", "bci-security")
    monkeypatch.setenv("QUERY_ENGINE_SECURITY_TOKEN_AUDIENCE", "bci-client")
    monkeypatch.setenv("SERVICE_TOKEN", "test-service-token")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    monkeypatch.setattr(module, "get_copied_report", lambda *args, **kwargs: None)
    return module


def _encode_token(payload, secret="test-secret"):
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
        + "."
        + base64.urlsafe_b64encode(signature).decode("utf-8").rstrip("=")
    )


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _token(client_key="rf"):
    return _encode_token(
        {
            "aud": "bci-client",
            "client_key": client_key,
            "display_name": "Jeanre",
            "email": "jeanre@example.test",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": "bci-security",
            "roles": ["developer"],
            "sub": "user-1",
        }
    )


def _summary(client_key, days):
    return {
        "client_key": client_key,
        "days": days,
        "period_start": "2026-08-05T00:00:00Z",
        "period_end": "2026-09-04T00:00:00Z",
        "totals": {"requests": 2, "active_users": 1},
        "daily": [],
        "artifacts": [],
        "users": [{"display_name": "Jeanre", "username": "jeanre@example.test", "requests": 2}],
        "login_outcomes": [],
    }


def test_usage_summary_is_authenticated_and_client_scoped(monkeypatch):
    monkeypatch.setenv("ANALYTICS_REPORTING_CLIENT_KEY", "rf")
    monkeypatch.setenv("ANALYTICS_BICYCLE_ADMIN_ROLE", "rfdev_admin")
    main = _load_main(monkeypatch)
    monkeypatch.setattr(main, "require_analytics_reporting_access", lambda *args: True)
    captured = []
    monkeypatch.setattr(
        main,
        "get_usage_summary",
        lambda *, client_key, days: captured.append((client_key, days)) or _summary(client_key, days),
    )
    client = TestClient(main.app)

    path = "/artifacts/rf/usage-monitoring-dashboard/usage-summary?days=30"
    assert client.get(path).status_code == 401
    assert client.get(
        path, headers=_auth_headers(_token("srp"))
    ).status_code == 403

    response = client.get(
        path, headers=_auth_headers(_token("rf"))
    )
    assert response.status_code == 200
    assert response.json()["totals"]["requests"] == 2
    assert response.json()["users"][0]["display_name"] == "Jeanre"
    assert captured == [("rf", 30)]


def test_usage_summary_rejects_unbounded_period(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    response = client.get(
        "/artifacts/rf/usage-monitoring-dashboard/usage-summary?days=365",
        headers=_auth_headers(_token("rf")),
    )
    assert response.status_code == 400


def test_usage_summary_is_available_only_for_the_monitoring_artifact(monkeypatch):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    response = client.get(
        "/artifacts/rf/market-penetration-dashboard/usage-summary?days=30",
        headers=_auth_headers(_token("rf")),
    )
    assert response.status_code == 404


def test_usage_summary_contract_excludes_sensitive_identifiers():
    monitoring = importlib.import_module("app.monitoring")
    source = importlib.import_module("inspect").getsource(monitoring.get_usage_summary)
    for forbidden in ("email", "request_id", "session_id", "response_body", "request_body"):
        assert f'"{forbidden}"' not in source


def test_usage_custom_filters_are_bounded_and_forwarded(monkeypatch):
    monkeypatch.setenv('ANALYTICS_REPORTING_CLIENT_KEY','rf')
    monkeypatch.setenv('ANALYTICS_BICYCLE_ADMIN_ROLE','rfdev_admin')
    main=_load_main(monkeypatch)
    monkeypatch.setattr(main,'require_analytics_reporting_access',lambda *args:True)
    seen=[]
    monkeypatch.setattr(main,'get_usage_summary',lambda **kw:seen.append(kw) or _summary(kw['client_key'],kw['days']))
    client=TestClient(main.app);base='/artifacts/rf/usage-monitoring-dashboard/usage-summary'
    ok=client.get(base+'?start_date=2026-10-01&end_date=2026-10-31&audience=bicycle&artifact_search=Example&user_search=Reviewer',headers=_auth_headers(_token()))
    assert ok.status_code==200
    assert str(seen[-1]['start_date'])=='2026-10-01'
    assert seen[-1]['audience']=='bicycle' and seen[-1]['artifact_search']=='Example'
    for query in ['start_date=2026-10-01','start_date=2026-10-02&end_date=2026-10-01','start_date=2026-01-01&end_date=2026-10-01','start_date=not-a-date&end_date=2026-10-01','audience=srp','user_search='+'x'*101]:
        assert client.get(base+'?'+query,headers=_auth_headers(_token())).status_code==400


def test_usage_client_reporter_cannot_select_bicycle_audience(monkeypatch):
    monkeypatch.setenv('ANALYTICS_REPORTING_CLIENT_KEY','rf')
    monkeypatch.setenv('ANALYTICS_BICYCLE_ADMIN_ROLE','rfdev_admin')
    main=_load_main(monkeypatch)
    monkeypatch.setattr(main,'require_analytics_reporting_access',lambda *args:False)
    seen=[]
    monkeypatch.setattr(main,'get_usage_summary',lambda **kw:seen.append(kw) or _summary(kw['client_key'],kw['days']))
    response=TestClient(main.app).get('/artifacts/rf/usage-monitoring-dashboard/usage-summary?audience=bicycle',headers=_auth_headers(_token()))
    assert response.status_code==200 and seen[0]['audience']=='rf'
    # Preserve the established default for all existing clients/reporters.
    response=TestClient(main.app).get('/artifacts/rf/usage-monitoring-dashboard/usage-summary',headers=_auth_headers(_token()))
    assert response.status_code==200 and 'audience' not in seen[-1]


def test_usage_filters_apply_in_sql_before_group_limits(monkeypatch):
    from contextlib import contextmanager
    from datetime import date, datetime, timezone
    from app import monitoring
    monkeypatch.setenv('ANALYTICS_REPORTING_CLIENT_KEY','rf')
    monkeypatch.setenv('ANALYTICS_BICYCLE_ADMIN_ROLE','rfdev_admin')
    queries=[]
    class Cursor:
        def __init__(self,sql):self.sql=sql
        def fetchone(self):
            if 'to_regclass' in self.sql:return ('monitoring.request_spans','monitoring.events')
            return (0,0,0,0,None,None,0,0,0,0)
        def fetchall(self):return []
    class Connection:
        def execute(self,sql,params=None):queries.append((sql,params));return Cursor(sql)
    @contextmanager
    def conn():yield Connection()
    monkeypatch.setattr(monitoring,'get_metadata_conn',conn)
    result=monitoring.get_usage_summary(client_key='rf',days=30,start_date=date(2026,10,1),end_date=date(2026,10,31),audience='bicycle',artifact_search="a' OR 1=1 --",user_search='Reviewer')
    assert result['period_start']==datetime(2026,10,1,tzinfo=timezone.utc)
    assert result['period_end']==datetime(2026,11,1,tzinfo=timezone.utc)
    aggregate_queries=[q for q in queries if q[1] is not None]
    assert len(aggregate_queries)==5
    for sql,params in aggregate_queries:
        assert "a' OR 1=1 --" not in sql
        assert "a' or 1=1 --" in params
        assert sql.count('%s')==len(params)
        from pglast import parse_sql
        parse_sql(sql.replace('%s', 'NULL'))
        assert 'usage_spans AS' in sql and 'usage_events AS' in sql
        assert "u.client_key = %s" in sql and 'u.admin_role_key = %s' in sql
        assert 'EXISTS (' in sql
    assert result['applied_filters']['audience']=='bicycle'
