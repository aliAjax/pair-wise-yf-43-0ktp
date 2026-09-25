import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _request(method, url, body=None, headers=None):
    data = None
    merged = {"Content-Type": "application/json"}
    if headers:
        merged.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=merged, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        service = DomainService(repo, RuleEngine())
        static_dir = str(Path(__file__).resolve().parent.parent / "static")
        self.server = create_server("127.0.0.1", 0, service, RuleEngine(), static_dir)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.admin = {"X-User-Id": "admin", "X-Role": "admin"}
        self.metrology = {"X-User-Id": "met-1", "X-Role": "metrology"}
        self.analyst = {"X-User-Id": "ana-1", "X-Role": "analyst"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _url(self, path):
        return "http://127.0.0.1:%s%s" % (self.port, path)

    def _create(self, kind, data, headers=None):
        status, payload = _request("POST", self._url("/api/" + kind), data, headers or self.admin)
        self.assertEqual(status, 201, payload)
        return payload

    def _action(self, entity_id, action, data, headers=None, idem=None, version=None):
        body = {"action": action, "data": data}
        if version is not None:
            body["expected_version"] = version
        req_headers = dict(headers or self.admin)
        if idem:
            req_headers["Idempotency-Key"] = idem
        return _request(
            "POST", self._url("/api/entities/%s/actions" % entity_id), body, req_headers
        )

    def test_metrology_flow_over_http(self):
        instrument = self._create("instrument", {"name": "Scale", "serial": "S-9"})

        # 计量员送检（幂等头重复提交）
        send_data = {"assignee": "lab-9", "planned_finish_at": "2026-09-20", "purpose": "年检"}
        status, first = self._action(instrument["id"], "send_calibration", send_data,
                                     self.metrology, idem="send-9")
        self.assertEqual(status, 200)
        self.assertEqual(first["status"], "calibrating")
        self.assertEqual(first["version"], 2)
        status, repeat = self._action(instrument["id"], "send_calibration", send_data,
                                      self.metrology, idem="send-9")
        self.assertEqual(status, 200)
        self.assertEqual(repeat["version"], 2)

        # 逾期筛选
        status, payload = _request(
            "GET", self._url("/api/calibrations?overdue=true&as_of=2026-09-25"),
            headers=self.admin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        ticket_id = payload["items"][0]["id"]

        # 结果未回填：放行被退回，错误体带工单编号
        method = self._create("method", {"name": "M", "version": "v1"})
        self._action(method["id"], "validate_method",
                     {"parameters": {"range": [0, 1]}, "instrument_ids": [instrument["id"]]})
        result = self._create("result", {"sample_id": "S", "measurement": "x"}, self.analyst)
        status, error = self._action(
            result["id"], "release",
            {"instrument_id": instrument["id"], "method_id": method["id"],
             "value": 1, "unit": "mg/L"},
            self.analyst,
        )
        self.assertEqual(status, 400)
        self.assertIn(ticket_id, error["error"])

        # 仪器已被本次送检推进到版本 2，仍持旧版本号重复送检：409 冲突退回
        status, conflict = self._action(
            instrument["id"], "send_calibration", send_data, self.metrology, version=1
        )
        self.assertEqual(status, 409)
        self.assertEqual(conflict["type"], "ConflictError")

        # 不带旧版本号回填成功，仪器恢复；之后放行成功
        status, ticket = self._action(
            ticket_id, "perform",
            {"result": "passed", "performed_at": "2026-09-24", "due_at": "2099-01-01"},
            self.metrology,
        )
        self.assertEqual(status, 200)
        status, released = self._action(
            result["id"], "release",
            {"instrument_id": instrument["id"], "method_id": method["id"],
             "value": 1, "unit": "mg/L"},
            self.analyst,
        )
        self.assertEqual(status, 200)
        self.assertEqual(released["status"], "released")


if __name__ == "__main__":
    unittest.main()
