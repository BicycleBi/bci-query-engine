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
