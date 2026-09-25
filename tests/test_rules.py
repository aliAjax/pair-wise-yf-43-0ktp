import unittest

from src.rules import calibration_current, calibration_outcome, work_order_overdue
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        self.assertTrue(calibration_current("2099-01-01", "2026-09-24"))
        self.assertFalse(calibration_current("2025-01-01", "2026-09-24"))
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(self.admin, {"kind": "calibration", "status": "requested", "data": {}}, "perform", {"result": "unknown", "performed_at": "2026-01-01", "uncertainty": 0.1})

    def test_send_calibration_registration_rules(self):
        instrument = {"kind": "instrument", "status": "active", "data": {}}
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(self.admin, instrument, "send_calibration", {"assignee": "M-7"})
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(self.admin, instrument, "send_calibration", {"assignee": "M-7", "planned_date": "not-a-date", "purpose": "p"})
        status, patch = self.rules.validate_transition(
            self.admin,
            instrument,
            "send_calibration",
            {"assignee": "M-7", "planned_date": "2026-10-01", "purpose": "annual check"},
        )
        self.assertEqual(status, "calibrating")
        self.assertEqual(patch["assignee"], "M-7")

    def test_perform_requires_outcome_fields(self):
        calibration = {"kind": "calibration", "status": "requested", "data": {}}
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(self.admin, calibration, "perform", {"result": "passed", "performed_at": "2026-01-01", "uncertainty": 0.1})
        with self.assertRaises(ValidationError):
            self.rules.validate_transition(self.admin, calibration, "perform", {"result": "failed", "performed_at": "2026-01-01", "uncertainty": 0.1})

    def test_calibration_outcome(self):
        self.assertEqual(
            calibration_outcome({"result": "passed", "due_at": "2099-01-01"}),
            ("active", {"due_at": "2099-01-01"}),
        )
        self.assertEqual(
            calibration_outcome({"result": "failed", "disposition": "scrap"}),
            ("quarantined", {"disposition": "scrap"}),
        )

    def test_work_order_overdue(self):
        order = {"status": "requested", "data": {"planned_date": "2020-01-01"}}
        self.assertTrue(work_order_overdue(order, as_of="2026-09-25"))
        self.assertFalse(work_order_overdue(order, as_of="2019-01-01"))
        done = {"status": "passed", "data": {"planned_date": "2020-01-01"}}
        self.assertFalse(work_order_overdue(done, as_of="2026-09-25"))
        unplanned = {"status": "requested", "data": {}}
        self.assertFalse(work_order_overdue(unplanned, as_of="2026-09-25"))


if __name__ == "__main__":
    unittest.main()
