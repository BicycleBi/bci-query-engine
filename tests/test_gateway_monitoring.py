from app.monitoring import parse_gateway_record


def test_parse_gateway_record_accepts_exact_bounded_contract() -> None:
    record = parse_gateway_record(
        b'<190>bci_usage: {"timestamp":"2026-09-04T09:15:00+00:00",'
        b'"request_id":"abc-123","method":"GET","uri":"/artifacts/rf/market",'
        b'"status":200,"request_time_seconds":0.125,'
        b'"upstream_connect_seconds":"0.002","upstream_header_seconds":"0.100",'
        b'"upstream_response_seconds":"0.120","upstream_status":"200"}'
    )
    assert record is not None
    assert record["request_id"] == "abc-123"
    assert record["gateway_duration_ms"] == 125.0
    assert record["upstream_response_ms"] == 120.0


def test_parse_gateway_record_rejects_extra_or_sensitive_fields() -> None:
    assert parse_gateway_record(
        b'{"timestamp":"2026-09-04T09:15:00+00:00","request_id":"abc",'
        b'"method":"GET","uri":"/","status":200,"request_time_seconds":0.1,'
        b'"upstream_connect_seconds":"0.01","upstream_header_seconds":"0.05",'
        b'"upstream_response_seconds":"0.08","upstream_status":"200",'
        b'"cookie":"secret"}'
    ) is None
