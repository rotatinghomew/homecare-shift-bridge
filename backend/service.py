"""CareBackend：供住户/家属、站点、值班台调用的后端门面。

所有办理方法返回 JSON 可序列化的 dict；业务规则不通过异常表达，而是
``{"ok": False, "code": ..., ...}``，便于 HTTP/RPC 与脚本统一处理。
资源不存在等编程错误仍会抛出 :mod:`backend.errors` 中的异常。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from . import boards, rules
from .derive import DUTY_ACTORS, Deriver
from .errors import NotFoundError
from .store import DATA_FILE, Store
from .timeutil import format_minutes, parse_minutes

DEFAULT_CLOCK = "19:40"


class CareBackend:
    def __init__(self, source: Any = None, *, clock: str = DEFAULT_CLOCK,
                 state_path: Optional[Union[str, Path]] = None,
                 autosave: bool = False):
        if source is None and state_path is None:
            source = DATA_FILE
        self.store = Store(source, state_path=state_path)
        self.clock = clock
        self.autosave = autosave or state_path is not None

    # ------------------------------------------------------------------ 基础
    def deriver(self, at: Optional[str] = None) -> Deriver:
        return Deriver(self.store, at or self.clock)

    def now(self, at: Optional[str] = None) -> str:
        return at or self.clock

    def save(self, path=None):
        return self.store.save(path)

    def rows(self, group: str) -> List[dict]:
        return self.store.rows(group)

    def get(self, group: str, row_id: str) -> dict:
        return self.store.require(group, row_id)

    def ingest(self, packet: Optional[dict] = None, *, at: Optional[str] = None,
               actor: str = "window", **collections) -> Dict[str, Any]:
        """汇入后来转介的新计划、授权、补传回执等（按 id upsert，不复制）。"""
        payload = dict(packet or {})
        payload.update({k: v for k, v in collections.items() if v is not None})
        summary = self.store.ingest(payload)
        self._event("ingest", at=at, actor=actor, summary=summary)
        return {"ok": True, **summary}

    #: 语义化别名
    receive_referral = ingest

    def _event(self, action: str, *, at=None, actor="system", target=None, **detail):
        row = self.store.event(action, at=self.now(at), actor=actor,
                              target=target, **detail)
        if self.autosave and self.store.state_path:
            self.store.save()
        return row

    # ------------------------------------------------------------------ 角色
    def resolve_resident_actor(self, actor: str) -> tuple[str, dict]:
        """H7=住户本人；D7=residents.delegate 中的授权家属。"""
        resident = self.store.get("residents", actor) if actor else None
        if resident:
            return resident["id"], resident
        found = next((r for r in self.store.rows("residents")
                      if r.get("delegate") == actor), None)
        if found:
            return found["id"], found
        raise NotFoundError(f"无法识别的住户/家属身份：{actor}", actor=actor)

    def board_for(self, actor: str, at: Optional[str] = None) -> Dict[str, Any]:
        if actor in DUTY_ACTORS:
            return self.duty_board(at)
        # 住户本人或授权家属
        if self.store.get("residents", actor) or \
                any(r.get("delegate") == actor for r in self.store.rows("residents")):
            return self.family_board(actor, at)
        # 站点（staff.station 中出现过的 S 编号）
        if any(s.get("station") == actor for s in self.store.rows("staff")):
            return self.station_board(actor, at)
        if actor and actor.startswith("S"):
            return self.station_board(actor, at)
        return self.family_board(actor, at)

    def family_board(self, actor: str, at: Optional[str] = None) -> Dict[str, Any]:
        return boards.family_board(self, actor, at)

    #: 别名
    resident_board = family_board
    delegate_board = family_board

    def station_board(self, station: str, at: Optional[str] = None) -> Dict[str, Any]:
        return boards.station_board(self, station, at)

    def duty_board(self, at: Optional[str] = None) -> Dict[str, Any]:
        return boards.duty_board(self, at)

    def overview(self, at: Optional[str] = None) -> Dict[str, Any]:
        d = self.deriver(at)
        return {
            "as_of": d.at_text,
            "residents": [self.family_board(r["id"], at) for r in self.store.rows("residents")],
            "duty": self.duty_board(at),
            "stations": sorted({s.get("station") for s in self.store.rows("staff")}),
        }

    # ------------------------------------------------------------------ 授权
    def authorize(self, *, resident: str, station: str, at: Optional[str] = None,
                  action: str = "deliver", categories=None, purpose=None) -> dict:
        return rules.authorize(
            self.store.rows("grants"), resident=resident, station=station,
            at=self.now(at), action=action, purpose=purpose, categories=categories)

    def issue_grant(self, *, resident: str, station: str,
                    categories: Sequence[str], purpose: str = "deliver",
                    issued: Optional[str] = None, expires: str = "23:59",
                    revoked: Optional[str] = None, grant_id: Optional[str] = None,
                    actor: str = "resident", at: Optional[str] = None) -> dict:
        action_time = self.now(at)
        row = {
            "id": grant_id or self.store.new_id("G", "grants"),
            "resident": resident, "station": station, "purpose": purpose,
            "categories": list(categories),
            "issued": issued or action_time, "expires": expires,
            "revoked": revoked,
        }
        saved, inserted = self.store.upsert("grants", row)
        self._event("grant_issued" if inserted else "grant_updated",
                    at=action_time, actor=actor, target=saved["id"],
                    resident=resident, station=station, categories=list(categories))
        return {"ok": True, "grant": saved}

    def revoke_grant(self, grant_id: str, at: Optional[str] = None,
                     actor: str = "resident") -> dict:
        g = self.store.require("grants", grant_id)
        g["revoked"] = self.now(at)
        self._event("grant_revoked", at=g["revoked"], actor=actor, target=grant_id)
        if self.autosave:
            self.store.save()
        return {"ok": True, "grant": g}

    # ------------------------------------------------------------- 询班/取资料
    def inquire_shifts(self, station: str, resident: str,
                       at: Optional[str] = None) -> Dict[str, Any]:
        """初次询班：授权校验通过后，只返回时段、片区、技能（加必要 id）。"""
        decision = self.authorize(resident=resident, station=station,
                                  at=at, action="inquire")
        if not decision["allowed"]:
            return {"ok": False, "allowed": False, "station": station,
                    "resident": resident, "authorization": decision,
                    "code": decision["code"], "message": decision.get("message")}
        d = self.deriver(at)
        shifts = []
        for u in d.units_for(resident):
            shifts.append({
                "unit_key": u["key"],
                "plan_ids": u["plan_ids"],
                "bundled": u["bundled"],
                "splittable": u["splittable"],
                "window_from": u.get("window_from_text") or format_minutes(u["window_from"]),
                "window_to": u.get("window_to_text") or format_minutes(u["window_to"]),
                "region": d.resident_region(resident),
                "skills": u["skills"],
                # 刻意不含 categories / 病历资料
            })
        return {"ok": True, "allowed": True, "station": station,
                "resident": resident, "as_of": self.now(at),
                "authorization": {"grant_id": decision.get("grant_id")},
                "shifts": shifts}

    def fetch_materials(self, station: str, resident: str,
                        categories: Optional[Sequence[str]] = None,
                        at: Optional[str] = None) -> Dict[str, Any]:
        """按 deliver 授权的用途、类别、有效期取资料；当时授权当时判断。"""
        d = self.deriver(at)
        wanted = list(categories) if categories else sorted({
            c for u in d.units_for(resident) for c in u["categories"]})
        decision = self.authorize(resident=resident, station=station, at=at,
                                  action="materials", categories=wanted)
        if not decision["allowed"]:
            self._event("materials_denied", at=self.now(at), actor=station,
                        target=resident, code=decision["code"], categories=wanted)
            return {"ok": False, "allowed": False, "station": station,
                    "resident": resident, "categories": wanted,
                    "missing_categories": decision.get("missing_categories", []),
                    "grant_id": decision.get("grant_id"),
                    "authorization": decision, "code": decision["code"],
                    "message": decision.get("message")}
        materials = self._materials_for(resident, wanted)
        self._event("materials_fetched", at=self.now(at), actor=station,
                    target=resident, grant_id=decision.get("grant_id"),
                    categories=wanted)
        return {"ok": True, "allowed": True, "station": station,
                "resident": resident, "categories": wanted,
                "grant_id": decision.get("grant_id"),
                "as_of": self.now(at), "materials": materials}

    def _materials_for(self, resident: str, categories: Sequence[str]) -> List[dict]:
        """根据授权类别返回计划关联资料。

        报文中没有正文护理记录，这里给出按类别隔离的资料条目描述；真实系统
        可把 ``items`` 替换为文书内容，访问判定不变。
        """
        titles = {
            "schedule": "排班/时间窗",
            "mobility": "行动协助记录",
            "dressing": "换药记录",
        }
        result = []
        for cat in categories:
            plan_ids = [p["id"] for p in self.store.rows("plans")
                        if p.get("resident") == resident
                        and cat in (p.get("categories") or [])]
            result.append({
                "category": cat,
                "title": titles.get(cat, cat),
                "restricted": cat == "dressing",
                "plan_ids": plan_ids,
                "items": [{"plan": pid, "kind": cat,
                           "label": f"{titles.get(cat, cat)}·{pid}"} for pid in plan_ids],
            })
        return result

    # ------------------------------------------------------------------ 选择承接项
    def select_items(self, station: str, resident: str,
                     plan_ids: Optional[Sequence[str]] = None,
                     unit_key: Optional[str] = None,
                     at: Optional[str] = None) -> Dict[str, Any]:
        """接方挑选可承接内容。

        * 可拆（splittable）项目可逐个选择；
        * 不可拆同组必须整组一次承接，只选其中一部分会被拒绝。

        返回所选单元、授权结论与可行人员/派工检查。
        """
        d = self.deriver(at)
        if unit_key:
            unit = self._require_unit(d, resident, unit_key, None)
            requested = set(unit["plan_ids"])
            units = [unit]
        else:
            if isinstance(plan_ids, str):
                plan_ids = [plan_ids]
            requested = set(plan_ids or [])
            if not requested:
                return {"ok": False, "code": "nothing_selected",
                        "message": "请选择至少一个项目"}
            units = [u for u in d.units_for(resident)
                     if requested & set(u["plan_ids"])]
            for u in units:
                if u["bundled"] and not set(u["plan_ids"]) <= requested:
                    return {
                        "ok": False, "code": "bundle_must_select_whole_group",
                        "unit_key": u["key"], "required_plan_ids": u["plan_ids"],
                        "message": f"{u['key']} 是要求一起做的整组项目，"
                                   "须整组选择并由具备全组资质的人员一次完成",
                    }
        selections = []
        for u in units:
            auth = d.auth_for_unit(u, station, at or self.clock)
            cands = d.candidates_for_unit(u, station)
            selections.append({
                "unit_key": u["key"], "plan_ids": u["plan_ids"],
                "bundled": u["bundled"], "window_from": u.get("window_from_text"),
                "window_to": u.get("window_to_text"), "skills": u["skills"],
                "categories": u["categories"], "authorization": auth,
                "eligible_staff": [c["staff_id"] for c in cands if c["eligible"]],
            })
        self._event("items_selected", at=self.now(at), actor=station,
                    target=resident, plan_ids=sorted(requested))
        return {"ok": True, "station": station, "resident": resident,
                "selections": selections}

    # ------------------------------------------------------------------ 派工校验
    def evaluate_assignment(self, *, staff=None, staff_id=None, skills=None,
                            start=None, end=None, station=None, resident=None,
                            region=None, exclude=(), window_from=None,
                            window_to=None, at=None, plan_ids=None,
                            unit_key=None) -> dict:
        d = self.deriver(at)
        unit = self._resolve_unit(d, resident=resident, unit_key=unit_key,
                                 plan_ids=plan_ids)
        if unit is not None:
            skills = skills or unit["skills"]
            region = region or d.resident_region(unit["resident"])
            resident = resident or unit["resident"]
            window_from = window_from if window_from is not None else unit["window_from"]
            window_to = window_to if window_to is not None else unit["window_to"]
        region = region or d.resident_region(resident)
        staff_row = staff or (self.store.get("staff", staff_id) if staff_id else None)
        return rules.evaluate_assignment(
            staff=staff_row, staff_rows=self.store.rows("staff"), staff_id=staff_id,
            skills=skills or [], start=start, end=end, station=station,
            region=region, resident=resident,
            travel_rows=self.store.rows("travel"),
            commitments=d.schedule_commitments(staff_id or (staff_row or {}).get("id")),
            exclude=exclude, window_from=window_from, window_to=window_to)

    def candidates(self, station: str, *, resident: Optional[str] = None,
                   unit_key: Optional[str] = None, plan_ids=None,
                   start=None, end=None, at=None) -> List[dict]:
        d = self.deriver(at)
        unit = self._resolve_unit(d, resident=resident, unit_key=unit_key,
                                  plan_ids=plan_ids)
        if unit is None:
            raise NotFoundError("找不到对应服务段", resident=resident,
                                unit_key=unit_key, plan_ids=plan_ids)
        return d.candidates_for_unit(unit, station, start, end)

    def _resolve_unit(self, d: Deriver, *, resident=None, unit_key=None,
                      plan_ids=None) -> Optional[dict]:
        if unit_key:
            return next((u for u in d.units if u["key"] == unit_key
                          and (resident is None or u["resident"] == resident)), None)
        if plan_ids:
            ids = {plan_ids} if isinstance(plan_ids, str) else set(plan_ids)
            return next((u for u in d.units if set(u["plan_ids"]) == ids
                         or ids & set(u["plan_ids"])), None)
        return None

    # ------------------------------------------------------------------ 交接
    def propose_handover(self, *, station_to: str, staff: str,
                         start: str, end: str,
                         resident: Optional[str] = None,
                         plan: Optional[str] = None,
                         plan_ids: Optional[Sequence[str]] = None,
                         unit_key: Optional[str] = None,
                         station_from: Optional[str] = None,
                         at: Optional[str] = None, actor: str = "S1",
                         confirm_from: bool = False,
                         handover_id: Optional[str] = None,
                         force: bool = False) -> Dict[str, Any]:
        d = self.deriver(at)
        unit = self._require_unit(d, resident, unit_key, plan_ids or ([plan] if plan else None))
        station_from = station_from or d.responsible_station(unit)
        evaluation = self.evaluate_assignment(
            staff_id=staff, skills=unit["skills"], start=start, end=end,
            station=station_to, resident=unit["resident"], unit_key=unit["key"],
            exclude=[handover_id] if handover_id else ())
        auth = self.auth_for_assignment(unit, station_to, start, at)
        row = {
            "id": handover_id or self.store.new_id("X", "handover"),
            "plan": unit["plan_ids"][0],
            "plan_ids": unit["plan_ids"],
            "unit_key": unit["key"],
            "station_from": station_from,
            "station_to": station_to,
            "staff": staff,
            "proposed": self.now(at),
            "from_confirmed": self.now(at) if confirm_from else None,
            "to_confirmed": None,
            "service_start": start,
            "service_end": end,
            "status": "proposed",
        }
        saved, _ = self.store.upsert("handover", row)
        self._event("handover_proposed", at=row["proposed"], actor=actor,
                    target=saved["id"], handover_id=saved["id"], plan=row["plan"],
                    plan_ids=unit["plan_ids"], station_from=station_from,
                    station_to=station_to, staff=staff,
                    evaluation_ok=evaluation["ok"],
                    evaluation_errors=evaluation["errors"],
                    authorization_allowed=auth.get("allowed"))
        if self.autosave:
            self.store.save()
        return {"ok": True, "handover": saved, "evaluation": evaluation,
                "authorization": auth,
                "ready": bool(evaluation["ok"] and auth.get("allowed")),
                "warning": None if evaluation["ok"] else
                "拟派工未通过检查；接站确认时将被拒绝（除非值班台 force）"}
    def auth_for_assignment(self, unit: dict, station: str,
                            start: str, at: Optional[str]) -> dict:
        # 上门开工时点按服务开始时刻判断授权；同时给出当前取资料判断
        return rules.authorize(
            self.store.rows("grants"), resident=unit["resident"], station=station,
            at=start or self.now(at), action="start", categories=unit["categories"])

    def confirm_handover(self, handover_id: str, side: Optional[str] = None,
                         *, at: Optional[str] = None, actor: Optional[str] = None,
                         force: bool = False) -> Dict[str, Any]:
        x = self.store.require("handover", handover_id)
        when = self.now(at)
        side = side or self._infer_side(x, actor)
        if side not in ("from", "to"):
            return {"ok": False, "code": "bad_side",
                    "message": "side 必须是 from 或 to"}
        field = f"{side}_confirmed"
        by_field = f"{side}_confirmed_by"
        previous_value = x.get(field)
        previous_by = x.get(by_field)
        x[field] = when
        if actor:
            x[by_field] = actor

        d = self.deriver(at)
        view = d.handover_view(x)
        evaluation = view["evaluation"]
        if x.get("from_confirmed") and x.get("to_confirmed"):
            if not evaluation["ok"] and not force:
                # 硬条件不满足：恢复本次确认前的原值，旧承诺/边界维持不变
                x[field] = previous_value
                if previous_by is None:
                    x.pop(by_field, None)
                else:
                    x[by_field] = previous_by
                self._event("handover_confirm_rejected", at=when, actor=actor or side,
                            target=handover_id, handover_id=handover_id,
                            errors=evaluation["errors"])
                return {"ok": False, "code": "validation_failed",
                        "handover": x, "evaluation": evaluation,
                        "message": "新派工不满足全组资质/路程/同时段条件，"
                                   "不能接下；原承诺继续有效"}
            # 双方确认且通过（或 force）：生效，并取代同段旧交接链
            x["status"] = "active"
            x["binding_forced"] = force and not evaluation["ok"]
            self._supersede_previous(d, x)
        self._event("handover_confirmed", at=when, actor=actor or side,
                    target=handover_id, handover_id=handover_id, side=side,
                    mutual=bool(x.get("from_confirmed") and x.get("to_confirmed")),
                    evaluation_errors=evaluation["errors"])
        if self.autosave:
            self.store.save()
        d2 = self.deriver(at)
        view = d2.handover_view(x)
        unit = d2.unit_of_plan(x.get("plan"))
        auth = d2.auth_for_unit(unit, x.get("station_to")) if unit else None
        return {"ok": True, "handover": view,
                "evaluation": view["evaluation"], "authorization": auth,
                "ready": bool(view["binding"] and (not auth or auth.get("allowed")))}

    def accept_handover(self, handover_id: str, **kw) -> Dict[str, Any]:
        """接站确认（``side='to'``）的语义化别名。"""
        kw["side"] = "to"
        return self.confirm_handover(handover_id, **kw)

    def reject_handover(self, handover_id: str, side: str = "to", *,
                        reason: str = "", at: Optional[str] = None,
                        actor: Optional[str] = None) -> Dict[str, Any]:
        x = self.store.require("handover", handover_id)
        when = self.now(at)
        x["status"] = "rejected"
        x[f"{side}_rejected"] = when
        x[f"{side}_reject_reason"] = reason
        self._event("handover_rejected", at=when, actor=actor or side,
                    target=handover_id, handover_id=handover_id, side=side, reason=reason)
        if self.autosave:
            self.store.save()
        return {"ok": True, "handover": x}

    def reassign(self, handover_id: str, *, station_to: str, staff: str,
                 start: str, end: str, at: Optional[str] = None,
                 actor: str = "S2", confirm_from: bool = True,
                 force: bool = False, **kw) -> Dict[str, Any]:
        """临时改派：生成新交接单；新单双方确认且合格前，旧承诺不消失。"""
        old = self.store.require("handover", handover_id)
        result = self.propose_handover(
            resident=kw.get("resident"), plan_ids=old.get("plan_ids") or [old.get("plan")],
            station_to=station_to, staff=staff, start=start, end=end, at=at,
            actor=actor, station_from=old.get("station_to"),
            confirm_from=confirm_from, force=force)
        new_x = result["handover"]
        result["old_commitment"] = {
            "handover_id": old["id"],
            "still_active": not old.get("superseded_by"),
            "message": "改派请求已发出，但新人尚未合格接下；旧承诺继续有效",
        }
        self._event("reassign_requested", at=self.now(at), actor=actor,
                    target=new_x["id"], handover_id=new_x["id"],
                    old_handover=old["id"])
        return result

    def _infer_side(self, x: dict, actor: Optional[str]) -> str:
        if not actor:
            return "to"
        if actor == x.get("station_from"):
            return "from"
        if actor == x.get("station_to"):
            return "to"
        return "to"

    def _supersede_previous(self, d: Deriver, new_x: dict) -> None:
        unit = d.unit_of_plan(new_x.get("plan"))
        if not unit:
            return
        for old in d.handovers_for_unit(unit):
            if old.get("id") == new_x.get("id"):
                continue
            if old.get("superseded_by") or old.get("status") == "rejected":
                continue
            # 仅取代同方向链上的前一有效边界
            if old.get("station_to") == new_x.get("station_from") or old.get("binding"):
                old["superseded_by"] = new_x["id"]
                old["status"] = "superseded"
                self._event("handover_superseded", at=new_x.get("to_confirmed"),
                            actor="system", target=old["id"],
                            handover_id=old["id"], by=new_x["id"])

    def _require_unit(self, d: Deriver, resident, unit_key, plan_ids) -> dict:
        unit = self._resolve_unit(d, resident=resident, unit_key=unit_key,
                                  plan_ids=plan_ids)
        if unit is None:
            raise NotFoundError("找不到对应服务段/组", resident=resident,
                                unit_key=unit_key, plan_ids=plan_ids)
        return unit

    # ------------------------------------------------------------------ 值班派工
    def open_dispatch(self, *, resident: str, plan_ids=None, unit_key=None,
                      station: Optional[str] = None, note: str = "",
                      at: Optional[str] = None, actor: str = "duty") -> dict:
        d = self.deriver(at)
        unit = self._require_unit(d, resident, unit_key, plan_ids)
        row = {
            "id": self.store.new_id("R", "dispatch_requests"),
            "resident": resident, "unit_key": unit["key"],
            "plan_ids": unit["plan_ids"], "station": station,
            "status": "searching", "opened_at": self.now(at),
            "note": note,
        }
        saved, _ = self.store.upsert("dispatch_requests", row)
        self._event("dispatch_opened", at=row["opened_at"], actor=actor,
                    target=saved["id"], **{k: row[k] for k in
                                          ("resident", "unit_key", "plan_ids")})
        return {"ok": True, "dispatch_request": saved}

    def close_dispatch(self, request_id: str, outcome: str = "closed", *,
                       at: Optional[str] = None, actor: str = "duty",
                       handover_id: Optional[str] = None) -> dict:
        r = self.store.require("dispatch_requests", request_id)
        r["status"] = outcome
        r["closed_at"] = self.now(at)
        if handover_id:
            r["handover_id"] = handover_id
        self._event("dispatch_closed", at=r["closed_at"], actor=actor,
                    target=request_id, outcome=outcome, handover_id=handover_id)
        return {"ok": True, "dispatch_request": r}

    def mark_no_show(self, *, resident: str, plan_ids=None, unit_key=None,
                     reporter: str = "duty", note: str = "",
                     at: Optional[str] = None, actor: str = "duty") -> dict:
        d = self.deriver(at)
        unit = self._require_unit(d, resident, unit_key, plan_ids)
        row = {
            "id": self.store.new_id("N", "no_shows"),
            "resident": resident, "unit_key": unit["key"],
            "plan_ids": unit["plan_ids"], "reported_by": reporter,
            "at": self.now(at), "note": note or "与登记端/站点核实：该户根本未到访",
        }
        saved, _ = self.store.upsert("no_shows", row)
        self._event("no_show_recorded", at=row["at"], actor=actor,
                    target=saved["id"], unit_key=unit["key"])
        return {"ok": True, "no_show": saved}

    # ---------------------------------------------------------- 证明/回执/判定
    def register_proof(self, *, resident: str, station: str, staff: str,
                       start: str, end: str, issued: Optional[str] = None,
                       proof_id: Optional[str] = None,
                       actor: str = "registry", at: Optional[str] = None) -> dict:
        row = {
            "id": proof_id or self.store.new_id("K", "proofs"),
            "resident": resident, "station": station, "staff": staff,
            "issued": issued or self.now(at),
            "recorded_start": start, "recorded_end": end,
        }
        saved, inserted = self.store.upsert("proofs", row)
        self._event("proof_registered", at=self.now(at), actor=actor,
                    target=saved["id"], resident=resident, station=station, staff=staff)
        return {"ok": True, "proof": saved}

    def upload_visit(self, *, plan: str, station: str, staff: str,
                     proof: Optional[str] = None,
                     start: Optional[str] = None, end: Optional[str] = None,
                     completed: bool = True, received: Optional[str] = None,
                     visit_id: Optional[str] = None,
                     actor: Optional[str] = None,
                     at: Optional[str] = None) -> dict:
        """站点回传到访回执。``received`` 是上传时间，缺省取当前钟；
        实际开始以 proof 登记端可匹配记录为准做授权核认。"""
        proof_row = self.store.get("proofs", proof) if proof else None
        start = start or (proof_row or {}).get("recorded_start")
        end = end or (proof_row or {}).get("recorded_end")
        row = {
            "id": visit_id or self.store.new_id("V", "visits"),
            "plan": plan, "station": station, "staff": staff, "proof": proof,
            "start": start, "end": end,
            "completed": bool(completed),
            "received": received or self.now(at),
        }
        saved, inserted = self.store.upsert("visits", row)
        d = self.deriver(at)
        verification = d.verify_visit(saved)
        self._event("visit_received" if inserted else "visit_updated",
                    at=row["received"], actor=actor or station, target=saved["id"],
                    visit_id=saved["id"], plan=plan, proof=proof,
                    valid_completion=verification["valid_completion"],
                    late_valid=verification["late_report_but_performed_authorized"])
        if self.autosave:
            self.store.save()
        return {"ok": True, "visit": saved, "verification": verification}

    #: 语义化别名：补传回执
    attach_receipt = upload_visit

    def adjudicate_proof(self, proof_id: str, *, outcome: str = "confirmed",
                         station: Optional[str] = None, staff: Optional[str] = None,
                         visit_id: Optional[str] = None, counts: Optional[bool] = None,
                         note: str = "", at: Optional[str] = None,
                         actor: str = "duty") -> dict:
        """值班台对照双方证据判定同一次到户的归属。

        确认后该 proof 在住户视角只形成一次履行；若确认的是迟到的原履行，
        同段仍在 searching 的补派单自动作废，避免重复上门/重复计数。
        """
        proof = self.store.require("proofs", proof_id)
        when = self.now(at)
        row = next((j for j in self.store.rows("adjudications")
                    if j.get("proof") == proof_id), None)
        if row is None:
            row = {"id": self.store.new_id("J", "adjudications"), "proof": proof_id}
            self.store.rows("adjudications").append(row)
        row.update({
            "outcome": outcome,
            "station": station or proof.get("station"),
            "staff": staff or proof.get("staff"),
            "visit_id": visit_id,
            "counts": counts if counts is not None else outcome == "confirmed",
            "note": note, "decided_by": actor, "at": when,
        })
        # 若确认了一次真实履行，作废同段仍在找人的补派请求
        obsolete = []
        if row["counts"]:
            d = self.deriver(at)
            group = next((g for g in d.performance_groups()
                          if g["proof_id"] == proof_id), None)
            for plan_id in (group or {}).get("plan_ids", []):
                unit = d.unit_of_plan(plan_id)
                if not unit:
                    continue
                for r in d.open_dispatch_for_unit(unit):
                    r["status"] = "obsolete_performance_found"
                    r["closed_at"] = when
                    obsolete.append(r["id"])
        self._event("proof_adjudicated", at=when, actor=actor, target=proof_id,
                    outcome=outcome, station=row["station"], staff=row["staff"],
                    visit_id=visit_id, obsolete_requests=obsolete, note=note)
        if self.autosave:
            self.store.save()
        return {"ok": True, "adjudication": row,
                "obsolete_dispatch_requests": obsolete}

    #: 别名
    resolve_duplicate = adjudicate_proof

    def events(self, **kw) -> List[dict]:
        return self.store.query_events(**kw)


# 常见命名别名，方便不同调用习惯/接入方
CareService = CareBackend
Backend = CareBackend
Service = CareBackend
StoreBackend = CareBackend
ExchangeBackend = CareBackend
ExchangeService = CareBackend
Exchange = CareBackend


def create_backend(*args, **kwargs) -> CareBackend:
    return CareBackend(*args, **kwargs)


def load_backend(path: Optional[str] = None, **kwargs) -> CareBackend:
    return CareBackend(path, **kwargs)
