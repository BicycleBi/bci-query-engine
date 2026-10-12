"""Execute the authorization predicate against synthetic metadata, never client data."""
from contextlib import contextmanager
import json
import re
import sqlite3

import pytest
from fastapi import HTTPException
from app import usage_access


@pytest.fixture
def metadata(monkeypatch):
    monkeypatch.setenv('ANALYTICS_REPORTING_CLIENT_KEY', 'rag')
    monkeypatch.setenv('ANALYTICS_BICYCLE_ADMIN_ROLE', 'ragqa_admin')
    monkeypatch.delenv('ANALYTICS_CLIENT_REPORTING_ROLE', raising=False)
    db = sqlite3.connect(':memory:')
    db.execute("ATTACH DATABASE ':memory:' AS analytics_reporting")
    db.execute('''CREATE TABLE analytics_reporting.effective_grants (
        client_key TEXT, active BOOLEAN, admin_role_key TEXT,
        client_reporting_role_key TEXT, user_id TEXT, email TEXT,
        role_key TEXT, resource_key TEXT, permission_key TEXT)''')
    db.execute('''CREATE TABLE analytics_reporting.effective_assignments (
        client_key TEXT, active BOOLEAN, admin_role_key TEXT,
        client_reporting_role_key TEXT, user_id TEXT, email TEXT,
        is_bicycle_admin BOOLEAN)''')
    class Connection:
        def execute(self, sql, params=None):
            if sql.startswith('SET'): return None
            # Adapt only PostgreSQL parameter/array syntax. Execute the unchanged
            # authorization conditions, including all identity and role guards.
            sql = sql.replace('ANY(%(wildcard_resources)s)',
                              '(SELECT value FROM json_each(:wildcard_resources))')
            sql = sql.replace('g.resource_key = (SELECT', 'g.resource_key IN (SELECT')
            sql = re.sub(r'%\((\w+)\)s', r':\1', sql)
            return db.execute(sql, {**params,
                'wildcard_resources': json.dumps(params.get('wildcard_resources', []))})
    @contextmanager
    def connection(): yield Connection()
    monkeypatch.setattr(usage_access, 'get_metadata_conn', connection)
    yield db
    db.close()


def grant(db, resource, permission, **changes):
    values = dict(client_key='rag', active=True, admin_role_key='ragqa_admin',
                  client_reporting_role_key=None, user_id='synthetic-user',
                  email='synthetic@example.test', role_key='ragqa_admin',
                  resource_key=resource, permission_key=permission)
    values.update(changes)
    db.execute('INSERT INTO analytics_reporting.effective_grants VALUES (?,?,?,?,?,?,?,?,?)',
               tuple(values.values()))
    db.execute('INSERT INTO analytics_reporting.effective_assignments VALUES (?,?,?,?,?,?,?)',
               tuple(values[k] for k in ('client_key','active','admin_role_key',
                      'client_reporting_role_key','user_id','email')) + (True,))


def authorize(report='access', **identity):
    return usage_access.require_analytics_reporting_access(
        {'client_key':'rag', 'sub':'synthetic-user', **identity}, 'rag', report)


@pytest.mark.parametrize('resource', [
    'artifact:rag:usage-monitoring-dashboard', 'artifact:rag:*', 'artifact:*:*', '*'])
@pytest.mark.parametrize('permission', ['artifact:read', '*'])
def test_access_accepts_canonical_existing_grants(metadata, resource, permission):
    grant(metadata, resource, permission)
    assert authorize() is True


def test_qa_wildcard_admin_access_does_not_replace_usage_permission(metadata):
    grant(metadata, 'artifact:rag:*', 'artifact:read')
    assert authorize() is True
    with pytest.raises(HTTPException) as error: authorize('usage')
    assert error.value.status_code == 403
    grant(metadata, 'artifact:rag:usage-monitoring-dashboard', 'usage:read')
    assert authorize('usage') is True


@pytest.mark.parametrize('resource,permission,changes', [
    ('artifact:rf:*', 'artifact:read', {}),
    ('artifact:rag:home', 'artifact:read', {}),
    ('artifact:*:usage-monitoring-dashboard', 'artifact:read', {}),
    ('artifact:rag:*', 'artifact:execute', {}),
    ('artifact:rag:*', 'usage:read', {}),
    ('artifact:rag:*', 'artifact:read', {'active':False}),
    ('artifact:rag:*', 'artifact:read', {'client_key':'rf'}),
    ('artifact:rag:*', 'artifact:read', {'role_key':'ragqa_client_uat'}),
    ('artifact:rag:*', 'artifact:read', {'admin_role_key':'ragdev_admin'}),
    ('artifact:rag:*', 'artifact:read', {'client_reporting_role_key':'unconfigured'}),
    ('artifact:rag:*', 'artifact:read', {'user_id':'other-user'}),
])
def test_wildcards_do_not_bypass_authorization_guards(metadata, resource, permission, changes):
    grant(metadata, resource, permission, **changes)
    with pytest.raises(HTTPException) as error: authorize()
    assert error.value.status_code == 403


def test_verified_email_resolution_preserved(metadata):
    grant(metadata, 'artifact:rag:*', 'artifact:read', user_id='canonical-user')
    assert authorize(email='SYNTHETIC@EXAMPLE.TEST') is True


def test_cross_client_identity_still_denied(metadata):
    grant(metadata, '*', '*')
    with pytest.raises(HTTPException) as error: authorize(client_key='rf')
    assert error.value.status_code == 403
