from pathlib import Path

from app.engine import write_artifact_definition


TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "srp_visit_counts_quick.html"


def main() -> None:
    template_html = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = {
        "client_key": "srp",
        "client_display_name": "Spine Rehab Partners",
        "artifact_key": "visit-counts-quick-page",
        "display_name": "SRP Visit Counts Quick Page",
        "description": "Quick HTML preview page for SRP visit counts.",
        "view_name": "public.srp_visit_counts_from_csv",
        "delivery_mode": "web",
        "active": True,
        "template": {
            "template_key": "srp-visit-counts-quick-page",
            "version": 1,
            "display_name": "SRP Visit Counts Quick Page",
            "content_type": "html",
            "html_content": template_html,
            "is_active": True,
        },
        "recipients": [],
    }
    result = write_artifact_definition(payload)
    print(result)


if __name__ == "__main__":
    main()