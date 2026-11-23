"""Resolve declared artifacts, execute their database contracts, render and cache results."""
import hashlib
import json
import logging
import os
from collections.abc import Callable
import re
import shutil
import uuid
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Optional

from .analytics import get_analytics_reporting_config
from fastapi import HTTPException

from .composition import compose, load_email_composition
from .db import get_data_conn, get_metadata_conn
from .outputs import _generate_csv_output, _generate_pdf_outputs, _insert_artifact_outputs, create_output_directory

logger = logging.getLogger(__name__)

def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _fetch_artifact(
    meta,
    *,
    client_key: Optional[str] = None,
    artifact_key: Optional[str] = None,
    artifact_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    where_sql = "a.artifact_id = %s::uuid" if artifact_id else "a.client_key = %s AND a.artifact_key = %s"
    params: tuple[Any, ...] = (artifact_id,) if artifact_id else (client_key, artifact_key)
    row = meta.execute(
        f"""
        SELECT
            a.artifact_id,
            a.client_key,
            a.artifact_key,
            a.view_name,
            a.delivery_mode,
            a.display_name AS subject,
            t.html_content AS template_body,
            a.template_id
        FROM app.artifacts a
        LEFT JOIN app.templates t
          ON t.template_id = a.template_id AND t.is_active
        WHERE {where_sql}
          AND a.active
        """,
        params,
    ).fetchone()
    if row is None:
        return None

    keys = [
        "artifact_id",
        "client_key",
        "artifact_key",
        "view_name",
        "delivery_mode",
        "subject",
        "template_body",
        "template_id",
    ]
    artifact = dict(zip(keys, row))
    artifact["artifact_id"] = str(artifact["artifact_id"])
    artifact["template_id"] = str(artifact["template_id"]) if artifact["template_id"] else None
    return artifact


def _lookup_body_reference(meta, artifact_id: str) -> Optional[str]:
    row = meta.execute(
        """
        SELECT referenced_artifact_id
        FROM app.artifact_references
        WHERE artifact_id = %s::uuid
          AND reference_role = 'body'
          AND active
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (artifact_id,),
    ).fetchone()
    return str(row[0]) if row else None


def _resolve_render_artifact(meta, artifact: dict[str, Any], seen: Optional[set[str]] = None, check_reference=None) -> dict[str, Any]:
    seen = seen or set()
    artifact_id = artifact["artifact_id"]

    if artifact_id in seen:
        raise ValueError(f"Cyclic artifact reference detected for artifact {artifact['artifact_key']}")

    if artifact.get("view_name") and artifact.get("template_body"):
        return artifact

    seen.add(artifact_id)
    referenced_artifact_id = _lookup_body_reference(meta, artifact_id)
    if referenced_artifact_id is None:
        raise ValueError(
            f"Artifact {artifact['artifact_key']} has no direct source and no active body reference"
        )

    referenced_artifact = _fetch_artifact(meta, artifact_id=referenced_artifact_id)
    if referenced_artifact is None or referenced_artifact["client_key"] != artifact["client_key"]:
        raise ValueError(
            f"Referenced artifact {referenced_artifact_id} could not be resolved for {artifact['artifact_key']}"
        )

    if check_reference is not None:
        check_reference(referenced_artifact["artifact_key"])
    return _resolve_render_artifact(meta, referenced_artifact, seen, check_reference)


def _artifact_cache_freshness_timestamp(data, client_key: str, artifact_key: str) -> Optional[str]:
    """Return an optional client-defined freshness timestamp for cache keys."""
    try:
        exists = data.execute(
            "SELECT to_regprocedure('public.bci_artifact_cache_freshness(text,text)') IS NOT NULL"
        ).fetchone()
        if not exists or not bool(exists[0]):
            return None

        row = data.execute(
            "SELECT public.bci_artifact_cache_freshness(%s, %s)::text",
            (client_key, artifact_key),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return str(row[0])
    except Exception:
        return None


def _subject_hash(authenticated_subject: Optional[str]) -> Optional[str]:
    if not authenticated_subject:
        return None
    return hashlib.sha256(authenticated_subject.encode("utf-8")).hexdigest()


def _authorization_context_hash(authorized_roles: Optional[list[str]]) -> Optional[str]:
    if not authorized_roles:
        return None
    normalized = json.dumps(sorted(set(authorized_roles)), separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _begin_data_read(data, *, consistent=False):
    if consistent:
        data.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    else:
        data.execute("SET TRANSACTION READ ONLY")
    data.execute("SET LOCAL statement_timeout = '30s'")


def _set_authorization_context(
    data,
    client_key: str,
    authenticated_subject: Optional[str],
    authorized_roles: Optional[list[str]],
    *,
    authenticated_email: Optional[str] = None,
) -> None:
    """Bind trusted Security identity and resolved roles to the transaction."""
    data.execute(
        "SELECT set_config('bci.authenticated_subject', %s, true)",
        (authenticated_subject or "",),
    )
    data.execute(
        "SELECT set_config('bci.authenticated_email', %s, true)",
        (authenticated_email or "",),
    )
    data.execute(
        "SELECT set_config('bci.authorized_roles', %s, true)",
        (json.dumps(sorted(set(authorized_roles or [])), separators=(",", ":")),),
    )

    analytics = get_analytics_reporting_config(client_key, required=False)
    data.execute(
        "SELECT set_config('bci.platform_admin_role', %s, true)",
        (analytics.admin_role if analytics else "",),
    )
    data.execute(
        "SELECT set_config('bci.platform_corporate_role', %s, true)",
        (analytics.client_reporting_role if analytics else "",),
    )


def _safe_view_name(view_name: str) -> str:
    """Allow only schema-qualified identifiers from trusted metadata."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?", view_name or ""):
        raise ValueError("Artifact data view name is not a safe identifier")
    return view_name


def _query_function_name(view_name: str) -> str:
    """Return the database-owned query contract associated with a render view."""
    return _safe_view_name(f"{_safe_view_name(view_name)}_query")


def _query_cache_scope_function_name(view_name: str) -> str:
    """Return the optional database-owned cache-scope contract."""
    return _safe_view_name(f"{_safe_view_name(view_name)}_query_cache_scope")


def _query_cache_policy_function_name(view_name: str) -> str:
    """Return the optional database-owned cache-retention contract."""
    return _safe_view_name(f"{_safe_view_name(view_name)}_query_cache_policy")


def _serialized_artifact_query(query: dict[str, Any]) -> str:
    if not isinstance(query, dict):
        raise ValueError("Artifact query must be a JSON object")
    serialized_query = json.dumps(query, separators=(",", ":"), sort_keys=True)
    max_query_bytes = int(os.getenv("ARTIFACT_QUERY_MAX_BYTES", "65536"))
    if len(serialized_query.encode("utf-8")) > max_query_bytes:
        raise ValueError(f"Artifact query exceeds the {max_query_bytes}-byte request limit")
    return serialized_query


def _artifact_query_cache_scope(data, view_name: str, serialized_query: str) -> str:
    """Resolve an artifact-owned cache scope, defaulting safely to identity."""
    scope_function = _query_cache_scope_function_name(view_name)
    function_exists = data.execute(
        "SELECT to_regprocedure(%s)",
        (f"{scope_function}(jsonb)",),
    ).fetchone()[0]
    if function_exists is None:
        return "identity"
    row = data.execute(
        f"SELECT {scope_function}(%s::jsonb)",  # noqa: S608
        (serialized_query,),
    ).fetchone()
    scope = str(row[0]).strip().lower() if row and row[0] is not None else ""
    if scope not in {"identity", "shared"}:
        raise ValueError(f"Artifact query cache scope is invalid: {scope or '<empty>'}")
    return scope


def _artifact_query_cache_policy(data, view_name: str, default_ttl_seconds: int) -> dict[str, Any]:
    """Resolve retention policy without changing the safe TTL default."""
    policy_function = _query_cache_policy_function_name(view_name)
    function_exists = data.execute(
        "SELECT to_regprocedure(%s)",
        (f"{policy_function}()",),
    ).fetchone()[0]
    if function_exists is None:
        return {"refresh_mode": "ttl", "ttl_seconds": default_ttl_seconds}
    row = data.execute(f"SELECT {policy_function}()",).fetchone()  # noqa: S608
    policy = row[0] if row else None
    if isinstance(policy, str):
        policy = json.loads(policy)
    if not isinstance(policy, dict):
        raise ValueError("Artifact query cache policy must be a JSON object")
    if set(policy) - {"refresh_mode", "ttl_seconds"}:
        raise ValueError("Artifact query cache policy contains unsupported fields")
    refresh_mode = str(policy.get("refresh_mode") or "").strip().lower()
    ttl_seconds = policy.get("ttl_seconds")
    if refresh_mode not in {"ttl", "prewarm_swap"}:
        raise ValueError(f"Artifact query cache refresh mode is invalid: {refresh_mode or '<empty>'}")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 0:
        raise ValueError("Artifact query cache TTL must be a non-negative integer")
    if refresh_mode == "prewarm_swap" and ttl_seconds != 0:
        raise ValueError("Prewarm-swap query caches must use a zero (persistent) TTL")
    return {"refresh_mode": refresh_mode, "ttl_seconds": ttl_seconds}


def _apply_artifact_query_timeout(data, view_name: str) -> None:
    timeout_function = _safe_view_name(f"{view_name}_query_timeout_seconds")
    if data.execute("SELECT to_regprocedure(%s)", (f"{timeout_function}()",)).fetchone()[0] is None:
        return
    seconds = data.execute(f"SELECT {timeout_function}()").fetchone()[0]  # noqa: S608
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 120:
        raise ValueError("Artifact query timeout must be an integer from 1 to 120 seconds")
    data.execute("SELECT set_config('statement_timeout', %s, true)", (f"{seconds}s",))


def _query_cache_type(generation: Optional[str]) -> str:
    return "artifact-query" if generation is None else f"artifact-query-generation:{generation}"


def _artifact_query_cache_params(
    *,
    query: dict[str, Any],
    freshness: Optional[str],
    cache_scope: str,
    authenticated_subject: Optional[str] = None,
    authorized_roles: Optional[list[str]] = None,
    authenticated_email: Optional[str] = None,
) -> dict[str, Any]:
    if cache_scope == "shared":
        return {
            "cache_version": 3,
            "cache_scope": "shared",
            "data_freshness_timestamp": freshness,
            "query": query,
        }
    params = {
        "cache_version": 2,
        "data_freshness_timestamp": freshness,
        "authenticated_subject_hash": _subject_hash(authenticated_subject),
        "authorization_context_hash": _authorization_context_hash(authorized_roles),
        "query": query,
    }
    if authenticated_email:
        params["authenticated_email_hash"] = _subject_hash(authenticated_email)
    return params


def _execute_artifact_query_contract(data, view_name: str, serialized_query: str) -> Any:
    return _execute_registered_query(data, _query_function_name(view_name), serialized_query)


def _execute_registered_query(data, query_function: str, serialized_query: str) -> Any:
    function_signature = f"{query_function}(jsonb)"
    function_exists = data.execute(
        "SELECT to_regprocedure(%s)",
        (function_signature,),
    ).fetchone()[0]
    if function_exists is None:
        raise ValueError("Artifact has no database query contract")
    result = data.execute(
        f"SELECT {query_function}(%s::jsonb)",  # noqa: S608
        (serialized_query,),
    ).fetchone()[0]
    if isinstance(result, str):
        result = json.loads(result)
    return result


def get_artifact_asset(client_key: str, artifact_key: str, asset_path: str) -> dict[str, Any]:
    """Return one active package-owned artifact asset from Metadata."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", asset_path or ""):
        raise ValueError("Artifact asset path is invalid")
    if ".." in asset_path.split("/"):
        raise ValueError("Artifact asset path is invalid")

    with get_metadata_conn() as meta:
        relation = meta.execute(
            "SELECT to_regclass('app.artifact_assets')"
        ).fetchone()
        if not relation or relation[0] is None:
            raise ValueError("Artifact asset registry is unavailable")

        row = meta.execute(
            """
            SELECT content, content_type, sha256
            FROM app.artifact_assets
            WHERE client_key = %s
              AND artifact_key = %s
              AND asset_path = %s
              AND active
            """,
            (client_key, artifact_key, asset_path),
        ).fetchone()
    if row is None:
        raise ValueError(
            f"No active artifact asset found: client={client_key} "
            f"artifact={artifact_key} asset={asset_path}"
        )
    return {
        "content": bytes(row[0]),
        "content_type": str(row[1]),
        "sha256": str(row[2]),
    }


def execute_artifact_query(
    client_key: str,
    artifact_key: str,
    *,
    query: dict[str, Any],
    authenticated_subject: Optional[str] = None,
    authorized_roles: Optional[list[str]] = None,
    authenticated_email: Optional[str] = None,
    check_reference: Optional[Callable[[str], None]] = None,
) -> Any:
    """Execute a database-owned artifact query without interpreting its payload."""
    serialized_query = _serialized_artifact_query(query)

    with get_metadata_conn() as meta:
        artifact = _fetch_artifact(meta, client_key=client_key, artifact_key=artifact_key)
        if artifact is None:
            raise ValueError(f"No active artifact found: client={client_key} artifact={artifact_key}")
        render_artifact = _resolve_render_artifact(meta, artifact, check_reference=check_reference)
        view_name = _safe_view_name(render_artifact["view_name"])

    from .cache import get_artifact_cache, get_cache_settings

    cache_settings = get_cache_settings()
    with get_data_conn() as data:
        _begin_data_read(data)
        _set_authorization_context(
            data, client_key, authenticated_subject, authorized_roles,
            authenticated_email=authenticated_email,
        )
        policy = _artifact_query_cache_policy(data, view_name, cache_settings.ttl_seconds)
        cache_scope = _artifact_query_cache_scope(data, view_name, serialized_query)
        cache = get_artifact_cache(cache_settings) if cache_settings.enabled else None
        generation = None
        if cache and policy["refresh_mode"] == "prewarm_swap":
            try:
                generation = cache.get_query_generation(client_key, artifact_key)
            except Exception:
                cache = None
        freshness = (
            None
            if policy["refresh_mode"] == "prewarm_swap"
            else _artifact_cache_freshness_timestamp(data, client_key, artifact_key)
        )
        cache_params = _artifact_query_cache_params(
            query=query,
            freshness=freshness,
            cache_scope=cache_scope,
            authenticated_subject=authenticated_subject,
            authorized_roles=authorized_roles,
            authenticated_email=authenticated_email,
        )
        cache_key = cache.build_key(
            client_key, artifact_key, _query_cache_type(generation), cache_params
        ) if cache else None
        if cache and cache_key:
            try:
                cached = cache.get(cache_key)
                if cached is not None:
                    return cached
            except Exception:
                cache = None

        try:
            _apply_artifact_query_timeout(data, view_name)
            result = _execute_artifact_query_contract(data, view_name, serialized_query)
        except ValueError as exc:
            if str(exc) == "Artifact has no database query contract":
                raise ValueError(
                    f"Artifact has no database query contract: client={client_key} "
                    f"artifact={artifact_key}"
                ) from exc
            raise

        if cache and cache_key:
            try:
                ttl_seconds = policy["ttl_seconds"] or None
                cache.set(cache_key, result, ttl_seconds=ttl_seconds)
            except Exception:
                pass
        return result


def execute_artifact_metadata_query(
    client_key: str,
    artifact_key: str,
    query_key: str,
    *,
    query: dict[str, Any],
    authorize_query: Callable[[str], bool],
    authenticated_subject: str,
    authorized_roles: list[str],
    authenticated_email: Optional[str] = None,
) -> Any:
    """Run an explicitly registered report function on the Metadata connection."""
    serialized_query = _serialized_artifact_query(query)
    with get_metadata_conn() as meta:
        meta.execute("SET TRANSACTION READ ONLY")
        meta.execute("SET LOCAL statement_timeout = '10s'")
        meta.execute("SET LOCAL search_path = pg_catalog")
        registry = meta.execute(
            "SELECT to_regclass('app.artifact_metadata_queries')"
        ).fetchone()
        registration = None
        if registry and registry[0] is not None:
            registration = meta.execute(
                """
                SELECT q.function_name, q.access_policy
                FROM app.artifacts a
                JOIN app.artifact_metadata_queries q USING (artifact_id)
                WHERE a.client_key = %s AND a.artifact_key = %s AND a.active
                  AND q.query_key = %s
                """,
                (client_key, artifact_key, query_key),
            ).fetchone()
        if registration is None:
            raise ValueError("Artifact has no registered Metadata query")

        function_name, access_policy = registration
        if access_policy not in {"artifact", "usage", "access"}:
            raise ValueError("Invalid Metadata query access policy")
        client_only = authorize_query(access_policy)
        if client_only:
            serialized_query = _serialized_artifact_query({**query, "audience": client_key})

        if "." not in function_name:
            raise ValueError("Metadata query function must be schema-qualified")
        function_name = _safe_view_name(function_name)
        _set_authorization_context(
            meta, client_key, authenticated_subject, authorized_roles,
            authenticated_email=authenticated_email,
        )
        return _execute_registered_query(meta, function_name, serialized_query)


def prewarm_artifact_query_cache(
    client_key: str,
    artifact_key: str,
    *,
    queries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Rebuild the exact shared Redis entries declared by an artifact contract."""
    if not queries or len(queries) > 16:
        raise ValueError("Artifact query cache prewarm requires between 1 and 16 queries")

    serialized_queries = [(_serialized_artifact_query(query), query) for query in queries]
    with get_metadata_conn() as meta:
        artifact = _fetch_artifact(meta, client_key=client_key, artifact_key=artifact_key)
        if artifact is None:
            raise ValueError(f"No active artifact found: client={client_key} artifact={artifact_key}")
        render_artifact = _resolve_render_artifact(meta, artifact)
        view_name = _safe_view_name(render_artifact["view_name"])

    from .cache import get_artifact_cache, get_cache_settings

    cache_settings = get_cache_settings()
    if not cache_settings.enabled:
        raise ValueError("Artifact query cache prewarm requires Redis caching to be enabled")
    cache = get_artifact_cache(cache_settings)
    if cache is None:
        raise ValueError("Artifact query cache prewarm could not connect to Redis")

    entries: list[tuple[str, Any]] = []
    with get_data_conn() as data:
        _begin_data_read(data)
        _set_authorization_context(data, client_key, None, None)
        policy = _artifact_query_cache_policy(data, view_name, cache_settings.ttl_seconds)
        generation = uuid.uuid4().hex if policy["refresh_mode"] == "prewarm_swap" else None
        freshness = (
            None
            if policy["refresh_mode"] == "prewarm_swap"
            else _artifact_cache_freshness_timestamp(data, client_key, artifact_key)
        )
        _apply_artifact_query_timeout(data, view_name)
        for serialized_query, query in serialized_queries:
            cache_scope = _artifact_query_cache_scope(data, view_name, serialized_query)
            if cache_scope != "shared":
                raise ValueError("Artifact query cache prewarm accepts only shared query contracts")
            result = _execute_artifact_query_contract(data, view_name, serialized_query)
            cache_params = _artifact_query_cache_params(
                query=query,
                freshness=freshness,
                cache_scope="shared",
            )
            cache_key = cache.build_key(
                client_key, artifact_key, _query_cache_type(generation), cache_params
            )
            entries.append((cache_key, result))

    if policy["refresh_mode"] == "prewarm_swap":
        candidate_pattern = (
            f"bci:cache:{client_key}:{artifact_key}:artifact-query-generation:{generation}:*"
        )
        try:
            for cache_key, result in entries:
                cache.set(cache_key, result, ttl_seconds=None)
        except Exception:
            try:
                cache.invalidate_pattern(candidate_pattern)
            except Exception:
                pass
            raise
        # Do not delete a fully written candidate when activation has an
        # ambiguous network outcome: GETSET may have succeeded before its
        # response was lost. In either outcome a complete generation remains.
        previous_generation = cache.activate_query_generation(
            client_key, artifact_key, generation
        )
        # Cleanup is deliberately after the single-key activation boundary.
        # A cleanup failure wastes space but cannot make the active generation stale.
        try:
            cache.invalidate_pattern(
                f"bci:cache:{client_key}:{artifact_key}:artifact-query:*"
                if previous_generation is None
                else f"bci:cache:{client_key}:{artifact_key}:artifact-query-generation:{previous_generation}:*"
            )
        except Exception:
            pass
    else:
        cache.invalidate_pattern(f"bci:cache:{client_key}:{artifact_key}:artifact-query:*")
        for cache_key, result in entries:
            cache.set(cache_key, result, ttl_seconds=policy["ttl_seconds"])

    return {
        "client_key": client_key,
        "artifact_key": artifact_key,
        "status": "prewarmed",
        "entry_count": len(entries),
        "refresh_mode": policy["refresh_mode"],
    }


def _report_rows(data, name, query):
    """Read the declared view or bind the caller's JSON to its query function."""
    view = _safe_view_name(name)
    if query is None:
        cursor = data.execute(f"SELECT * FROM {view}")
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()], None
    result = _execute_artifact_query_contract(data, view, _serialized_artifact_query(query))
    if isinstance(result, dict):
        return [result], result
    if isinstance(result, list) and all(isinstance(row, dict) for row in result):
        return result, result
    return [{"value": result}], result


def execute_artifact(
    client_key: str,
    artifact_key: str,
    behavior: str = "display",
    output_formats: Optional[list[str]] = None,
    execution_query: Optional[dict[str, Any]] = None,
    refresh_cache: bool = False,
    authenticated_subject: Optional[str] = None,
    authorized_roles: Optional[list[str]] = None,
    check_reference: Optional[Callable[[str], None]] = None,
) -> dict:
    """Render declared data, optionally export files, and record the outcome."""
    if behavior not in {"display", "preview", "dry-run"}:
        raise ValueError("Unsupported render behavior")
    started_at = _now()
    run_id = str(uuid.uuid4())
    started_perf = perf_counter()
    artifact_id: Optional[str] = None
    output_formats = output_formats or []
    output_directory = None
    committing = False
    from .renderer import render
    from .cache import (
        build_render_cache_params,
        get_artifact_cache,
        get_cache_settings,
        get_cached_render,
        set_cached_render,
    )

    try:
        with get_metadata_conn() as meta:
            # ── 1. Read artifact config ────────────────────────────────
            artifact = _fetch_artifact(meta, client_key=client_key, artifact_key=artifact_key)

            if artifact is None:
                raise ValueError(
                    f"No active artifact found: client={client_key} artifact={artifact_key}"
                )

            artifact_id = artifact["artifact_id"]
            delivery_mode = artifact["delivery_mode"]
            subject = artifact["subject"]

            composition = (
                load_email_composition(meta, artifact, _fetch_artifact)
                if artifact["delivery_mode"] in {"email", "both"} and artifact["template_body"]
                else None
            )
            if composition and check_reference:
                for child in {content["artifact_key"] for _, content, _ in composition.slots}:
                    check_reference(child)
            if composition is not None and output_formats:
                raise ValueError("Email compositions use render-only HTML execution")
            render_artifact = artifact if composition else _resolve_render_artifact(meta, artifact, check_reference=check_reference)
            view_name = None if composition else _safe_view_name(render_artifact["view_name"])

            template_body = render_artifact["template_body"]
            render_artifact_id = render_artifact["artifact_id"]
            render_template_id = render_artifact["template_id"]

            cache_settings = get_cache_settings()
            cacheable_render = (
                behavior in {"display", "preview"}
                and not output_formats
                and execution_query is None
                and composition is None
            )
            cache_status = "bypass"
            cache_read_ms: Optional[float] = None
            data_query_ms: Optional[float] = None
            render_ms: Optional[float] = None
            data_freshness_timestamp: Optional[str] = None
            if cacheable_render and cache_settings.enabled:
                with get_data_conn() as data:
                    _begin_data_read(data)
                    _set_authorization_context(data, client_key, authenticated_subject, authorized_roles)
                    data_freshness_timestamp = _artifact_cache_freshness_timestamp(data, client_key, artifact_key)
            cache = get_artifact_cache(cache_settings) if cacheable_render and cache_settings.enabled else None
            if not cacheable_render:
                cache_status = "bypass"
            elif not cache_settings.enabled or not cache_settings.cache_rendered:
                cache_status = "disabled"
            elif cache is None:
                cache_status = "unavailable"
            elif refresh_cache:
                cache_status = "refresh"
            cache_params = build_render_cache_params(
                behavior=behavior,
                view_name=view_name,
                template_body=template_body,
                template_id=render_template_id,
                render_artifact_id=render_artifact_id,
                authenticated_subject_hash=_subject_hash(authenticated_subject),
                authorization_context_hash=_authorization_context_hash(authorized_roles),
                data_freshness_timestamp=data_freshness_timestamp,
            )
            cached_render = None
            if cache is not None and cache_settings.cache_rendered and not refresh_cache:
                cache_read_started = perf_counter()
                cached_render = get_cached_render(cache, client_key, artifact_key, cache_params)
                cache_read_ms = (perf_counter() - cache_read_started) * 1000
                cache_status = "hit" if cached_render is not None else "miss"

            # ── 2. Query data DB ───────────────────────────────────────
            query_result: Any = None
            if cached_render is not None:
                html = cached_render["html"]
                row_count = int(cached_render.get("row_count", 0))
            else:
                data_query_started = perf_counter()
                with get_data_conn() as data:
                    _begin_data_read(data, consistent=bool(composition))
                    _set_authorization_context(data, client_key, authenticated_subject, authorized_roles)
                    if composition:
                        def content_rows(name, parameters):
                            rows, _ = _report_rows(data, name, parameters)
                            return rows

                        subject, composed_html, content_count = compose(
                            composition, template_body, execution_query, content_rows, render,
                        )
                    else:
                        data_rows, query_result = _report_rows(data, view_name, execution_query)
                data_query_ms = (perf_counter() - data_query_started) * 1000

            if cached_render is None:
                # 3. Render
                render_started = perf_counter()
                html = composed_html if composition else render(template_body, data_rows)
                render_ms = (perf_counter() - render_started) * 1000
                row_count = content_count if composition else len(data_rows)
                if (
                    isinstance(query_result, dict)
                    and isinstance(query_result.get("csv"), dict)
                    and isinstance(query_result["csv"].get("rows"), list)
                ):
                    row_count = len(query_result["csv"]["rows"])
                if cache is not None and cache_settings.cache_rendered:
                    set_cached_render(
                        cache,
                        client_key,
                        artifact_key,
                        cache_params,
                        html=html,
                        row_count=row_count,
                        ttl_seconds=cache_settings.ttl_seconds,
                    )

            # ── 4. Legacy preview mode — return HTML without logging or sending
            if behavior == "preview":
                return {
                    "run_id": None,
                    "client_key": client_key,
                    "artifact_key": artifact_key,
                    "status": "preview",
                    "started_at": started_at,
                    "completed_at": _now(),
                    "preview_html": html,
                    "outputs": [],
                    "cache": {
                        "status": cache_status,
                        "enabled": cache_settings.enabled,
                        "row_count": row_count,
                        "cache_read_ms": cache_read_ms,
                        "data_freshness_timestamp": data_freshness_timestamp,
                        "data_query_ms": data_query_ms,
                        "render_ms": render_ms,
                        "total_ms": (perf_counter() - started_perf) * 1000,
                    },
                }

            # ── 5. Generate requested file outputs ─────────────────────
            outputs: list[dict[str, Any]] = []
            if set(output_formats) & {"pdf", "csv"}:
                output_directory = create_output_directory(client_key, artifact_key, run_id)
            if "pdf" in output_formats:
                outputs.extend(
                    _generate_pdf_outputs(
                        run_id=run_id,
                        artifact_id=artifact_id,
                        client_key=client_key,
                        artifact_key=artifact_key,
                        subject=subject,
                        template_body=template_body,
                        data_rows=data_rows,
                        render=render,
                        rendered_at=started_at,
                    )
                )
            if "csv" in output_formats:
                outputs.append(
                    _generate_csv_output(
                        run_id=run_id,
                        artifact_id=artifact_id,
                        client_key=client_key,
                        artifact_key=artifact_key,
                        query_result=query_result,
                    )
                )

            completed_at = _now()
            meta.execute(
                """
                INSERT INTO log.artifact_runs
                    (run_id, artifact_id, artifact_key, client_key,
                     triggered_by, status, delivery_mode,
                     row_count, slice_count, recipient_count,
                     started_at, completed_at)
                VALUES (%s::uuid, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s)
                """,
                (
                    run_id, artifact_id, artifact_key, client_key,
                    "api", "completed", delivery_mode,
                    row_count, len(outputs) if outputs else None, 0,
                    started_at, completed_at,
                ),
            )
            _insert_artifact_outputs(meta, outputs)
            committing = True
            meta.commit()

            return {
                "run_id": run_id,
                "client_key": client_key,
                "artifact_key": artifact_key,
                "status": "success",
                "started_at": started_at,
                "completed_at": completed_at,
                "preview_html": html if behavior == "display" else None,
                "email_subject": subject if composition else None,
                "outputs": outputs,
                "cache": {
                    "status": cache_status,
                    "enabled": cache_settings.enabled,
                    "row_count": row_count,
                    "cache_read_ms": cache_read_ms,
                    "data_freshness_timestamp": data_freshness_timestamp,
                    "data_query_ms": data_query_ms,
                    "render_ms": render_ms,
                    "total_ms": (perf_counter() - started_perf) * 1000,
                },
            }

    except HTTPException:
        raise
    except Exception as exc:
        completed_at = _now()
        if output_directory is not None and not committing:
            # This run exclusively created the directory. A failed commit has
            # an uncertain outcome and must retain any potentially referenced files.
            try:
                shutil.rmtree(output_directory)
            except OSError:
                logger.warning("Could not clean up incomplete output for render %s", run_id)
        if artifact_id and not committing:
            try:
                with get_metadata_conn() as meta:
                    meta.execute(
                        """INSERT INTO log.artifact_runs
                           (run_id, artifact_id, artifact_key, client_key, triggered_by,
                            status, started_at, completed_at, error_message)
                           VALUES (%s, %s, %s, %s, 'api', 'failed', %s, %s, %s)""",
                        (run_id, artifact_id, artifact_key, client_key, started_at,
                         completed_at, type(exc).__name__),
                    )
            except Exception:
                logger.exception("Could not record failed render %s", run_id)
        logger.warning("Render %s failed (%s)", run_id, type(exc).__name__)
        return {"run_id": run_id, "status": "error", "error_message": "Artifact render failed"}
