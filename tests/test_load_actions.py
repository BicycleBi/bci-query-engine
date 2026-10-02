from types import SimpleNamespace

from app import load_actions


def _action():
    return {
        "action_id": "11111111-1111-1111-1111-111111111111",
        "client_key": "srp",
        "artifact_key": "quickbooks-profit-loss-report",
        "action_key": "quickbooks-full-load",
    }


def test_worker_calls_only_quickbooks_full_loader_with_internal_token(monkeypatch):
    captured = {}
    completed = []
    monkeypatch.setenv("SERVICE_TOKEN", "internal-secret")
    monkeypatch.setenv("QUICKBOOKS_FULL_LOAD_URL", "http://data-integration:8080/loads/srp-quickbooks-loader/run")

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(load_actions.requests, "post", fake_post)
    monkeypatch.setattr(
        load_actions,
        "_finish_action",
        lambda action_id, status, error_code=None: completed.append((action_id, status, error_code)),
    )

    load_actions.execute_data_load_action(_action())

    assert captured["url"].endswith("/loads/srp-quickbooks-loader/run")
    assert captured["headers"]["Authorization"] == "Bearer internal-secret"
    assert captured["headers"]["X-BCI-Trigger-Source"] == "manual"
    assert captured["headers"]["X-BCI-Load-Scope"] == "full"
    assert captured["headers"]["X-BCI-Correlation-ID"] == "11111111-1111-1111-1111-111111111111"
    assert completed == [("11111111-1111-1111-1111-111111111111", "completed", None)]


def test_worker_never_retries_or_exposes_unknown_timeout(monkeypatch):
    completed = []
    monkeypatch.setenv("SERVICE_TOKEN", "internal-secret")
    monkeypatch.setattr(
        load_actions.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(load_actions.RequestsTimeout("secret detail")),
    )
    monkeypatch.setattr(
        load_actions,
        "_finish_action",
        lambda action_id, status, error_code=None: completed.append((status, error_code)),
    )

    load_actions.execute_data_load_action(_action())

    assert completed == [("attention_required", "timeout_unknown")]
    assert "secret" not in load_actions._safe_error_message("timeout_unknown")


def test_unknown_action_fails_closed_without_calling_data_integration(monkeypatch):
    completed = []
    monkeypatch.setattr(
        load_actions.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call")),
    )
    monkeypatch.setattr(
        load_actions,
        "_finish_action",
        lambda action_id, status, error_code=None: completed.append((status, error_code)),
    )
    action = _action() | {"action_key": "all-client-loads"}

    load_actions.execute_data_load_action(action)

    assert completed == [("failed", "unexpected_error")]
