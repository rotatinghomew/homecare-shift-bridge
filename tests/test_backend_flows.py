"""业务全流程测试：种子态势、家属待办、证据核对、改派、补授权、续办不复制。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import CareBackend  # noqa: E402


class SeedBoardTest(unittest.TestCase):
    def setUp(self):
        self.b = CareBackend()
        self.d = self.b.deriver("19:40")

    def test_responsibility_boundaries(self):
        rows = {s["unit_key"]: s for s in self.d.segment_rows()}
        # X3 双方确认且可行 -> P4 由 S2 负责
        self.assertEqual(rows["P4"]["responsible_station"], "S2")
        self.assertTrue(rows["P4"]["boundary"]["binding"])
        # X1 接站未确认、X2 原站未确认且人不合规 -> P1/g2 仍归 S1
        self.assertEqual(rows["P1"]["responsible_station"], "S1")
        self.assertEqual(rows["g2"]["responsible_station"], "S1")
        # X5 双方确认 -> P5 S3 承诺
        self.assertEqual(rows["P5"]["responsible_station"], "S3")

    def test_seed_triage(self):
        rows = {s["unit_key"]: s for s in self.d.segment_rows()}
        self.assertEqual(rows["P4"]["triage"], "pending_verification")
        self.assertEqual(rows["P1"]["triage"], "not_visited")
        self.assertEqual(rows["g2"]["triage"], "not_visited")
        # H8 没有给 S3 的 deliver 授权：卡点是补授权（同时保留无到访事实）
        self.assertEqual(rows["P5"]["duty_state"], "authorization_required")
        self.assertEqual(rows["P5"]["triage"], "not_visited")

    def test_d7_and_d8_todos(self):
        d7 = self.b.family_board("D7")["todos"]
        self.assertTrue(any(t["station"] == "S2" and t["type"] == "reissue_grant"
                            and set(t["categories"]) == {"schedule", "mobility"}
                            for t in d7))
        # 不应要求家属把 dressing 授权给 S2（换药仍由原站掌握）
        self.assertFalse(any("dressing" in t["categories"] and t["station"] == "S2"
                             for t in d7))
        d8 = self.b.family_board("D8")["todos"]
        self.assertTrue(any(t["station"] == "S3" and t["type"] == "issue_grant"
                            for t in d8))

    def test_family_identity_resolution(self):
        self.assertEqual(self.b.family_board("H7")["resident"]["id"], "H7")
        self.assertEqual(self.b.board_for("D8")["view"], "family")
        self.assertEqual(self.b.board_for("S1")["view"], "station")
        self.assertEqual(self.b.board_for("duty")["view"], "duty")

    def test_inquiry_is_minimal_and_authorized(self):
        ok = self.b.inquire_shifts("S2", "H7", at="10:00")
        self.assertTrue(ok["allowed"])
        self.assertTrue(ok["shifts"])
        for s in ok["shifts"]:
            self.assertNotIn("categories", s)
            self.assertTrue({"window_from", "window_to", "region", "skills"} <= set(s))
        denied = self.b.inquire_shifts("S3", "H8", at="10:00")
        self.assertFalse(denied["allowed"])

    def test_materials_enforced(self):
        self.assertFalse(self.b.fetch_materials(
            "S2", "H7", ["schedule", "mobility"], at="10:00")["allowed"])  # G2 11 点才生效
        self.assertTrue(self.b.fetch_materials(
            "S2", "H7", ["schedule", "mobility"], at="15:00")["allowed"])
        d = self.b.fetch_materials("S2", "H7", ["dressing"], at="15:00")
        self.assertFalse(d["allowed"])
        self.assertEqual(d["code"], "missing_categories")
        self.assertFalse(self.b.fetch_materials(
            "S2", "H7", ["schedule"], at="19:10")["allowed"])  # 已撤回


class AttributionTest(unittest.TestCase):
    def setUp(self):
        self.b = CareBackend()

    def test_k1_dual_report_single_performance_after_adjudication(self):
        duty = self.b.duty_board()
        k1 = next(q for q in duty["attribution_queue"] if q["proof_id"] == "K1")
        self.assertTrue(k1["conflict"])
        # 中立证明是 S2/A7；V1 一致且授权/范围/完成齐备，且为撤回后上传的迟到有效回执
        v1 = next(c for c in k1["claims"] if c["visit_id"] == "V1")
        self.assertTrue(v1["consistent_with_proof"])
        self.assertTrue(v1["valid_completion"])
        self.assertTrue(v1["late_valid"])
        v2 = next(c for c in k1["claims"] if c["visit_id"] == "V2")
        self.assertFalse(v2["consistent_with_proof"])
        self.assertIn("qualification_ended_at_start", v2["claim_issues"])

        self.b.adjudicate_proof("K1", outcome="confirmed", at="19:40")
        family = self.b.family_board("D7")
        k1_cards = [p for p in family["performances"] if p["proof_id"] == "K1"]
        self.assertEqual(len(k1_cards), 1)  # 住户只看到一次履行
        p4 = next(s for s in family["segments"] if s["unit_key"] == "P4")
        self.assertEqual(p4["triage"], "fulfilled")

    def test_v3_unauthorized_start_not_counted(self):
        duty = self.b.duty_board()
        k2 = next(q for q in duty["attribution_queue"] if q["proof_id"] == "K2")
        v3 = k2["claims"][0]
        self.assertEqual(v3["auth_code_at_start"], "grant_revoked")  # 19:06 > 19:05
        self.assertFalse(v3["valid_completion"])

    def test_late_adjudication_does_not_restore_materials_access(self):
        self.b.adjudicate_proof("K1", outcome="confirmed", at="19:40")
        # 撤回后的下一次取资料仍按 19:40 当时授权判断 -> 依旧拒绝
        m = self.b.fetch_materials("S2", "H7", ["schedule", "mobility"], at="19:41")
        self.assertFalse(m["allowed"])
        self.assertEqual(m["code"], "grant_revoked")
        # 但 K1 那次撤回前的履行仍然成立且只计一次
        p4 = next(s for s in self.b.family_board("D7")["segments"]
                  if s["unit_key"] == "P4")
        self.assertEqual(p4["triage"], "fulfilled")


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.b = CareBackend()

    def test_splittable_item_selectable_alone(self):
        r = self.b.select_items("S2", "H7", plan_ids=["P4"], at="15:00")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["selections"]), 1)
        self.assertEqual(r["selections"][0]["unit_key"], "P4")

    def test_bundled_group_must_be_selected_whole(self):
        r = self.b.select_items("S2", "H7", plan_ids=["P2"], at="15:00")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "bundle_must_select_whole_group")
        self.assertEqual(r["required_plan_ids"], ["P2", "P3"])
        whole = self.b.select_items("S1", "H7", plan_ids=["P2", "P3"], at="15:00")
        self.assertTrue(whole["ok"])
        self.assertEqual(whole["selections"][0]["skills"], ["dressing", "mobility"])


class ReassignmentTest(unittest.TestCase):
    def setUp(self):
        self.b = CareBackend()

    def test_x4_confirm_rejected_old_commitment_persists(self):
        result = self.b.confirm_handover("X4", side="to", at="19:41", actor="S3")
        self.assertFalse(result["ok"])
        self.assertIn("travel_insufficient", result["evaluation"]["errors"])
        self.assertIsNone(self.b.get("handover", "X4")["to_confirmed"])
        x3 = self.b.get("handover", "X3")
        self.assertFalse(x3.get("superseded_by"))  # 旧承诺不消失

    def test_x2_from_confirm_rejected_preserves_existing_to_time(self):
        self.assertEqual(self.b.get("handover", "X2")["to_confirmed"], "13:00")
        r = self.b.confirm_handover("X2", side="from", at="13:10", actor="S1")
        self.assertFalse(r["ok"])
        self.assertIn("missing_skill", r["evaluation"]["errors"])
        x2 = self.b.get("handover", "X2")
        self.assertIsNone(x2["from_confirmed"])          # 本次确认回滚
        self.assertEqual(x2["to_confirmed"], "13:00")    # 既有凭据保留

    def test_successful_reassign_supersedes_only_after_accept(self):
        # 责任站 S1 先对 P1 建立有效内部承诺
        first = self.b.propose_handover(
            resident="H7", unit_key="P1", station_from="S1", station_to="S1",
            staff="A8", start="09:10", end="09:40", at="08:20",
            actor="S1", confirm_from=True)["handover"]
        self.b.confirm_handover(first["id"], side="to", at="08:21", actor="S1")
        view = self.b.deriver("08:21").handover_view(self.b.get("handover", first["id"]))
        self.assertTrue(view["binding"])
        # 改派 S2/A7：新单未被接下前，旧单仍无 superseded_by
        moved = self.b.reassign(first["id"], station_to="S2", staff="A7",
                                start="10:30", end="11:00", at="08:30", actor="S1")
        new_id = moved["handover"]["id"]
        self.assertIsNone(self.b.get("handover", first["id"]).get("superseded_by"))
        # 接站合格并接下 -> 新边界生效，旧承诺被取代，P1 责任转到 S2
        ok = self.b.confirm_handover(new_id, side="to", at="08:35", actor="S2")
        self.assertTrue(ok["ok"], ok.get("evaluation"))
        self.assertEqual(self.b.get("handover", first["id"]).get("superseded_by"), new_id)
        p1 = next(s for s in self.b.duty_board("12:00")["segments"]
                  if s["unit_key"] == "P1")
        self.assertEqual(p1["responsible_station"], "S2")

    def test_double_booking_blocked_for_other_household(self):
        # A9 已承诺 X5（H8 17:40-19:00）；同时段把它许给另一户 H7 的 P4 -> 冲突
        result = self.b.propose_handover(
            resident="H7", plan="P4", station_to="S3", staff="A9",
            start="18:00", end="18:30", at="12:00", actor="S3")
        self.assertIn("double_booked", result["evaluation"]["errors"])


class ContinuityTest(unittest.TestCase):
    def setUp(self):
        self.b = CareBackend()

    def test_late_receipt_merges_into_original_segment(self):
        # P5 原本无证明；补来 proof+回执，应挂在原 P5/X5 上，不新建分段
        proof = self.b.register_proof(
            resident="H8", station="S3", staff="A9",
            start="17:45", end="18:50", at="20:05")["proof"]
        before = len(self.b.rows("visits"))
        self.b.upload_visit(plan="P5", station="S3", staff="A9",
                            proof=proof["id"], completed=True, received="20:05")
        self.assertEqual(len(self.b.rows("visits")), before + 1)
        seg = next(s for s in self.b.duty_board("20:10")["segments"]
                   if s["unit_key"] == "P5")
        self.assertTrue(any(p["proof_id"] == proof["id"] for p in seg["performances"]))
        # 有了现场证明 -> 先进入“履行待核”，不能当没去而重复补派
        self.assertEqual(seg["triage"], "pending_verification")
        s3 = next(o for o in seg["station_options"] if o["station"] == "S3")
        self.assertFalse(s3["auth_allowed"])  # H8 始终没给 S3 授权
        self.assertEqual(s3["authorization"]["code"], "no_grant")

    def test_new_plan_continues_on_same_backend(self):
        self.b.ingest({"plans": [{
            "id": "P6", "resident": "H7", "referral": "C9",
            "window_from": "20:10", "window_to": "20:40", "skill": "mobility",
            "group": "g6", "splittable": True,
            "categories": ["schedule", "mobility"]}]})
        duty = self.b.duty_board("19:46")
        p6 = next(s for s in duty["segments"] if s["unit_key"] == "P6")
        # S3/A9 资质与路程可行且 G4 有效 -> 可承接
        self.assertIn("S3", p6["coverable_stations"])
        # 家属补 S2 授权后，S2 授权转为有效（但 A7 资质不覆盖 20:10，仍不可排）
        self.b.issue_grant(resident="H7", station="S2",
                           categories=["schedule", "mobility"],
                           expires="22:00", at="19:46", actor="D7")
        p6 = next(s for s in self.b.duty_board("19:47")["segments"]
                  if s["unit_key"] == "P6")
        s2 = next(o for o in p6["station_options"] if o["station"] == "S2")
        self.assertTrue(s2["auth_allowed"])
        self.assertEqual(s2["eligible_staff"], [])

    def test_dispatch_lifecycle_and_obsolete_on_late_performance(self):
        r = self.b.open_dispatch(resident="H7", unit_key="P1", at="08:30")
        rid = r["dispatch_request"]["id"]
        seg = next(s for s in self.b.duty_board("08:40")["segments"]
                   if s["unit_key"] == "P1")
        self.assertEqual(seg["duty_state"], "searching")
        # 若此时补来一张可认定的履行并判定，寻找中的补派单应作废
        proof = self.b.register_proof(resident="H7", station="S1", staff="A8",
                                      start="09:12", end="09:40", at="10:20")["proof"]
        self.b.upload_visit(plan="P1", station="S1", staff="A8",
                            proof=proof["id"], completed=True, received="10:20")
        self.b.adjudicate_proof(proof["id"], outcome="confirmed", at="10:21")
        req = self.b.get("dispatch_requests", rid)
        self.assertEqual(req["status"], "obsolete_performance_found")

    def test_ingest_is_idempotent_upsert(self):
        self.b.ingest({"plans": [{"id": "P1", "window_to": "10:05"}]})
        p1 = self.b.get("plans", "P1")
        self.assertEqual(p1["window_to"], "10:05")
        self.assertEqual(len([p for p in self.b.rows("plans") if p["id"] == "P1"]), 1)


if __name__ == "__main__":
    unittest.main()
