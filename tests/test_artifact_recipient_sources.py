from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.modules.setdefault("psycopg", SimpleNamespace(connect=None, Connection=object))
sys.modules.setdefault("requests", SimpleNamespace(post=None))

engine = importlib.import_module("app.engine")


class Result:
    def __init__(self, *, one=None, all_rows=None):
        self.one = one
        self.all_rows = all_rows or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.all_rows


class Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, exc_type, exc, traceback):
        return False


class ArtifactRecipientSourceTests(unittest.TestCase):
    def test_recipient_source_uses_data_view_without_static_registry(self) -> None:
        class Metadata:
            def execute(self, query, params=None):
                if "to_regclass" in query:
                    return Result(one=("app.artifact_recipient_sources",))
                if "FROM app.artifact_recipient_sources" in query:
                    return Result(one=("public.governed_recipient_source",))
                raise AssertionError("static recipient registry must not be queried")

        class Data:
            def execute(self, query, params):
                self_test.assertIn("FROM public.governed_recipient_source", query)
                self_test.assertEqual(params, ("facility-email-artifact",))
                return Result(all_rows=[("owner@example.test", "to")])

        self_test = self
        with patch.object(engine, "get_data_conn", return_value=Context(Data())):
            recipients = engine._delivery_recipients(
                Metadata(),
                "11111111-1111-1111-1111-111111111111",
                "facility-email-artifact",
            )

        self.assertEqual(recipients, [("owner@example.test", "to")])

    def test_recipient_source_fails_closed_when_facility_is_not_eligible(self) -> None:
        class Metadata:
            def execute(self, query, params=None):
                if "to_regclass" in query:
                    return Result(one=("app.artifact_recipient_sources",))
                return Result(one=("public.governed_recipient_source",))

        class Data:
            def execute(self, query, params):
                return Result(all_rows=[])

        with patch.object(engine, "get_data_conn", return_value=Context(Data())):
            with self.assertRaisesRegex(ValueError, "No eligible recipient"):
                engine._delivery_recipients(
                    Metadata(),
                    "11111111-1111-1111-1111-111111111111",
                    "facility-email-artifact",
                )

    def test_static_recipient_registry_remains_backward_compatible(self) -> None:
        class Metadata:
            def execute(self, query, params=None):
                if "to_regclass" in query:
                    return Result(one=(None,))
                if "FROM app.artifact_recipients" not in query:
                    raise AssertionError("static recipient registry query expected")
                return Result(all_rows=[("legacy@example.test", "to")])

        self.assertEqual(
            engine._delivery_recipients(
                Metadata(),
                "11111111-1111-1111-1111-111111111111",
                "legacy-email-artifact",
            ),
            [("legacy@example.test", "to")],
        )

    def test_recipient_source_rejects_unsafe_view_name(self) -> None:
        class Metadata:
            def execute(self, query, params=None):
                if "to_regclass" in query:
                    return Result(one=("app.artifact_recipient_sources",))
                return Result(one=("public.source;drop table app.artifacts",))

        with self.assertRaisesRegex(ValueError, "not a safe identifier"):
            engine._delivery_recipients(
                Metadata(),
                "11111111-1111-1111-1111-111111111111",
                "facility-email-artifact",
            )


if __name__ == "__main__":
    unittest.main()
