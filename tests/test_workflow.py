import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_workflow(self):
        created = {}
        steps = [
            {'op': 'create', 'as': 'instrument', 'kind': 'instrument', 'data': {'name': 'Analyzer', 'serial': 'A-1'}},
            # 计量员送检：登记承担人、计划完成日、用途，仪器随即待校准并生成工单
            {'op': 'transition', 'target': 'instrument', 'action': 'send_calibration',
             'data': {'assignee': 'M-1', 'planned_finish_at': '2026-01-10', 'purpose': '年度送检', 'requested_at': '2026-01-01'},
             'expect': 'calibrating'},
            # 合格结果回填并填上新到期日，仪器恢复
            {'op': 'lookup_ticket', 'as': 'calibration'},
            {'op': 'transition', 'target': 'calibration', 'action': 'perform',
             'data': {'result': 'passed', 'performed_at': '2026-01-08', 'due_at': '2099-01-01'},
             'expect': 'passed'},
            {'op': 'transition', 'target': 'calibration', 'action': 'approve', 'data': {'authorized_by': 'QA-1'}, 'expect': 'approved'},
            {'op': 'create', 'as': 'method', 'kind': 'method', 'data': {'name': 'Assay-A', 'version': 'v1'}},
            {'op': 'transition', 'target': 'method', 'action': 'validate_method', 'data': {'parameters': {'range': [0, 10]}, 'instrument_ids': ['{instrument}']}, 'expect': 'validated'},
            {'op': 'create', 'as': 'result', 'kind': 'result', 'data': {'sample_id': 'S-1', 'measurement': 'initial'}},
            {'op': 'transition', 'target': 'result', 'action': 'release', 'data': {'instrument_id': '{instrument}', 'method_id': '{method}', 'value': 4.2, 'unit': 'mg/L'}, 'expect': 'released'},
        ]
        for step in steps:
            if step["op"] == "create":
                entity = self.service.create(
                    self.actor,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            elif step["op"] == "lookup_ticket":
                tickets = self.service.list("calibration")
                self.assertEqual(len(tickets), 1)
                created[step["as"]] = tickets[0]["id"]
            else:
                entity = self.service.transition(
                    self.actor,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])

        # 合格回填后仪器恢复可用，且带有新到期日
        instrument = self.service.get(created["instrument"])
        self.assertEqual(instrument["status"], "active")
        self.assertEqual(instrument["data"]["due_at"], "2099-01-01")
        self.assertNotIn("calibration_id", instrument["data"])


if __name__ == "__main__":
    unittest.main()
