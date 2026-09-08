from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import pytest


sys.modules.setdefault("psycopg", SimpleNamespace(connect=None, Connection=object))
sys.modules.setdefault("requests", SimpleNamespace(post=None))

engine = importlib.import_module("app.engine")


def test_internal_test_changes_only_recipient_envelope(monkeypatch) -> None:
    monkeypatch.setenv(
        "ARTIFACT_INTERNAL_TEST_RECIPIENTS",
        " reviewer@example.test,second@example.test ",
    )
    subject = "Your Profit & Loss report is ready"
    html = "<html><body><p>Your report is ready.</p></body></html>"

    recipients, resolved_subject, resolved_html = (
        engine._internal_test_delivery_envelope(subject, html)
    )

    assert recipients == [
        ("reviewer@example.test", "to"),
        ("second@example.test", "to"),
    ]
    assert resolved_subject == subject
    assert resolved_html == html


def test_internal_test_requires_a_recipient_allowlist(monkeypatch) -> None:
    monkeypatch.delenv("ARTIFACT_INTERNAL_TEST_RECIPIENTS", raising=False)

    with pytest.raises(
        ValueError,
        match="Internal test recipient allowlist is not configured",
    ):
        engine._internal_test_delivery_envelope("Subject", "<body>Message</body>")


@pytest.mark.parametrize("status", ["sent", "submitted", " Submitted "])
def test_email_service_accepted_statuses(status: str) -> None:
    assert engine._email_service_delivery_accepted({"status": status})


@pytest.mark.parametrize("status", ["failed", "status_unknown", "", None])
def test_email_service_nonaccepted_statuses(status: str | None) -> None:
    assert not engine._email_service_delivery_accepted({"status": status})


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("submitted", "provider_accepted"),
        ("sent", "provider_accepted"),
        ("delivered", "delivered"),
    ],
)
def test_email_service_status_normalization_preserves_sent_evidence(
    status: str,
    expected: str,
) -> None:
    assert engine._normalized_email_service_delivery_status({"status": status}) == expected


def test_reconcile_artifact_delivery_records_exchange_delivery(monkeypatch) -> None:
    original = {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "client_key": "srp",
        "artifact_key": "pnl-owner-single-training-email",
        "status": "completed",
        "delivery_id": "22222222-2222-2222-2222-222222222222",
        "delivery_status": "provider_accepted",
    }
    delivered = {**original, "delivery_status": "delivered"}
    records = iter([original, delivered])
    monkeypatch.setattr(engine, "get_run", lambda run_id: next(records))

    from app import mailer

    monkeypatch.setattr(
        mailer,
        "reconcile_delivery_trace",
        lambda delivery_id: {
            "reconciliation_status": "matched",
            "trace_delivery_status": "delivered",
        },
    )

    class FakeConnection:
        def __init__(self):
            self.calls = []
            self.commits = 0

        def execute(self, query, params):
            self.calls.append((query, params))
            return self

        def commit(self):
            self.commits += 1

    class FakeContext:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            return self.connection

        def __exit__(self, exc_type, exc, traceback):
            return False

    connection = FakeConnection()
    monkeypatch.setattr(engine, "get_metadata_conn", lambda: FakeContext(connection))

    result = engine.reconcile_artifact_delivery(original["run_id"])

    assert result["delivery_status"] == "delivered"
    assert any(params and params[0] == "delivered" for _, params in connection.calls)
    assert connection.commits == 1


def test_reconcile_delivery_batch_runs_one_bounded_trace_per_outstanding_item(monkeypatch) -> None:
    run_ids = [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]

    class FakeResult:
        def fetchall(self):
            return [(run_id,) for run_id in run_ids]

    class FakeConnection:
        def execute(self, query, params):
            assert "artifact_delivery_batch_items" in query
            return FakeResult()

    class FakeContext:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, exc_type, exc, traceback):
            return False

    reconciled = []
    monkeypatch.setattr(engine, "get_metadata_conn", FakeContext)
    monkeypatch.setattr(
        engine,
        "reconcile_artifact_delivery",
        lambda run_id: reconciled.append(run_id),
    )
    expected = {"batch_id": "33333333-3333-3333-3333-333333333333", "items": []}
    monkeypatch.setattr(engine, "get_delivery_batch", lambda batch_id: expected)

    result = engine.reconcile_delivery_batch(expected["batch_id"])

    assert result == expected
    assert reconciled == run_ids
