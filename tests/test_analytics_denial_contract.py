from pathlib import Path
from pglast import parse_sql


def test_shared_sql_and_function_body_parse():
    sql = Path('docs/analytics-denial-summary.sql').read_text()
    parse_sql(sql)
    parse_sql(sql.split('$summary$')[1])
    # Source boundary checks complement the mock API tests; actual PostgreSQL
    # semantic execution is a separate secured-host synthetic prerequisite.
    assert 'SECURITY INVOKER' in sql and 'SECURITY DEFINER' not in sql
    assert 'HAVING count(*) = 1' in sql
    assert "p_audience = 'all' OR EXISTS" in sql
    assert 'count(DISTINCT d.request_id)' in sql
    assert "audit.event_type = 'identity_upsert'" in sql
    assert 'analytics_reporting.artifact_access_edges edge' in sql
    assert "'No current artifact grants'::TEXT" in sql
    assert 'd.client_key = p_client' in sql
    assert 'p_limit BETWEEN 1 AND 201' in sql
    assert 'rfqa_admin' not in sql and "'rf'" not in sql
