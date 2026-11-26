#!/usr/bin/env python3
"""分段转介与到访交换 —— 一日联调演示（只读种子 + 内存办理，不改数据文件）。

运行：``python3 demo.py``
"""
from __future__ import annotations

import json

from backend import CareBackend

LINE = "─" * 78


def show(title, value):
    print("\n" + "=" * 78)
    print(title)
    print(LINE)
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main():
    b = CareBackend(clock="19:40")

    print("""
出院转介：H7 早晚都要有人上门。S1 原站接白天；S2 愿接晚班，但家属只授权
行动协助(mobility)+排班(schedule)，换药(dressing)仍留在 S1。
下面按 2026-12-02 的合成报文逐段核对。""")

    # 1) S2 初次询班：最小字段
    inq = b.inquire_shifts("S2", "H7", at="09:00")
    show("① S2 初次询班（G1 inquire，只见时段/片区/技能）",
         [{"时段": f"{s['window_from']}-{s['window_to']}", "片区": s["region"],
           "技能": s["skills"], "整组": s["bundled"], "项目": s["plan_ids"]}
          for s in inq["shifts"]])

    # 2) 取资料：类别授权
    show("② S2 15:00 取 schedule+mobility（G2 deliver，允许）",
         b.fetch_materials("S2", "H7", ["schedule", "mobility"], at="15:00"))
    denied = b.fetch_materials("S2", "H7", ["dressing"], at="15:00")
    show("③ S2 试取 dressing（G2 不含换药类别，拒绝；换药留 S1/G3）",
         {k: denied[k] for k in ("allowed", "code", "missing_categories", "message")})

    # 3) 19:40 总览：分段状态
    rows = b.duty_board("19:40")["segments"]
    show("④ 19:40 值班台：具体缺的时段/项目与分流",
         [{"段": s["unit_key"], "时间": f"{s['window_from']}-{s['window_to']}",
           "项目": s["plan_ids"], "责任站": s["responsible_station"],
           "分流": s["triage"], "值班状态": s["duty_state_label"],
           "可排人员(责任站)": s["feasible_candidate_ids"],
           "可承接站": s["coverable_stations"],
           "授权卡住的站": [g["station"] for g in s["auth_gated_stations"]]}
          for s in rows])

    # 4) 交接单检查
    d = b.deriver("19:40")
    show("⑤ 交接凭据：X1 接站未确认 / X2 原站未确认且 A7 无换药全组资质 / "
         "X4 改派 S3 但 A9 赶路不及且未接下",
         [{"单": x["id"], "段": x["plan"],
           "边界": f"{x['station_from']}->{x['station_to']}/{x['staff']} "
                   f"{x['service_start']}-{x['service_end']}",
           "双方确认": v["mutual_confirmed"], "生效": v["binding"],
           "检查错误": v["evaluation"]["errors"]}
          for x in b.rows("handover") for v in [d.handover_view(x)]])

    # 5) 证据核对队列
    show("⑥ 两站同报 K1：值班台证据对照（V1=S2/A7 与登记一致；V2=S1/A8 冲突，"
         "且 A8 资质 19:00 到期）",
         b.duty_board("19:40")["attribution_queue"])

    # 6) 家属 D7 看待办与履行（判定前）
    fam = b.family_board("D7")
    show("⑦ 家属 D7：待补办（撤回的 G2 需重开；不会要求把 dressing 给 S2）",
         fam["todos"])

    # 7) 值班判定 K1
    b.adjudicate_proof("K1", outcome="confirmed", at="19:40",
                       note="K1 中立登记=S2/A7，授权时点 19:00<撤回 19:05，X3 范围内")
    fam = b.family_board("D7")
    show("⑧ 判定 K1 后家属端：同一次到户只显示一次履行，P4=fulfilled",
         {"performances": fam["performances"],
          "P4": {k: next(s for s in fam["segments"] if s["unit_key"] == "P4")[k]
                 for k in ("triage", "settled_performances")}})

    # 8) 迟到回执不恢复权限
    after = b.fetch_materials("S2", "H7", ["schedule", "mobility"], at="19:41")
    show("⑨ 判定后 S2 再取资料（撤回不恢复之后权限，仍拒绝）",
         {k: after[k] for k in ("allowed", "code", "message")})

    # 10) D8 待授权
    show("⑩ 家属 D8：H8 未给 S3 授权，X5/A9 虽已排，仍需补授权",
         b.family_board("D8")["todos"])

    # 11) 新计划续办
    b.ingest({"plans": [{
        "id": "P6", "resident": "H7", "referral": "C9",
        "window_from": "20:10", "window_to": "20:40", "skill": "mobility",
        "group": "g6", "splittable": True,
        "categories": ["schedule", "mobility"]}]})
    p6 = next(s for s in b.duty_board("19:46")["segments"] if s["unit_key"] == "P6")
    show("⑪ 后来转介的新计划 P6 直接在同一后端续算（无需另抄执行记录）：S3/A9 可承接",
         {"值班状态": p6["duty_state"], "可承接站": p6["coverable_stations"],
          "S2选项": next(o for o in p6["station_options"] if o["station"] == "S2")})

    print("\n演示完成。启动可交互控制台：python3 -m backend.server --port 8000\n")


if __name__ == "__main__":
    main()
