from types import SimpleNamespace
from datetime import datetime, timezone

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
        lambda action_id, status, error_code=None, progress=None: completed.append(
            (action_id, status, error_code, progress)
        ),
    )

    load_actions.execute_data_load_action(_action())

    assert captured["url"].endswith("/loads/srp-quickbooks-loader/run")
    assert captured["headers"]["Authorization"] == "Bearer internal-secret"
    assert captured["headers"]["X-BCI-Trigger-Source"] == "manual"
    assert captured["headers"]["X-BCI-Load-Scope"] == "full"
    assert captured["headers"]["X-BCI-Correlation-ID"] == "11111111-1111-1111-1111-111111111111"
    assert completed == [("11111111-1111-1111-1111-111111111111", "completed", None, None)]


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
        lambda action_id, status, error_code=None, progress=None: completed.append((status, error_code)),
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
        lambda action_id, status, error_code=None, progress=None: completed.append((status, error_code)),
    )
    action = _action() | {"action_key": "all-client-loads"}

    load_actions.execute_data_load_action(action)

    assert completed == [("failed", "unexpected_error")]


def test_worker_persists_partial_success_progress(monkeypatch):
    completed = []
    monkeypatch.setenv("SERVICE_TOKEN", "internal-secret")
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "status": "completed_with_warnings",
            "progress": {
                "phase": "completed_with_warnings",
                "entities_completed": 12,
                "entities_failed": 1,
                "entities_total": 13,
                "failures": [
                    {
                        "entity_name": "Hogan Spine",
                        "reason_code": "authorization_required",
                        "message": "untrusted detail",
                    }
                ],
            },
        },
    )
    monkeypatch.setattr(load_actions.requests, "post", lambda *args, **kwargs: response)
    monkeypatch.setattr(
        load_actions,
        "_finish_action",
        lambda action_id, status, error_code=None, progress=None: completed.append(
            (status, error_code, load_actions._normalize_progress(progress))
        ),
    )

    load_actions.execute_data_load_action(_action())

    assert completed[0][0] == "completed_with_warnings"
    assert completed[0][2]["entities_completed"] == 12
    assert completed[0][2]["entity_failures"] == [
        {
            "entity_name": "Hogan Spine",
            "reason_code": "authorization_required",
            "message": "QuickBooks authorization needs attention.",
        }
    ]


def test_live_progress_is_bounded_and_uses_safe_failure_copy(monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN", "internal-secret")
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "phase": "running",
            "current_entity": "Austin Spine and Rehab",
            "current_step": "Loading Profit & Loss detail (2 of 4)",
            "entities_completed": 3,
            "entities_failed": 1,
            "entities_total": 13,
            "failures": [
                {
                    "entity_name": "Hogan Spine",
                    "reason_code": "authorization_required",
                    "message": "secret raw provider detail",
                }
            ],
        },
    )
    monkeypatch.setattr(load_actions.requests, "get", lambda *args, **kwargs: response)

    result = load_actions.get_live_quickbooks_load_progress(_action()["action_id"])

    assert result["current_entity"] == "Austin Spine and Rehab"
    assert result["entities_total"] == 13
    assert "secret" not in str(result)


def test_canonical_status_covers_scheduled_runs_and_uses_safe_fields(monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN", "internal-secret")
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "run_id": "11111111-1111-1111-1111-111111111111",
            "status": "completed",
            "trigger_source": "scheduled",
            "started_at": "2026-10-28T10:00:00Z",
            "updated_at": "2026-10-28T10:08:00Z",
            "completed_at": "2026-10-28T10:08:00Z",
            "error_code": None,
            "progress": {
                "phase": "completed",
                "entities_completed": 13,
                "entities_total": 13,
                "secret": "must not cross the boundary",
            },
        },
    )
    captured = {}
    monkeypatch.setattr(
        load_actions.requests,
        "get",
        lambda url, **kwargs: captured.update(url=url, **kwargs) or response,
    )

    result = load_actions.get_canonical_quickbooks_load_status()

    assert captured["url"].endswith("/loads/srp-quickbooks-loader/latest")
    assert result["status"] == "completed"
    assert result["trigger_source"] == "scheduled"
    assert result["entities_completed"] == 13
    assert "secret" not in str(result)


def test_canonical_run_wins_over_stale_manual_request_status():
    manual = {
        "action_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "status": "failed",
        "requested_at": datetime(2026, 10, 16, tzinfo=timezone.utc),
    }
    canonical = {
        "action_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "status": "completed",
        "started_at": "2026-10-28T10:00:00Z",
    }

    assert load_actions.latest_quickbooks_load_status(manual, canonical) is canonical


def test_new_queued_request_temporarily_wins_before_canonical_run_exists():
    manual = {
        "action_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "status": "queued",
        "requested_at": datetime(2026, 10, 28, 11, 0, tzinfo=timezone.utc),
    }
    canonical = {
        "action_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "status": "completed",
        "started_at": "2026-10-28T10:00:00Z",
    }

    assert load_actions.latest_quickbooks_load_status(manual, canonical) is manual
