"""Configuration-driven SRP facility reporting; read-only and fact-table free."""

from collections import defaultdict
from datetime import datetime, timezone

from .analytics import get_analytics_reporting_config
from .db import get_data_conn, get_metadata_conn

MAX_SOURCE_ROWS = 5000
PAGE_SIZE = 100


def _read(factory, relation: str, query: str, params=None):
    """Run one bounded fixed projection from an approved configuration source."""
    with factory() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '10s'")
        if not conn.execute("SELECT to_regclass(%s)", (relation,)).fetchone()[0]:
            return None
        rows = conn.execute(query + " LIMIT 5001", params).fetchall()
        if len(rows) > MAX_SOURCE_ROWS:
            raise RuntimeError("Facility configuration source exceeds reporting bound")
        return rows


def attach_copied_facility_access(payload, access_payload):
    """Preserve an inert copied Production snapshot without joining live Dev data."""
    if not isinstance(payload, dict):
        return payload
    copied = payload.get("source_environment") == "production"
    access_copied = isinstance(access_payload, dict) and access_payload.get("source_environment") == "production"
    status = "available" if copied and access_copied else "unavailable"
    sources = payload.setdefault("sources", [])
    sources[:] = [source for source in sources if source.get("source") != "facility_access_snapshot"]
    sources.append({"source": "facility_access_snapshot", "status": status})
    return payload


def _load_configuration():
    source_queries = (
        (
            "facilities",
            "public.srp_core_facility",
            """
            SELECT 'ecw:' || btrim(facility_id), btrim(facility_id), btrim(facility_name)
            FROM public.srp_core_facility
            WHERE _is_current
              AND NULLIF(btrim(facility_id), '') IS NOT NULL
              AND NULLIF(btrim(facility_name), '') IS NOT NULL
            ORDER BY btrim(facility_name), btrim(facility_id)
            """,
        ),
        (
            "report_scope",
            "public.srp_client_access_facility_report_scope",
            """
            SELECT facility_key, facility_id, facility_name, artifact_key, entity_code, mapping_source
            FROM public.srp_client_access_facility_report_scope
            ORDER BY facility_name, artifact_key
            """,
        ),
        (
            "distributions",
            "public.srp_client_access_facility_distribution",
            """
            SELECT facility_key, facility_name, artifact_key, delivery_artifact_key,
                   recipient_email, delivery_type, enabled, configuration_source
            FROM public.srp_client_access_facility_distribution
            ORDER BY facility_name, artifact_key, recipient_email, delivery_type
            """,
        ),
        (
            "role_scope",
            "public.srp_client_access_facility_role_scope",
            """
            SELECT facility_key, facility_name, artifact_key, role_key, entity_code, configuration_source
            FROM public.srp_client_access_facility_role_scope
            ORDER BY facility_name, artifact_key, role_key
            """,
        ),
    )
    data = {}
    sources = []
    for name, relation, query in source_queries:
        try:
            data[name] = _read(get_data_conn, relation, query)
        except Exception:
            data[name] = None
        sources.append({"source": name, "status": "available" if data[name] is not None else "unavailable"})
    if data["facilities"] is None:
        raise RuntimeError("Facility roster is unavailable")
    if data["report_scope"] is None or data["distributions"] is None or data["role_scope"] is None:
        raise RuntimeError("Facility configuration projection is unavailable")
    return data, sources


def _artifact_catalog(artifact_keys):
    if not artifact_keys:
        return {}, []
    rows = _read(
        get_metadata_conn,
        "app.artifacts",
        """
        SELECT artifact_key, display_name, delivery_mode, active
        FROM app.artifacts
        WHERE client_key='srp' AND artifact_key = ANY(%s)
        ORDER BY display_name, artifact_key
        """,
        (sorted(artifact_keys),),
    )
    catalog = {
        key: {
            "artifact_key": key,
            "display_name": display_name or key,
            "delivery_mode": delivery_mode,
            "front_end": delivery_mode in {"web", "both"},
            "active": bool(active),
        }
        for key, display_name, delivery_mode, active in rows or []
    }
    return catalog, rows


def _scoped_users(artifact_keys):
    if not artifact_keys:
        return []
    config = get_analytics_reporting_config("srp")
    return _read(
        get_metadata_conn,
        "analytics_reporting.client_grants",
        """
        SELECT DISTINCT u.display_name, u.email, e.artifact_key, g.role_key, g.source, g.group_key
        FROM analytics_reporting.authorized_user_audiences u
        JOIN analytics_reporting.client_grants g
          ON g.client_key=u.client_key AND g.user_id=u.user_id
        JOIN analytics_reporting.artifact_access_edges e
          ON e.client_key=u.client_key AND e.admin_role_key=u.admin_role_key AND e.user_id=u.user_id
        WHERE u.client_key='srp' AND u.admin_role_key=%(admin)s
          AND u.audience_key='srp' AND u.active
          AND e.artifact_key = ANY(%(artifacts)s)
          AND e.artifact_active AND (e.assigned_view OR e.assigned_run)
        ORDER BY e.artifact_key, u.display_name, u.email, g.role_key
        """,
        {"admin": config.admin_role, "artifacts": sorted(artifact_keys)},
    ) or []


def _home_artifacts():
    rows = _read(
        get_data_conn,
        "public.home_artifact_components",
        """
        SELECT artifact_key
        FROM public.home_artifact_components
        WHERE client_key='srp' AND home_artifact_key='home' AND card_state='active'
          AND NULLIF(btrim(component_json->>'href'), '') IS NOT NULL
        ORDER BY artifact_key
        """,
    )
    return {row[0] for row in rows or []}, rows


def _build_rows(data, catalog, user_rows, home_artifacts=None):
    home_artifacts = home_artifacts or set()
    facilities = {}
    for facility_key, facility_id, facility_name in data["facilities"]:
        facilities[facility_key] = {
            "facility_key": facility_key,
            "facility_id": facility_id,
            "facility_name": facility_name,
            "entity_codes": [],
            "reports": {},
        }

    for facility_key, _facility_id, _facility_name, artifact_key, entity_code, mapping_source in data["report_scope"]:
        facility = facilities.get(facility_key)
        artifact = catalog.get(artifact_key)
        if facility is None or artifact is None or not artifact["active"]:
            continue
        if entity_code and entity_code not in facility["entity_codes"]:
            facility["entity_codes"].append(entity_code)
        facility["reports"].setdefault(
            artifact_key,
            {**artifact, "mapping_sources": [], "distributions": [], "users": []},
        )
        report = facility["reports"][artifact_key]
        if mapping_source not in report["mapping_sources"]:
            report["mapping_sources"].append(mapping_source)

    for facility_key, _facility_name, artifact_key, delivery_artifact_key, email, delivery_type, enabled, source in data["distributions"]:
        report = facilities.get(facility_key, {}).get("reports", {}).get(artifact_key)
        if report is None or not enabled or not email:
            continue
        report["distributions"].append(
            {
                "delivery_artifact_key": delivery_artifact_key,
                "recipient_email": email,
                "delivery_type": delivery_type,
                "configuration_source": source,
            }
        )

    role_scope = defaultdict(set)
    for facility_key, _facility_name, artifact_key, role_key, entity_code, _source in data["role_scope"]:
        if role_key:
            role_scope[(facility_key, artifact_key)].add(role_key)
        facility = facilities.get(facility_key)
        if facility is not None and entity_code and entity_code not in facility["entity_codes"]:
            facility["entity_codes"].append(entity_code)

    restricted_artifacts = {artifact_key for _facility_key, artifact_key in role_scope}
    for display_name, username, artifact_key, role_key, source, group_key in user_rows or []:
        for facility_key, facility in facilities.items():
            if artifact_key in restricted_artifacts and role_key not in role_scope.get((facility_key, artifact_key), set()):
                continue
            report = facility["reports"].get(artifact_key)
            if report is None:
                continue
            user = {
                "display_name": display_name or username,
                "username": username,
                "access_basis": "Via group: " + group_key if source == "group" and group_key else "Direct assignment",
                "available_from_home": artifact_key in home_artifacts,
            }
            if user not in report["users"]:
                report["users"].append(user)

    result = []
    for facility in facilities.values():
        reports = sorted(facility.pop("reports").values(), key=lambda item: (item["display_name"].casefold(), item["artifact_key"]))
        users = {}
        for report in reports:
            report["distributions"].sort(key=lambda item: (str(item["recipient_email"]).casefold(), str(item["delivery_type"])))
            report["users"].sort(key=lambda item: (str(item["display_name"]).casefold(), str(item["username"]).casefold()))
            for user in report["users"]:
                key = str(user["username"]).casefold() + "|" + report["artifact_key"]
                users[key] = {
                    **user,
                    "artifact_key": report["artifact_key"],
                    "artifact_name": report["display_name"],
                }
        facility["reports"] = reports
        facility["user_access_status"] = "available" if user_rows is not None else "unavailable"
        facility["user_access"] = sorted(users.values(), key=lambda item: (str(item["display_name"]).casefold(), str(item["username"]).casefold()))
        facility["entity_codes"].sort()
        result.append(facility)
    return sorted(result, key=lambda item: (item["facility_name"].casefold(), item["facility_key"]))


def get_facility_report(*, search="", status="", offset=0):
    if len(search) > 100 or status or not 0 <= offset <= 100000:
        raise ValueError("Invalid facility reporting bounds")
    data, sources = _load_configuration()
    artifact_keys = {row[3] for row in data["report_scope"] if row[3]}
    try:
        catalog, catalog_rows = _artifact_catalog(artifact_keys)
    except Exception:
        catalog, catalog_rows = {}, None
    try:
        user_rows = _scoped_users(artifact_keys)
    except Exception:
        user_rows = None
    try:
        home_artifacts, home_rows = _home_artifacts()
    except Exception:
        home_artifacts, home_rows = set(), None
    sources.extend(
        [
            {"source": "artifact_registry", "status": "available" if catalog_rows is not None else "unavailable"},
            {"source": "security_metadata", "status": "available" if user_rows is not None else "unavailable"},
            {"source": "home_navigation", "status": "available" if home_rows is not None else "unavailable"},
        ]
    )
    rows = _build_rows(data, catalog, user_rows, home_artifacts)
    query = search.strip().casefold()
    if query:
        rows = [row for row in rows if query in " ".join([row["facility_name"], row["facility_key"], *row["entity_codes"]]).casefold()]
    return {
        "client_key": "srp",
        "audience": "srp",
        "as_of": datetime.now(timezone.utc),
        "rows": rows[offset : offset + PAGE_SIZE],
        "total": len(rows),
        "offset": offset,
        "limit": PAGE_SIZE,
        "has_more": offset + PAGE_SIZE < len(rows),
        "sources": sources,
        "coverage": "configuration",
    }
