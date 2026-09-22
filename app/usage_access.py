"""Bounded security-metadata reporting. Never reads report or recipient data."""
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from .analytics import get_analytics_reporting_config
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


def require_analytics_reporting_access(identity: dict[str, Any], client_key: str, report: str) -> bool:
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
              'resource': f'artifact:{client_key}:usage-monitoring-dashboard',
              'admin_role': admin_role,
              'corporate_role': client_role if report == 'access' else admin_role,
              'permission': 'usage:read' if report == 'usage' else 'artifact:read'}
    try:
        with get_metadata_conn() as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            row = conn.execute(_ASSIGNMENTS + """
              SELECT EXISTS (
                  SELECT 1 FROM assignments a
                  JOIN security_users u ON u.user_id = a.user_id AND u.active
                  JOIN security_role_permissions p ON p.role_key = a.role_key
                  WHERE (u.user_id = %(subject)s
                         OR (%(email)s <> '' AND lower(u.email) = lower(%(email)s)))
                    AND a.role_key IN (%(admin_role)s, %(corporate_role)s)
                    AND p.resource_key = %(resource)s AND p.permission_key = %(permission)s
                ), EXISTS (
                  SELECT 1 FROM assignments a
                  JOIN security_users u ON u.user_id = a.user_id AND u.active
                  WHERE (u.user_id = %(subject)s
                         OR (%(email)s <> '' AND lower(u.email) = lower(%(email)s)))
                    AND a.role_key = %(admin_role)s
                )
            """, params).fetchone()
    except Exception:
        raise HTTPException(503, 'Analytics reporting authorization is unavailable') from None
    if not row or not row[0]:
        raise HTTPException(403, 'Analytics reporting access denied')
    return bool(row[1])


def require_usage_reporting_access(identity: dict[str, Any], client_key: str) -> bool:
    """Backward-compatible wrapper for callers that specifically request Usage."""
    return require_analytics_reporting_access(identity, client_key, 'usage')


def get_access_summary(*, client_key: str, days: int, search: str = '', offset: int = 0,
                       audience: str = 'all') -> dict:
    """Report active users, including active users without assignments or observed usage."""
    config = get_analytics_reporting_config(client_key)
    if days not in {7, 30, 90} or audience not in config.audiences or not 0 <= offset <= 100000 or len(search) > 100:
        raise ValueError('Invalid access reporting bounds')
    now = datetime.now(timezone.utc)
    params = {'client': client_key, 'now': now, 'start': now - timedelta(days=days),
              'search': search.strip().lower(), 'offset': offset, 'audience': audience,
              'admin_role': config.admin_role}
    scope = """
      FROM security_users u
      WHERE (u.client_key = %(client)s
        OR EXISTS (SELECT 1 FROM security_user_roles ur WHERE ur.user_id=u.user_id AND ur.client_key=%(client)s)
        OR EXISTS (SELECT 1 FROM security_group_members gm WHERE gm.user_id=u.user_id AND gm.client_key=%(client)s))
      AND u.active
      AND (%(audience)s='all'
        OR (%(audience)s='bicycle' AND EXISTS (SELECT 1 FROM assignments aa WHERE aa.user_id=u.user_id AND aa.role_key=%(admin_role)s))
        OR (%(audience)s=%(client)s AND NOT EXISTS (SELECT 1 FROM assignments aa WHERE aa.user_id=u.user_id AND aa.role_key=%(admin_role)s)))
      AND (%(search)s = '' OR strpos(lower(u.display_name), %(search)s)>0
           OR strpos(lower(u.email), %(search)s)>0)
    """
    with get_metadata_conn() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute("SET LOCAL statement_timeout = '10s'")
        total = conn.execute(_ASSIGNMENTS + 'SELECT count(*) ' + scope, params).fetchone()[0]
        users = conn.execute(_ASSIGNMENTS + 'SELECT u.user_id, u.display_name, u.email, u.active ' + scope +
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
        authenticated_denials = []
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
        if available:
            authenticated_denials = conn.execute("""
              SELECT
                COALESCE(NULLIF(s.display_name, ''), NULLIF(u.display_name, ''), 'Authenticated user'),
                COALESCE(NULLIF(s.username, ''), NULLIF(u.email, '')),
                COALESCE(NULLIF(s.artifact_key, ''), 'Unscoped route'),
                count(*)::integer,
                max(s.started_at)
              FROM monitoring.request_spans s
              LEFT JOIN security_users u ON u.user_id = s.user_id
              WHERE s.client_key = %(client)s
                AND s.user_id IS NOT NULL
                AND s.response_status = 403
                AND s.route_template NOT IN (
                  '/artifacts/{client_key}/{artifact_key}/usage-summary',
                  '/artifacts/{client_key}/{artifact_key}/access-summary')
                AND s.started_at >= %(start)s AND s.started_at < %(now)s
              GROUP BY 1, 2, 3
              ORDER BY max(s.started_at) DESC, count(*) DESC, 1
              LIMIT 50
            """, params).fetchall()
    by_user = {}
    for user_id, role, source, group, resource, permission in grants:
        by_user.setdefault(user_id, []).append({'role': role, 'source': source, 'group': group,
                                                'resource': resource, 'permission': permission})
    usage = {row[0]: row[1:] for row in activity}
    return {
        'client_key': client_key, 'as_of': now, 'days': days, 'audience': audience, 'total_users': total,
        'offset': offset, 'limit': 50, 'has_more': offset + len(users) < total,
        'monitoring_available': available,
        'authenticated_denials_available': available,
        'authenticated_denials': [
            {'display_name': display_name, 'username': username, 'artifact_key': artifact_key,
             'denied_requests': denied_requests, 'last_denied_at': last_denied_at}
            for display_name, username, artifact_key, denied_requests, last_denied_at in authenticated_denials
        ],
        'users': [{'display_name': name, 'username': email, 'active': active,
                   'access_status': 'Disabled' if not active else
                     'Granted' if any(g['permission'] for g in by_user.get(uid, [])) else 'No current grants',
                   'grants': by_user.get(uid, [])[:200],
                   'grants_truncated': len(by_user.get(uid, [])) > 200,
                   'requests': usage.get(uid, (0, None))[0] if available else None,
                   'last_activity_at': usage.get(uid, (0, None))[1]}
                  for uid, name, email, active in users],
    }


def get_access_matrix(*, client_key: str, perspective: str, search: str = '', offset: int = 0, audience: str = 'all') -> dict:
    """Artifact-centric metadata projection matching Security resource wildcards."""
    config = get_analytics_reporting_config(client_key)
    if audience not in config.audiences or perspective not in {'users', 'artifacts'} or len(search) > 100 or not 0 <= offset <= 100000:
        raise ValueError('Invalid access matrix bounds')
    now = datetime.now(timezone.utc)
    params = {'client': client_key, 'now': now, 'search': search.strip().lower(), 'offset': offset, 'audience': audience, 'admin_role': config.admin_role}
    # Filter the catalog before counting, paging, or resolving wildcard grants.
    # Other clients retain their existing catalog until they request this scope.
    artifact_scope = "a.client_key=%(client)s AND (%(client)s <> 'srp' OR a.delivery_mode='web')"
    users_scope = """FROM security_users u WHERE (u.client_key=%(client)s
      OR EXISTS (SELECT 1 FROM security_user_roles r WHERE r.user_id=u.user_id AND r.client_key=%(client)s)
      OR EXISTS (SELECT 1 FROM security_group_members g WHERE g.user_id=u.user_id AND g.client_key=%(client)s))
      AND u.active
      AND (%(audience)s='all'
        OR (%(audience)s='bicycle' AND EXISTS (SELECT 1 FROM assignments aa WHERE aa.user_id=u.user_id AND aa.role_key=%(admin_role)s))
        OR (%(audience)s=%(client)s AND NOT EXISTS (SELECT 1 FROM assignments aa WHERE aa.user_id=u.user_id AND aa.role_key=%(admin_role)s)))"""
    with get_metadata_conn() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute("SET LOCAL statement_timeout = '10s'")
        if perspective == 'users':
            scope = users_scope + " AND (%(search)s='' OR strpos(lower(u.display_name),%(search)s)>0 OR strpos(lower(u.email),%(search)s)>0)"
            total = conn.execute(_ASSIGNMENTS+'SELECT count(*) '+scope, params).fetchone()[0]
            subjects = conn.execute(_ASSIGNMENTS+'SELECT u.user_id,u.display_name,u.email,u.active '+scope+
                                    ' ORDER BY lower(u.display_name),u.user_id LIMIT 50 OFFSET %(offset)s',params).fetchall()
            params['ids'] = [r[0] for r in subjects]
            selection = 'u.user_id=ANY(%(ids)s)'
        else:
            scope = "FROM app.artifacts a WHERE " + artifact_scope + " AND (%(search)s='' OR strpos(lower(a.display_name),%(search)s)>0 OR strpos(lower(a.artifact_key),%(search)s)>0)"
            total = conn.execute('SELECT count(*) '+scope,params).fetchone()[0]
            subjects = conn.execute('SELECT a.artifact_key,a.display_name,a.active '+scope+
                                    ' ORDER BY lower(a.display_name),a.artifact_key LIMIT 50 OFFSET %(offset)s',params).fetchall()
            params['ids'] = [r[0] for r in subjects]
            selection = 'a.artifact_key=ANY(%(ids)s)'
        # Resolve complete permissions before bounding displayed drilldown details.
        edges = conn.execute(_ASSIGNMENTS + ", scoped_users AS (SELECT u.user_id "+users_scope+"""),
          matched AS (
            SELECT a.artifact_key,a.display_name AS artifact_name,a.active AS artifact_active,
              u.user_id,u.display_name,u.email,u.active,
              p.permission_key,p.resource_key,ass.role_key,ass.source,ass.group_key
            FROM app.artifacts a
            JOIN security_role_permissions p ON p.resource_key IN
              ('artifact:' || %(client)s || ':' || a.artifact_key,
               'artifact:' || %(client)s || ':*','artifact:*:*','*')
              AND p.permission_key IN ('artifact:read','artifact:execute','*')
            JOIN assignments ass ON ass.role_key=p.role_key
            JOIN security_users u ON u.user_id=ass.user_id
            JOIN scoped_users su ON su.user_id=u.user_id
            WHERE """+artifact_scope+" AND "+selection+"""
          ), grouped AS (
            SELECT artifact_key,artifact_name,artifact_active,user_id,display_name,email,active,
              bool_or(permission_key IN ('artifact:read','*')) AS can_view,
              bool_or(permission_key IN ('artifact:execute','*')) AS can_run,
              count(*) AS grant_count
            FROM matched GROUP BY artifact_key,artifact_name,artifact_active,user_id,display_name,email,active
          ), ranked AS (
            SELECT *,row_number() OVER (PARTITION BY """+('user_id' if perspective=='users' else 'artifact_key')+"""
              ORDER BY lower("""+('artifact_name' if perspective=='users' else 'display_name')+"""),artifact_key,user_id) AS edge_number,
              count(*) OVER (PARTITION BY """+('user_id' if perspective=='users' else 'artifact_key')+""" ) AS edge_count
            FROM grouped
          )
          SELECT artifact_key,artifact_name,artifact_active,user_id,display_name,email,active,
            can_view,can_run,edge_count,
            (SELECT jsonb_agg(detail) FROM
              (SELECT DISTINCT jsonb_build_object('role',m.role_key,'source',m.source,
                'group',m.group_key,'permission',m.permission_key,'resource',m.resource_key) AS detail
               FROM matched m WHERE m.artifact_key=r.artifact_key AND m.user_id=r.user_id
               LIMIT 200) d),grant_count>200
          FROM ranked r WHERE edge_number<=201 ORDER BY edge_number
        """,params).fetchall()
    by_subject = {}
    for key,title,artifact_active,uid,name,email,active,view,run,count,details,details_truncated in edges:
        subject = uid if perspective == 'users' else key
        by_subject.setdefault(subject,[]).append({'artifact_key':key,'artifact_name':title,
          'artifact_active':artifact_active,'display_name':name,'username':email,'active':active,
          'can_view':bool(view and active and artifact_active),'can_run':bool(run and active and artifact_active),
          'assigned_view':view,'assigned_run':run,'details':details or [],'details_truncated':details_truncated,
          'total_matches':count})
    rows = []
    for item in subjects:
        matches = by_subject.get(item[0],[])
        row = ({'display_name':item[1],'username':item[2],'active':item[3]} if perspective=='users'
               else {'artifact_key':item[0],'display_name':item[1],'active':item[2]})
        row.update(matches=matches[:200],matches_truncated=len(matches)>200,
                   total_matches=matches[0]['total_matches'] if matches else 0)
        rows.append(row)
    return {'client_key':client_key,'as_of':now,'perspective':perspective,'audience':audience,'rows':rows,
            'total':total,'offset':offset,'limit':50,'has_more':offset+len(subjects)<total}
