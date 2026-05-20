"""
renderer.py — Jinja2 HTML report renderer.
"""
from typing import Any

from jinja2 import Environment, Undefined


def render(template_content: str, rows: list[dict[str, Any]]) -> str:
    """
    Render a Jinja2 HTML template with the supplied data rows.

    The template receives:
      - rows    list of dicts, one per result row from the view
      - columns list of column names (derived from the first row)
    """
    env = Environment(
        autoescape=True,
        undefined=Undefined,
    )
    tmpl = env.from_string(template_content)
    columns = list(rows[0].keys()) if rows else []
    return tmpl.render(rows=rows, columns=columns)
