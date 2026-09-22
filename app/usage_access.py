"""Bounded security-metadata reporting. Never reads report or recipient data."""
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from .analytics import get_analytics_reporting_config
from .db import get_metadata_conn

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
                    AND g.resource_key = %(resource)s AND g.permission_key = %(permission)s
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
      FROM analytics_reporting.active_user_audiences u
      WHERE u.client_key = %(client)s
        AND u.admin_role_key = %(admin_role)s
        AND u.audience_key = %(audience)s
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
        grants = conn.execute("""
          WITH ranked_grants AS (
          SELECT g.user_id, g.role_key, g.source, g.group_key, g.resource_key, g.permission_key,
            row_number() OVER (PARTITION BY g.user_id ORDER BY g.permission_key NULLS LAST,
              g.role_key, g.source, g.group_key, g.resource_key) AS grant_number
          FROM analytics_reporting.client_grants g
          WHERE g.client_key = %(client)s
            AND g.admin_role_key = %(admin_role)s
            AND g.user_id = ANY(%(ids)s)
          )
          SELECT user_id, role_key, source, group_key, resource_key, permission_key
          FROM ranked_grants WHERE grant_number <= 201
          ORDER BY user_id, grant_number
        """, params).fetchall()
        relation = conn.execute("SELECT to_regclass('analytics_reporting.request_activity')").fetchone()
        available = bool(relation and relation[0])
        activity = []
        denials = []
        if available and users:
            activity = conn.execute("""
              SELECT a.user_id, count(a.request_id), max(a.started_at)
              FROM analytics_reporting.request_activity a
              WHERE a.client_key = %(client)s AND a.user_id = ANY(%(ids)s)
                AND a.started_at >= %(start)s AND a.started_at < %(now)s
              GROUP BY a.user_id
            """, params).fetchall()
            denials = conn.execute("""
              SELECT max(d.display_name), max(d.username), d.artifact_key,
                     count(d.request_id), max(d.started_at)
              FROM analytics_reporting.authenticated_denial_events d
              JOIN analytics_reporting.active_user_audiences u
                ON u.client_key = d.client_key
               AND (u.user_id = d.subject_key OR lower(u.email) = lower(d.subject_key))
              WHERE d.client_key = %(client)s
                AND u.admin_role_key = %(admin_role)s
                AND u.audience_key = %(audience)s
                AND d.started_at >= %(start)s AND d.started_at < %(now)s
              GROUP BY d.subject_key, d.artifact_key
              ORDER BY max(d.started_at) DESC, d.artifact_key
              LIMIT 200
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
            {'display_name': name, 'username': username, 'artifact_key': artifact,
             'denied_requests': count, 'last_denied_at': last_denied}
            for name, username, artifact, count, last_denied in denials
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
    users_scope = """FROM analytics_reporting.active_user_audiences u
      WHERE u.client_key=%(client)s AND u.admin_role_key=%(admin_role)s
        AND u.audience_key=%(audience)s"""
    artifact_scope = """FROM analytics_reporting.reportable_artifacts a
      WHERE a.client_key=%(client)s AND a.admin_role_key=%(admin_role)s"""
    with get_metadata_conn() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute("SET LOCAL statement_timeout = '10s'")
        if perspective == 'users':
            scope = users_scope + " AND (%(search)s='' OR strpos(lower(u.display_name),%(search)s)>0 OR strpos(lower(u.email),%(search)s)>0)"
            total = conn.execute('SELECT count(*) '+scope, params).fetchone()[0]
            subjects = conn.execute('SELECT u.user_id,u.display_name,u.email,u.active '+scope+
                                    ' ORDER BY lower(u.display_name),u.user_id LIMIT 50 OFFSET %(offset)s',params).fetchall()
            params['ids'] = [r[0] for r in subjects]
            selection = 'e.user_id=ANY(%(ids)s)'
        else:
            scope = artifact_scope + " AND (%(search)s='' OR strpos(lower(a.display_name),%(search)s)>0 OR strpos(lower(a.artifact_key),%(search)s)>0)"
            total = conn.execute('SELECT count(*) '+scope,params).fetchone()[0]
            subjects = conn.execute('SELECT a.artifact_key,a.display_name,a.active '+scope+
                                    ' ORDER BY lower(a.display_name),a.artifact_key LIMIT 50 OFFSET %(offset)s',params).fetchall()
            params['ids'] = [r[0] for r in subjects]
            selection = 'e.artifact_key=ANY(%(ids)s)'
        # Resolve complete permissions before bounding displayed drilldown details.
        edges = conn.execute("""WITH matched AS (
            SELECT e.*
            FROM analytics_reporting.artifact_access_edges e
            JOIN analytics_reporting.active_user_audiences u
              ON u.client_key=e.client_key AND u.user_id=e.user_id
             AND u.audience_key=%(audience)s
            WHERE e.client_key=%(client)s AND e.admin_role_key=%(admin_role)s
              AND """+selection+"""
          ), grouped AS (
            SELECT artifact_key,artifact_name,artifact_active,user_id,display_name,email,active,
              bool_or(assigned_view) AS can_view,
              bool_or(assigned_run) AS can_run,
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
