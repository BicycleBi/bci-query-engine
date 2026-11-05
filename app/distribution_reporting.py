"""Read-only SRP distribution inventory. No sends, mutations or inferred approval."""
from datetime import datetime, timezone
import hashlib
import re
from typing import Any

from .analytics import get_analytics_reporting_config
from .db import get_data_conn, get_metadata_conn

MAX_SOURCE_ROWS = 5000
PAGE_SIZE = 50
RESULTS = {'not_confirmed', 'matches', 'missing', 'extra', 'scope_mismatch', 'source_unavailable'}


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


def _slug(value: str) -> str:
    normalized = re.sub(r'[^a-z0-9]+', '_', value.lower()).strip('_') or 'slice'
    return normalized[:28].strip('_') + '_' + hashlib.sha1(value.encode()).hexdigest()[:8]


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
    config = get_analytics_reporting_config('srp')
    params = {'client': 'srp', 'admin': config.admin_role}
    # Registry is the configured email target, not approval and not proof of delivery.
    registry = _read(get_metadata_conn, 'app.artifact_recipients', """
        SELECT a.artifact_key, a.display_name, a.active, r.email, r.delivery_type, r.active
        FROM app.artifacts a JOIN app.artifact_recipients r USING (artifact_id)
        WHERE a.client_key = %(client)s AND a.delivery_mode IN ('email','both')
          AND NOT EXISTS (
            SELECT 1 FROM analytics_reporting.effective_assignments g
            WHERE g.client_key = %(client)s AND g.active AND g.is_bicycle_admin
              AND g.admin_role_key = %(admin)s AND lower(g.email) = lower(r.email))
        ORDER BY a.artifact_key, lower(r.email), r.delivery_type
    """, params)
    if registry is None:
        raise RuntimeError('Distribution registry unavailable')
    excluded = _read(get_metadata_conn, 'analytics_reporting.effective_assignments', """
        SELECT DISTINCT lower(email) FROM analytics_reporting.effective_assignments
        WHERE client_key=%(client)s AND active AND is_bicycle_admin AND admin_role_key=%(admin)s
        ORDER BY lower(email)
    """, params)
    if excluded is None:
        raise RuntimeError('Distribution audience unavailable')
    excluded_emails = {r[0].strip() for r in excluded if r[0]}
    sources = [{'source': 'metadata_artifact_recipients', 'status': 'available'}]
    scopes: dict[str, set[str]] = {}
    mappings = []
    # Missing optional sources remain visibly unavailable, never empty proof.
    queries = [
        ('weekly_mapping', 'public.srp_weekly_report_recipient_mapping', """
          SELECT facility_name, recipient_email, recipient_type, send_enabled, ready_for_release
          FROM public.srp_weekly_report_recipient_mapping ORDER BY facility_name, recipient_email
        """),
        ('pnl_scope', 'public.srp_pnl_owner_delivery_scope', """
          SELECT artifact_key, entity_code, active
          FROM public.srp_pnl_owner_delivery_scope ORDER BY artifact_key, entity_code
        """),
    ]
    for name, relation, query in queries:
        try:
            data = _read(get_data_conn, relation, query)
        except Exception:
            data = None
        sources.append({'source': name, 'status': 'available' if data is not None else 'unavailable'})
        if data is None:
            continue
        if name == 'weekly_mapping':
            for label, email, kind, enabled, ready in data:
                if not label:
                    continue
                for family in ('weekly', 'monthly'):
                    key = f'srp-{family}-practice-report-{_slug(label)}-email'
                    scopes.setdefault(key, set()).add(label)
                    mappings.append((key, label, str(email or '').strip().casefold(), bool(enabled and ready), kind, family + '_mapping'))
        else:
            for key, entity, active in data:
                # Inactive scopes still explain configured recipients; do not discard them.
                scopes.setdefault(key, set()).add(entity)
    for name, relation, query in [
        ('pnl_effective_recipients', 'public.srp_pnl_owner_delivery_recipient_source', """
          SELECT artifact_key, entity_code, recipient_email, recipient_type, send_enabled, ready_for_release
          FROM public.srp_pnl_owner_delivery_recipient_source ORDER BY artifact_key, recipient_email
        """),
        ('pnl_recipient_overrides', 'public.srp_pnl_owner_delivery_recipient_override', """
          SELECT s.artifact_key, o.entity_code, o.recipient_email, o.recipient_type,
                 o.send_enabled AND s.active, o.ready_for_release
          FROM public.srp_pnl_owner_delivery_recipient_override o
          JOIN public.srp_pnl_owner_delivery_scope s USING (entity_code)
          ORDER BY s.artifact_key, o.recipient_email
        """),
    ]:
        try:
            data = _read(get_data_conn, relation, query)
        except Exception:
            data = None
        sources.append({'source': name, 'status': 'available' if data is not None else 'unavailable'})
        for artifact, entity, email, kind, enabled, ready in data or []:
            scopes.setdefault(artifact, set()).add(entity)
            mappings.append((artifact, entity, str(email or '').strip().casefold(), bool(enabled and ready), kind, name))
    sources.extend([
        {'source': 'monthly_source_activity_eligibility', 'status': 'not_assessed'},
        {'source': 'consolidated_facility_scope', 'status': 'unverified'},
        {'source': 'workflow_distribution_group_selection', 'status': 'unverified'},
    ])
    # Inert approval relation is populated only after explicit SRP confirmation.
    try:
        approvals = _read(get_data_conn, 'public.srp_report_distribution_approval', """
          SELECT artifact_key, recipient_email, delivery_type, facility_keys, excluded,
                 approval_source, approver, approval_date, maintenance_owner, approval_version
          FROM public.srp_report_distribution_approval
          WHERE client_key='srp' AND active AND (effective_from IS NULL OR effective_from <= CURRENT_DATE)
            AND (effective_until IS NULL OR effective_until >= CURRENT_DATE)
          ORDER BY artifact_key, lower(recipient_email), delivery_type
        """)
    except Exception:
        approvals = None
    approval_map = {}
    for a in approvals or []:
        key = (a[0], a[1].strip().casefold(), a[2])
        if key in approval_map:
            # Conflicting approvals cannot authorize a discrepancy verdict.
            approval_map[key] = None
            continue
        approval_map[key] = dict(facilities=a[3], excluded=a[4], approval_source=a[5], approver=a[6], approval_date=a[7], maintenance_owner=a[8], approval_version=a[9], delivery_type=a[2])
    rows = []
    seen = set()
    for artifact, name, artifact_active, email, delivery_type, recipient_active in registry:
        if not email:
            continue
        key = (artifact, email.strip().casefold(), delivery_type)
        if key[1] in excluded_emails:
            continue
        seen.add(key)
        facilities = sorted(scopes.get(artifact, set()))
        configured = dict(enabled=bool(artifact_active and recipient_active), facilities=facilities,
                          facility_resolved=bool(facilities), delivery_type=delivery_type)
        approved = approval_map.get(key)
        mapping = [m for m in mappings if m[0] == artifact and m[2] == key[1]]
        rows.append(dict(artifact_key=artifact, report_name=name, recipient_email=email,
                         delivery_type=delivery_type, facilities=facilities,
                         configuration_state='enabled' if configured['enabled'] else 'disabled',
                         mapping_state=('enabled' if any(m[3] for m in mapping) else 'not_ready') if mapping else 'not_verified',
                         facility_state='resolved' if facilities else 'unresolved',
                         eligibility='not_assessed', approved=approved,
                         result=compare_assignment(configured, approved), source='metadata_artifact_recipients'))
    for key, approved in approval_map.items():
        if key in seen or not approved or key[1] in excluded_emails:
            continue
        rows.append(dict(artifact_key=key[0], report_name=key[0], recipient_email=key[1], delivery_type=key[2],
                         facilities=approved['facilities'], configuration_state='not_configured', mapping_state='not_verified',
                         facility_state='resolved', eligibility='not_assessed', approved=approved,
                         result=compare_assignment(None, approved), source='srp_approved_list'))
    # Mapping sources are configured intent too, even before generated targets
    # exist in Metadata. Show the registry gap without treating it as approval.
    mapping_rows = {}
    for artifact, label, email, enabled, kind, source in mappings:
        key = (artifact, email, kind)
        if not email or email in excluded_emails or key in seen:
            continue
        if key in mapping_rows:
            mapping_rows[key]['facilities'] = sorted(set(mapping_rows[key]['facilities']) | {label})
            continue
        configured = dict(enabled=enabled, facilities=[label], facility_resolved=True, delivery_type=kind)
        approved = approval_map.get(key)
        mapping_rows[key] = dict(artifact_key=artifact, report_name=artifact, recipient_email=email,
            delivery_type=kind, facilities=[label], configuration_state='enabled' if enabled else 'disabled_or_not_ready',
            registry_state='not_configured', mapping_state='enabled' if enabled else 'not_ready',
            facility_state='resolved', eligibility='not_assessed', approved=approved,
            result=compare_assignment(configured, approved), source=source)
    # Approved-only rows were added above; replace them with mapping evidence
    # rather than counting a configured intent twice.
    rows = [r for r in rows if (r['artifact_key'], r['recipient_email'].strip().casefold(), r['delivery_type']) not in mapping_rows]
    for key, item in mapping_rows.items():
        configured = dict(enabled=item['configuration_state']=='enabled', facilities=item['facilities'], facility_resolved=True, delivery_type=item['delivery_type'])
        item['result'] = compare_assignment(configured, item['approved'])
    rows.extend(mapping_rows.values())
    query = search.strip().casefold()
    rows = [r for r in rows if (not query or query in ' '.join([r['report_name'], r['artifact_key'], r['recipient_email'], *r['facilities']]).casefold())
            and (not facility or facility in r['facilities']) and (not result or r['result'] == result)]
    rows.sort(key=lambda r: (r['report_name'].casefold(), r['recipient_email'].casefold(), r['delivery_type']))
    return dict(client_key='srp', audience='srp', as_of=datetime.now(timezone.utc),
                rows=rows[offset:offset+PAGE_SIZE], total=len(rows), offset=offset, limit=PAGE_SIZE,
                has_more=offset+PAGE_SIZE < len(rows), sources=sources,
                approval_status='available' if approvals else 'not_confirmed',
                coverage='partial', delivery_history_included=False)
