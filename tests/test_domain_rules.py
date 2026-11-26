"""领域规则单测：授权时点、整组资质、右端排除、路程、同时段冲突。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import rules  # noqa: E402
from backend.timeutil import overlaps, parse_minutes  # noqa: E402


def grant(**kw):
    base = {"id": "G", "resident": "H", "station": "S", "purpose": "deliver",
            "categories": ["schedule", "mobility"], "issued": "08:00",
            "expires": "22:00", "revoked": None}
    base.update(kw)
    return base


class TimeTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_minutes("19:05"), 19 * 60 + 5)
        self.assertEqual(parse_minutes("2026-12-02T19:05:00+08:00"), 1145)

    def test_half_open_overlap(self):
        self.assertTrue(overlaps(0, 10, 5, 15))
        self.assertFalse(overlaps(0, 10, 10, 20))  # 首尾相接不算重叠


class GrantTest(unittest.TestCase):
    def test_active_revoked_expired_boundaries(self):
        g = grant(revoked="19:05", expires="22:00")
        self.assertEqual(rules.grant_timing(g, "19:04"), "active")
        self.assertEqual(rules.grant_timing(g, "19:05"), "revoked")  # 撤回点截断
        self.assertEqual(rules.grant_timing(g, "22:00"), "expired")  # 到期点不在内

    def test_authorize_purpose_and_categories(self):
        rows = [grant(id="GI", purpose="inquire", categories=["schedule"]),
                grant(id="GD", purpose="deliver",
                      categories=["schedule", "mobility"])]
        self.assertTrue(rules.authorize(rows, resident="H", station="S",
                                        at="10:00", action="inquire")["allowed"])
        ok = rules.authorize(rows, resident="H", station="S", at="15:00",
                             action="materials",
                             categories=["mobility"])
        self.assertTrue(ok["allowed"])
        no = rules.authorize(rows, resident="H", station="S", at="15:00",
                             action="materials", categories=["dressing"])
        self.assertFalse(no["allowed"])
        self.assertEqual(no["code"], "missing_categories")
        self.assertIn("dressing", no["missing_categories"])

    def test_inquire_grant_cannot_fetch_materials(self):
        rows = [grant(purpose="inquire", categories=["schedule"])]
        d = rules.authorize(rows, resident="H", station="S", at="10:00",
                            action="materials", categories=["schedule"])
        self.assertFalse(d["allowed"])
        self.assertEqual(d["code"], "wrong_purpose")

    def test_no_grant_and_revoked(self):
        d = rules.authorize([], resident="H", station="S", at="10:00",
                            action="inquire")
        self.assertEqual(d["code"], "no_grant")
        rows = [grant(revoked="19:05")]
        d = rules.authorize(rows, resident="H", station="S", at="19:06",
                            action="start", categories=["mobility"])
        self.assertEqual(d["code"], "grant_revoked")


class QualificationTest(unittest.TestCase):
    A8 = {"id": "A8", "skills": ["mobility", "dressing"],
          "qualification_from": "08:00", "qualification_to": "19:00"}

    def test_covers_to_endpoint_but_not_start_at_deadline(self):
        # 半开区间：18:15-19:00 的所有服务时刻都 < 19:00，覆盖
        self.assertEqual(
            rules.qualification_issues(self.A8, ["mobility", "dressing"],
                                       "18:15", "19:00"), [])
        # qualification_to 所在时点已失效：19:00 开工不行
        codes = [i["code"] for i in rules.qualification_issues(
            self.A8, ["mobility"], "19:00", "19:30")]
        self.assertIn("qualification_ended_at_start", codes)

    def test_whole_group_skill(self):
        codes = [i["code"] for i in rules.qualification_issues(
            {"id": "A7", "skills": ["mobility"],
             "qualification_from": "08:00", "qualification_to": "20:00"},
            ["mobility", "dressing"], "18:00", "18:45")]
        self.assertIn("missing_skill", codes)


class TravelAndBookingTest(unittest.TestCase):
    travel = [
        {"from_region": "east", "to_region": "north", "minutes": 25},
        {"from_region": "north", "to_region": "east", "minutes": 25},
        {"from_region": "north", "to_region": "north", "minutes": 10},
    ]

    def test_seed_x4_travel_gap(self):
        # X5 17:40-19:00 east(H8) -> X4 19:10 north(H7) 只有 10 分钟，需要 25
        result = rules.evaluate_assignment(
            staff={"id": "A9", "station": "S3", "skills": ["mobility"],
                   "qualification_from": "08:00", "qualification_to": "21:00"},
            skills=["mobility"], start="19:10", end="19:45", station="S3",
            region="north", resident="H7", travel_rows=self.travel,
            commitments=[{"id": "X5", "staff": "A9", "start": 1060,
                          "end": 1140, "resident": "H8", "region": "east"}])
        self.assertIn("travel_insufficient", result["errors"])

    def test_double_book_other_household(self):
        result = rules.evaluate_assignment(
            staff={"id": "A7", "station": "S2", "skills": ["mobility"],
                   "qualification_from": "08:00", "qualification_to": "20:00"},
            skills=["mobility"], start="19:15", end="19:40", station="S2",
            region="north", resident="H7", travel_rows=self.travel,
            commitments=[{"id": "X3", "staff": "A7", "start": 1140,
                          "end": 1185, "resident": "H9", "region": "north"}])
        self.assertIn("double_booked", result["errors"])

    def test_same_household_overlap_not_double_book(self):
        result = rules.evaluate_assignment(
            staff={"id": "A7", "station": "S2", "skills": ["mobility"],
                   "qualification_from": "08:00", "qualification_to": "20:00"},
            skills=["mobility"], start="19:00", end="19:30", station="S2",
            region="north", resident="H7", travel_rows=self.travel,
            commitments=[{"id": "X3", "staff": "A7", "start": 1140,
                          "end": 1185, "resident": "H7", "region": "north"}])
        self.assertNotIn("double_booked", result["errors"])
        self.assertIn("same_household_overlap", result["warnings"])

    def test_back_to_back_needs_same_region_buffer(self):
        # north 连续两户：09:40 结束，09:50 开工，gap=10 刚好
        ok = rules.evaluate_assignment(
            staff={"id": "A7", "station": "S2", "skills": ["mobility"],
                   "qualification_from": "08:00", "qualification_to": "20:00"},
            skills=["mobility"], start="09:50", end="10:20", station="S2",
            region="north", resident="H9", travel_rows=self.travel,
            commitments=[{"id": "X1", "staff": "A7", "start": 550,
                          "end": 580, "resident": "H_other", "region": "north"}])
        self.assertTrue(ok["ok"], ok["issues"])


class UnitsTest(unittest.TestCase):
    def test_bundle_g2_and_splittable_p4(self):
        from backend.store import DATA_FILE
        import json
        packet = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        units = rules.build_units(packet["plans"], resident="H7")
        by_key = {u["key"]: u for u in units}
        self.assertTrue(by_key["g2"]["bundled"])
        self.assertEqual(by_key["g2"]["plan_ids"], ["P2", "P3"])
        self.assertEqual(set(by_key["g2"]["skills"]), {"mobility", "dressing"})
        self.assertFalse(by_key["P4"]["bundled"])


if __name__ == "__main__":
    unittest.main()
