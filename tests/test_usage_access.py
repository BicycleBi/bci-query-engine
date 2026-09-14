from contextlib import contextmanager
from datetime import datetime, timezone
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app import usage_access
from test_usage_summary import _load_main, _auth_headers, _token, _summary


@pytest.mark.parametrize('route', ['usage-summary', 'access-summary'])
def test_srp_reporting_requires_explicit_live_permission(monkeypatch, route):
    main = _load_main(monkeypatch)
    monkeypatch.setattr(main, 'get_usage_summary', lambda **kw: pytest.fail('unauthorized query'))
    monkeypatch.setattr(main, 'get_access_summary', lambda **kw: pytest.fail('unauthorized query'))
    def denied(*args):
        raise HTTPException(403, 'Usage reporting access denied')
    monkeypatch.setattr(main, 'require_usage_reporting_access', denied)
    client = TestClient(main.app)
    path = f'/artifacts/srp/usage-monitoring-dashboard/{route}'
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_auth_headers(_token('rf'))).status_code == 403
    assert client.get(path, headers=_auth_headers(_token('srp'))).status_code == 403


@pytest.mark.parametrize('query', ['days=365', 'offset=-1', 'offset=100001', 'search='+'x'*101])
def test_access_report_is_bounded(monkeypatch, query):
    main = _load_main(monkeypatch)
    client = TestClient(main.app)
    assert client.get('/artifacts/srp/usage-monitoring-dashboard/access-summary?'+query,
                      headers=_auth_headers(_token('srp'))).status_code == 400


def test_access_summary_requires_monitoring_artifact(monkeypatch):
    main = _load_main(monkeypatch)
    assert TestClient(main.app).get('/artifacts/srp/home/access-summary',
            headers=_auth_headers(_token('srp'))).status_code == 404


def test_authorized_report_forwards_literal_search_and_disables_cache(monkeypatch):
    main = _load_main(monkeypatch)
    monkeypatch.setattr(main, 'require_usage_reporting_access', lambda *args: None)
    captured = []
    monkeypatch.setattr(main, 'get_access_summary', lambda **kw: captured.append(kw) or {'users': []})
    response = TestClient(main.app).get('/artifacts/srp/usage-monitoring-dashboard/access-summary',
            params={'search': "Beyer's_%", 'offset': 50, 'days': 7}, headers=_auth_headers(_token('srp')))
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert captured == [{'client_key': 'srp', 'days': 7, 'offset': 50, 'search': "Beyer's_%"}]


def test_unavailable_report_never_returns_underlying_error(monkeypatch):
    main = _load_main(monkeypatch)
    monkeypatch.setattr(main, 'require_usage_reporting_access', lambda *args: None)
    def fail(**kwargs):
        raise RuntimeError('synthetic database error with private details')
    monkeypatch.setattr(main, 'get_access_summary', fail)
    response=TestClient(main.app).get('/artifacts/srp/usage-monitoring-dashboard/access-summary',
                     headers=_auth_headers(_token('srp')))
    assert response.status_code == 503
    assert 'private details' not in response.text


class Result:
    def __init__(self, rows): self.rows=rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows


class Metadata:
    def __init__(self, answers): self.answers=iter(answers); self.calls=[]
    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql.startswith('SET'): return Result([])
        return Result(next(self.answers))


def install_metadata(monkeypatch, answers):
    db = Metadata(answers)
    @contextmanager
    def connection(): yield db
    monkeypatch.setattr(usage_access, 'get_metadata_conn', connection)
    return db


@pytest.mark.parametrize('active,permission,expected', [
    (True,'artifact:read','Granted'), (False,'artifact:read','Disabled'),
    (True,None,'No current grants')])
def test_unobserved_accounts_and_disabled_accounts_are_preserved(monkeypatch,active,permission,expected):
    grants=[('test-1','reader','direct',None,'artifact:srp:home',permission)] if permission else []
    db=install_metadata(monkeypatch, [[(1,)], [('test-1','Synthetic User','synthetic@example.test',active)], grants, [(None,)]])
    result=usage_access.get_access_summary(client_key='srp',days=30)
    assert result['monitoring_available'] is False
    assert result['users'][0]['access_status'] == expected
    assert result['users'][0]['requests'] is None
    assert result['users'][0]['last_activity_at'] is None
    assert db.calls[0][0].endswith('READ ONLY')


def test_known_zero_usage_is_different_from_unavailable(monkeypatch):
    install_metadata(monkeypatch, [[(51,)], [('test-1','Synthetic User','synthetic@example.test',True)], [], [('monitoring.request_spans',)], [('test-1',0,None)]])
    result=usage_access.get_access_summary(client_key='srp',days=7)
    assert result['users'][0]['requests'] == 0
    assert result['has_more'] is True


@pytest.mark.parametrize('allowed', [True, False])
def test_live_authorization_result_is_enforced(monkeypatch, allowed):
    db=install_metadata(monkeypatch, [[(allowed,)]])
    identity={'client_key':'srp','sub':'synthetic-user','roles':['untrusted-admin-label']}
    if allowed: usage_access.require_usage_reporting_access(identity,'srp')
    else:
        with pytest.raises(HTTPException) as exc: usage_access.require_usage_reporting_access(identity,'srp')
        assert exc.value.status_code == 403
    sql,params=db.calls[-1]
    assert params['subject']=='synthetic-user'
    assert "p.permission_key = 'usage:read'" in sql
    assert 'identity' not in params


def test_authorization_database_failure_is_closed(monkeypatch):
    @contextmanager
    def broken(): raise RuntimeError('private error'); yield
    monkeypatch.setattr(usage_access,'get_metadata_conn',broken)
    with pytest.raises(HTTPException) as exc:
        usage_access.require_usage_reporting_access({'client_key':'srp','sub':'synthetic'},'srp')
    assert exc.value.status_code==503
    assert 'private' not in exc.value.detail


def test_grants_are_bounded_and_truncation_is_explicit(monkeypatch):
    grants=[('test-1','reader','direct',None,f'artifact:srp:synthetic-{i}','artifact:read') for i in range(201)]
    install_metadata(monkeypatch, [[(1,)], [('test-1','Synthetic User','synthetic@example.test',True)], grants, [(None,)]])
    result=usage_access.get_access_summary(client_key='srp',days=30)
    assert len(result['users'][0]['grants']) == 200
    assert result['users'][0]['grants_truncated'] is True
    assert result['users'][0]['access_status'] == 'Granted'


def test_usage_unavailable_is_not_reported_as_observed_zero(monkeypatch):
    from app import monitoring
    @contextmanager
    def missing_schema(): yield Metadata([[(None,None)]])
    monkeypatch.setattr(monitoring,'get_metadata_conn',missing_schema)
    result=monitoring.get_usage_summary(client_key='srp',days=30)
    assert result['monitoring_available'] is False
    assert result['totals'] == {}
