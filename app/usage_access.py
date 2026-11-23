"""Enforce optional platform analytics grants on registered Metadata queries."""
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from .analytics import get_analytics_reporting_config
from .db import get_metadata_conn


def require_analytics_reporting_access(identity: dict[str, Any], client_key: str, report: str, artifact_key: str) -> bool:
    """Require a live report-specific grant and return whether the viewer is a Bicycle admin."""
    # Security also resolves existing canonical users through verified token email
    # when a provider subject differs from their metadata user ID.
    if identity.get('client_key') != client_key or not identity.get('sub'):
        raise HTTPException(403, 'Analytics reporting access denied')
    if report not in {'usage', 'access'}:
        raise HTTPException(400, 'Unknown analytics report')
    config = get_analytics_reporting_config(client_key)
    admin_role = config.admin_role
    client_role = config.client_reporting_role
    allowed_roles = [admin_role] if report == 'usage' else [role for role in (admin_role, client_role) if role]
    if not allowed_roles:
        raise HTTPException(503, 'Analytics reporting authorization is unavailable')
    params = {'client': client_key, 'now': datetime.now(timezone.utc),
              'subject': identity['sub'], 'email': str(identity.get('email') or ''),
              'resource': f'artifact:{client_key}:{artifact_key}',
              # Match Security's canonical resource hierarchy, within the live
              # client/role/identity contract enforced by the query below.
              'wildcard_resources': [f'artifact:{client_key}:*', 'artifact:*:*', '*'],
              'admin_role': admin_role,
              'configured_client_role': client_role or None,
              'corporate_role': client_role if report == 'access' else admin_role,
              'permission': 'usage:read' if report == 'usage' else 'artifact:read'}
    try:
        with get_metadata_conn() as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            row = conn.execute("""
              SELECT EXISTS (
                  SELECT 1 FROM analytics_reporting.effective_grants g
                  WHERE g.client_key = %(client)s AND g.active
                    AND g.admin_role_key = %(admin_role)s
                    AND g.client_reporting_role_key IS NOT DISTINCT FROM %(configured_client_role)s
                    AND (g.user_id = %(subject)s
                         OR (%(email)s <> '' AND lower(g.email) = lower(%(email)s)))
                    AND g.role_key IN (%(admin_role)s, %(corporate_role)s)
                    AND (g.resource_key = %(resource)s
                         OR g.resource_key = ANY(%(wildcard_resources)s))
                    AND (g.permission_key = %(permission)s OR g.permission_key = '*')
                ), EXISTS (
                  SELECT 1 FROM analytics_reporting.effective_assignments a
                  WHERE a.client_key = %(client)s AND a.active
                    AND a.admin_role_key = %(admin_role)s
                    AND a.client_reporting_role_key IS NOT DISTINCT FROM %(configured_client_role)s
                    AND (a.user_id = %(subject)s
                         OR (%(email)s <> '' AND lower(a.email) = lower(%(email)s)))
                    AND a.is_bicycle_admin
                )
            """, params).fetchone()
    except Exception:
        raise HTTPException(503, 'Analytics reporting authorization is unavailable') from None
    if not row or not row[0]:
        raise HTTPException(403, 'Analytics reporting access denied')
    return bool(row[1])
