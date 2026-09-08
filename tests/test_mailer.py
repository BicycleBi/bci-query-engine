from app import mailer


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "delivery_id": "delivery-1",
            "reconciliation_status": "matched",
            "trace_delivery_status": "delivered",
        }


def test_email_service_timeout_allows_cold_credential_retrieval(monkeypatch):
    monkeypatch.delenv("EMAIL_SERVICE_TIMEOUT_SECONDS", raising=False)

    assert mailer._email_service_timeout_seconds() == 390.0


def test_email_service_timeout_can_be_overridden(monkeypatch):
    monkeypatch.setenv("EMAIL_SERVICE_TIMEOUT_SECONDS", "45")

    assert mailer._email_service_timeout_seconds() == 45.0


def test_reconcile_delivery_trace_uses_one_nonblocking_attempt(monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN", "secret")
    monkeypatch.setenv("EMAIL_SERVICE_URL", "http://email-service:8200")
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return _Response()

    monkeypatch.setattr(mailer.requests, "post", fake_post)

    result = mailer.reconcile_delivery_trace("delivery-1")

    assert result["trace_delivery_status"] == "delivered"
    assert captured == {
        "url": "http://email-service:8200/deliveries/delivery-1/trace",
        "json": {"max_attempts": 1, "poll_interval_seconds": 0},
        "headers": {"Authorization": "Bearer secret"},
        "timeout": 45,
    }
