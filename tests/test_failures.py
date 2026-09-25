import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

METROLOGY = Actor("met-1", "metrology")
ADMIN = Actor("admin", "admin")
ANALYST = Actor("an-1", "analyst")

REGISTRATION = {"assignee": "M-7", "planned_date": "2026-10-01", "purpose": "annual check"}


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def _instrument(self):
        return self.service.create(
            ADMIN, "instrument", {"name": "I", "serial": "S"}
        )

    def _sent_instrument(self, planned_date="2026-10-01"):
        instrument = self._instrument()
        data = dict(REGISTRATION, planned_date=planned_date)
        self.service.transition(METROLOGY, instrument["id"], "send_calibration", data)
        orders = [
            order
            for order in self.service.list("calibration")
            if order["data"]["instrument_id"] == instrument["id"]
        ]
        return instrument, orders[0]

    def test_permission_denied(self):
        entity = self._instrument()
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                entity["id"],
                "send_calibration",
                dict(REGISTRATION),
            )

    def test_send_calibration_requires_registration(self):
        entity = self._instrument()
        with self.assertRaises(ValidationError):
            self.service.transition(
                METROLOGY, entity["id"], "send_calibration", {"assignee": "M-7"}
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                METROLOGY,
                entity["id"],
                "send_calibration",
                dict(REGISTRATION, planned_date="not-a-date"),
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("tech-1", "technician"),
                entity["id"],
                "send_calibration",
                dict(REGISTRATION),
            )

    def test_version_conflict(self):
        entity = self._instrument()
        with self.assertRaises(ConflictError):
            self.service.transition(
                ADMIN,
                entity["id"],
                "send_calibration",
                dict(REGISTRATION),
                expected_version=999,
            )

    def test_conflict_leaves_state_and_audit_untouched(self):
        entity = self._instrument()
        audit_before = self.service.audit_log(entity["id"])
        with self.assertRaises(ConflictError):
            self.service.transition(
                ADMIN,
                entity["id"],
                "send_calibration",
                dict(REGISTRATION),
                expected_version=999,
            )
        after = self.service.get(entity["id"])
        self.assertEqual(after["status"], "active")
        self.assertEqual(after["version"], entity["version"])
        self.assertEqual(after["data"], entity["data"])
        self.assertEqual(self.service.audit_log(entity["id"]), audit_before)
        self.assertEqual(self.service.list("calibration"), [])

    def test_duplicate_idempotency_key_returns_same_entity(self):
        first = self.service.create(
            ADMIN,
            "instrument",
            {"name": "I", "serial": "S"},
            idempotency_key="duplicate-check",
        )
        second = self.service.create(
            ADMIN,
            "instrument",
            {"name": "I", "serial": "S"},
            idempotency_key="duplicate-check",
        )
        self.assertEqual(first["id"], second["id"])

    def test_duplicate_transition_reuses_first_record(self):
        instrument = self._instrument()
        first = self.service.transition(
            METROLOGY,
            instrument["id"],
            "send_calibration",
            dict(REGISTRATION),
            idempotency_key="send-1",
        )
        second = self.service.transition(
            METROLOGY,
            instrument["id"],
            "send_calibration",
            dict(REGISTRATION),
            idempotency_key="send-1",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["version"], first["version"])
        self.assertEqual(len(self.service.list("calibration")), 1)

    def test_release_blocked_with_work_order_until_result_backfilled(self):
        instrument, order = self._sent_instrument()
        method = self.service.create(ADMIN, "method", {"name": "M", "version": "v1"})
        self.service.transition(
            ADMIN,
            method["id"],
            "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [instrument["id"]]},
        )
        result = self.service.create(
            ANALYST, "result", {"sample_id": "S-1", "measurement": "m"}
        )
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                ANALYST,
                result["id"],
                "release",
                {
                    "instrument_id": instrument["id"],
                    "method_id": method["id"],
                    "value": 1.0,
                    "unit": "mg/L",
                },
            )
        self.assertIn(order["id"], str(ctx.exception))

    def test_failed_calibration_keeps_instrument_disabled(self):
        instrument, order = self._sent_instrument()
        self.service.transition(
            METROLOGY,
            order["id"],
            "perform",
            {
                "result": "failed",
                "performed_at": "2026-09-25",
                "uncertainty": 0.5,
                "disposition": "return to vendor",
            },
        )
        instrument = self.service.get(instrument["id"])
        self.assertEqual(instrument["status"], "quarantined")
        self.assertEqual(instrument["data"]["disposition"], "return to vendor")
        with self.assertRaises(ValidationError):
            self.service.transition(
                METROLOGY,
                order["id"],
                "perform",
                {"result": "failed", "performed_at": "2026-09-26", "uncertainty": 0.5},
            )

    def test_overdue_work_orders_filter(self):
        _, overdue_order = self._sent_instrument("2020-01-01")
        self._sent_instrument("2099-01-01")
        overdue = self.service.list("calibration", status="overdue")
        self.assertEqual([item["id"] for item in overdue], [overdue_order["id"]])
        self.service.transition(
            METROLOGY,
            overdue_order["id"],
            "perform",
            {
                "result": "passed",
                "performed_at": "2026-09-25",
                "uncertainty": 0.01,
                "due_at": "2099-01-01",
            },
        )
        self.assertEqual(self.service.list("calibration", status="overdue"), [])


if __name__ == "__main__":
    unittest.main()
