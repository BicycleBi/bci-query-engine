"""Render email layouts from ordered, named Metadata content references."""

from dataclasses import dataclass
import re

from jinja2 import Environment, StrictUndefined
from markupsafe import Markup


@dataclass(frozen=True)
class EmailComposition:
    subject_template: str
    slots: list


def load_email_composition(meta, parent, fetch_artifact):
    if not meta.execute("SELECT to_regclass('app.artifact_email_composition')").fetchone()[0]:
        return None
    config = meta.execute(
        "SELECT subject_template FROM app.artifact_email_composition WHERE artifact_id=%s::uuid",
        (parent["artifact_id"],),
    ).fetchone()
    if not config:
        return None
    if parent["delivery_mode"] not in ("email", "both") or not parent["template_body"]:
        raise ValueError("An email composition needs an active email layout")
    rows = meta.execute(
        """SELECT slot_key,content_artifact_id::text,parameter_map
           FROM app.artifact_email_content
           WHERE artifact_id=%s::uuid ORDER BY position,slot_key LIMIT 33""",
        (parent["artifact_id"],),
    ).fetchall()
    if not 1 <= len(rows) <= 32:
        raise ValueError("An email composition needs between 1 and 32 content slots")
    slots = []
    for key, content_id, mapping in rows:
        content = fetch_artifact(meta, artifact_id=content_id)
        if (
            content_id == parent["artifact_id"]
            or not content
            or content["client_key"] != parent["client_key"]
            or not content["view_name"]
            or not content["template_body"]
        ):
            raise ValueError("Email content must be an active same-client data artifact")
        if mapping is not None and (
            not isinstance(mapping, dict)
            or any(not isinstance(value, str) for value in mapping.values())
        ):
            raise ValueError("Invalid email content parameter mapping")
        slots.append((key, content, mapping))
    return EmailComposition(config[0], slots)


def compose(composition, layout, query, fetch_rows, render_rows):
    arguments = query or {}
    slots = {}
    for key, content, mapping in composition.slots:
        if mapping is None:
            child_query = query
        else:
            if any(parameter not in arguments for parameter in mapping.values()):
                raise ValueError("A required email content parameter is missing")
            child_query = {target: arguments[source] for target, source in mapping.items()}
        rows = fetch_rows(content["view_name"], child_query)
        # Only HTML produced by a trusted content template is marked safe.
        slots[key] = Markup(render_rows(content["template_body"], rows))

    html_env = Environment(autoescape=True, undefined=StrictUndefined)
    subject_env = Environment(autoescape=False, undefined=StrictUndefined)
    context = {"slots": slots, "parameters": arguments}
    subject = subject_env.from_string(composition.subject_template).render(parameters=arguments)
    if not subject.strip() or len(subject) > 998 or re.search(r"[\x00-\x1f\x7f]", subject):
        raise ValueError("Invalid composed email subject")
    html = html_env.from_string(layout).render(**context)
    return subject, html, len(slots)
