"""Bounded security-metadata reporting. Never reads report or recipient data."""
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from .db import get_metadata_conn

# Match Security's current metadata permission evaluator: scope assignments and
# expiry to the client, including central Bicycle identities. The legacy runtime
# does not use security_groups.active to invalidate a membership.
_ASSIGNMENTS = """
WITH assignments AS (
 SELECT ur.user_id, ur.role_key, 'direct'::text AS source, NULL::text AS group_key
 FROM security_user_roles ur
 WHERE ur.client_key = %(client)s
   AND (ur.expires_at IS NULL OR ur.expires_at > %(now)s)
 UNION
 SELECT gm.user_id, gr.role_key, 'group', gm.group_key
 FROM security_group_members gm
 JOIN security_group_roles gr ON gr.group_key = gm.group_key
 WHERE gm.client_key = %(client)s AND gr.client_key = %(client)s
   AND (gm.expires_at IS NULL OR gm.expires_at > %(now)s)
   AND (gr.expires_at IS NULL OR gr.expires_at > %(now)s)
)
"""


def require_usage_reporting_access(identity: dict[str, Any], client_key: str) -> None:
    """Require a live, exact reporting grant; broad artifact grants don't qualify."""
    if identity.get('client_key') != client_key or not identity.get('sub'):
        raise HTTPException(403, 'Usage reporting access denied')
    params = {'client': client_key, 'now': datetime.now(timezone.utc),
              'subject': identity['sub'], 'resource': f'artifact:{client_key}:usage-monitoring-dashboard'}
    try:
        with get_metadata_conn() as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            row = conn.execute(_ASSIGNMENTS + """
                SELECT EXISTS (
                  SELECT 1 FROM assignments a
                  JOIN security_users u ON u.user_id = a.user_id AND u.active
                  JOIN security_role_permissions p ON p.role_key = a.role_key
                  WHERE u.user_id = %(subject)s
                    AND p.resource_key = %(resource)s AND p.permission_key = 'usage:read'
                )
            """, params).fetchone()
    except Exception:
        raise HTTPException(503, 'Usage reporting authorization is unavailable') from None
    if not row or not row[0]:
        raise HTTPException(403, 'Usage reporting access denied')


def get_access_summary(*, client_key: str, days: int, search: str = '', offset: int = 0) -> dict:
    """Include inactive/unassigned users; usage absence never proves access absence."""
    if days not in {7, 30, 90} or not 0 <= offset <= 100000 or len(search) > 100:
        raise ValueError('Invalid access reporting bounds')
    now = datetime.now(timezone.utc)
    params = {'client': client_key, 'now': now, 'start': now - timedelta(days=days),
              'search': search.strip().lower(), 'offset': offset}
    scope = """
      FROM security_users u
      WHERE (u.client_key = %(client)s
        OR EXISTS (SELECT 1 FROM security_user_roles ur WHERE ur.user_id=u.user_id AND ur.client_key=%(client)s)
        OR EXISTS (SELECT 1 FROM security_group_members gm WHERE gm.user_id=u.user_id AND gm.client_key=%(client)s))
      AND (%(search)s = '' OR strpos(lower(u.display_name), %(search)s)>0
           OR strpos(lower(u.email), %(search)s)>0)
    """
    with get_metadata_conn() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute("SET LOCAL statement_timeout = '10s'")
        total = conn.execute('SELECT count(*) ' + scope, params).fetchone()[0]
        users = conn.execute('SELECT u.user_id, u.display_name, u.email, u.active ' + scope +
                             ' ORDER BY lower(u.display_name), u.user_id LIMIT 50 OFFSET %(offset)s', params).fetchall()
        params['ids'] = [u[0] for u in users]
        grants = conn.execute(_ASSIGNMENTS + """
          , ranked_grants AS (
          SELECT a.user_id, a.role_key, a.source, a.group_key, p.resource_key, p.permission_key,
            row_number() OVER (PARTITION BY a.user_id ORDER BY p.permission_key NULLS LAST,
              a.role_key, a.source, a.group_key, p.resource_key) AS grant_number
          FROM assignments a
          LEFT JOIN security_role_permissions p ON p.role_key=a.role_key
            AND (p.resource_key = '*'
              OR split_part(p.resource_key, ':', 2) = %(client)s
              OR (split_part(p.resource_key, ':', 2) = '*'
                AND split_part(p.resource_key, ':', 3) = '*'))
          WHERE a.user_id = ANY(%(ids)s)
          )
          SELECT user_id, role_key, source, group_key, resource_key, permission_key
          FROM ranked_grants WHERE grant_number <= 201
          ORDER BY user_id, grant_number
        """, params).fetchall()
        relation = conn.execute("SELECT to_regclass('monitoring.request_spans')").fetchone()
        available = bool(relation and relation[0])
        activity = []
        if available and users:
            activity = conn.execute("""
              SELECT u.user_id, count(s.request_id), max(s.started_at)
              FROM security_users u
              LEFT JOIN monitoring.request_spans s ON s.client_key=%(client)s
                AND (s.user_id=u.user_id OR
                  ((NULLIF(s.user_id,'') IS NULL OR NOT EXISTS
                    (SELECT 1 FROM security_users known WHERE known.user_id=s.user_id))
                    AND lower(s.username)=lower(u.email)))
                AND s.artifact_key IS NOT NULL
                AND s.route_template NOT IN (
                  '/artifacts/{client_key}/{artifact_key}/usage-summary',
                  '/artifacts/{client_key}/{artifact_key}/access-summary')
                AND s.started_at >= %(start)s AND s.started_at < %(now)s
              WHERE u.user_id=ANY(%(ids)s) GROUP BY u.user_id
            """, params).fetchall()
    by_user = {}
    for user_id, role, source, group, resource, permission in grants:
        by_user.setdefault(user_id, []).append({'role': role, 'source': source, 'group': group,
                                                'resource': resource, 'permission': permission})
    usage = {row[0]: row[1:] for row in activity}
    return {
        'client_key': client_key, 'as_of': now, 'days': days, 'total_users': total,
        'offset': offset, 'limit': 50, 'has_more': offset + len(users) < total,
        'monitoring_available': available,
        'users': [{'display_name': name, 'username': email, 'active': active,
                   'access_status': 'Disabled' if not active else
                     'Granted' if any(g['permission'] for g in by_user.get(uid, [])) else 'No current grants',
                   'grants': by_user.get(uid, [])[:200],
                   'grants_truncated': len(by_user.get(uid, [])) > 200,
                   'requests': usage.get(uid, (0, None))[0] if available else None,
                   'last_activity_at': usage.get(uid, (0, None))[1]}
                  for uid, name, email, active in users],
    }
