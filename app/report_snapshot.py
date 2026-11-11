"""Inert copied report results. Never used to grant access or send deliveries."""
from copy import deepcopy
from .db import get_data_conn

ARTIFACT = 'client-access-distribution-report'
SECTIONS = {'access-users', 'access-artifacts', 'distributions', 'facilities', 'slas'}


def get_copied_snapshot(section):
    """Return one complete, verified inert snapshot section without exposing it publicly."""
    if section not in SECTIONS:
        raise ValueError('Invalid report section')
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
    if {r[0] for r in rows} != SECTIONS or len({(r[2], r[3], r[4]) for r in rows}) != 1 or rows[0][2] != 'production':
        raise RuntimeError('Copied report is incomplete')
    selected = next(r for r in rows if r[0] == section)
    payload = deepcopy(selected[1])
    if payload.get('client_key') != 'srp' or payload.get('audience') != 'srp' or not isinstance(payload.get('rows'), list):
        raise RuntimeError('Copied report could not be verified')
    payload.update(source_environment=selected[2], captured_at=selected[3].isoformat())
    return payload


def get_copied_report(section, *, search='', facility='', result='', status='', offset=0):
    if section not in SECTIONS - {'slas'}:
        raise ValueError('Invalid report section')
    if len(search) > 100 or len(facility) > 200 or not 0 <= offset <= 100000:
        raise ValueError('Invalid reporting bounds')
    if result not in {'', 'not_confirmed', 'matches', 'missing', 'extra', 'scope_mismatch', 'source_unavailable'}:
        raise ValueError('Invalid distribution result')
    if status not in {'', 'exceptions', 'no_data', 'not_configured', 'disabled', 'unresolved', 'not_confirmed'}:
        raise ValueError('Invalid facility status')
    payload = get_copied_snapshot(section)
    if payload is None:
        return None
    # The redesigned facility shell comes from live Dev configuration. Its
    # copied Production snapshot is joined internally only as review evidence.
    if section == 'facilities' and payload.get('coverage') != 'configuration':
        return None
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
        if not any(source.get('source') == 'facilities' and source.get('status') == 'available' for source in payload.get('sources', [])):
            raise RuntimeError('Copied facility roster unavailable')
        records = [
            r for r in records
            if not q or q in ' '.join(
                [r['facility_name'], r['facility_key'], *r.get('entity_codes', [])]
            ).casefold()
        ]
    payload.update(rows=records[offset:offset+50], total=len(records), offset=offset, limit=50,
                   has_more=offset+50<len(records))
    return payload
