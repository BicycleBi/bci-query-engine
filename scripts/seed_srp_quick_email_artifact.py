from app.engine import write_artifact_definition


def main() -> None:
    payload = {
        "client_key": "srp",
        "client_display_name": "Spine Rehab Partners",
        "artifact_key": "visit-counts-quick-email",
        "display_name": "SRP Visit Counts Snapshot",
        "description": "Quick HTML email artifact for SRP visit counts.",
        "delivery_mode": "email",
        "active": True,
        "recipients": [
            {
                "email": "daniel@bicyclebi.com",
                "delivery_type": "to",
                "active": True,
            }
        ],
        "references": [
            {
                "referenced_artifact_key": "visit-counts-quick-page",
                "reference_role": "body",
                "output_format": "html",
                "active": True,
            }
        ],
    }
    result = write_artifact_definition(payload)
    print(result)


if __name__ == "__main__":
    main()