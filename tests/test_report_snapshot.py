from contextlib import contextmanager
from datetime import datetime, timezone
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app import report_snapshot as m
from test_usage_summary import _load_main, _auth_headers, _token


def bind(monkeypatch, documents=None, missing=False):
    class Cursor:
        def __init__(self,value):self.value=value
        def fetchone(self):return (None if missing else 'reporting.report_output_snapshot',)
        def fetchall(self):return self.value
    class Conn:
        def execute(self,sql,params=None):
            if 'FROM reporting.report_output_snapshot' in sql:
                assert params == ('srp',m.ARTIFACT)
            assert not any(w in sql.lower() for w in ('insert ','update ','delete ','security.'))
            return Cursor(documents or [])
    @contextmanager
    def factory():yield Conn()
    monkeypatch.setattr(m,'get_data_conn',factory)


def documents(rows,section='access-users'):
    stamp=datetime(2026,11,5,tzinfo=timezone.utc)
    return [(key,dict(client_key='srp',audience='srp',rows=rows if key==section else [],total=len(rows) if key==section else 0,has_more=False),
             'production',stamp,'a'*64) for key in ('access-users','access-artifacts','distributions','facilities','slas')]


def test_no_copy_uses_existing_live_path(monkeypatch):
    bind(monkeypatch,missing=True)
    assert m.get_copied_report('access-users') is None
    bind(monkeypatch)
    assert m.get_copied_report('facilities') is None


def test_search_pagination_and_source_stamp(monkeypatch):
    rows=[dict(display_name=f'Example {i}',username=f'user{i}@example.invalid',active=True,matches=[]) for i in range(65)]
    bind(monkeypatch,documents(rows))
    data=m.get_copied_report('access-users',offset=50)
    assert len(data['rows'])==15 and data['total']==65 and not data['has_more']
    assert data['source_environment']=='production' and data['captured_at']=='2026-11-05T00:00:00+00:00'
    assert m.get_copied_report('access-users',search='user60@')['total']==1


def test_distribution_filters_match_live_contract(monkeypatch):
    rows=[dict(report_name='Weekly',artifact_key='weekly',recipient_email='example@example.invalid',facilities=['North'],result='missing'),
          dict(report_name='Monthly',artifact_key='monthly',recipient_email='other@example.invalid',facilities=['South'],result='extra')]
    bind(monkeypatch,documents(rows,'distributions'))
    assert m.get_copied_report('distributions',facility='North',result='missing')['rows']==rows[:1]
    assert m.get_copied_report('distributions',search='South')['rows']==rows[1:]


def test_incomplete_copy_never_falls_back_to_mixed_live_data(monkeypatch):
    bind(monkeypatch,documents([])[:-1])
    with pytest.raises(RuntimeError):m.get_copied_report('facilities')


def test_named_access_route_reads_copy_and_keeps_auth(monkeypatch):
    main=_load_main(monkeypatch)
    monkeypatch.setattr(main,'require_analytics_reporting_access',lambda *args:True)
    monkeypatch.setattr(main,'get_copied_report',lambda *args,**kwargs:dict(client_key='srp',audience='srp',rows=[],total=0))
    monkeypatch.setattr(main,'get_access_matrix',lambda **kwargs:pytest.fail('live source used'))
    client=TestClient(main.app);path='/artifacts/srp/client-access-distribution-report/access-summary?perspective=users&audience=srp'
    assert client.get(path).status_code==401
    assert client.get(path,headers=_auth_headers(_token('rf'))).status_code==403
    response=client.get(path,headers=_auth_headers(_token('srp')))
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    assert response.json()['allowed_audiences']==['all','srp','bicycle']


def test_denied_viewer_never_reads_snapshot(monkeypatch):
    main=_load_main(monkeypatch)
    def deny(*args):raise HTTPException(403,'denied')
    monkeypatch.setattr(main,'require_analytics_reporting_access',deny)
    monkeypatch.setattr(main,'get_copied_report',lambda *args,**kwargs:pytest.fail('snapshot read'))
    client=TestClient(main.app)
    for section in ('access-summary?perspective=users&audience=srp','distribution-summary','facility-summary'):
        assert client.get('/artifacts/srp/client-access-distribution-report/'+section,headers=_auth_headers(_token('srp'))).status_code==403


def test_copied_facilities_uses_only_current_roster(monkeypatch):
    rows=[dict(facility_name='Example '+str(i),facility_key='ecw:'+str(i),entity_codes=[],sources=['facilities'],operational_data='no_data',approved=None) for i in range(55)]
    rows.append(dict(facility_name='Excel only',facility_key='name:Excel only',entity_codes=[],sources=['facility_reference']))
    docs=documents(rows,'facilities')
    for key,payload,*_ in docs:
        if key=='facilities':payload['sources']=[dict(source='facilities',status='available')]
    bind(monkeypatch,docs)
    assert m.get_copied_report('facilities')['total']==55
    assert len(m.get_copied_report('facilities',offset=50)['rows'])==5
    assert m.get_copied_report('facilities',search='Excel only')['total']==0
    for key,payload,*_ in docs:
        if key=='facilities':payload['sources'][0]['status']='unavailable'
    bind(monkeypatch,docs)
    with pytest.raises(RuntimeError):m.get_copied_report('facilities')
