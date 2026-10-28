"""Synthetic-only tests for facility identity, coverage and authorization."""
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from app import facility_reporting as reporting
from test_usage_summary import _load_main, _auth_headers, _token


def sources(**extra):
    data = {name: [] for name, _, _ in reporting.SOURCES}
    data['recipient_registry'] = []
    data.update(extra)
    return data


def test_union_preserves_no_data_disabled_unmapped_and_accounting_only():
    data = sources(facilities=[('1','Example North'),('2','Example Empty')],
        appointments=[('1',)],claims=[('1',)],
        weekly_mapping=[('Example North',True,True,True),('Example Legacy',False,False,True)],
        pnl_entities=[('example_entity','Example Accounting')])
    rows = {r['facility_name']:r for r in reporting.build_inventory(data)}
    assert set(rows) == {'Example North','Example Empty','Example Legacy','Example Accounting'}
    assert rows['Example Empty']['operational_data']=='no_data'
    assert rows['Example Legacy']['weekly']['configuration']=='disabled'
    assert 'unresolved_identity' in rows['Example Legacy']['exceptions']
    assert rows['Example Accounting']['pnl']['source']=='entity_only'
    assert all(r['approved'] is None for r in rows.values())


def test_partial_and_unavailable_data_are_distinct_from_no_data():
    data=sources(facilities=[('1','Example')],appointments=[('1',)])
    assert reporting.build_inventory(data)[0]['operational_data']=='partial'
    data['claims']=None
    assert reporting.build_inventory(data)[0]['operational_data']=='unavailable'


def test_monthly_configuration_is_separate_from_eligibility_and_registry():
    data=sources(facilities=[('1','Example')],weekly_mapping=[('Example',True,True,True)],
        monthly_report=[('1','Example','unavailable')])
    row=reporting.build_inventory(data)[0]
    assert row['monthly']['configuration']=='configured'
    assert row['monthly']['eligibility']=='no_source_activity'
    assert row['monthly']['registry']=='not_registered'
    data['monthly_report']=[('1','Example','partial')]
    assert reporting.build_inventory(data)[0]['monthly']['eligibility']=='eligible'
    data['monthly_report'].append(('1','Example','available'))
    assert reporting.build_inventory(data)[0]['monthly']['eligibility']=='unresolved'


def test_exact_crosswalk_matches_but_similar_names_are_not_guessed():
    data=sources(facilities=[('1','Example North')],weekly_mapping=[('Example North Old',True,True,True)],
        pnl_crosswalk=[('entity','Example North',True)],pnl_entities=[('entity','Accounting Name')],pnl_data=[('entity',)])
    rows=reporting.build_inventory(data)
    assert len(rows)==2
    canonical=next(r for r in rows if r['facility_key']=='ecw:1')
    assert canonical['pnl']['source']=='available'
    assert canonical['entity_codes']==['entity']
    assert canonical['weekly']['configuration']=='not_configured'


def test_duplicate_names_never_attach_mapping_to_arbitrary_facility():
    rows=reporting.build_inventory(sources(facilities=[('1','Example'),('2','Example')],weekly_mapping=[('Example',True,True,True)]))
    assert len(rows)==3
    assert all(r['weekly']['configuration']=='not_configured' for r in rows if r['facility_ids'])
    assert any('ambiguous_name' in r['exceptions'] for r in rows)
    assert all(r['report_statuses']['weekly']=='check_facility_name' for r in rows)


def test_orphan_operational_and_monthly_facilities_are_not_lost():
    rows=reporting.build_inventory(sources(appointments=[('1',)],monthly_report=[('2','Example','partial')]))
    assert {r['facility_key'] for r in rows}=={'ecw:1','ecw:2'}
    assert all('missing_facility_roster' in r['exceptions'] for r in rows)


def test_approval_requires_full_provenance_and_conflicts_stay_unknown():
    record=('ecw:1','Example','inactive',False,False,False,'synthetic','Example Reviewer','2026-10-28','Example Owner','v1','SRP review')
    data=sources(facilities=[('1','Example')],weekly_mapping=[('Example',True,True,True)],facility_approval=[record])
    row=reporting.build_inventory(data)[0]
    assert row['practice_status']=='inactive'
    assert 'weekly_excluded_but_present' in row['exceptions']
    data['facility_approval']=[record,record]
    row=reporting.build_inventory(data)[0]
    assert row['approved'] is None and 'approval_conflict' in row['exceptions']
    data['facility_approval']=[record[:-1]+(None,)]
    assert reporting.build_inventory(data)[0]['approved'] is None


def test_approval_only_facility_keeps_stable_key():
    record=('approved:example','Example','active',True,False,False,'synthetic','Reviewer','2026-10-28','Owner','v1','SRP review')
    row=reporting.build_inventory(sources(facility_approval=[record]))[0]
    assert row['facility_key']=='approved:example'
    assert 'weekly_missing_report' in row['exceptions']


def test_bounds_paging_search_and_unavailable_sources(monkeypatch):
    monkeypatch.setattr(reporting,'get_analytics_reporting_config',lambda client:type('Config',(),{'admin_role':'example-admin'})())
    data=sources(facilities=[(str(i),'Example '+str(i)) for i in range(55)])
    relations={relation:data[name] for name,relation,_ in reporting.SOURCES}
    monkeypatch.setattr(reporting,'_read',lambda factory,relation,query,*args: relations.get(relation,[]))
    first=reporting.get_facility_report();last=reporting.get_facility_report(offset=50)
    assert first['total']==55 and len(first['rows'])==50 and first['has_more']
    assert len(last['rows'])==5 and not last['has_more']
    assert reporting.get_facility_report(search='Example 54')['total']==1
    assert reporting.get_facility_report(status='no_data')['total']==55
    with pytest.raises(ValueError):reporting.get_facility_report(status='invented')
    with pytest.raises(ValueError):reporting.get_facility_report(offset=-1)
    monkeypatch.setattr(reporting,'_read',lambda *a:None)
    with pytest.raises(RuntimeError):reporting.get_facility_report()


def test_facility_route_preserves_existing_authorization_and_no_store(monkeypatch):
    main=_load_main(monkeypatch);calls=[]
    monkeypatch.setattr(main,'require_analytics_reporting_access',lambda *args:calls.append(args))
    monkeypatch.setattr(main,'get_facility_report',lambda **kw:{'client_key':'srp','audience':'srp','rows':[]})
    client=TestClient(main.app);path='/artifacts/srp/client-access-distribution-report/facility-summary'
    assert client.get(path).status_code==401
    assert client.get(path,headers=_auth_headers(_token('rf'))).status_code==403
    response=client.get(path,headers=_auth_headers(_token('srp')))
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    assert calls[0][1:]==('srp','access')
    def denied(*args):raise HTTPException(403,'denied')
    monkeypatch.setattr(main,'require_analytics_reporting_access',denied)
    monkeypatch.setattr(main,'get_facility_report',lambda **kw:pytest.fail('source read before authorization'))
    assert client.get(path,headers=_auth_headers(_token('srp'))).status_code==403


def test_business_statuses_are_concise_and_registration_is_not_an_inclusion_test():
    data=sources(facilities=[('1','Example')],weekly_mapping=[('Example',True,True,True)],
        weekly_report=[('Example',)],monthly_report=[('1','Example','available')],
        pnl_crosswalk=[('entity','Example',True)],pnl_entities=[('entity','Example')],
        pnl_data=[('entity',)],pnl_scope=[('entity','example-email',True)],pnl_recipients=[('entity',True)])
    row=reporting.build_inventory(data)[0]
    assert row['report_statuses']=={'weekly':'included','monthly':'included','pnl':'included'}
    assert row['weekly']['registry']=='not_registered'
    data['recipient_registry']=None
    assert reporting.build_inventory(data)[0]['report_statuses']==row['report_statuses']
    data['monthly_report']=[('1','Example','unavailable')]
    assert reporting.build_inventory(data)[0]['report_statuses']['monthly']=='waiting_for_activity'
    data['monthly_report']=None
    assert reporting.build_inventory(data)[0]['report_statuses']['monthly']=='not_verified'


@pytest.mark.parametrize('mapping,expected', [
    ([], 'not_configured'),
    ([('Example',False,False,True)], 'excluded'),
    ([('Example',True,False,True)], 'not_ready'),
    ([('Example',True,True,False)], 'not_ready'),
    (None,'not_verified'),
])
def test_business_configuration_statuses(mapping,expected):
    row=reporting.build_inventory(sources(facilities=[('1','Example')],weekly_mapping=mapping))[0]
    assert row['report_statuses']['weekly']==row['report_statuses']['monthly']==expected


def test_ready_mapping_without_facility_is_not_claimed_included():
    row=reporting.build_inventory(sources(weekly_mapping=[('Example Unmatched',True,True,True)],weekly_report=[]))[0]
    assert row['report_statuses']['weekly']==row['report_statuses']['monthly']=='check_facility_name'


def test_pnl_available_without_active_scope_is_not_claimed_included():
    data=sources(pnl_entities=[('example','Example')],pnl_data=[('example',)])
    assert reporting.build_inventory(data)[0]['report_statuses']['pnl']=='available_not_included'
    data['pnl_data']=[]
    assert reporting.build_inventory(data)[0]['report_statuses']['pnl']=='no_report_data'


def test_source_checks_do_not_execute_report_renderers():
    queries={name:query for name,_,query in reporting.SOURCES}
    assert 'render_payload' not in queries['weekly_report']+queries['monthly_report']
    assert 'srp_weekly_email_prototype_burst_recipients' in queries['weekly_report']
    assert 'send_enabled AND ready_for_release' in queries['weekly_report']
    monthly=queries['monthly_report']
    assert "AT TIME ZONE 'America/New_York'" in monthly
    assert "interval '1 month'" in monthly
    assert 'a._is_current' in monthly and 'c._is_current' in monthly
    assert '>= m.start_date' in monthly and '< m.end_date' in monthly


def test_pnl_scope_requires_ready_recipient_before_claiming_included():
    data=sources(pnl_entities=[('example','Example')],pnl_data=[('example',)],pnl_scope=[('example','example-email',True)])
    assert reporting.build_inventory(data)[0]['report_statuses']['pnl']=='not_ready'
    data['pnl_recipients']=None
    assert reporting.build_inventory(data)[0]['report_statuses']['pnl']=='not_verified'
    data['pnl_recipients']=[('example',True)]
    assert reporting.build_inventory(data)[0]['report_statuses']['pnl']=='included'
