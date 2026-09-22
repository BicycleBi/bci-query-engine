from contextlib import contextmanager
from datetime import datetime, timezone
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app import usage_access
from test_usage_summary import _load_main, _auth_headers, _token, _summary, _encode_token


@pytest.mark.parametrize('route', ['usage-summary', 'access-summary'])
def test_srp_reporting_requires_explicit_live_permission(monkeypatch, route):
    main = _load_main(monkeypatch)
    monkeypatch.setattr(main, 'get_usage_summary', lambda **kw: pytest.fail('unauthorized query'))
    monkeypatch.setattr(main, 'get_access_summary', lambda **kw: pytest.fail('unauthorized query'))
    def denied(*args):
        raise HTTPException(403, 'Usage reporting access denied')
    monkeypatch.setattr(main, 'require_analytics_reporting_access', denied)
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
    monkeypatch.setattr(main, 'require_analytics_reporting_access', lambda *args: True)
    captured = []
    monkeypatch.setattr(main, 'get_access_summary', lambda **kw: captured.append(kw) or {'users': []})
    response = TestClient(main.app).get('/artifacts/srp/usage-monitoring-dashboard/access-summary',
            params={'search': "Beyer's_%", 'offset': 50, 'days': 7}, headers=_auth_headers(_token('srp')))
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert captured == [{'client_key': 'srp', 'days': 7, 'offset': 50, 'search': "Beyer's_%", 'audience': 'all'}]


def test_unavailable_report_never_returns_underlying_error(monkeypatch):
    main = _load_main(monkeypatch)
    monkeypatch.setattr(main, 'require_analytics_reporting_access', lambda *args: None)
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


@pytest.mark.parametrize('permission,expected', [
    ('artifact:read','Granted'), (None,'No current grants')])
def test_active_unobserved_accounts_are_preserved(monkeypatch,permission,expected):
    grants=[('test-1','reader','direct',None,'artifact:srp:home',permission)] if permission else []
    db=install_metadata(monkeypatch, [[(1,)], [('test-1','Synthetic User','synthetic@example.test',True)], grants, [(None,)]])
    result=usage_access.get_access_summary(client_key='srp',days=30)
    assert result['monitoring_available'] is False
    assert result['users'][0]['access_status'] == expected
    assert result['users'][0]['requests'] is None
    assert result['users'][0]['last_activity_at'] is None
    assert db.calls[0][0].endswith('READ ONLY')
    assert 'analytics_reporting.active_user_audiences' in db.calls[2][0]


def test_known_zero_usage_is_different_from_unavailable(monkeypatch):
    install_metadata(monkeypatch, [[(51,)], [('test-1','Synthetic User','synthetic@example.test',True)], [], [('analytics_reporting.request_activity',)], [('test-1',0,None)], []])
    result=usage_access.get_access_summary(client_key='srp',days=7)
    assert result['users'][0]['requests'] == 0
    assert result['has_more'] is True


@pytest.mark.parametrize('allowed', [True, False])
def test_live_authorization_result_is_enforced(monkeypatch, allowed):
    db=install_metadata(monkeypatch, [[(allowed, True)]])
    identity={'client_key':'srp','sub':'synthetic-user','roles':['untrusted-admin-label']}
    if allowed: assert usage_access.require_usage_reporting_access(identity,'srp') is True
    else:
        with pytest.raises(HTTPException) as exc: usage_access.require_usage_reporting_access(identity,'srp')
        assert exc.value.status_code == 403
    sql,params=db.calls[-1]
    assert params['subject']=='synthetic-user'
    assert "g.permission_key = %(permission)s" in sql
    assert 'analytics_reporting.effective_grants' in sql
    assert 'security_role_permissions' not in sql
    assert params['permission'] == 'usage:read'
    assert params['admin_role'] == 'srpdev_bicycle_dev'
    assert params['corporate_role'] == 'srpdev_bicycle_dev'
    assert 'identity' not in params


def test_authorization_database_failure_is_closed(monkeypatch):
    @contextmanager
    def broken(): raise RuntimeError('private error'); yield
    monkeypatch.setattr(usage_access,'get_metadata_conn',broken)
    with pytest.raises(HTTPException) as exc:
        usage_access.require_usage_reporting_access({'client_key':'srp','sub':'synthetic'},'srp')
    assert exc.value.status_code==503
    assert 'private' not in exc.value.detail


def test_non_srp_reporting_requires_complete_matching_configuration(monkeypatch):
    identity = {'client_key': 'rf', 'sub': 'synthetic-user'}
    with pytest.raises(HTTPException) as missing:
        usage_access.require_usage_reporting_access(identity, 'rf')
    assert missing.value.status_code == 503

    monkeypatch.setenv('ANALYTICS_REPORTING_CLIENT_KEY', 'rag')
    monkeypatch.setenv('ANALYTICS_BICYCLE_ADMIN_ROLE', 'ragdev_admin')
    with pytest.raises(HTTPException) as mismatch:
        usage_access.require_usage_reporting_access(identity, 'rf')
    assert mismatch.value.status_code == 503


def test_generic_reporting_configuration_drives_live_authorization(monkeypatch):
    monkeypatch.setenv('ANALYTICS_REPORTING_CLIENT_KEY', 'rf')
    monkeypatch.setenv('ANALYTICS_BICYCLE_ADMIN_ROLE', 'rfdev_admin')
    monkeypatch.setenv('ANALYTICS_CLIENT_REPORTING_ROLE', 'rfdev_analytics')
    db = install_metadata(monkeypatch, [[(True, False)]])
    identity = {'client_key': 'rf', 'sub': 'synthetic-user'}
    assert usage_access.require_analytics_reporting_access(identity, 'rf', 'access') is False
    params = db.calls[-1][1]
    assert params['client'] == 'rf'
    assert params['admin_role'] == 'rfdev_admin'
    assert params['corporate_role'] == 'rfdev_analytics'
    assert params['resource'] == 'artifact:rf:usage-monitoring-dashboard'


@pytest.mark.parametrize('client_key', ['rf', 'rag', 'rbp'])
def test_generic_analytics_artifact_gate_uses_configured_client_roles(monkeypatch, client_key):
    admin_role = f'{client_key}dev_admin'
    reporting_role = f'{client_key}dev_analytics'
    monkeypatch.setenv('ANALYTICS_REPORTING_CLIENT_KEY', client_key)
    monkeypatch.setenv('ANALYTICS_BICYCLE_ADMIN_ROLE', admin_role)
    monkeypatch.setenv('ANALYTICS_CLIENT_REPORTING_ROLE', reporting_role)
    main = _load_main(monkeypatch)
    monkeypatch.setattr(main, 'execute_artifact', lambda *args, **kwargs: {
        'run_id': '11111111-1111-1111-1111-111111111111',
        'status': 'success',
        'preview_html': '<p>ok</p>',
        'cache': {},
    })
    client = TestClient(main.app)

    def token(client_key, roles):
        return _encode_token({
            'aud': 'bci-client', 'client_key': client_key, 'exp': 4102444800,
            'iat': 1, 'iss': 'bci-security', 'roles': roles, 'sub': 'synthetic-user',
        })

    path = f'/artifacts/{client_key}/usage-monitoring-dashboard?report=usage'
    assert client.get(path, headers=_auth_headers(token(client_key, [admin_role]))).status_code == 200
    assert client.get(path, headers=_auth_headers(token(client_key, [reporting_role]))).status_code == 403
    assert client.get(f'/artifacts/{client_key}/usage-monitoring-dashboard?report=access',
                      headers=_auth_headers(token(client_key, [reporting_role]))).status_code == 200
    assert client.get(f'/artifacts/{client_key}/usage-monitoring-dashboard',
                      headers=_auth_headers(token(client_key, [reporting_role]))).status_code == 403
    assert client.get(path, headers=_auth_headers(token(client_key, ['same-role-other-scope']))).status_code == 403
    other_client = 'rag' if client_key != 'rag' else 'rf'
    assert client.get(path, headers=_auth_headers(token(other_client, [admin_role]))).status_code == 403


@pytest.mark.parametrize('client_key', ['rf', 'rag', 'rbp'])
def test_generic_client_reporter_is_forced_to_its_client_audience(monkeypatch, client_key):
    monkeypatch.setenv('ANALYTICS_REPORTING_CLIENT_KEY', client_key)
    monkeypatch.setenv('ANALYTICS_BICYCLE_ADMIN_ROLE', f'{client_key}dev_admin')
    monkeypatch.setenv('ANALYTICS_CLIENT_REPORTING_ROLE', f'{client_key}dev_analytics')
    main = _load_main(monkeypatch)
    monkeypatch.setattr(main, 'require_analytics_reporting_access', lambda *args: False)
    client = TestClient(main.app)
    for requested in ('all', client_key, 'bicycle'):
        captured = []
        monkeypatch.setattr(main, 'get_access_matrix',
                            lambda **kw: captured.append(kw) or {'rows': [], 'audience': kw['audience']})
        response = client.get(f'/artifacts/{client_key}/usage-monitoring-dashboard/access-summary',
            params={'perspective': 'users', 'audience': requested},
            headers=_auth_headers(_token(client_key)))
        assert response.status_code == 200
        assert captured[0]['audience'] == client_key
        assert response.json()['allowed_audiences'] == [client_key]


@pytest.mark.parametrize('client_key', ['rf', 'rag', 'rbp'])
def test_non_analytics_artifacts_do_not_require_analytics_configuration(monkeypatch, client_key):
    for name in ('ANALYTICS_REPORTING_CLIENT_KEY', 'ANALYTICS_BICYCLE_ADMIN_ROLE',
                 'ANALYTICS_CLIENT_REPORTING_ROLE'):
        monkeypatch.delenv(name, raising=False)
    main = _load_main(monkeypatch)
    monkeypatch.setattr(main, 'execute_artifact', lambda *args, **kwargs: {
        'run_id': '11111111-1111-1111-1111-111111111111',
        'status': 'success',
        'preview_html': '<p>ok</p>',
        'cache': {},
    })
    response = TestClient(main.app).get(
        f'/artifacts/{client_key}/existing-dashboard',
        headers=_auth_headers(_token(client_key)),
    )
    assert response.status_code == 200


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

@pytest.mark.parametrize('perspective,subjects,expected_name', [
    ('users',[('u1','Synthetic User','user@example.test',True)],'Synthetic User'),
    ('artifacts',[('home','SRP Home',True)],'SRP Home'),
])
def test_access_matrix_resolves_artifact_names_and_both_perspectives(monkeypatch,perspective,subjects,expected_name):
    edge=('home','SRP Home',True,'u1','Synthetic User','user@example.test',True,True,False,1,
          [{'role':'reader','source':'group','group':'synthetic','permission':'artifact:read','resource':'artifact:srp:*'}],False)
    db=install_metadata(monkeypatch,[[(1,)],subjects,[edge]])
    result=usage_access.get_access_matrix(client_key='srp',perspective=perspective)
    assert result['rows'][0]['display_name']==expected_name
    assert result['rows'][0]['matches'][0]['artifact_name']=='SRP Home'
    assert result['rows'][0]['matches'][0]['can_view'] is True
    assert result['rows'][0]['matches'][0]['can_run'] is False
    sql=db.calls[-1][0]
    assert 'analytics_reporting.artifact_access_edges' in sql
    assert 'analytics_reporting.active_user_audiences' in sql
    assert 'edge_number<=201' in sql

def test_access_matrix_inactive_artifacts_preserve_assignments_without_effective_access(monkeypatch):
    edge=('home','SRP Home',False,'u1','Synthetic User','user@example.test',True,True,True,1,[],False)
    install_metadata(monkeypatch,[[(1,)],[('u1','Synthetic User','user@example.test',True)],[edge]])
    match=usage_access.get_access_matrix(client_key='srp',perspective='users')['rows'][0]['matches'][0]
    assert match['assigned_view'] and match['assigned_run']
    assert not match['can_view'] and not match['can_run']


@pytest.mark.parametrize('perspective',['users','artifacts'])
def test_access_matrix_filters_inactive_users_before_paging_and_matches(monkeypatch,perspective):
    db=install_metadata(monkeypatch,[[(0,)],[],[]])
    usage_access.get_access_matrix(client_key='srp',perspective=perspective)
    user_queries=[sql for sql, _ in db.calls if 'analytics_reporting.active_user_audiences' in sql]
    assert user_queries
    assert all('security_users' not in sql for sql in user_queries)


def test_access_matrix_no_assignments_remain_visible_and_paginate(monkeypatch):
    install_metadata(monkeypatch,[[(51,)],[('u1','Synthetic User','user@example.test',True)],[]])
    result=usage_access.get_access_matrix(client_key='srp',perspective='users')
    assert result['has_more']
    assert result['rows'][0]['matches']==[]


def test_matrix_route_keeps_exact_reporting_authorization(monkeypatch):
    main = _load_main(monkeypatch)
    monkeypatch.setattr(main,'require_analytics_reporting_access',lambda *a: (_ for _ in ()).throw(HTTPException(403,'Denied')))
    monkeypatch.setattr(main,'get_access_matrix',lambda **kw: pytest.fail('unauthorized matrix query'))
    assert TestClient(main.app).get('/artifacts/srp/usage-monitoring-dashboard/access-summary?perspective=artifacts',headers=_auth_headers(_token("srp"))).status_code==403


@pytest.mark.parametrize('perspective',['users','artifacts'])
def test_matrix_route_forwards_perspective_and_literal_search(monkeypatch,perspective):
    main=_load_main(monkeypatch)
    monkeypatch.setattr(main,'require_analytics_reporting_access',lambda *args: True)
    captured=[]
    monkeypatch.setattr(main,'get_access_matrix',lambda **kw: captured.append(kw) or {'rows':[]})
    response=TestClient(main.app).get('/artifacts/srp/usage-monitoring-dashboard/access-summary',params={'perspective':perspective,'search':"synthetic_%'",'offset':50},headers=_auth_headers(_token('srp')))
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    assert captured==[{'client_key':'srp','perspective':perspective,'search':"synthetic_%'",'offset':50,'audience':'all'}]


def test_matrix_query_parses_for_both_perspectives(monkeypatch):
    import re
    from pglast import parse_sql
    for perspective in ['users','artifacts']:
        db=install_metadata(monkeypatch,[[(0,)],[],[]])
        usage_access.get_access_matrix(client_key='srp',perspective=perspective)
        for sql,params in db.calls:
            replacements={'client':"'srp'",'now':"now()",'search':"''",'offset':'0','ids':"ARRAY[]::text[]",'audience':"'all'",'admin_role':"'srpdev_bicycle_dev'"}
            parsed=re.sub(r'%\((\w+)\)s',lambda m:replacements[m[1]],sql)
            parse_sql(parsed)


@pytest.mark.parametrize('audience',['srp','bicycle'])
@pytest.mark.parametrize('perspective',['users','artifacts'])
def test_audience_filter_is_applied_to_scoped_assignments_before_pagination(monkeypatch,audience,perspective):
    db=install_metadata(monkeypatch,[[(0,)],[],[]])
    result=usage_access.get_access_matrix(client_key='srp',perspective=perspective,audience=audience)
    assert result['audience']==audience
    sql,params=db.calls[-1]
    assert 'u.audience_key=%(audience)s' in sql
    assert params['admin_role']=='srpdev_bicycle_dev' and params['audience']==audience
    if perspective=='users':
        assert 'u.audience_key=%(audience)s' in db.calls[2][0]
        assert 'u.audience_key=%(audience)s' in db.calls[3][0]


@pytest.mark.parametrize('query',['perspective=users&audience=unknown','audience=unknown'])
def test_invalid_audience_is_rejected(monkeypatch,query):
    main=_load_main(monkeypatch)
    response=TestClient(main.app).get('/artifacts/srp/usage-monitoring-dashboard/access-summary?'+query,headers=_auth_headers(_token('srp')))
    assert response.status_code==400


def test_bounded_summary_accepts_database_owned_bicycle_audience(monkeypatch):
    main = _load_main(monkeypatch)
    monkeypatch.setattr(main, 'require_analytics_reporting_access', lambda *args: True)
    captured = []
    monkeypatch.setattr(main, 'get_access_summary',
                        lambda **kw: captured.append(kw) or {'users': [], 'audience': kw['audience']})
    response = TestClient(main.app).get(
        '/artifacts/srp/usage-monitoring-dashboard/access-summary',
        params={'audience': 'bicycle', 'days': 30},
        headers=_auth_headers(_token('srp')),
    )
    assert response.status_code == 200
    assert captured == [{
        'client_key': 'srp', 'days': 30, 'offset': 0,
        'search': '', 'audience': 'bicycle',
    }]

def test_qa_admin_cohort_uses_configured_role(monkeypatch):
    monkeypatch.setenv('SRP_BICYCLE_ADMIN_ROLE', 'srpqa_admin')
    db=install_metadata(monkeypatch,[[(0,)],[],[]])
    result=usage_access.get_access_matrix(client_key='srp',perspective='users',audience='bicycle')
    assert result['audience']=='bicycle'
    assert db.calls[-1][1]['admin_role']=='srpqa_admin'


@pytest.mark.parametrize('requested', ['all', 'srp', 'bicycle'])
def test_corporate_viewer_is_forced_to_srp_audience(monkeypatch, requested):
    main=_load_main(monkeypatch)
    monkeypatch.setattr(main,'require_analytics_reporting_access',lambda *args: False)
    captured=[]
    monkeypatch.setattr(main,'get_access_matrix',lambda **kw: captured.append(kw) or {'rows':[], 'audience':kw['audience']})
    response=TestClient(main.app).get('/artifacts/srp/usage-monitoring-dashboard/access-summary',
        params={'perspective':'users','audience':requested},headers=_auth_headers(_token('srp')))
    assert response.status_code==200
    assert captured[0]['audience']=='srp'
    assert response.json()['allowed_audiences']==['srp']


def test_bicycle_admin_keeps_all_access_audiences(monkeypatch):
    main=_load_main(monkeypatch)
    monkeypatch.setattr(main,'require_analytics_reporting_access',lambda *args: True)
    monkeypatch.setattr(main,'get_access_matrix',lambda **kw: {'rows':[], 'audience':kw['audience']})
    response=TestClient(main.app).get('/artifacts/srp/usage-monitoring-dashboard/access-summary',
        params={'perspective':'artifacts','audience':'bicycle'},headers=_auth_headers(_token('srp')))
    assert response.status_code==200
    assert response.json()['audience']=='bicycle'
    assert response.json()['allowed_audiences']==['all','srp','bicycle']


@pytest.mark.parametrize('client', ['srp', 'rf'])
@pytest.mark.parametrize('perspective', ['users', 'artifacts'])
def test_catalog_scope_is_resolved_by_database_contract(monkeypatch, client, perspective):
    if client == 'rf':
        monkeypatch.setenv('ANALYTICS_REPORTING_CLIENT_KEY', 'rf')
        monkeypatch.setenv('ANALYTICS_BICYCLE_ADMIN_ROLE', 'rfdev_admin')
    db = install_metadata(monkeypatch, [[(0,)], [], []])
    usage_access.get_access_matrix(client_key=client, perspective=perspective)
    queries = '\n'.join(sql for sql, _ in db.calls)
    assert 'analytics_reporting.artifact_access_edges' in queries
    if perspective == 'artifacts':
        assert 'analytics_reporting.reportable_artifacts' in queries
    assert 'delivery_mode' not in queries
    assert "client)s <> 'srp'" not in queries
