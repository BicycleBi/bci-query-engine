"""Read-only SRP distribution inventory. No sends, mutations or inferred approval."""
from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .db import get_data_conn, get_metadata_conn

MAX_SOURCE_ROWS = 5000
PAGE_SIZE = 50
RESULTS = {'not_confirmed', 'matches', 'missing', 'extra', 'scope_mismatch', 'source_unavailable'}


def _central_display_time(schedule: str | None, timezone_name: str | None) -> str | None:
    match = re.search(r'(?<!\d)(\d{1,2}):(\d{2})(?::\d{2})?(?!\d)', schedule or '')
    if not match:
        return schedule
    zone_name = 'America/New_York' if timezone_name in {'ET', 'Eastern Time'} else timezone_name
    try:
        source_zone = ZoneInfo(zone_name or 'America/Chicago')
        local = datetime.now(source_zone).replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
        central = local.astimezone(ZoneInfo('America/Chicago'))
    except (ValueError, ZoneInfoNotFoundError):
        return schedule
    return central.strftime('%-I:%M %p CT')

def _read(factory, relation: str, query: str, params=None):
    # Relation and query are fixed source definitions, never caller input.
    with factory() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute("SET LOCAL statement_timeout = '10s'")
        if not conn.execute('SELECT to_regclass(%s)', (relation,)).fetchone()[0]:
            return None
        rows = conn.execute(query + ' LIMIT 5001', params).fetchall()
        if len(rows) > MAX_SOURCE_ROWS:
            raise RuntimeError('Distribution source exceeds reporting bound')
        return rows


def get_report_configurations() -> dict:
    """Return schedules joined through the maintained artifact/load mapping."""
    try:
        rows = _read(get_data_conn, 'public.srp_client_access_artifact_schedule', """
          SELECT artifact_key, load_key, trigger_id, schedule, timezone,
                 enabled, timer_active
          FROM public.srp_client_access_artifact_schedule
          ORDER BY artifact_key, trigger_id
        """)
    except Exception:
        rows = None
    by_artifact = {}
    load_keys = {}
    for artifact_key, load_key, trigger_id, schedule, timezone_name, enabled, timer_active in rows or []:
        load_keys[artifact_key] = load_key
        if not trigger_id:
            continue
        by_artifact.setdefault(artifact_key, []).append({
            'trigger_id': trigger_id,
            'schedule': schedule,
            'display_time': _central_display_time(schedule, timezone_name),
            'timezone': timezone_name,
            'enabled': bool(enabled),
            'timer_active': bool(timer_active),
            'source': 'load_setup.trigger',
        })
    return {
        artifact_key: {
            'load_key': load_key,
            'status': 'available' if by_artifact.get(artifact_key) else 'not_scheduled',
            'schedules': by_artifact.get(artifact_key, []),
            'time_meaning': 'configured_workflow_start',
        }
        for artifact_key, load_key in load_keys.items()
    }


def attach_report_configurations(payload: dict, *, facilities: bool = False) -> dict:
    """Enrich an existing bounded report response with configuration metadata."""
    configurations = get_report_configurations()
    payload['report_configurations'] = configurations
    if facilities:
        for row in payload.get('rows') or []:
            for report in row.get('reports') or []:
                report['schedule_configuration'] = configurations.get(report.get('artifact_key'))
    else:
        for row in payload.get('rows') or []:
            row['report_artifact_key'] = row.get('report_artifact_key') or row.get('artifact_key')
    sources = payload.setdefault('sources', [])
    sources[:] = [source for source in sources if source.get('source') != 'load_setup_configuration']
    sources.append({
        'source': 'load_setup_configuration',
        'status': 'available' if configurations else 'not_configured',
    })
    return payload


def compare_assignment(configured: dict | None, approved: dict | None) -> str:
    """Missing approval is unknown. Never treat configured state as approval."""
    if not approved or not approved.get('approval_source') or not approved.get('approver') or not approved.get('approval_date') or not approved.get('maintenance_owner'):
        return 'not_confirmed'
    if approved.get('excluded'):
        return 'extra' if configured and configured.get('enabled') else 'matches'
    if configured is None or not configured.get('enabled'):
        return 'missing'
    if not configured.get('facility_resolved'):
        return 'not_confirmed'
    if set(configured['facilities']) != set(approved['facilities']) or configured.get('delivery_type') != approved.get('delivery_type'):
        return 'scope_mismatch'
    return 'matches'


def get_distribution_report(*, search: str = '', facility: str = '', result: str = '', offset: int = 0) -> dict:
    if len(search) > 100 or len(facility) > 200 or result not in RESULTS | {''} or not 0 <= offset <= 100000:
        raise ValueError('Invalid distribution reporting bounds')
    distributions = _read(get_data_conn, 'public.srp_client_access_facility_distribution', """
        SELECT facility_name, artifact_key, delivery_artifact_key, recipient_email,
               delivery_type, enabled, configuration_source
        FROM public.srp_client_access_facility_distribution
        ORDER BY artifact_key, lower(recipient_email), facility_name, delivery_type
    """)
    if distributions is None:
        raise RuntimeError('Facility distribution configuration is unavailable')
    artifact_keys = sorted({row[1] for row in distributions if row[1]})
    artifacts = _read(get_metadata_conn, 'app.artifacts', """
        SELECT artifact_key, display_name, active
        FROM app.artifacts
        WHERE client_key='srp' AND artifact_key = ANY(%s)
        ORDER BY artifact_key
    """, (artifact_keys,)) if artifact_keys else []
    if artifacts is None:
        raise RuntimeError('Artifact registry is unavailable')
    sources = [
        {'source': 'facility_distribution_configuration', 'status': 'available'},
        {'source': 'artifact_registry', 'status': 'available'},
    ]
    catalog = {key: (name or key, bool(active)) for key, name, active in artifacts}
    grouped = {}
    for facility_name, artifact_key, delivery_key, email, delivery_type, enabled, source in distributions:
        if not email or artifact_key not in catalog:
            continue
        key = (artifact_key, str(email).strip().casefold(), delivery_type, delivery_key)
        item = grouped.setdefault(key, dict(
            artifact_key=artifact_key,
            report_artifact_key=artifact_key,
            delivery_artifact_key=delivery_key,
            report_name=catalog[artifact_key][0],
            recipient_email=email,
            delivery_type=delivery_type,
            facilities=[],
            configuration_state='enabled' if enabled and catalog[artifact_key][1] else 'disabled',
            mapping_state='enabled' if enabled else 'disabled',
            facility_state='resolved',
            eligibility='configured_metadata',
            approved=None,
            result='not_confirmed',
            source=source,
        ))
        if facility_name and facility_name not in item['facilities']:
            item['facilities'].append(facility_name)
        if not enabled:
            item['configuration_state'] = 'disabled'
            item['mapping_state'] = 'disabled'
    rows = list(grouped.values())
    for row in rows:
        row['facilities'].sort(key=str.casefold)
    query = search.strip().casefold()
    rows = [r for r in rows if (not query or query in ' '.join([r['report_name'], r['artifact_key'], r['recipient_email'], *r['facilities']]).casefold())
            and (not facility or facility in r['facilities']) and (not result or r['result'] == result)]
    rows.sort(key=lambda r: (r['report_name'].casefold(), r['recipient_email'].casefold(), r['delivery_type']))
    return dict(client_key='srp', audience='srp', as_of=datetime.now(timezone.utc),
                rows=rows[offset:offset+PAGE_SIZE], total=len(rows), offset=offset, limit=PAGE_SIZE,
                has_more=offset+PAGE_SIZE < len(rows), sources=sources,
                approval_status='not_applicable', coverage='configuration', delivery_history_included=False)
