"""Inert copied report results. Never used to grant access or send deliveries."""
from copy import deepcopy
from .db import get_data_conn

ARTIFACT = 'client-access-distribution-report'


def get_copied_report(section, *, search='', facility='', result='', status='', offset=0):
    if section not in {'access-users', 'access-artifacts', 'distributions', 'facilities'}:
        raise ValueError('Invalid report section')
    if len(search) > 100 or len(facility) > 200 or not 0 <= offset <= 100000:
        raise ValueError('Invalid reporting bounds')
    if result not in {'', 'not_confirmed', 'matches', 'missing', 'extra', 'scope_mismatch', 'source_unavailable'}:
        raise ValueError('Invalid distribution result')
    if status not in {'', 'exceptions', 'no_data', 'not_configured', 'disabled', 'unresolved', 'not_confirmed'}:
        raise ValueError('Invalid facility status')
    with get_data_conn() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout='10s'")
        if conn.execute("SELECT to_regclass('reporting.report_output_snapshot')").fetchone()[0] is None:
            return None
        rows = conn.execute('''SELECT section_key,payload,source_environment,captured_at,snapshot_sha256
          FROM reporting.report_output_snapshot
          WHERE client_key=%s AND artifact_key=%s ORDER BY section_key''', ('srp', ARTIFACT)).fetchall()
    if not rows:
        return None
    expected = {'access-users', 'access-artifacts', 'distributions', 'facilities', 'slas'}
    if {r[0] for r in rows} != expected or len({(r[2], r[3], r[4]) for r in rows}) != 1 or rows[0][2] != 'production':
        raise RuntimeError('Copied report is incomplete')
    selected = next(r for r in rows if r[0] == section)
    payload = deepcopy(selected[1])
    if payload.get('client_key') != 'srp' or payload.get('audience') != 'srp' or not isinstance(payload.get('rows'), list):
        raise RuntimeError('Copied report could not be verified')
    q = search.strip().casefold()
    records = payload['rows']
    if section.startswith('access-'):
        records = [r for r in records if not q or q in r['display_name'].casefold()
                   or q in str(r.get('username') if section == 'access-users' else r.get('artifact_key')).casefold()]
    elif section == 'distributions':
        # Match the live report's search and facility/result filters.
        records = [r for r in records if (not q or q in ' '.join([r['report_name'],r['artifact_key'],r['recipient_email'],*r['facilities']]).casefold())
                   and (not facility or facility in r['facilities']) and (not result or r['result'] == result)]
    else:
        records = [r for r in records if (not q or q in ' '.join([r['facility_name'], r['facility_key'], *r['entity_codes']]).casefold()) and (
            not status or status == 'exceptions' and any(x != 'approval_not_confirmed' for x in r['exceptions']) or
            status == 'no_data' and r['operational_data'] == 'no_data' or
            status == 'not_configured' and any(r[x]['configuration'] == 'not_configured' for x in ('weekly','monthly')) or
            status == 'disabled' and any(r[x]['configuration'] == 'disabled' for x in ('weekly','monthly')) or
            status == 'unresolved' and any('unresolved' in x or 'ambiguous' in x or 'mismatch' in x for x in r['exceptions']) or
            status == 'not_confirmed' and r['approved'] is None)]
    payload.update(rows=records[offset:offset+50], total=len(records), offset=offset, limit=50,
                   has_more=offset+50<len(records), source_environment=selected[2],
                   captured_at=selected[3].isoformat())
    return payload
