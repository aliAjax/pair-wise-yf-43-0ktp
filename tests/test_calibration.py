import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class CalibrationFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.metrologist = Actor("met-1", "metrology")
        self.admin = Actor("admin", "admin")
        self.analyst = Actor("ana-1", "analyst")

    def tearDown(self):
        self.tmp.cleanup()

    def _instrument(self, serial="X-1"):
        return self.service.create(
            self.admin, "instrument", {"name": "Scale", "serial": serial}
        )

    def _method(self, instrument_id):
        method = self.service.create(self.admin, "method", {"name": "M", "version": "v1"})
        return self.service.transition(
            self.admin,
            method["id"],
            "validate_method",
            {"parameters": {"range": [0, 1]}, "instrument_ids": [instrument_id]},
        )

    def _result(self):
        return self.service.create(
            self.analyst, "result", {"sample_id": "S-1", "measurement": "x"}
        )

    def _send(self, instrument, planned="2026-10-01", key=None):
        return self.service.transition(
            self.metrologist,
            instrument["id"],
            "send_calibration",
            {
                "assignee": "external-lab-7",
                "planned_finish_at": planned,
                "purpose": "年度送检",
            },
            idempotency_key=key,
        )

    def _ticket(self, instrument_id):
        tickets = self.repo.find_entities("calibration", "instrument_id", instrument_id)
        self.assertEqual(len(tickets), 1)
        return tickets[0]

    def test_send_registers_ticket_and_puts_instrument_on_hold(self):
        instrument = self._instrument()
        updated = self._send(instrument)
        self.assertEqual(updated["status"], "calibrating")

        ticket = self._ticket(instrument["id"])
        self.assertEqual(ticket["status"], "requested")
        self.assertEqual(ticket["data"]["assignee"], "external-lab-7")
        self.assertEqual(ticket["data"]["planned_finish_at"], "2026-10-01")
        self.assertEqual(ticket["data"]["purpose"], "年度送检")
        stored = self.service.get(instrument["id"])
        self.assertEqual(stored["data"]["calibration_id"], ticket["id"])

    def test_overdue_filter_returns_only_tickets_past_planned_date(self):
        early = self._send(self._instrument("S-1"), planned="2026-09-20")
        self._send(self._instrument("S-2"), planned="2099-01-01")

        overdue = self.service.list("calibration", overdue=True, as_of="2026-09-25")
        self.assertEqual([item["id"] for item in overdue], [self._ticket_of("S-1")])
        # 计划完成日当天不算逾期
        none = self.service.list("calibration", overdue=True, as_of="2026-09-20")
        self.assertEqual(none, [])
        # 已回填的工单即使计划日已过也不再逾期
        self.service.transition(
            self.metrologist,
            self._ticket_of("S-1"),
            "perform",
            {"result": "passed", "performed_at": "2026-09-19", "due_at": "2027-09-19"},
        )
        self.assertEqual(
            self.service.list("calibration", overdue=True, as_of="2026-09-25"), []
        )

    def _ticket_of(self, serial):
        instrument = self.repo.find_entities("instrument", "serial", serial)[0]
        return instrument["data"]["calibration_id"]

    def test_release_is_returned_with_work_order_number_until_backfilled(self):
        instrument = self._instrument()
        self._send(instrument)
        method = self._method(instrument["id"])
        result = self._result()

        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.analyst,
                result["id"],
                "release",
                {"instrument_id": instrument["id"], "method_id": method["id"],
                 "value": 1.0, "unit": "mg/L"},
            )
        ticket_id = self._ticket(instrument["id"])["id"]
        self.assertIn(ticket_id, str(ctx.exception))
        # 结果保持未放行
        self.assertEqual(self.service.get(result["id"])["status"], "pending")

        # 合格结果回填新到期日，仪器恢复后放行成功
        self.service.transition(
            self.metrologist,
            ticket_id,
            "perform",
            {"result": "passed", "performed_at": "2026-09-24", "due_at": "2099-01-01"},
        )
        released = self.service.transition(
            self.analyst,
            result["id"],
            "release",
            {"instrument_id": instrument["id"], "method_id": method["id"],
             "value": 1.0, "unit": "mg/L"},
        )
        self.assertEqual(released["status"], "released")
        self.assertEqual(self.service.get(instrument["id"])["status"], "active")

    def test_failed_result_keeps_instrument_out_of_service_with_disposition(self):
        instrument = self._instrument()
        self._send(instrument)
        ticket_id = self._ticket(instrument["id"])["id"]

        # 不合格必须登记处置说明
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.metrologist,
                ticket_id,
                "perform",
                {"result": "failed", "performed_at": "2026-09-24"},
            )

        ticket = self.service.transition(
            self.metrologist,
            ticket_id,
            "perform",
            {"result": "failed", "performed_at": "2026-09-24",
             "disposition": "示值超差，退回供应商维修"},
        )
        self.assertEqual(ticket["status"], "failed")
        stored = self.service.get(instrument["id"])
        self.assertEqual(stored["status"], "quarantined")
        self.assertEqual(stored["data"]["disposition"], "示值超差，退回供应商维修")

    def test_duplicate_submission_reuses_first_record(self):
        instrument = self._instrument()
        first = self._send(instrument, key="send-key-1")
        # 模拟仪器已被推进到下一阶段后，重复提交仍返回首次记录
        ticket_id = first["data"]["calibration_id"]
        self.service.transition(
            self.metrologist, ticket_id, "perform",
            {"result": "passed", "performed_at": "2026-09-24", "due_at": "2099-01-01"},
        )
        repeat = self._send(self.service.get(instrument["id"]), key="send-key-1")
        self.assertEqual(repeat["version"], first["version"])
        self.assertEqual(repeat["status"], "calibrating")
        # 没有重复建工单、没有重复审计
        self.assertEqual(len(self.repo.find_entities("calibration", "instrument_id", instrument["id"])), 1)
        actions = [row["action"] for row in self.service.audit_log(instrument["id"])]
        self.assertEqual(actions.count("send_calibration"), 1)

    def test_stale_version_conflict_preserves_state_and_audit(self):
        instrument = self._instrument()
        # 别人先完成一次送检
        self._send(instrument)
        advanced = self.service.get(instrument["id"])
        self.assertEqual(advanced["version"], 2)

        # 拿着旧版本号再提交：冲突，且当前状态不被覆盖、不新增审计
        audit_before = self.service.audit_log(instrument["id"])
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.metrologist,
                instrument["id"],
                "send_calibration",
                {"assignee": "late", "planned_finish_at": "2026-11-01", "purpose": "重复送检"},
                expected_version=instrument["version"],
            )
        stored = self.service.get(instrument["id"])
        self.assertEqual(stored["status"], "calibrating")
        # 登记信息只存在工单上，仪器上只保留工单引用
        self.assertNotIn("assignee", stored["data"])
        self.assertNotIn("planned_finish_at", stored["data"])
        self.assertEqual(
            len(self.service.audit_log(instrument["id"])), len(audit_before)
        )

    def test_backfill_conflict_when_instrument_changed_elsewhere(self):
        instrument = self._instrument()
        self._send(instrument)
        ticket_id = self._ticket(instrument["id"])["id"]

        # 别人在回填前更新了仪器数据（版本被推进，但仍待校准）
        current = self.service.get(instrument["id"])
        self.repo.update_entity(
            instrument["id"], current["version"], "calibrating",
            dict(current["data"], note="concurrent edit"),
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.metrologist,
                ticket_id,
                "perform",
                {"result": "passed", "performed_at": "2026-09-24", "due_at": "2099-01-01"},
            )
        # 工单也停留在原状态，未被半更新
        self.assertEqual(self.service.get(ticket_id)["status"], "requested")
        self.assertEqual(self.service.get(instrument["id"])["status"], "calibrating")
        self.assertEqual(self.service.get(instrument["id"])["data"].get("note"), "concurrent edit")


if __name__ == "__main__":
    unittest.main()
