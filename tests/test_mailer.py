from app import mailer


def test_email_service_timeout_allows_cold_credential_retrieval(monkeypatch):
    monkeypatch.delenv("EMAIL_SERVICE_TIMEOUT_SECONDS", raising=False)

    assert mailer._email_service_timeout_seconds() == 390.0


def test_email_service_timeout_can_be_overridden(monkeypatch):
    monkeypatch.setenv("EMAIL_SERVICE_TIMEOUT_SECONDS", "45")

    assert mailer._email_service_timeout_seconds() == 45.0
