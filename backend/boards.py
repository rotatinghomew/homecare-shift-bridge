"""面向住户/授权家属、站点、值班台的只读看板组装。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .derive import Deriver
from . import rules


def family_todos(d: Deriver, resident: str) -> List[Dict[str, Any]]:
    """列出“还需要住户/家属补办”的事项——核心是补授权。

    两类来源：
    1. 未完成分段的责任站在当前/服务时点缺少覆盖该段类别的 deliver 授权；
    2. 该住户已有 deliver 授权被撤回/到期（即使某次服务在撤回前已有效完成，
       也不恢复之后的资料访问），若该站仍涉及交接/后续段，提示重新开具。
    """
    todos: List[Dict[str, Any]] = []
    seen = set()

    def add_todo(key: str, row: Dict[str, Any]):
        if key in seen:
            return
        seen.add(key)
        row["key"] = key
        todos.append(row)

    delegate = (d.resident(resident) or {}).get("delegate")

    for seg in d.segment_rows(resident):
        # 已完成或已有现场证据待核的分段，不再按“缺授权待办”打扰家属；
        # 撤回授权的续办由下面的 grant 维度统一提示一次。
        if seg["triage"] in ("fulfilled", "pending_verification"):
            continue
        action = seg.get("authorization_action")
        auth = seg["authorization"]
        if action is None and not auth.get("allowed"):
            action = {
                "station": seg["responsible_station"],
                "code": auth.get("code"), "grant_id": auth.get("grant_id"),
                "missing_categories": auth.get("missing_categories", []),
                "message": auth.get("message"),
            }
        if action is None:
            continue
        station = action["station"]
        missing = action.get("missing_categories") or seg["categories"]
        code = action.get("code")
        add_todo(
            f"auth:{resident}:{station}:{','.join(sorted(missing))}:{seg['unit_key']}",
            {
                "type": "issue_grant",
                "action": "请住户/家属为站点补开 deliver 授权",
                "resident": resident, "delegate": delegate, "station": station,
                "categories": sorted(missing),
                "all_segment_categories": seg["categories"],
                "reason_code": code,
                "message": action.get("message") or f"{station} 缺少覆盖该段全部类别的有效授权",
                "unit_key": seg["unit_key"], "plan_ids": seg["plan_ids"],
                "window": f"{seg['window_from']}-{seg['window_to']}",
                "items": seg["skills"],
                "prefill": {"purpose": "deliver", "categories": sorted(missing),
                            "issued": d.at_text, "expires": "23:59"},
            },
        )

    # 被撤回/到期的既有 deliver 授权：提示续办（不自动恢复任何权限）
    involved_stations = set()
    for x in d.store.rows("handover"):
        plan = d.store.get("plans", x.get("plan"))
        if (plan or {}).get("resident") == resident:
            involved_stations.add(x.get("station_to"))
            involved_stations.add(x.get("station_from"))
    for g in d.store.rows("grants"):
        if g.get("resident") != resident or g.get("purpose") != "deliver":
            continue
        timing = rules.grant_timing(g, d.at_text)
        if timing == "active":
            continue
        station = g.get("station")
        if station not in involved_stations:
            continue
        cats = sorted(g.get("categories") or [])
        add_todo(
            f"reissue:{g['id']}",
            {
                "type": "reissue_grant",
                "action": "重新开具/延长 deliver 授权",
                "resident": resident, "delegate": delegate, "station": station,
                "categories": cats,
                "reason_code": f"grant_{timing}",
                "revoked": g.get("revoked"), "expires": g.get("expires"),
                "message": f"{g['id']}（{station}）当前状态为 {timing}；撤回后迟到回执可证明"
                           "撤回前的到访，但不恢复该站之后取资料/开工的权限，如需继续请补授权",
                "grant_id": g["id"],
                "prefill": {"purpose": "deliver", "categories": cats,
                            "issued": d.at_text, "expires": "23:59"},
            },
        )
    return todos


def family_board(backend, actor: str, at: Optional[str] = None) -> Dict[str, Any]:
    d = backend.deriver(at)
    resident_id, resident = backend.resolve_resident_actor(actor)
    segments = d.segment_rows(resident_id)
    performances = [g for g in d.performance_groups(resident_id)]
    # 家属只看“履行”：已计入，或至少有一份标记完成的到户申报（待核）。
    # 未完成/未开工的尝试（如 K2）只留在站点与值班台视图，不打扰家属。
    visible_performances = [_resident_perf(g) for g in performances
                           if g.get("proof_id") and (
                               g["counts_as_performance"] or g.get("apparent_completion"))]
    todos = family_todos(d, resident_id)
    grants = [_grant_view(g) for g in d.store.rows("grants")
              if g.get("resident") == resident_id]
    counts = {
        "segments": len(segments),
        "fulfilled": sum(s["triage"] == "fulfilled" for s in segments),
        "pending_verification": sum(s["triage"] == "pending_verification" for s in segments),
        "gaps": sum(s["triage"] not in ("fulfilled",) for s in segments),
        "todos": len(todos),
    }
    return {
        "view": "family",
        "actor": actor,
        "as_of": d.at_text,
        "resident": resident,
        "delegate": (resident or {}).get("delegate"),
        "counts": counts,
        "segments": segments,
        "performances": visible_performances,
        "todos": todos,
        "authorizations": grants,
    }


def _resident_perf(g: dict) -> Dict[str, Any]:
    """住户视角：按 proof 去重，同一次到户只出现一次。"""
    return {
        "proof_id": g.get("proof_id"),
        "date": "2026-12-02",
        "start": g.get("recorded_start"),
        "end": g.get("recorded_end"),
        "station": g.get("attributed_station"),
        "staff": g.get("attributed_staff"),
        "state": g.get("resident_visible_state"),
        "state_label": {
            "completed": "已到访并完成（同次到户的多站申报只显示这一次）",
            "under_review": "已有到户记录，归属正在由值班台核对",
            "recorded": "已有到户登记",
        }.get(g.get("resident_visible_state"), g.get("resident_visible_state")),
        "plan_ids": g.get("plan_ids"),
    }


def _grant_view(g: dict) -> dict:
    return {k: g.get(k) for k in ("id", "station", "purpose", "categories",
                                  "issued", "expires", "revoked")}


def duty_board(backend, at: Optional[str] = None) -> Dict[str, Any]:
    d = backend.deriver(at)
    segments = d.segment_rows()
    gaps = [s for s in segments if s["triage"] != "fulfilled"]
    attribution_queue = [_duty_proof(g) for g in d.performance_groups()
                         if g["conflict"] or g["state"] in ("under_review", "needs_review")]
    alerts = _alerts(d)
    return {
        "view": "duty",
        "as_of": d.at_text,
        "summary": {
            "segments": len(segments),
            "gaps": len(gaps),
            "pending_verification": sum(s["triage"] == "pending_verification" for s in segments),
            "not_visited": sum(s["triage"] == "not_visited" for s in segments),
            "searching": sum(s["duty_state"] == "searching" for s in segments),
            "unassignable": sum(s["duty_state"] == "unassignable" for s in segments),
            "authorization_required": sum(s["duty_state"] == "authorization_required" for s in segments),
            "attribution_queue": len(attribution_queue),
            "alerts": len(alerts),
        },
        "gaps": gaps,
        "segments": segments,
        "attribution_queue": attribution_queue,
        "dispatch_requests": d.store.rows("dispatch_requests"),
        "no_shows": d.store.rows("no_shows"),
        "adjudications": d.store.rows("adjudications"),
        "alerts": alerts,
        "events": d.store.rows("events")[-50:],
    }


def _duty_proof(g: dict) -> dict:
    return {
        "proof_id": g.get("proof_id"),
        "resident": g.get("resident"),
        "recorded_start": g.get("recorded_start"),
        "recorded_end": g.get("recorded_end"),
        "neutral_attribution": {"station": (g.get("proof") or {}).get("station"),
                                "staff": (g.get("proof") or {}).get("staff")},
        "claims": [{
            "visit_id": v["visit_id"], "station": v["claimed_station"],
            "staff": v["claimed_staff"], "completed": v["completed"],
            "received": v["received"],
            "consistent_with_proof": v["claim_consistent_with_proof"],
            "auth_allowed_at_start": v["auth_at_actual_start"].get("allowed"),
            "auth_code_at_start": v["auth_at_actual_start"].get("code"),
            "claim_auth_allowed_at_start": v["claim_auth_at_actual_start"].get("allowed"),
            "claim_auth_code_at_start": v["claim_auth_at_actual_start"].get("code"),
            "in_handover_scope": v["in_handover_scope"],
            "claim_in_handover_scope": v["claim_in_handover_scope"],
            "late_valid": v["late_report_but_performed_authorized"],
            "valid_completion": v["valid_completion"],
            "issues": [i.get("code") for i in v["qualification_issues"]],
            "claim_issues": [i.get("code") for i in v["claim_qualification_issues"]],
        } for v in g["verifications"]],
        "conflict": g["conflict"],
        "state": g["state"],
        "recommendation": g["recommendation"],
        "adjudication": g["adjudication"],
    }


def _alerts(d: Deriver) -> List[Dict[str, Any]]:
    alerts: List[Dict[str, Any]] = []
    for x in d.store.rows("handover"):
        view = d.handover_view(x)
        if view["binding"]:
            continue
        codes = set(view["evaluation"].get("errors") or [])
        if not x.get("from_confirmed") and x.get("to_confirmed"):
            reason = "original_not_confirmed"
            msg = "接站虽确认，但原站未确认同一份边界，交接不成立，原站责任不解除"
        elif x.get("from_confirmed") and not x.get("to_confirmed"):
            reason = "receiver_not_accepted"
            msg = "已发出/原站已确认，但接站尚未接下；不能据此停止原承诺，临时改派同理"
        elif not x.get("from_confirmed") and not x.get("to_confirmed"):
            reason = "neither_confirmed"
            msg = "双方均未确认，仅为意向/拟排"
        else:
            reason = "evaluation_failed"
            msg = "双方确认但派工检查未通过"
        alerts.append({
            "kind": "handover_not_binding", "handover_id": x.get("id"),
            "plan": x.get("plan"), "reason": reason,
            "message": msg, "evaluation_errors": sorted(codes),
            "issues": view["evaluation"].get("issues"),
        })
    for v in d.store.rows("visits"):
        ver = d.verify_visit(v)
        if ver["valid_completion"]:
            continue
        codes = []
        if not ver["claim_consistent_with_proof"]:
            codes.append("attribution_conflict")
        if not ver["auth_at_actual_start"].get("allowed"):
            codes.append("start_authorization_" + str(ver["auth_at_actual_start"].get("code")))
        if not ver["in_handover_scope"]:
            codes.append("outside_handover_scope")
        # 申报方自身（即便与中立证明不同）的资质/授权/范围问题
        for i in ver.get("claim_qualification_issues", []):
            if i.get("severity") == "error":
                codes.append("claim_" + str(i.get("code")))
        if not ver.get("claim_auth_at_actual_start", {}).get("allowed", True):
            codes.append("claim_auth_" + str(ver["claim_auth_at_actual_start"].get("code")))
        if not ver.get("claim_in_handover_scope", True):
            codes.append("claim_outside_scope")
        if not ver["completed"]:
            codes.append("not_completed")
        if ver["match"]["problems"]:
            codes.extend(ver["match"]["problems"])
        if codes:
            alerts.append({
                "kind": "visit_not_counted", "visit_id": v.get("id"),
                "proof_id": v.get("proof"), "plan": v.get("plan"),
                "codes": codes,
                "late_valid_note": "实际开始在授权期内，仅回执晚于撤回：可认定撤回前履行"
                if ver["late_report_but_performed_authorized"] else None,
            })
    return alerts


def station_board(backend, station: str, at: Optional[str] = None) -> Dict[str, Any]:
    d = backend.deriver(at)
    staff = [s for s in d.store.rows("staff") if s.get("station") == station]
    my_handovers = [d.handover_view(x) for x in d.store.rows("handover")
                    if x.get("station_to") == station or x.get("station_from") == station]
    commitments = d.schedule_commitments()
    mine = [c for c in commitments if _staff_station(d, c["staff"]) == station]
    resident_ids = {u["resident"] for u in d.units}
    resident_access = []
    for rid in sorted(resident_ids):
        inq = rules.authorize(d.store.rows("grants"), resident=rid, station=station,
                              at=d.at_text, action="inquire")
        mat = rules.authorize(d.store.rows("grants"), resident=rid, station=station,
                              at=d.at_text, action="materials",
                              categories=_all_cats_for(d, rid))
        resident_access.append({"resident": rid, "inquire": inq, "materials_preview": mat})
    return {
        "view": "station",
        "station": station,
        "as_of": d.at_text,
        "staff": staff,
        "commitments": sorted(mine, key=lambda c: (c.get("start") or 0)),
        "handovers": my_handovers,
        "resident_access": resident_access,
        "events": [e for e in d.store.rows("events")
                   if station in str(e.get("actor")) or station in str(e.get("target"))][-30:],
    }


def _staff_station(d: Deriver, staff_id: str) -> Optional[str]:
    s = d.store.get("staff", staff_id)
    return (s or {}).get("station")


def _all_cats_for(d: Deriver, resident: str) -> List[str]:
    cats = set()
    for u in d.units_for(resident):
        cats.update(u["categories"])
    return sorted(cats)
