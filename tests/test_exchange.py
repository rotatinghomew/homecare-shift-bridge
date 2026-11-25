"""检查本地交换资料的完整性和取件接口。"""
from hashlib import sha256
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from functools import partial
from pathlib import Path
from threading import Thread
import unittest
from urllib.request import urlopen

DATA = Path(__file__).resolve().parents[1] / "data"
FILE = DATA / "care_20261202.json"


def by_id(packet, group, key):
    return next(row for row in packet[group] if row["id"] == key)


class ExchangeTest(unittest.TestCase):
    def test_records(self):
        data = json.loads(FILE.read_text(encoding="utf-8"))
        for group, rows in data.items():
            with self.subTest(group=group):
                self.assertTrue(rows)
                self.assertEqual(len(rows), len({row["id"] for row in rows}))
                self.assertTrue(all(row.keys() == rows[0].keys() for row in rows))
        grant = by_id(data, "grants", "G2")
        first, duplicate, late = (by_id(data, "visits", visit) for visit in ("V1", "V2", "V3"))
        self.assertEqual(first["proof"], duplicate["proof"])
        self.assertNotEqual(first["station"], duplicate["station"])
        self.assertLess(first["start"], grant["revoked"])
        self.assertGreater(first["received"], grant["revoked"])
        self.assertGreater(late["start"], grant["revoked"])
        self.assertEqual(by_id(data, "handover", "X5")["staff"], by_id(data, "handover", "X4")["staff"])

    def test_source_over_http(self):
        handler = partial(SimpleHTTPRequestHandler, directory=str(DATA))
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                address = f"http://127.0.0.1:{server.server_port}/{FILE.name}"
                with urlopen(address, timeout=5) as reply:
                    self.assertEqual(reply.status, 200)
                    self.assertEqual(sha256(reply.read()).digest(), sha256(FILE.read_bytes()).digest())
            finally:
                server.shutdown()
                thread.join(timeout=5)
