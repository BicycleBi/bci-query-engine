import importlib
import json

engine = importlib.import_module("app.engine")
renderer = importlib.import_module("app.renderer")


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class Meta:
    def __init__(self):
        self.sql = None
        self.params = None

    def execute(self, sql, params):
        self.sql = sql
        self.params = params
        return Result([("home",), ("vendor-coverage-map",)])


def test_authorized_artifact_keys_match_security_read_wildcards():
    meta = Meta()

    keys = engine._authorized_artifact_keys(
        meta,
        "rf",
        ["rfqa_construction_team", "rfqa_construction_team"],
    )

    assert keys == ["home", "vendor-coverage-map"]
    assert meta.params == ("rf", ["rfqa_construction_team"])
    assert "permission.permission_key IN ('artifact:read', '*')" in meta.sql
    assert "'artifact:' || artifact.client_key || ':' || artifact.artifact_key" in meta.sql
    assert "'artifact:' || artifact.client_key || ':*'" in meta.sql
    assert "'artifact:*:*'" in meta.sql
    assert "artifact.active" in meta.sql


def test_authorized_artifact_keys_fail_closed_without_roles():
    meta = Meta()

    assert engine._authorized_artifact_keys(meta, "rf", []) == []
    assert meta.sql is None


def test_renderer_exposes_trusted_authorized_artifact_keys():
    template = (
        "{% if 'vendor-coverage-map' in authorized_artifact_keys %}vendor{% endif %}"
        "{% if 'market-penetration-dashboard' in authorized_artifact_keys %}market{% endif %}"
    )

    rendered = renderer.render(
        template,
        [],
        context={"authorized_artifact_keys": ["home", "vendor-coverage-map"]},
    )

    assert rendered == "vendor"


def test_permission_changes_change_render_cache_context():
    roles = ["rfqa_construction_team"]
    before = engine._authorization_context_hash(
        roles,
        ["home", "vendor-coverage-map"],
    )
    after = engine._authorization_context_hash(
        roles,
        ["home", "market-penetration-dashboard", "vendor-coverage-map"],
    )

    assert before != after
    expected = hashlib_sha256(
        {
            "artifact_read": ["home", "vendor-coverage-map"],
            "roles": roles,
        }
    )
    assert before == expected


def hashlib_sha256(value):
    import hashlib

    normalized = json.dumps(value, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
