from datetime import datetime, timezone
from pathlib import Path

import pytest
from pglast import parse_sql

from app import usage_access
from test_usage_access import install_metadata


@pytest.fixture(autouse=True)
def rf_contract(monkeypatch):
    monkeypatch.setenv('ANALYTICS_REPORTING_CLIENT_KEY', 'rf')
    monkeypatch.setenv('ANALYTICS_BICYCLE_ADMIN_ROLE', 'rfqa_admin')


@pytest.mark.parametrize('offset,search', [(0, ''), (50, ''), (0, 'no accounts match')])
def test_denials_are_independent_of_the_account_page(monkeypatch, offset, search):
    denied = ('Synthetic denied subject', 'denied@example.test', 'home', 3,
              datetime(2026, 9, 22, tzinfo=timezone.utc))
    db = install_metadata(monkeypatch, [[(0,)], [], [], [(None, 'summary')], [denied]])
    result = usage_access.get_access_summary(client_key='rf', days=30, offset=offset, search=search)
    assert result['users'] == []
    assert result['monitoring_available'] is False
    assert result['authenticated_denials_available'] is True
    assert result['authenticated_denials'][0]['denied_requests'] == 3
    sql, params = db.calls[-1]
    assert 'analytics_reporting.authenticated_denial_summary' in sql
    assert 'active_user_audiences' not in sql
    assert '%(offset)s' not in sql and '%(search)s' not in sql and '%(ids)s' not in sql
    assert params['client'] == 'rf' and params['admin_role'] == 'rfqa_admin'
    assert params['audience'] == 'all'


def test_missing_denial_function_is_unavailable_not_observed_zero(monkeypatch):
    db = install_metadata(monkeypatch, [[(0,)], [], [], [('activity', None)]])
    result = usage_access.get_access_summary(client_key='rf', days=7)
    assert result['monitoring_available'] is True
    assert result['authenticated_denials_available'] is False
    assert result['authenticated_denials'] == []
    assert not any('FROM analytics_reporting.authenticated_denial_summary' in sql for sql, _ in db.calls)


def test_denial_limit_exposes_truncation_without_raw_identifiers(monkeypatch):
    denied = ('Synthetic subject', 'subject@example.test', 'home', 1, None)
    install_metadata(monkeypatch, [[(0,)], [], [], [(None, 'summary')], [denied] * 201])
    result = usage_access.get_access_summary(client_key='rf', days=90)
    assert len(result['authenticated_denials']) == 200
    assert result['authenticated_denials_limit'] == 200
    assert result['authenticated_denials_truncated'] is True
    assert set(result['authenticated_denials'][0]) == {
        'display_name', 'username', 'artifact_key', 'denied_requests', 'last_denied_at'}


def test_empty_available_denials_are_distinct_from_missing_monitoring(monkeypatch):
    install_metadata(monkeypatch, [[(0,)], [], [], [(None, 'summary')], []])
    result = usage_access.get_access_summary(client_key='rf', days=30)
    assert result['authenticated_denials_available'] is True
    assert result['authenticated_denials'] == []
    assert result['authenticated_denials_truncated'] is False


@pytest.mark.parametrize('audience', ['all', 'rf', 'bicycle'])
def test_audience_is_bound_to_the_shared_database_function(monkeypatch, audience):
    db = install_metadata(monkeypatch, [[(0,)], [], [], [(None, 'summary')], []])
    usage_access.get_access_summary(client_key='rf', days=30, audience=audience)
    assert db.calls[-1][1]['audience'] == audience


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
