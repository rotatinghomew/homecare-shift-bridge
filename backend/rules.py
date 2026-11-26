"""《分段转介与到访交换》的纯领域规则。

本模块不持有可变状态、不做 IO，便于单独测试。时间入参既可以是 ``"HH:MM"``
文本，也可以是当日分钟数。所有区间均为半开区间 ``[start, end)``。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from .timeutil import overlaps, parse_minutes

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def as_minutes(value) -> Optional[int]:
    return value if isinstance(value, int) else parse_minutes(value)


def _row_id(row: Any) -> str:
    return row.get("id") if isinstance(row, dict) else str(row)


def index_by_id(rows: Iterable[dict]) -> Dict[str, dict]:
    return {row["id"]: row for row in rows if isinstance(row, dict) and row.get("id")}


# ---------------------------------------------------------------------------
# 住户授权 grants
# ---------------------------------------------------------------------------

#: 授权时点状态
GRANT_ACTIVE = "active"
GRANT_PENDING = "pending"          # 尚未到 issued
GRANT_REVOKED = "revoked"
GRANT_EXPIRED = "expired"
GRANT_NONE = "no_grant"


def grant_timing(grant: Optional[dict], at) -> str:
    """判断某条授权在 ``at`` 时点的时间状态。

    撤回截断未来部分；到期点（``expires``）与 ``qualification_to`` 一样，
    所在时点已不在有效区间内。
    """
    if not grant:
        return GRANT_NONE
    t = as_minutes(at)
    issued = parse_minutes(grant.get("issued"))
    expires = parse_minutes(grant.get("expires"))
    revoked = parse_minutes(grant.get("revoked"))
    if t is None:
        t = 23 * 60 + 59
    if issued is not None and t < issued:
        return GRANT_PENDING
    if expires is not None and t >= expires:
        return GRANT_EXPIRED
    if revoked is not None and t >= revoked:
        return GRANT_REVOKED
    return GRANT_ACTIVE


def grant_categories(grant: dict) -> Set[str]:
    return set(grant.get("categories") or [])


def grant_covers(grant: dict, categories: Iterable[str]) -> bool:
    wanted = set(categories or [])
    return wanted <= grant_categories(grant)


def _decision(allowed: bool, reason: str, grant: Optional[dict] = None,
              missing: Optional[Sequence[str]] = None, **extra) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "allowed": allowed,
        "ok": allowed,
        "reason": reason if allowed else reason,
        "code": reason,
        "grant_id": (grant or {}).get("id"),
        "timing": grant_timing(grant, extra.get("at")) if grant else GRANT_NONE,
    }
    if missing is not None:
        result["missing_categories"] = sorted(set(missing))
    result.update(extra)
    return result


def authorize(
    grants: Iterable[dict],
    *,
    resident: str,
    station: str,
    at=None,
    action: str = "deliver",
    purpose: Optional[str] = None,
    categories: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """判断站点在某时点能否询班 / 取资料 / 上门。

    ``action``:
      * ``"inquire"``  —— 持有效 inquire 或 deliver 授权即可，只见时段/片区/技能；
      * ``"materials"`` / ``"deliver"`` / ``"start"`` —— 必须有有效 deliver
        授权，且覆盖所请求（或该段全部）资料类别。

    返回纯 dict：``allowed`` 为最终结论；拒绝时带 ``code``、``timing``、
    ``missing_categories`` 与可解释的候选授权。
    """
    wanted = list(categories or [])
    need_purpose = purpose or ("inquire" if action == "inquire" else "deliver")
    rows = [
        g for g in (grants or [])
        if g.get("resident") == resident and g.get("station") == station
    ]
    if not rows:
        return _decision(False, "no_grant", at=at,
                         message=f"住户 {resident} 未给 {station} 任何授权，需请住户先补授权")

    explain: List[Dict[str, Any]] = []
    allowed_grant = None
    best: Optional[tuple] = None

    for g in rows:
        timing = grant_timing(g, at)
        g_purpose = g.get("purpose")
        purpose_ok = (
            action == "inquire" and g_purpose in ("inquire", "deliver")
        ) or (
            action != "inquire" and g_purpose == "deliver"
        )
        missing = sorted(set(wanted) - grant_categories(g)) if wanted else []
        covered = len(set(wanted) & grant_categories(g))
        info = {
            "grant_id": g.get("id"),
            "purpose": g_purpose,
            "timing": timing,
            "categories": sorted(grant_categories(g)),
            "purpose_ok": purpose_ok,
            "missing_categories": missing,
        }
        explain.append(info)
        if timing == GRANT_ACTIVE and purpose_ok and not missing:
            allowed_grant = g
            break

        # 选择“最接近可用”的授权来解释拒绝：用途匹配 >> 时点有效 >> 类别覆盖
        timing_score = {GRANT_ACTIVE: 100, GRANT_PENDING: 40,
                        GRANT_REVOKED: 30, GRANT_EXPIRED: 20}.get(timing, 0)
        score = (10_000 if purpose_ok else 0) + timing_score + covered
        if best is None or score > best[0]:
            best = (score, g, timing, missing, purpose_ok)

    if allowed_grant is not None:
        return _decision(True, "ok", allowed_grant, at=at, candidates=explain)

    _, g, timing, missing, purpose_ok = best  # type: ignore[misc]
    if not purpose_ok and timing == GRANT_ACTIVE:
        return _decision(False, "wrong_purpose", g, at=at, candidates=explain,
                         message=f"{g.get('id')} 的用途是 {g.get('purpose')}，不能用于 {action}")
    if timing != GRANT_ACTIVE:
        code = f"grant_{timing}" if timing != GRANT_NONE else "no_grant"
        messages = {
            "grant_revoked": f"{g.get('id')} 已被住户撤回，撤回后的访问/开工按当时授权拒绝",
            "grant_expired": f"{g.get('id')} 已到期，需请住户补授权",
            "grant_pending": f"{g.get('id')} 尚未到生效时间",
        }
        return _decision(False, code, g, at=at, candidates=explain,
                         message=messages.get(code, code))
    return _decision(False, "missing_categories", g, missing=missing, at=at,
                     candidates=explain,
                     message="授权资料类别不足，缺少 " + "、".join(missing))


# ---------------------------------------------------------------------------
# 服务段 / 同组不拆分
# ---------------------------------------------------------------------------


def group_key(plan: dict) -> tuple:
    return plan.get("resident"), plan.get("group")


def is_bundled(plans: Sequence[dict]) -> bool:
    """同组内只要有一个项目声明不可拆，整组就须一次到访完成。"""
    return any(not p.get("splittable", True) for p in plans)


def build_units(plans: Iterable[dict], resident: Optional[str] = None) -> List[Dict[str, Any]]:
    """把 plans 归并成“可承接单元”。

    * 不可拆组 -> 一个 bundle 单元，``plan_ids`` 为整组；
    * 可拆项目 -> 每个 plan 一个独立可选单元。
    """
    rows = [p for p in plans if resident is None or p.get("resident") == resident]
    groups: Dict[tuple, List[dict]] = {}
    for p in rows:
        groups.setdefault(group_key(p), []).append(p)

    units: List[Dict[str, Any]] = []
    for (res, grp), members in sorted(groups.items(), key=lambda kv: str(kv[0])):
        members = sorted(members, key=lambda p: p.get("id", ""))
        if is_bundled(members):
            units.append(_make_unit(f"{grp}", members, bundled=True))
        else:
            for p in members:
                units.append(_make_unit(p["id"], [p], bundled=False))
    return units


def _make_unit(key: str, members: List[dict], *, bundled: bool) -> Dict[str, Any]:
    starts = [parse_minutes(p.get("window_from")) for p in members]
    ends = [parse_minutes(p.get("window_to")) for p in members]
    starts = [s for s in starts if s is not None]
    ends = [e for e in ends if e is not None]
    skills = sorted({p.get("skill") for p in members if p.get("skill")})
    categories = sorted({c for p in members for c in (p.get("categories") or [])})
    return {
        "key": key,
        "resident": members[0].get("resident"),
        "group": members[0].get("group"),
        "plan_ids": [p["id"] for p in members],
        "bundled": bundled,
        "splittable": not bundled,
        "window_from": min(starts) if starts else None,
        "window_to": max(ends) if ends else None,
        "window_from_text": members[0].get("window_from"),
        "window_to_text": members[0].get("window_to"),
        "skills": skills,
        "categories": categories,
        "referrals": sorted({p.get("referral") for p in members if p.get("referral")}),
    }


def unit_for_plan(units: Sequence[dict], plan_id: str) -> Optional[dict]:
    return next((u for u in units if plan_id in u["plan_ids"]), None)


# ---------------------------------------------------------------------------
# 护工资质
# ---------------------------------------------------------------------------


def qualification_issues(staff: dict, skills: Iterable[str], start, end) -> List[Dict[str, Any]]:
    """检查人员技能与资质有效期是否覆盖整次到访 ``[start, end)``。"""
    issues: List[Dict[str, Any]] = []
    s = as_minutes(start)
    e = as_minutes(end)
    q_from = parse_minutes(staff.get("qualification_from"))
    q_to = parse_minutes(staff.get("qualification_to"))
    staff_skills = set(staff.get("skills") or [])
    missing = sorted(set(skills or []) - staff_skills)
    if missing:
        issues.append({
            "severity": "error", "code": "missing_skill",
            "staff": staff.get("id"), "missing_skills": missing,
            "message": f"人员 {staff.get('id')} 缺少技能：" + "、".join(missing)
                       + "；整组须由具备全组资质的人员一次完成",
        })
    if s is None or e is None:
        issues.append({"severity": "error", "code": "bad_interval",
                       "message": "到访起止时刻缺失或无法解析"})
        return issues
    if q_from is not None and s < q_from:
        issues.append({
            "severity": "error", "code": "qualification_not_started",
            "staff": staff.get("id"), "start": s, "qualification_from": q_from,
            "message": f"人员 {staff.get('id')} 资质 {q_from // 60:02d}:{q_from % 60:02d} 才生效",
        })
    # qualification_to 所在时点不属有效期：start == q_to 已无资质
    if q_to is not None and s >= q_to:
        issues.append({
            "severity": "error", "code": "qualification_ended_at_start",
            "staff": staff.get("id"), "start": s, "qualification_to": q_to,
            "message": f"人员 {staff.get('id')} 资质到 "
                       f"{q_to // 60:02d}:{q_to % 60:02d} 截止（该时点已失效），不能承接此时段",
        })
    elif q_to is not None and e > q_to:
        issues.append({
            "severity": "error", "code": "qualification_covers_not_whole_visit",
            "staff": staff.get("id"), "end": e, "qualification_to": q_to,
            "message": f"人员 {staff.get('id')} 资质不能覆盖整次到访（结束晚于 "
                       f"{q_to // 60:02d}:{q_to % 60:02d}）",
        })
    return issues


# ---------------------------------------------------------------------------
# 片区路程
# ---------------------------------------------------------------------------


def travel_minutes(travel_rows: Iterable[dict], from_region: str,
                   to_region: str) -> Optional[int]:
    """查询两片区之间的最低路程（分钟）。

    精确配对优先；同区但缺该行时，采用数据中同区出入时间的最小值（约定中
    “同区连续到访也保留出入时间”）；跨区缺主数据返回 ``None``。
    """
    rows = list(travel_rows or [])
    exact = next((r for r in rows
                  if r.get("from_region") == from_region
                  and r.get("to_region") == to_region), None)
    if exact is not None:
        return exact.get("minutes")
    if from_region == to_region:
        same = [r.get("minutes") for r in rows
                if r.get("from_region") == r.get("to_region")
                and isinstance(r.get("minutes"), (int, float))]
        return min(same) if same else 10
    return None


# ---------------------------------------------------------------------------
# 派工可行性：资质 + 同时段冲突 + 赶路
# ---------------------------------------------------------------------------


def evaluate_assignment(
    *,
    staff: Optional[dict] = None,
    staff_rows: Optional[Sequence[dict]] = None,
    staff_id: Optional[str] = None,
    skills: Iterable[str] = (),
    start=None,
    end=None,
    station: Optional[str] = None,
    region: Optional[str] = None,
    resident: Optional[str] = None,
    travel_rows: Iterable[dict] = (),
    commitments: Sequence[dict] = (),
    exclude: Iterable[str] = (),
    window_from=None,
    window_to=None,
) -> Dict[str, Any]:
    """完整检查一次拟派工是否可承诺。

    ``commitments`` 为该护工已有的有效承诺列表，每项至少含
    ``id, staff, start, end``，建议含 ``region/resident``。
    ``exclude`` 中的承诺 id（如正在改派的旧单）不参与冲突检查。
    """
    s = as_minutes(start)
    e = as_minutes(end)
    exclude_set = set(exclude or [])
    issues: List[Dict[str, Any]] = []
    travel_checks: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []

    if staff is None and staff_id and staff_rows is not None:
        staff = next((x for x in staff_rows if x.get("id") == staff_id), None)
    if staff is None:
        return {
            "ok": False, "feasible": False,
            "issues": [{"severity": "error", "code": "staff_not_found",
                        "staff": staff_id, "message": f"找不到人员 {staff_id}"}],
            "errors": ["staff_not_found"], "warnings": [],
            "conflicts": [], "travel": [],
        }
    sid = staff.get("id")

    warnings: List[Dict[str, Any]] = []

    if station and staff.get("station") != station:
        issues.append({"severity": "error", "code": "wrong_station", "staff": sid,
                       "station": station, "staff_station": staff.get("station"),
                       "message": f"人员 {sid} 不属于承接站 {station}"})

    issues.extend(qualification_issues(staff, skills, s, e))

    mine = [c for c in commitments
            if c.get("staff") == sid and c.get("id") not in exclude_set]

    # 同一时段不能又许给“另一户”（半开区间，首尾相接允许，但还要过路程）。
    # 同一住户的重叠/相邻段可并入同一次到访，只给提示，不算双重承诺。
    for c in mine:
        cs, ce = as_minutes(c.get("start")), as_minutes(c.get("end"))
        if cs is None or ce is None or s is None or e is None:
            continue
        if overlaps(s, e, cs, ce):
            same_household = resident is not None and c.get("resident") == resident
            if same_household:
                warnings.append({"severity": "warning",
                                 "code": "same_household_overlap", "staff": sid,
                                 "with": c.get("id"), "resident": resident,
                                 "message": "与同一住户的相邻/重叠段可由一次到访合并完成"})
            else:
                item = {"severity": "error", "code": "double_booked", "staff": sid,
                        "with": c.get("id"), "resident": c.get("resident"),
                        "interval": [cs, ce],
                        "message": f"{sid} 在 {c.get('id')}（住户 {c.get('resident')}）"
                                   "的同一时段已有承诺，不能再许给另一户"}
                issues.append(item)
                conflicts.append(item)

    # 前一户 -> 新户（同一住户无需赶路）
    if s is not None and e is not None:
        prev_candidates = [c for c in mine
                           if as_minutes(c.get("end")) is not None
                           and as_minutes(c.get("end")) <= s]
        if prev_candidates:
            prev = max(prev_candidates, key=lambda c: as_minutes(c.get("end")))
            prev_end = as_minutes(prev.get("end"))
            gap = s - prev_end
            if resident is not None and prev.get("resident") == resident:
                travel_checks.append({"direction": "prev_to_new", "with": prev.get("id"),
                                      "code": "same_household", "gap_minutes": gap,
                                      "required_minutes": 0})
            else:
                need = travel_minutes(travel_rows, prev.get("region"), region)
                check = {"direction": "prev_to_new", "with": prev.get("id"),
                         "from_region": prev.get("region"), "to_region": region,
                         "gap_minutes": gap, "required_minutes": need}
                if need is None:
                    check["code"] = "travel_data_missing"
                    issues.append({"severity": "error", "code": "travel_data_missing",
                                    **check,
                                    "message": f"缺少 {prev.get('region')}→{region} 的路程标准，无法确认赶得及"})
                elif gap < need:
                    item = {"severity": "error", "code": "travel_insufficient", **check,
                            "message": f"前一户 {prev.get('id')} {prev_end // 60:02d}:{prev_end % 60:02d} "
                                       f"结束后只有 {gap} 分钟，{prev.get('region')}→{region} "
                                       f"至少 {need} 分钟，来不及赶路"}
                    issues.append(item)
                    travel_checks.append(item)
                else:
                    check["code"] = "ok"
                    travel_checks.append(check)

        # 新户 -> 后一户（把新承诺插到两户之间时同样要检查）
        next_candidates = [c for c in mine
                           if as_minutes(c.get("start")) is not None
                           and as_minutes(c.get("start")) >= e]
        if next_candidates:
            nxt = min(next_candidates, key=lambda c: as_minutes(c.get("start")))
            next_start = as_minutes(nxt.get("start"))
            gap = next_start - e
            if resident is not None and nxt.get("resident") == resident:
                travel_checks.append({"direction": "new_to_next", "with": nxt.get("id"),
                                      "code": "same_household", "gap_minutes": gap,
                                      "required_minutes": 0})
            else:
                need = travel_minutes(travel_rows, region, nxt.get("region"))
                check = {"direction": "new_to_next", "with": nxt.get("id"),
                         "from_region": region, "to_region": nxt.get("region"),
                         "gap_minutes": gap, "required_minutes": need}
                if need is None:
                    check["code"] = "travel_data_missing"
                    issues.append({"severity": "error", "code": "travel_data_missing", **check,
                                   "message": f"缺少 {region}→{nxt.get('region')} 的路程标准"})
                elif gap < need:
                    item = {"severity": "error", "code": "travel_insufficient", **check,
                            "message": f"与后一户 {nxt.get('id')} 之间只有 {gap} 分钟，"
                                       f"{region}→{nxt.get('region')} 至少 {need} 分钟，来不及赶路"}
                    issues.append(item)
                    travel_checks.append(item)
                else:
                    check["code"] = "ok"
                    travel_checks.append(check)

    window_warnings: List[Dict[str, Any]] = []
    w0, w1 = as_minutes(window_from), as_minutes(window_to)
    if None not in (w0, w1, s, e) and (s < w0 or e > w1):
        window_warnings.append({"severity": "warning", "code": "outside_plan_window",
                         "message": "预约时段超出计划时间窗，需与住户/出院窗口确认"})
    warnings.extend(window_warnings)

    errors = [i["code"] for i in issues if i.get("severity") == "error"]
    return {
        "ok": not errors,
        "feasible": not errors,
        "staff": sid,
        "station": station,
        "interval": [s, e],
        "issues": issues + warnings,
        "errors": errors,
        "warnings": [w["code"] for w in warnings],
        "conflicts": conflicts,
        "travel": travel_checks,
    }


# ---------------------------------------------------------------------------
# 到户证明 / 回执核验
# ---------------------------------------------------------------------------


def proof_matches_visit(proof: dict, visit: dict) -> Dict[str, Any]:
    """核对回执与独立到户登记端证明的一致性。"""
    problems = []
    if not proof:
        return {"matched": False, "problems": ["proof_missing"]}
    if proof.get("resident") and visit.get("resident") and \
            proof.get("resident") != visit.get("resident"):
        problems.append("resident_mismatch")
    p_start = parse_minutes(proof.get("recorded_start"))
    v_start = parse_minutes(visit.get("start"))
    p_end = parse_minutes(proof.get("recorded_end"))
    v_end = parse_minutes(visit.get("end"))
    if p_start is not None and v_start is not None and p_start != v_start:
        problems.append("start_time_mismatch")
    if p_end is not None and v_end is not None and p_end != v_end:
        problems.append("end_time_mismatch")
    return {"matched": not problems, "problems": problems,
            "actual_start": p_start if p_start is not None else v_start,
            "actual_end": p_end if p_end is not None else v_end}


def attribution_of(proof: dict) -> Dict[str, Optional[str]]:
    """登记端证明给出的中立现场归属。"""
    return {"station": proof.get("station"), "staff": proof.get("staff")}
