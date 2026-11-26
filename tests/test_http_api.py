"""HTTP/JSON 接口与控制台可达性测试（临时端口，标准库 urllib）。"""
import json
import sys
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.server import create_server  # noqa: E402


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = create_server("127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        with urlopen(self.url(path), timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def post(self, path, payload):
        req = Request(self.url(path), data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except Exception as e:  # urllib 对 4xx/5xx 抛 HTTPError
            body = getattr(e, "read", lambda: b"{}")()
            return getattr(e, "code", 500), json.loads(body.decode("utf-8"))

    def test_health_and_console(self):
        status, data = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        with urlopen(self.url("/"), timeout=5) as r:
            html = r.read().decode("utf-8")
        self.assertIn("分段转介", html)

    def test_board_routing(self):
        _, d = self.get("/api/board?actor=D7&at=19:40")
        self.assertEqual(d["view"], "family")
        _, d = self.get("/api/duty/board?at=19:40")
        self.assertIn("attribution_queue", d)

    def test_inquire_and_materials_over_http(self):
        _, d = self.get("/api/station/S2/inquire?resident=H7&at=10:00")
        self.assertTrue(d["allowed"])
        status, d = self.get(
            "/api/station/S2/materials?resident=H7&categories=dressing&at=15:00")
        # 业务拒绝以 200 + allowed:false 返回（便于接入方处理），且确实不放行
        self.assertFalse(d["allowed"])
        self.assertEqual(d["code"], "missing_categories")

    def test_adjudication_and_rpc(self):
        status, d = self.post("/api/adjudications",
                             {"proof_id": "K1", "outcome": "confirmed", "at": "19:40"})
        self.assertEqual(status, 200)
        self.assertTrue(d["ok"])
        status, d = self.post("/api/rpc/family_board",
                               {"actor": "D7", "at": "19:40"})
        self.assertEqual(status, 200)
        self.assertEqual(len([p for p in d["performances"] if p["proof_id"] == "K1"]), 1)

    def test_ingest_new_plan_over_http(self):
        status, d = self.post("/api/ingest", {"plans": [{
            "id": "PX1", "resident": "H7", "referral": "CX",
            "window_from": "21:00", "window_to": "21:30", "skill": "mobility",
            "group": "gx1", "splittable": True,
            "categories": ["schedule", "mobility"]}]})
        self.assertTrue(d["ok"])
        _, board = self.get("/api/duty/board?at=20:00")
        self.assertTrue(any(s["unit_key"] == "PX1" for s in board["segments"]))


if __name__ == "__main__":
    unittest.main()
