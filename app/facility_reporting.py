"""Current SRP facility inventory; read-only, no contacts or inferred approvals."""
from collections import defaultdict
from datetime import datetime, timezone

from .db import get_data_conn, get_metadata_conn
from .distribution_reporting import _read, _slug
from .analytics import get_analytics_reporting_config

PAGE_SIZE = 50
FILTERS = {'', 'exceptions', 'no_data', 'not_configured', 'disabled', 'unresolved', 'not_confirmed'}
# Fixed projections only: never return metrics, contacts or full report payloads.
SOURCES = [
    ('facilities', 'public.srp_core_facility', "SELECT facility_id, facility_name FROM public.srp_core_facility WHERE _is_current AND nullif(btrim(facility_id),'') IS NOT NULL AND nullif(btrim(facility_name),'') IS NOT NULL ORDER BY facility_id"),
    ('appointments', 'public.srp_core_appointments', "SELECT DISTINCT nullif(btrim(facility_id),'') FROM public.srp_core_appointments WHERE _is_current AND nullif(btrim(facility_id),'') IS NOT NULL ORDER BY 1"),
    ('claims', 'public.srp_core_claim_activity_associated_by_service_date', "SELECT DISTINCT nullif(btrim(facility_id),'') FROM public.srp_core_claim_activity_associated_by_service_date WHERE _is_current AND nullif(btrim(facility_id),'') IS NOT NULL ORDER BY 1"),
    ('weekly_mapping', 'public.srp_weekly_report_recipient_mapping', "SELECT facility_name, send_enabled, ready_for_release, nullif(btrim(recipient_email),'') IS NOT NULL FROM public.srp_weekly_report_recipient_mapping ORDER BY facility_name"),
    ('weekly_report', 'public.srp_weekly_email_prototype_burst_recipients', "SELECT DISTINCT slice_key FROM public.srp_weekly_email_prototype_burst_recipients WHERE send_enabled AND ready_for_release AND recipient_email IS NOT NULL ORDER BY slice_key"),
    # Monthly releases accept both available and partial coverage. That boundary
    # depends only on source-row presence in the report month, not any measure.
    # Project the same current facility/date selection without constructing JSON,
    # provider tables, EWC or financial calculations in the full renderer.
    ('monthly_report', 'public.srp_weekly_report_facility_input_v1', """
      WITH report_month AS (
        SELECT (date_trunc('month', CURRENT_TIMESTAMP AT TIME ZONE 'America/New_York') - interval '1 month')::date AS start_date,
               date_trunc('month', CURRENT_TIMESTAMP AT TIME ZONE 'America/New_York')::date AS end_date
      ), activity AS (
        SELECT DISTINCT nullif(btrim(a.facility_id),'') AS facility_id
        FROM public.srp_core_appointments a CROSS JOIN report_month m
        WHERE a._is_current AND nullif(btrim(a.facility_id),'') IS NOT NULL
          AND nullif(btrim(a.appointment_date),'')::date >= m.start_date
          AND nullif(btrim(a.appointment_date),'')::date < m.end_date
        UNION
        SELECT DISTINCT nullif(btrim(c.facility_id),'')
        FROM public.srp_core_claim_activity_associated_by_service_date c CROSS JOIN report_month m
        WHERE c._is_current AND nullif(btrim(c.facility_id),'') IS NOT NULL
          AND nullif(btrim(c.service_date),'')::date >= m.start_date
          AND nullif(btrim(c.service_date),'')::date < m.end_date
      )
      SELECT f.facility_id, f.facility_name,
             CASE WHEN a.facility_id IS NOT NULL THEN 'available' ELSE 'unavailable' END
      FROM public.srp_weekly_report_facility_input_v1 f
      LEFT JOIN activity a USING (facility_id)
      ORDER BY f.facility_id,f.facility_name
    """),
    ('pnl_crosswalk', 'public.srp_pnl_weekly_recipient_facility_map', 'SELECT entity_code, weekly_facility_name, active FROM public.srp_pnl_weekly_recipient_facility_map ORDER BY entity_code'),
    ('pnl_entities', 'public.srp_qbo_pnl_account_min', 'SELECT DISTINCT entity_code, entity_name FROM public.srp_qbo_pnl_account_min ORDER BY entity_code,entity_name'),
    ('pnl_data', 'public.srp_qbo_pnl_detail_min', 'SELECT DISTINCT entity_code FROM public.srp_qbo_pnl_detail_min ORDER BY entity_code'),
    ('pnl_scope', 'public.srp_pnl_owner_delivery_scope', 'SELECT entity_code, artifact_key, active FROM public.srp_pnl_owner_delivery_scope ORDER BY entity_code'),
    ('pnl_recipients', 'public.srp_pnl_owner_delivery_recipient_source', """
      SELECT entity_code, bool_or(send_enabled AND ready_for_release AND nullif(btrim(recipient_email),'') IS NOT NULL)
      FROM public.srp_pnl_owner_delivery_recipient_source GROUP BY entity_code ORDER BY entity_code
    """),
    ('facility_approval', 'public.srp_report_facility_approval', "SELECT facility_key, facility_name, practice_status, weekly_included, monthly_included, pnl_included, approval_source, approver, approval_date, maintenance_owner, approval_version, maintenance_process FROM public.srp_report_facility_approval WHERE client_key='srp' AND active AND (effective_from IS NULL OR effective_from<=CURRENT_DATE) AND (effective_until IS NULL OR effective_until>=CURRENT_DATE) ORDER BY facility_key"),
]


def business_statuses(row):
    """Describe actual configured inclusion; approval stays independent."""
    configuration = row['weekly']['configuration']
    if configuration == 'unavailable':
        weekly = monthly = 'not_verified'
    elif 'ambiguous_name' in row['exceptions']:
        weekly = monthly = 'check_facility_name'
    elif configuration == 'not_configured':
        weekly = monthly = 'not_configured'
    elif configuration == 'disabled':
        weekly = monthly = 'excluded'
    elif configuration == 'not_ready':
        weekly = monthly = 'not_ready'
    elif not row['facility_ids'] or 'ambiguous_name' in row['exceptions']:
        weekly = monthly = 'check_facility_name'
    else:
        weekly = ('not_verified' if row['weekly']['report_inclusion'] == 'unavailable' else
                  'included' if row['weekly']['report_inclusion'] == 'present' else 'check_facility_name')
        monthly = ('included' if row['monthly']['eligibility'] == 'eligible' else
                   'waiting_for_activity' if row['monthly']['eligibility'] == 'no_source_activity' else 'not_verified')
    pnl = row['pnl']
    if pnl['source'] == 'unavailable' or pnl['scope'] == 'unavailable':
        pnl_status = 'not_verified'
    elif 'ambiguous_pnl_crosswalk' in row['exceptions'] or 'inactive_pnl_crosswalk' in row['exceptions']:
        pnl_status = 'check_facility_name'
    elif pnl['source'] == 'available':
        pnl_status = ('available_not_included' if pnl['scope'] != 'configured' else
                      'not_verified' if pnl['recipient_ready'] is None else
                      'included' if pnl['recipient_ready'] else 'not_ready')
    elif pnl['source'] == 'entity_only':
        pnl_status = 'no_report_data'
    else:
        pnl_status = 'not_available'
    return dict(weekly=weekly, monthly=monthly, pnl=pnl_status)


def build_inventory(data):
    """Union by stable IDs and exact, unambiguous source mappings; no fuzzy matching."""
    rows = {}
    names = defaultdict(set)

    def ensure(key, name):
        if key not in rows:
            rows[key] = dict(facility_key=key, facility_name=name, facility_ids=[], entity_codes=[],
                exceptions=[], approved=None, practice_status='not_confirmed', weekly_mapping=[], monthly_coverage=[], pnl_scope=[], sources=[])
        return rows[key]

    for identity, name in data.get('facilities') or []:
        key = 'ecw:' + str(identity).strip()
        row = ensure(key, name.strip())
        row['facility_ids'] = [str(identity).strip()]
        names[name.strip()].add(key)
        row['sources'].append('facilities')

    def by_name(name, source):
        name = str(name or '').strip()
        if not name:
            return None
        matches = names.get(name, set())
        key = next(iter(matches)) if len(matches) == 1 else 'name:' + name
        row = ensure(key, name)
        if not matches:
            names[name].add(key)
        if len(matches) > 1:
            row['exceptions'].append('ambiguous_name')
            for match in matches:
                rows[match]['exceptions'].append('ambiguous_name')
        if not row['facility_ids']:
            row['exceptions'].append('unresolved_identity')
        row['sources'].append(source)
        return row

    for source in ('appointments', 'claims'):
        for (identity,) in data.get(source) or []:
            if identity is None:
                continue
            key = 'ecw:' + str(identity).strip()
            if key not in rows:
                row = ensure(key, 'Unresolved facility ' + str(identity))
                row['facility_ids'] = [str(identity).strip()]
                row['exceptions'].append('missing_facility_roster')
            rows[key]['sources'].append(source)

    for name, enabled, ready, has_recipient in data.get('weekly_mapping') or []:
        row = by_name(name, 'weekly_mapping')
        if row is not None:
            row['weekly_mapping'].append(dict(enabled=bool(enabled), ready=bool(ready), has_recipient=bool(has_recipient)))
    weekly_names = set()
    for (name,) in data.get('weekly_report') or []:
        row = by_name(name, 'weekly_report')
        if row is not None:
            weekly_names.add(row['facility_key'])
    for identity, name, coverage in data.get('monthly_report') or []:
        key = 'ecw:' + str(identity or '').strip()
        row = rows.get(key) if identity else None
        if row is None and identity:
            row = ensure(key, str(name or 'Unresolved facility ' + str(identity)).strip())
            row['facility_ids'] = [str(identity).strip()]
            row['exceptions'].append('missing_facility_roster')
            names[row['facility_name']].add(key)
        elif row is None:
            row = by_name(name, 'monthly_report')
        elif name and name.strip() != row['facility_name']:
            row['exceptions'].append('monthly_name_mismatch')
        if row is not None:
            row['monthly_coverage'].append(coverage)
            row['sources'].append('monthly_report')

    entity_targets = defaultdict(set)
    for entity, name, active in data.get('pnl_crosswalk') or []:
        row = by_name(name, 'pnl_crosswalk')
        if row is None:
            continue
        entity_targets[entity].add(row['facility_key'])
        if not active:
            row['exceptions'].append('inactive_pnl_crosswalk')

    def by_entity(entity, name=None):
        targets = entity_targets.get(entity, set())
        row = rows[next(iter(targets))] if len(targets) == 1 else ensure('pnl:' + entity, name or entity)
        if not targets:
            row['exceptions'].append('unresolved_pnl_crosswalk')
        elif len(targets) > 1:
            row['exceptions'].append('ambiguous_pnl_crosswalk')
        if entity not in row['entity_codes']:
            row['entity_codes'].append(entity)
        return row

    for entity, name in data.get('pnl_entities') or []:
        by_entity(entity, name)['sources'].append('pnl_entities')
    pnl_data = {x[0] for x in data.get('pnl_data') or []}
    for entity in pnl_data:
        by_entity(entity)['sources'].append('pnl_data')
    for entity, artifact, active in data.get('pnl_scope') or []:
        row = by_entity(entity)
        row['pnl_scope'].append(dict(artifact_key=artifact, active=bool(active)))
    for entity in entity_targets:
        by_entity(entity)

    # Optional, inert relation: no table/approval data is created by this feature.
    approvals = defaultdict(list)
    for entry in data.get('facility_approval') or []:
        key, name, status, weekly, monthly, pnl, source, approver, date, owner, version, process = entry
        row = rows.get(key) or (ensure(key, str(name or key)) if key else by_name(name, 'facility_approval'))
        if row is None:
            continue
        approvals[row['facility_key']].append(dict(practice_status=status, weekly=weekly, monthly=monthly, pnl=pnl,
            approval_source=source, approver=approver, approval_date=date, maintenance_owner=owner, approval_version=version, maintenance_process=process))

    appointments = {str(x[0]).strip() for x in data.get('appointments') or []}
    claims = {str(x[0]).strip() for x in data.get('claims') or []}
    registry = {key: (active, count) for key, active, count in data.get('recipient_registry') or []}
    pnl_recipients = {entity:bool(ready) for entity,ready in data.get('pnl_recipients') or []}
    for key, row in rows.items():
        ids = set(row['facility_ids'])
        if data.get('appointments') is None or data.get('claims') is None or (not ids and data.get('facilities') is None):
            row['operational_data'] = 'unavailable'
        else:
            a, c = bool(ids & appointments), bool(ids & claims)
            row['operational_data'] = 'available' if a and c else 'partial' if a or c else 'no_data' if ids else 'unresolved'
        mapping = row.pop('weekly_mapping')
        configuration = ('unavailable' if data.get('weekly_mapping') is None else 'not_configured' if not mapping else
                         'disabled' if not any(m['enabled'] for m in mapping) else
                         'not_ready' if not any(m['enabled'] and m['ready'] and m['has_recipient'] for m in mapping) else 'configured')
        for family in ('weekly', 'monthly'):
            artifact = f"srp-{family}-practice-report-{_slug(row['facility_name'])}-email"
            target = registry.get(artifact)
            row[family] = dict(configuration=configuration, registry=('unavailable' if data.get('recipient_registry') is None else
                'not_registered' if target is None else 'enabled' if target[0] and target[1] else 'disabled_or_no_recipients'))
        row['weekly']['report_inclusion'] = 'unavailable' if data.get('weekly_report') is None else 'present' if key in weekly_names else 'not_present'
        coverage = set(row.pop('monthly_coverage'))
        row['monthly']['report_inclusion'] = 'unavailable' if data.get('monthly_report') is None else 'present' if coverage else 'not_present'
        row['monthly']['eligibility'] = ('unavailable' if data.get('monthly_report') is None else
            'unresolved' if len(coverage) != 1 or not coverage <= {'available','partial','unavailable'} else
            'no_source_activity' if coverage == {'unavailable'} else 'eligible')
        row['pnl'] = dict(source='unavailable' if data.get('pnl_data') is None or data.get('pnl_entities') is None else
            'available' if pnl_data & set(row['entity_codes']) else 'entity_only' if 'pnl_entities' in row['sources'] else 'not_present',
            recipient_ready=None if data.get('pnl_recipients') is None else any(pnl_recipients.get(entity,False) for entity in row['entity_codes']),
            scope='unavailable' if data.get('pnl_scope') is None else 'configured' if any(x['active'] for x in row['pnl_scope']) else 'disabled' if row['pnl_scope'] else 'not_configured')
        for family in ('weekly', 'monthly'):
            state = row[family]['configuration']
            if state != 'configured':
                row['exceptions'].append(family + '_' + state)
            if row[family]['registry'] != 'enabled':
                row['exceptions'].append(family + '_' + row[family]['registry'])
        if row['operational_data'] != 'available':
            row['exceptions'].append('operational_' + row['operational_data'])
        if row['monthly']['eligibility'] != 'eligible':
            row['exceptions'].append('monthly_' + row['monthly']['eligibility'])
        records = approvals.get(key, [])
        if len(records) == 1 and all(records[0].get(f) for f in ('approval_source','approver','approval_date','maintenance_owner','approval_version','maintenance_process')):
            row['approved'] = records[0]
            row['practice_status'] = records[0]['practice_status']
            for family in ('weekly','monthly','pnl'):
                included = records[0][family]
                actual = row[family]['report_inclusion'] if family != 'pnl' else row['pnl']['source']
                configured = row[family]['configuration'] if family != 'pnl' else row['pnl']['scope']
                if included is True and actual in ('not_present','entity_only'):
                    row['exceptions'].append(family + '_missing_report')
                if included is False and (actual in ('present','available') or configured == 'configured'):
                    row['exceptions'].append(family + '_excluded_but_present')
        else:
            row['exceptions'].append('approval_conflict' if len(records)>1 else 'approval_not_confirmed')
        row['exceptions'] = sorted(set(row['exceptions']))
        row['sources'] = sorted(set(row['sources']))
        row['entity_codes'].sort()
        row['report_statuses'] = business_statuses(row)
    return sorted(rows.values(), key=lambda row: (row['facility_name'].casefold(), row['facility_key']))


def get_facility_report(*, search='', status='', offset=0):
    if len(search)>100 or status not in FILTERS or not 0<=offset<=100000:
        raise ValueError('Invalid facility reporting bounds')
    data, sources = {}, []
    for name, relation, query in SOURCES:
        try:
            data[name] = _read(get_data_conn, relation, query)
        except Exception:
            data[name] = None
        sources.append(dict(source=name, status='unavailable' if data[name] is None else 'available'))
    config = get_analytics_reporting_config('srp')
    try:
        data['recipient_registry'] = _read(get_metadata_conn, 'app.artifact_recipients', """
          SELECT a.artifact_key, a.active, count(r.email) FILTER (WHERE r.active AND nullif(btrim(r.email),'') IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM analytics_reporting.effective_assignments g
              WHERE g.client_key='srp' AND g.active AND g.is_bicycle_admin
                AND g.admin_role_key=%(admin)s AND lower(g.email)=lower(r.email)))
          FROM app.artifacts a LEFT JOIN app.artifact_recipients r USING (artifact_id)
          WHERE a.client_key='srp' AND a.delivery_mode IN ('email','both')
          GROUP BY a.artifact_key,a.active ORDER BY a.artifact_key
        """, {'admin': config.admin_role})
    except Exception:
        data['recipient_registry'] = None
    sources.append(dict(source='recipient_registry',status='unavailable' if data['recipient_registry'] is None else 'available'))
    if not any(data.get(name) is not None for name in ('facilities','weekly_mapping','pnl_entities','facility_approval')):
        raise RuntimeError('Facility inventory sources unavailable')
    rows = build_inventory(data)
    approval_status = 'not_confirmed' if any(r['approved'] is None for r in rows) or not data.get('facility_approval') else 'available'
    query = search.strip().casefold()
    rows = [row for row in rows if (not query or query in ' '.join([row['facility_name'],row['facility_key'],*row['entity_codes']]).casefold()) and (
        not status or status=='exceptions' and any(x!='approval_not_confirmed' for x in row['exceptions']) or
        status=='no_data' and row['operational_data']=='no_data' or
        status=='not_configured' and any(row[f]['configuration']=='not_configured' for f in ('weekly','monthly')) or
        status=='disabled' and any(row[f]['configuration']=='disabled' for f in ('weekly','monthly')) or
        status=='unresolved' and any('unresolved' in x or 'ambiguous' in x or 'mismatch' in x for x in row['exceptions']) or
        status=='not_confirmed' and row['approved'] is None)]
    return dict(client_key='srp', audience='srp', as_of=datetime.now(timezone.utc), rows=rows[offset:offset+PAGE_SIZE],
        total=len(rows),offset=offset,limit=PAGE_SIZE,has_more=offset+PAGE_SIZE<len(rows),sources=sources,
        coverage='partial',approval_status=approval_status)
