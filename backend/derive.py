"""从可变状态派生交接边界、承诺日程、到访核验与分段处置。

所有“核对结论”都在读取时由原始记录 join 得出，因此后来汇入的新计划、
补传回执会直接落在原承诺/原分段上，不需要另抄已执行记录。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from . import rules
from .timeutil import parse_minutes

DUTY_ACTORS = {"duty", "duty-desk", "ops", "值班台", "DUTY"}

# 分流/状态词表（同时给出中文标签，供界面与核对使用）
FULFILLED = "fulfilled"
PENDING_VERIFICATION = "pending_verification"
NOT_VISITED = "not_visited"
SEARCHING = "searching"
UNASSIGNABLE = "unassignable"
OPEN = "open"

TRIAGE_LABELS = {
    FULFILLED: "已完成一次履行",
    PENDING_VERIFICATION: "现场已有履行待核",
    NOT_VISITED: "查无到户登记（按根本没去，迟到回执仍可翻案）",
    SEARCHING: "还在找人 / 等接站确认",
    UNASSIGNABLE: "没人能接（无资质/路程/时段可行人员）",
    OPEN: "待安排（有可派人選）",
}


class Deriver:
    def __init__(self, store, at: Optional[str] = "19:40"):
        self.store = store
        self.at_text = at or "19:40"
        self.at = parse_minutes(self.at_text)
        self._units_cache: Optional[List[dict]] = None

    # -- 基础索引 -------------------------------------------------------------
    @property
    def units(self) -> List[dict]:
        if self._units_cache is None:
            self._units_cache = rules.build_units(self.store.rows("plans"))
        return self._units_cache

    def units_for(self, resident: str) -> List[dict]:
        return [u for u in self.units if u["resident"] == resident]

    def unit_of_plan(self, plan_id: str) -> Optional[dict]:
        return rules.unit_for_plan(self.units, plan_id)

    def resident(self, resident_id: str) -> Optional[dict]:
        return self.store.get("residents", resident_id)

    def resident_region(self, resident_id: Optional[str]) -> Optional[str]:
        r = self.resident(resident_id) if resident_id else None
        return (r or {}).get("region")

    def adjudication_for_proof(self, proof_id: str) -> Optional[dict]:
        return next((j for j in self.store.rows("adjudications")
                     if j.get("proof") == proof_id or j.get("proof_id") == proof_id), None)

    # -- 交接 / 承诺 ----------------------------------------------------------
    def handovers_for_unit(self, unit: dict) -> List[dict]:
        ids = set(unit["plan_ids"])
        return [x for x in self.store.rows("handover") if x.get("plan") in ids
                or set(x.get("plan_ids") or []) & ids]

    def raw_mutual(self, x: dict) -> bool:
        return bool(x.get("from_confirmed") and x.get("to_confirmed"))

    def evaluate_handover(self, x: dict) -> Dict[str, Any]:
        unit = self.unit_of_plan(x.get("plan"))
        skills = (unit or {}).get("skills") or (
            [p.get("skill") for p in self.store.rows("plans") if p.get("id") == x.get("plan")]
        )
        resident = (unit or {}).get("resident") or self._plan_resident(x.get("plan"))
        region = self.resident_region(resident)
        staff_id = x.get("staff")
        station = x.get("station_to")
        return rules.evaluate_assignment(
            staff=self.store.get("staff", staff_id) if staff_id else None,
            skills=skills,
            start=x.get("service_start"), end=x.get("service_end"),
            station=station, region=region, resident=resident,
            travel_rows=self.store.rows("travel"),
            commitments=self.schedule_commitments(),
            exclude=[x.get("id")],
            window_from=(unit or {}).get("window_from"),
            window_to=(unit or {}).get("window_to"),
        )

    def _plan_resident(self, plan_id: Optional[str]) -> Optional[str]:
        p = self.store.get("plans", plan_id) if plan_id else None
        return (p or {}).get("resident")

    def schedule_commitments(self, staff_id: Optional[str] = None) -> List[dict]:
        """所有“双方确认且可行”的有效承诺，规范化为日程区间。"""
        result = []
        for x in self.store.rows("handover"):
            if x.get("status") == "rejected" or x.get("superseded_by"):
                continue
            if not self.raw_mutual(x):
                continue
            evaluation = self.evaluate_handover_light(x)
            if not evaluation["ok"]:
                continue
            unit = self.unit_of_plan(x.get("plan"))
            resident = (unit or {}).get("resident") or self._plan_resident(x.get("plan"))
            if staff_id and x.get("staff") != staff_id:
                continue
            result.append({
                "id": x["id"], "handover_id": x["id"], "staff": x.get("staff"),
                "start": parse_minutes(x.get("service_start")),
                "end": parse_minutes(x.get("service_end")),
                "resident": resident, "region": self.resident_region(resident),
                "plan_ids": (unit or {}).get("plan_ids", [x.get("plan")]),
            })
        return result

    def evaluate_handover_light(self, x: dict) -> Dict[str, Any]:
        """与 evaluate_handover 相同，但用“原始双方确认集”构造日程，避免递归。"""
        unit = self.unit_of_plan(x.get("plan"))
        skills = (unit or {}).get("skills") or []
        resident = (unit or {}).get("resident") or self._plan_resident(x.get("plan"))
        region = self.resident_region(resident)
        # 仅用原始 mutual 行（排除自身/废弃）作为承诺
        commitments = []
        for y in self.store.rows("handover"):
            if y.get("id") == x.get("id"):
                continue
            if y.get("status") == "rejected" or y.get("superseded_by"):
                continue
            if not self.raw_mutual(y):
                continue
            u2 = self.unit_of_plan(y.get("plan"))
            r2 = (u2 or {}).get("resident") or self._plan_resident(y.get("plan"))
            commitments.append({
                "id": y["id"], "staff": y.get("staff"),
                "start": parse_minutes(y.get("service_start")),
                "end": parse_minutes(y.get("service_end")),
                "resident": r2, "region": self.resident_region(r2),
            })
        return rules.evaluate_assignment(
            staff=self.store.get("staff", x.get("staff")) if x.get("staff") else None,
            skills=skills,
            start=x.get("service_start"), end=x.get("service_end"),
            station=x.get("station_to"), region=region, resident=resident,
            travel_rows=self.store.rows("travel"),
            commitments=commitments, exclude=[x.get("id")],
            window_from=(unit or {}).get("window_from"),
            window_to=(unit or {}).get("window_to"),
        )

    def handover_view(self, x: dict) -> Dict[str, Any]:
        evaluation = self.evaluate_handover_light(x)
        mutual = self.raw_mutual(x)
        binding = bool(mutual and evaluation["ok"]
                       and x.get("status") not in ("rejected",)
                       and not x.get("superseded_by"))
        unit = self.unit_of_plan(x.get("plan"))
        return {
            **{k: v for k, v in x.items()},
            "mutual_confirmed": mutual,
            "binding": binding,
            "unit_key": (unit or {}).get("key"),
            "evaluation": evaluation,
        }

    def active_boundary(self, unit: dict) -> Optional[dict]:
        """当前有效交接边界：双方确认、检查通过、未被后续改派取代。"""
        views = [self.handover_view(x) for x in self.handovers_for_unit(unit)]
        active = [v for v in views if v["binding"]]
        if not active:
            return None
        # 若有多条，取确认/提议时间最晚的一条（改派成功后旧条已 superseded）
        return sorted(active, key=lambda v: str(v.get("to_confirmed")
                                                or v.get("from_confirmed")
                                                or v.get("proposed") or ""))[-1]

    def responsible_station(self, unit: dict) -> str:
        boundary = self.active_boundary(unit)
        if boundary:
            return boundary.get("station_to")
        resident = self.resident(unit["resident"])
        return (resident or {}).get("initial_station")

    # -- 授权 -----------------------------------------------------------------
    def auth_for_unit(self, unit: dict, station: str, at: Optional[str] = None) -> dict:
        return rules.authorize(
            self.store.rows("grants"),
            resident=unit["resident"], station=station,
            at=at or self.at_text, action="deliver",
            categories=unit["categories"],
        )

    # -- 候选人员 -------------------------------------------------------------
    def candidates_for_unit(self, unit: dict, station: str,
                            start=None, end=None) -> List[dict]:
        s = start if start is not None else unit.get("window_from")
        e = end if end is not None else unit.get("window_to")
        all_commitments = self.schedule_commitments()
        unit_plan_ids = set(unit["plan_ids"])
        # 该段自己已有的承诺不算“另一户冲突”，排除后再评估人选
        exclude = {c["id"] for c in all_commitments
                   if set(c.get("plan_ids") or []) & unit_plan_ids}
        result = []
        for staff in self.store.rows("staff"):
            if staff.get("station") != station:
                continue
            evaluation = rules.evaluate_assignment(
                staff=staff, skills=unit["skills"], start=s, end=e,
                station=station, region=self.resident_region(unit["resident"]),
                resident=unit["resident"], travel_rows=self.store.rows("travel"),
                commitments=[c for c in all_commitments
                             if c.get("staff") == staff.get("id")],
                exclude=exclude,
            )
            result.append({
                "staff_id": staff.get("id"),
                "skills": staff.get("skills"),
                "region": staff.get("region"),
                "eligible": evaluation["ok"],
                "evaluation": evaluation,
            })
        return result

    # -- 到访核验 -------------------------------------------------------------
    def _scope_check(self, unit: Optional[dict], station: Optional[str],
                     staff: Optional[str], actual_start: Optional[int]):
        if not unit or not station or not staff:
            return False, None
        for x in self.handovers_for_unit(unit):
            if x.get("station_to") != station or x.get("staff") != staff:
                continue
            if not self.raw_mutual(x):
                continue
            xs, xe = parse_minutes(x.get("service_start")), parse_minutes(x.get("service_end"))
            w0, w1 = unit.get("window_from"), unit.get("window_to")
            inside_appointment = xs is not None and xe is not None and \
                actual_start is not None and xs <= actual_start < xe
            inside_window = w0 is not None and w1 is not None and \
                actual_start is not None and w0 <= actual_start < w1
            if inside_appointment or inside_window:
                return True, x.get("id")
        return False, None

    def verify_visit(self, visit: dict) -> Dict[str, Any]:
        plan = self.store.get("plans", visit.get("plan"))
        unit = self.unit_of_plan(visit.get("plan"))
        resident = (plan or {}).get("resident") or visit.get("resident")
        proof = self.store.get("proofs", visit.get("proof")) if visit.get("proof") else None
        match = rules.proof_matches_visit(proof, visit) if proof else {
            "matched": False, "problems": ["proof_missing"], "actual_start": parse_minutes(visit.get("start"))}
        actual_start = match.get("actual_start")
        actual_at = _to_text(actual_start) or visit.get("start")

        attributed = rules.attribution_of(proof) if proof else {
            "station": visit.get("station"), "staff": visit.get("staff")}
        claim_consistent = bool(
            proof and visit.get("station") == proof.get("station")
            and visit.get("staff") == proof.get("staff")
        )
        categories = (unit or {}).get("categories") or (plan or {}).get("categories") or []
        auth_at_start = rules.authorize(
            self.store.rows("grants"), resident=resident,
            station=attributed.get("station"), at=actual_at,
            action="start", categories=categories,
        ) if attributed.get("station") else {"allowed": False, "code": "no_station"}

        # 交接范围：存在双方确认、归属站/人一致、实际开始落在预约或计划窗内
        in_scope, scope_id = self._scope_check(unit, attributed.get("station"),
                                               attributed.get("staff"), actual_start)
        claim_in_scope, claim_scope_id = self._scope_check(
            unit, visit.get("station"), visit.get("staff"), actual_start)

        staff_row = self.store.get("staff", attributed.get("staff")) if attributed.get("staff") else None
        qual_issues = rules.qualification_issues(
            staff_row, (unit or {}).get("skills") or [],
            proof.get("recorded_start") if proof else visit.get("start"),
            proof.get("recorded_end") if proof else visit.get("end"),
        ) if staff_row else [{"code": "staff_not_found", "severity": "error"}]

        # 申报方（可能与中立证明不一致，例如 V2 申报 S1/A8）自身的条件
        claim_staff_row = self.store.get("staff", visit.get("staff")) if visit.get("staff") else None
        claim_qual_issues = rules.qualification_issues(
            claim_staff_row, (unit or {}).get("skills") or [],
            proof.get("recorded_start") if proof else visit.get("start"),
            proof.get("recorded_end") if proof else visit.get("end"),
        ) if claim_staff_row else [{"code": "staff_not_found", "severity": "error"}]
        claim_auth_at_start = rules.authorize(
            self.store.rows("grants"), resident=resident,
            station=visit.get("station"), at=actual_at,
            action="start", categories=categories,
        ) if visit.get("station") else {"allowed": False, "code": "no_station"}

        received_t = parse_minutes(visit.get("received"))
        access_lost_after = not rules.authorize(
            self.store.rows("grants"), resident=resident,
            station=attributed.get("station"),
            at=visit.get("received") or self.at_text,
            action="start", categories=categories,
        ).get("allowed", False)
        late_valid = bool(auth_at_start.get("allowed") and access_lost_after
                          and received_t is not None and actual_start is not None
                          and received_t > actual_start)

        valid_completion = bool(
            proof and match["matched"] and claim_consistent
            and auth_at_start.get("allowed") and in_scope
            and not [i for i in qual_issues if i.get("severity") == "error"]
            and visit.get("completed") is True
        )
        return {
            "visit_id": visit.get("id"),
            "plan": visit.get("plan"),
            "unit_key": (unit or {}).get("key"),
            "resident": resident,
            "claimed_station": visit.get("station"),
            "claimed_staff": visit.get("staff"),
            "proof_id": visit.get("proof"),
            "proof": proof,
            "match": match,
            "actual_start": _to_text(actual_start),
            "attributed_station": attributed.get("station"),
            "attributed_staff": attributed.get("staff"),
            "claim_consistent_with_proof": claim_consistent,
            "claim_auth_at_actual_start": claim_auth_at_start,
            "claim_in_handover_scope": claim_in_scope,
            "claim_scope_handover": claim_scope_id,
            "claim_qualification_issues": claim_qual_issues,
            "auth_at_actual_start": auth_at_start,
            "in_handover_scope": in_scope,
            "scope_handover": scope_id,
            "qualification_issues": qual_issues,
            "reported_after_access_lost": access_lost_after,
            "late_report_but_performed_authorized": late_valid,
            "completed": visit.get("completed") is True,
            "received": visit.get("received"),
            "valid_completion": valid_completion,
        }

    def performance_groups(self, resident: Optional[str] = None) -> List[Dict[str, Any]]:
        """按独立到户证明 proof 归并回执：同一次到户登记永远只有一组/一次履行。"""
        verifications = [self.verify_visit(v) for v in self.store.rows("visits")]
        groups: Dict[str, Dict[str, Any]] = {}
        for ver in verifications:
            pid = ver["proof_id"] or f"no-proof:{ver['visit_id']}"
            g = groups.setdefault(pid, {
                "proof_id": ver["proof_id"],
                "proof": ver["proof"],
                "resident": ver["resident"],
                "plan_ids": set(),
                "verifications": [],
            })
            g["verifications"].append(ver)
            g["plan_ids"].add(ver["plan"])
            if not g.get("resident"):
                g["resident"] = ver["resident"]

        # 也纳入暂无回执的独立证明
        for proof in self.store.rows("proofs"):
            groups.setdefault(proof["id"], {
                "proof_id": proof["id"], "proof": proof,
                "resident": proof.get("resident"),
                "plan_ids": set(), "verifications": [],
            })

        result: List[Dict[str, Any]] = []
        for pid, g in groups.items():
            if resident and g.get("resident") != resident:
                continue
            vers = g["verifications"]
            proof = g.get("proof") or {}
            stations = {v["claimed_station"] for v in vers}
            staff_set = {v["claimed_staff"] for v in vers}
            conflict = len({(v["claimed_station"], v["claimed_staff"]) for v in vers}) > 1
            adjudication = self.adjudication_for_proof(pid) if not str(pid).startswith("no-proof") else None

            coherent_valid = next((v for v in vers if v["valid_completion"]), None)
            coherent_claim = next((v for v in vers if v["claim_consistent_with_proof"]), None)
            recommendation = None
            if proof:
                rec_v = coherent_valid or coherent_claim
                recommendation = {
                    "station": proof.get("station"), "staff": proof.get("staff"),
                    "visit_id": (rec_v or {}).get("visit_id"),
                    "basis": "到户登记端中立记录" + ("，且授权/交接/完成标记齐备" if coherent_valid else ""),
                }

            if adjudication:
                state = "confirmed" if adjudication.get("outcome", "confirmed") == "confirmed" \
                    else "rejected"
                counts = bool(adjudication.get("counts", state == "confirmed"))
                attributed_station = adjudication.get("station") or proof.get("station")
                attributed_staff = adjudication.get("staff") or proof.get("staff")
            elif conflict:
                state, counts = "under_review", False
                attributed_station, attributed_staff = proof.get("station"), proof.get("staff")
            elif coherent_valid:
                state, counts = "auto_verified", True
                attributed_station, attributed_staff = coherent_valid["attributed_station"], \
                    coherent_valid["attributed_staff"]
            else:
                state, counts = "needs_review", False
                attributed_station, attributed_staff = (
                    (coherent_claim or vers[0])["attributed_station"] if vers else proof.get("station"),
                    (coherent_claim or vers[0])["attributed_staff"] if vers else proof.get("staff"),
                )

            apparent_completion = any(v["completed"] for v in vers) and proof is not None
            result.append({
                "proof_id": pid if not str(pid).startswith("no-proof:") else None,
                "group_key": pid,
                "proof": proof or None,
                "resident": g.get("resident"),
                "plan_ids": sorted(p for p in g["plan_ids"] if p),
                "unit_keys": sorted({v["unit_key"] for v in vers if v.get("unit_key")}),
                "recorded_start": proof.get("recorded_start"),
                "recorded_end": proof.get("recorded_end"),
                "attributed_station": attributed_station,
                "attributed_staff": attributed_staff,
                "claim_stations": sorted(s for s in stations if s),
                "claim_staff": sorted(s for s in staff_set if s),
                "conflict": conflict,
                "visit_ids": [v["visit_id"] for v in vers],
                "verifications": vers,
                "recommendation": recommendation,
                "adjudication": adjudication,
                "state": state,
                "counts_as_performance": counts,
                "apparent_completion": apparent_completion,
                "resident_visible_state": (
                    "completed" if counts else
                    "under_review" if state in ("under_review", "needs_review")
                    else "recorded"
                ),
            })
        return result

    # -- 分段处置 -------------------------------------------------------------
    def open_dispatch_for_unit(self, unit: dict) -> List[dict]:
        ids = set(unit["plan_ids"])
        return [r for r in self.store.rows("dispatch_requests")
                if r.get("status") == "searching"
                and (set(r.get("plan_ids") or []) & ids or r.get("unit_key") == unit["key"])]

    def no_show_for_unit(self, unit: dict) -> Optional[dict]:
        ids = set(unit["plan_ids"])
        return next((n for n in self.store.rows("no_shows")
                     if n.get("unit_key") == unit["key"]
                     or set(n.get("plan_ids") or []) & ids), None)

    def station_options_for_unit(self, unit: dict) -> List[Dict[str, Any]]:
        """各站点对该段的“授权 + 可派人员”选项，供值班台判断卡点。"""
        station_ids = sorted(
            {s.get("station") for s in self.store.rows("staff") if s.get("station")}
            | {g.get("station") for g in self.store.rows("grants") if g.get("station")}
            | {r.get("initial_station") for r in self.store.rows("residents")
               if r.get("initial_station")}
        )
        options = []
        for sid in station_ids:
            auth = self.auth_for_unit(unit, sid)
            cands = self.candidates_for_unit(unit, sid)
            eligible = [c["staff_id"] for c in cands if c["eligible"]]
            options.append({
                "station": sid,
                "authorization": auth,
                "auth_allowed": auth.get("allowed", False),
                "candidates": cands,
                "eligible_staff": eligible,
                "schedulable": bool(eligible),
                "can_take": bool(auth.get("allowed") and eligible),
            })
        return options

    def segment_row(self, unit: dict) -> Dict[str, Any]:
        boundary = self.active_boundary(unit)
        responsible = self.responsible_station(unit)
        auth = self.auth_for_unit(unit, responsible)
        handover_views = [self.handover_view(x) for x in self.handovers_for_unit(unit)]
        candidates = self.candidates_for_unit(unit, responsible)
        station_options = self.station_options_for_unit(unit)

        groups = [g for g in self.performance_groups(unit["resident"])
                  if set(g["plan_ids"]) & set(unit["plan_ids"])]
        settled = [g for g in groups if g["counts_as_performance"]]
        pending = [g for g in groups if not g["counts_as_performance"]
                   and g["state"] in ("under_review", "needs_review")]
        has_any_evidence = bool(groups)

        open_requests = self.open_dispatch_for_unit(unit)
        no_show = self.no_show_for_unit(unit)
        window_ended = unit.get("window_to") is not None and self.at >= unit["window_to"]

        resp_option = next((o for o in station_options
                            if o["station"] == responsible), None)
        resp_feasible = [c for c in candidates if c["eligible"]]
        coverable = [o["station"] for o in station_options if o["can_take"]]
        # 排班可行、但被住户授权卡住的站（典型：S2 授权被撤回，A7 仍可排晚班）
        auth_gated = [{
            "station": o["station"],
            "code": o["authorization"].get("code"),
            "grant_id": o["authorization"].get("grant_id"),
            "eligible_staff": o["eligible_staff"],
            "missing_categories": o["authorization"].get("missing_categories", []),
            "message": o["authorization"].get("message"),
        } for o in station_options
            if o["schedulable"] and not o["auth_allowed"]]

        # 分流 triage（回答“补派前属于哪种”）
        if settled:
            triage = FULFILLED
        elif has_any_evidence:
            triage = PENDING_VERIFICATION
        elif no_show:
            triage = NOT_VISITED
        elif window_ended:
            triage = NOT_VISITED
        elif open_requests or any(not h.get("to_confirmed") for h in handover_views
                                  if h.get("station_to")):
            triage = SEARCHING
        elif resp_feasible or coverable:
            triage = OPEN
        else:
            triage = UNASSIGNABLE

        auth_blocked = not auth.get("allowed")
        # 责任站排不出人、也没有可直接承接的站，但存在“有人能排却缺授权”的站：
        # 真正的卡点是授权，而不是没人。
        auth_gating_alternative = (
            auth_gated and not resp_feasible and not coverable
        )
        authorization_action = None
        if auth_blocked and triage != FULFILLED and triage != PENDING_VERIFICATION:
            authorization_action = {
                "station": responsible,
                "code": auth.get("code"),
                "grant_id": auth.get("grant_id"),
                "categories": unit["categories"],
                "missing_categories": auth.get("missing_categories", []),
                "message": auth.get("message"),
            }
        elif auth_gating_alternative:
            g0 = auth_gated[0]
            authorization_action = {
                "station": g0["station"], "code": g0["code"],
                "grant_id": g0["grant_id"],
                "categories": g0["missing_categories"] or unit["categories"],
                "missing_categories": g0["missing_categories"],
                "eligible_staff": g0["eligible_staff"], "message": g0["message"],
            }

        if triage == FULFILLED:
            blocker = "none"
        elif triage == PENDING_VERIFICATION:
            blocker = "verification"
        elif authorization_action is not None:
            blocker = "authorization"
        elif triage == UNASSIGNABLE:
            blocker = "staffing"
        elif triage == SEARCHING:
            blocker = "search"
        else:
            blocker = "none"

        feasible = resp_feasible
        duty_state = _duty_state(triage, blocker, authorization_action is not None,
                                 bool(boundary))
        return {
            "unit_key": unit["key"],
            "plan_ids": unit["plan_ids"],
            "resident": unit["resident"],
            "group": unit["group"],
            "bundled": unit["bundled"],
            "window_from": unit.get("window_from_text") or _to_text(unit.get("window_from")),
            "window_to": unit.get("window_to_text") or _to_text(unit.get("window_to")),
            "skills": unit["skills"],
            "categories": unit["categories"],
            "region": self.resident_region(unit["resident"]),
            "referrals": unit["referrals"],
            "responsible_station": responsible,
            "boundary": _brief_handover(boundary),
            "handovers": [_brief_handover(h) for h in handover_views],
            "commitment": _brief_handover(boundary),
            "authorization": auth,
            "authorization_action": authorization_action,
            "candidates": candidates,
            "feasible_candidate_ids": [c["staff_id"] for c in feasible],
            "station_options": station_options,
            "coverable_stations": coverable,
            "auth_gated_stations": auth_gated,
            "performances": [_brief_perf(g) for g in groups],
            "settled_performances": len(settled),
            "pending_performances": len(pending),
            "open_dispatch_requests": open_requests,
            "no_show": no_show,
            "window_ended": window_ended,
            "triage": triage,
            "triage_label": TRIAGE_LABELS[triage],
            "blocker": blocker,
            "duty_state": duty_state,
            "duty_state_label": DUTY_STATE_LABELS.get(duty_state),
        }

    def segment_rows(self, resident: Optional[str] = None) -> List[Dict[str, Any]]:
        units = self.units if resident is None else self.units_for(resident)
        return [self.segment_row(u) for u in units]


DUTY_STATE_LABELS = {
    FULFILLED: "已履行（同一次到户只计一次）",
    PENDING_VERIFICATION: "现场已有履行待核：先对照双方证据，不要补派",
    "authorization_required": "请住户先补授权，再安排/取资料",
    UNASSIGNABLE: "没人能接：无满足全组资质/路程/同时段条件的人",
    SEARCHING: "还在找人：等待新的合格人员接下，旧承诺仍有效",
    NOT_VISITED: "根本没去 / 查无到户登记：可补派，迟到回执会并回原段",
    OPEN: "有待安排时段和项目，可派合格人员",
}


def _duty_state(triage: str, blocker: str, auth_blocked: bool, committed: bool) -> str:
    if triage == FULFILLED:
        return FULFILLED
    if triage == PENDING_VERIFICATION:
        return PENDING_VERIFICATION
    if auth_blocked:
        return "authorization_required"
    if triage == UNASSIGNABLE:
        return UNASSIGNABLE
    if triage == SEARCHING:
        return SEARCHING
    if triage == NOT_VISITED:
        return NOT_VISITED
    return "scheduled" if committed else OPEN


def _brief_handover(h: Optional[dict]) -> Optional[dict]:
    if not h:
        return None
    return {
        "id": h.get("id"),
        "plan": h.get("plan"),
        "plan_ids": h.get("plan_ids"),
        "station_from": h.get("station_from"),
        "station_to": h.get("station_to"),
        "staff": h.get("staff"),
        "proposed": h.get("proposed"),
        "from_confirmed": h.get("from_confirmed"),
        "to_confirmed": h.get("to_confirmed"),
        "service_start": h.get("service_start"),
        "service_end": h.get("service_end"),
        "mutual_confirmed": h.get("mutual_confirmed"),
        "binding": h.get("binding"),
        "status": h.get("status"),
        "superseded_by": h.get("superseded_by"),
        "evaluation_ok": (h.get("evaluation") or {}).get("ok"),
        "evaluation_errors": (h.get("evaluation") or {}).get("errors"),
        "issues": (h.get("evaluation") or {}).get("issues"),
    }


def _brief_perf(g: dict) -> dict:
    return {
        "proof_id": g.get("proof_id"),
        "recorded_start": g.get("recorded_start"),
        "recorded_end": g.get("recorded_end"),
        "attributed_station": g.get("attributed_station"),
        "attributed_staff": g.get("attributed_staff"),
        "state": g.get("state"),
        "counts_as_performance": g.get("counts_as_performance"),
        "conflict": g.get("conflict"),
        "visit_ids": g.get("visit_ids"),
        "resident_visible_state": g.get("resident_visible_state"),
    }


def _to_text(m) -> Optional[str]:
    if m is None or isinstance(m, str):
        return m
    return f"{m // 60:02d}:{m % 60:02d}"
