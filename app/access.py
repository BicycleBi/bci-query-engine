"""Authorize artifact requests against the stack's existing Security grants."""

from fastapi import HTTPException
from psycopg import Error

from .db import get_metadata_conn


def permitted_roles(client_key, artifact_key, permission, subject, identity):
    """Use active direct/group memberships and return roles for Data and Redis scope."""
    resources = [
        f"artifact:{client_key}:{artifact_key}",
        f"artifact:{client_key}:*",
        "artifact:*:*",
        "*",
    ]
    # A forwarded roles header is not a grant. Look up the signed subject and
    # intersect their signed roles with their still-active assignments.
    roles = identity.get("roles")
    if (
        not isinstance(roles, list)
        or not roles
        or not all(isinstance(r, str) and r.strip() for r in roles)
    ):
        raise HTTPException(403, "Artifact access denied")
    try:
        with get_metadata_conn() as meta:
            rows = meta.execute(
                """
                WITH active_roles AS (
                    SELECT a.role_key
                    FROM security_user_roles a JOIN security_users u USING (user_id)
                    WHERE u.active AND (u.user_id = %(subject)s OR
                          (%(email)s <> '' AND lower(u.email) = lower(%(email)s)))
                      AND a.client_key = %(client)s AND (a.expires_at IS NULL OR a.expires_at > now())
                    UNION
                    SELECT g.role_key
                    FROM security_group_members m JOIN security_users u USING (user_id)
                    JOIN security_group_roles g ON (m.client_key, m.group_key) = (g.client_key, g.group_key)
                    WHERE u.active AND (u.user_id = %(subject)s OR
                          (%(email)s <> '' AND lower(u.email) = lower(%(email)s)))
                      AND m.client_key = %(client)s
                      AND (m.expires_at IS NULL OR m.expires_at > now())
                      AND (g.expires_at IS NULL OR g.expires_at > now())
                )
                SELECT r.role_key, EXISTS (
                    SELECT 1 FROM security_role_permissions p WHERE p.role_key = r.role_key
                      AND p.resource_key = ANY(%(resources)s) AND p.permission_key = ANY(%(permissions)s)
                ) FROM active_roles r WHERE r.role_key = ANY(%(roles)s) ORDER BY r.role_key
                """,
                {
                    "client": client_key,
                    "subject": subject,
                    "email": identity.get("email") or "",
                    "roles": roles,
                    "resources": resources,
                    "permissions": [permission, "*"],
                },
            ).fetchall()
    except (Error, OSError, KeyError) as exc:
        raise HTTPException(503, "Artifact grants unavailable") from exc
    if not any(granted for _, granted in rows):
        raise HTTPException(403, "Artifact access denied")
    return [role for role, _ in rows]
