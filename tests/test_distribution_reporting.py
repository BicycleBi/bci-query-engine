from contextlib import contextmanager
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from app import distribution_reporting as reporting
from test_usage_summary import _load_main, _auth_headers, _token


def approval(**extra):
    return dict(facilities=['north'], delivery_type='to', approval_source='synthetic approval',
                approver='Example Reviewer', approval_date='2026-10-27', maintenance_owner='Example Owner', **extra)


@pytest.mark.parametrize('configured,approved,expected', [
    ({'enabled': True, 'facilities': ['north'], 'facility_resolved': True, 'delivery_type': 'to'}, None, 'not_confirmed'),
    (None, approval(), 'missing'),
    ({'enabled': False}, approval(), 'missing'),
    ({'enabled': True, 'facilities': ['north'], 'facility_resolved': True, 'delivery_type': 'to'}, approval(), 'matches'),
    ({'enabled': True}, approval(excluded=True), 'extra'),
    ({'enabled': True, 'facilities': ['north','south'], 'facility_resolved': True, 'delivery_type': 'to'}, approval(), 'scope_mismatch'),
    ({'enabled': True, 'facilities': [], 'facility_resolved': False}, approval(), 'not_confirmed'),
    (None, {'approval_source': 'inferred'}, 'not_confirmed'),
])
def test_comparison_requires_approval_and_resolved_scope(configured, approved, expected):
    assert reporting.compare_assignment(configured, approved) == expected


def test_report_preserves_authorization_and_no_store(monkeypatch):
    main = _load_main(monkeypatch)
    calls = []
    monkeypatch.setattr(main, 'require_analytics_reporting_access', lambda *args: calls.append(args))
    monkeypatch.setattr(main, 'get_distribution_report', lambda **kw: {'audience':'srp','rows':[]})
    client = TestClient(main.app)
    path='/artifacts/srp/client-access-distribution-report/distribution-summary'
    assert client.get(path).status_code == 401
    assert client.get(path,headers=_auth_headers(_token('rf'))).status_code == 403
    response=client.get(path,headers=_auth_headers(_token('srp')))
    assert response.status_code == 200
    assert response.headers['cache-control']=='no-store'
    assert len(calls)==1
    assert calls[0][1:] == ('srp', 'access')


def test_denied_viewer_never_reads_contacts(monkeypatch):
    main = _load_main(monkeypatch)
    def denied(*args): raise HTTPException(403,'denied')
    monkeypatch.setattr(main,'require_analytics_reporting_access',denied)
    monkeypatch.setattr(main,'get_distribution_report',lambda **kw: pytest.fail('contacts read'))
    assert TestClient(main.app).get('/artifacts/srp/client-access-distribution-report/distribution-summary',headers=_auth_headers(_token('srp'))).status_code==403


def test_unavailable_sources_are_not_clean_results(monkeypatch):
    monkeypatch.setattr(reporting,'get_analytics_reporting_config',lambda client:type('Config',(),{'admin_role':'example-admin'})())
    monkeypatch.setattr(reporting,'_read',lambda *args,**kw:None)
    with pytest.raises(RuntimeError): reporting.get_distribution_report()


def test_partial_sources_remain_unknown_and_known_admins_excluded(monkeypatch):
    monkeypatch.setattr(reporting,'get_analytics_reporting_config',lambda client:type('Config',(),{'admin_role':'example-admin'})())
    def read(factory, relation, query, params=None):
        if relation=='app.artifact_recipients':
            return [('example-email','Example report',True,'shared@example.invalid','to',True)]
        if relation=='analytics_reporting.effective_assignments':return [('admin@example.invalid',)]
        if relation=='public.srp_report_distribution_approval':
            return [('admin-email','admin@example.invalid','to',['north'],False,'synthetic','Example','2026-10-27','Example Owner','v1')]
        return None
    monkeypatch.setattr(reporting,'_read',read)
    result=reporting.get_distribution_report()
    assert len(result['rows'])==1
    assert result['rows'][0]['result']=='not_confirmed'
    assert result['rows'][0]['recipient_email']=='shared@example.invalid'
    assert result['coverage']=='partial'
    assert any(source['status']=='unavailable' for source in result['sources'])


def test_mapping_without_registry_is_visible_and_admin_mapping_hidden(monkeypatch):
    monkeypatch.setattr(reporting,'get_analytics_reporting_config',lambda client:type('Config',(),{'admin_role':'example-admin'})())
    def read(factory, relation, query, params=None):
        if relation=='app.artifact_recipients':return []
        if relation=='analytics_reporting.effective_assignments':return [('admin@example.invalid',)]
        if relation=='public.srp_weekly_report_recipient_mapping':return [('Example North','shared@example.invalid','to',True,True),('Example South','admin@example.invalid','to',True,True)]
        return None
    monkeypatch.setattr(reporting,'_read',read)
    result=reporting.get_distribution_report()
    assert len(result['rows'])==2
    assert all(row['recipient_email']=='shared@example.invalid' for row in result['rows'])
    assert all(row['registry_state']=='not_configured' and row['result']=='not_confirmed' for row in result['rows'])
