from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
MONITORING = (ROOT / "app" / "monitoring.py").read_text(encoding="utf-8")
MODELS = (ROOT / "app" / "models.py").read_text(encoding="utf-8")
SCHEMA = (ROOT / "docs" / "usage-monitoring-schema.sql").read_text(encoding="utf-8")


class UsageMonitoringContractTests(unittest.TestCase):
    def test_request_lifecycle_is_correlated_and_fail_open(self) -> None:
        self.assertIn('@app.middleware("http")', MAIN)
        self.assertIn('request.headers.get("X-Request-ID")', MAIN)
        self.assertIn('response.headers["X-Request-ID"] = request_id', MAIN)
        self.assertIn("Monitoring must never break an artifact request", MONITORING)

    def test_writer_does_not_accept_payload_content(self) -> None:
        signature = MONITORING.split("def record_request_span(", 1)[1].split(") -> None:", 1)[0]
        for forbidden in ("request_body", "response_body", "query", "payload", "html"):
            self.assertNotIn(forbidden, signature)

    def test_interactions_are_bounded_to_safe_metadata(self) -> None:
        self.assertIn("class UsageInteractionRequest", MODELS)
        self.assertIn("dashboard_open|filter_apply|refresh|navigation|export_request|custom_action", MODELS)
        self.assertNotIn("metadata: dict", MODELS)
        self.assertIn('"/usage/interactions/{client_key}/{artifact_key}"', MAIN)

    def test_query_engine_is_the_single_ingestion_and_database_boundary(self) -> None:
        self.assertIn('"/internal/usage/events"', MAIN)
        self.assertIn("class UsageEventIngestRequest", MODELS)
        self.assertIn("def record_ingested_event", MONITORING)
        self.assertIn("def record_gateway_span", MONITORING)
        self.assertIn("CREATE TABLE IF NOT EXISTS monitoring.events", SCHEMA)
        self.assertIn("gateway_duration_ms", SCHEMA)

    def test_schema_and_writers_exclude_sensitive_payload_content(self) -> None:
        combined = (SCHEMA + MONITORING).lower()
        for forbidden in (
            "password text", "token text", "cookie text", "authorization_code",
            "request_body", "response_body", "rendered_html",
        ):
            self.assertNotIn(forbidden, combined)


if __name__ == "__main__":
    unittest.main()
