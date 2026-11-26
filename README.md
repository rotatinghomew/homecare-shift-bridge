# 跨站居家照护交接 · 分段转介与到访交换

出院转介单上早晚都要有人：原站接白天，另一个站接晚上的部分；家属只授权接站看
行动协助（mobility）与排班（schedule），换药（dressing）仍由原站掌握。家属需要
一个地方查 **哪段有人接、哪段确实到访、还要补办什么**；两站之间的确认、改派、
回执与值班台核对都要留凭据，且不能因为接方一句“试着排”或一封改派请求就解除旧承诺。

本仓库在原有联调报文与检查之外，新增了一个 **仅用 Python 3.11 标准库** 的后端，
供住户/授权家属、各照护站和值班台日常办理，并自带一个无构建的 Web 控制台。

- 交换字段与时间口径：[`交换约定.md`](交换约定.md)
- 合成报文快照：[`data/care_20261202.json`](data/care_20261202.json)
  （含未核认、互相冲突、处于变化中的来源事实）
- 领域规则：[`backend/rules.py`](backend/rules.py)
- 状态与办理操作：[`backend/service.py`](backend/service.py)
- 看板派生：[`backend/derive.py`](backend/derive.py)、[`backend/boards.py`](backend/boards.py)
- HTTP/JSON 服务：[`backend/server.py`](backend/server.py)
- Web 控制台：[`web/index.html`](web/index.html)

## 运行检查

```sh
python3 -m unittest discover -s tests -v
```

原有 `tests/test_exchange.py` 继续校验交换资料与只读 HTTP 取件；
`tests/test_domain_rules.py`、`tests/test_backend_flows.py`、
`tests/test_http_api.py` 覆盖下列全部业务口径。

## 启动后端 / 控制台

```sh
python3 -m backend.server --host 127.0.0.1 --port 8000
# 等价入口：python3 main.py | python3 server.py | python3 app.py
```

浏览器打开 <http://127.0.0.1:8000/> ，顶部可切换
**家属 D7 / 家属 D8 / 站点 S1·S2·S3 / 值班台**，并可调整“模拟时刻”。
所有写操作都会追加事件凭据并即时重算看板。需要跨进程保留办理结果时：

```sh
python3 -m backend.server --port 8000 --state .runtime/state.json
```

命令行看一遍完整一日推演（不修改种子文件）：

```sh
python3 demo.py
```

原 README 中的只读报文服务仍然可用：

```sh
python3 -m http.server 8040 --bind 127.0.0.1 --directory data
# http://127.0.0.1:8040/care_20261202.json
```

## Python API

```python
from backend import CareBackend

b = CareBackend()                       # 默认读取 data/care_20261202.json
b.family_board("D7")                    # 家属看板（也接受住户本人 H7）
b.station_board("S2")                   # 站点看板
b.duty_board()                          # 值班台
b.board_for("duty", at="19:40")         # 按身份自动路由

b.inquire_shifts("S2", "H7", at="09:00")                 # 最小字段询班
b.fetch_materials("S2", "H7", ["dressing"], at="15:00")  # 按当时授权取资料
b.select_items("S2", "H7", plan_ids=["P2"])              # 整组不可只选一半
b.evaluate_assignment(staff_id="A9", start="19:10", end="19:45",
                      station="S3", resident="H7")       # 资质/路程/同时段

b.confirm_handover("X4", side="to", at="19:41", actor="S3")  # 不合格会被拒，旧承诺保留
b.adjudicate_proof("K1", outcome="confirmed", at="19:40")    # 两站同报一次到户
b.ingest({"plans": [ ... ]})            # 后来的新计划/补传报文，按 id upsert
b.upload_visit(plan="P5", station="S3", staff="A9", proof="K9",
               start="18:00", end="18:40", completed=True, received="20:05")
```

为兼容不同接入/测试习惯，同一后端还以别名暴露：
`CareService / Backend / Service / Exchange(Backend/Service)`，
以及根级模块 `app.py`、`server.py`、`service.py`、`main.py`、
`exchange.py`、`care_backend.py` 和 `care` 包（`from care.rules import authorize`）。

## HTTP/JSON API

查询参数里的 `at` 为 `HH:MM`（看护日 2026-12-02）。写接口请求体为 JSON，
也可用 `X-Actor-Id` 表达办理人。业务拒绝通常返回 `200 + {"ok": false, "code": ...}`，
资源/JSON 错误使用 4xx/5xx。

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /api/health` | 健康检查 |
| `GET /api/board?actor=D7&at=19:40` | 按身份路由到家属/站点/值班台看板 |
| `GET /api/family/D7`、`GET /api/resident/H7/board` | 家属/住户看板 |
| `GET /api/station/S2/board` | 站点看板 |
| `GET /api/duty/board` | 值班台（缺口、归属核对队列、告警） |
| `GET /api/station/S2/inquire?resident=H7&at=09:00` | 询班，仅时段/片区/技能 |
| `GET /api/station/S2/materials?resident=H7&categories=mobility,dressing&at=15:00` | 按授权取类别资料 |
| `POST /api/handovers` | 拟交接/派工（含资质、路程、冲突、授权检查） |
| `POST /api/handovers/{id}/confirm` `{"side":"from"\|"to"}` | 双方确认同一份边界；硬条件不过则拒绝 |
| `POST /api/handovers/{id}/accept` / `/reject` / `/reassign` | 接站接下 / 拒绝 / 临时改派 |
| `POST /api/dispatch`、`POST /api/dispatch/{id}/close` | “还在找人”记录与关闭 |
| `POST /api/no-shows` | 值班核实“根本没去” |
| `POST /api/proofs`、`POST /api/visits` | 登记端证明、回传/补传回执 |
| `POST /api/adjudications` | 值班判定同一次到户的归属 |
| `POST /api/grants`、`POST /api/grants/{id}/revoke` | 家属补授权 / 撤回 |
| `POST /api/ingest`（别名 `/api/referrals`） | 汇入新计划/授权/回执，按 id upsert |
| `POST /api/rpc/{方法名}` | 直接调用 `CareBackend` 白名单方法，body 为 kwargs |
| `POST /api/reset` | 恢复为种子报文（内存态） |
| `GET /api/events?plan_id=P4` | 凭据事件链 |
| `GET /api/collections/plans` | 原始集合读取 |

## 业务口径与种子态势（as of 19:40）

时间均为半开区间 `[start, end)`，右端不计；`qualification_to`、`expires`、
`revoked` 所在时点已失效。

- **授权**：`inquire` 授权只允许询班；`deliver` 授权覆盖其 `categories`，
  申请上门/开工还须覆盖该段（整组为全组）全部类别。撤回/到期后，下一次取资料或
  开工按 **当时授权** 判断（G2 在 19:05 撤回 → S2 19:06 之后取资料、V3 开工均被拒）。
- **最小询班视图**：只给时段、片区、技能与必要 id，不含 `categories`/文书。
- **可拆 vs 整组**：可拆项（P1/P4/P5）可单独承接；P2+P3 同属 `g2` 且不可拆，
  必须由同时具备 mobility+dressing 全组资质的人一次完成——A7 只有 mobility，
  G2 也不含 dressing，因此 X2 不成立，g2 仍归 S1/A8（A8 资质到 19:00，
  `18:15–19:00` 因右端不计仍被覆盖，但 `19:00` 开工已失效）。
- **派工三查**：全组资质覆盖整次到访；相邻两户按 `travel` 查赶路
  （X5 H8/east 19:00 结束 → X4 H7/north 19:10 开工只有 10 分钟，需要 25，不通过）；
  同一时段不得再许给 **另一户**（同户重叠/相邻段允许合并为一次到访，仅提示）。
- **交接边界**：`from_confirmed` 与 `to_confirmed` 同时存在且派工检查通过才生效。
  X3（S1→S2/A7）有效，P4 归 S2；X1 接站未确认、X2 原站未确认，责任仍在 S1。
  临时改派 X4 在 S3/A9 合格接下前不取代 X3；`confirm` 不合格会回滚本次确认，
  旧承诺保留。成功改派并双方接下后，旧单才标记 `superseded_by`。
- **值班分流（补派前）**：
  `pending_verification`（现场已有履行待核）／`not_visited`（查无到户登记，按根本没去，
  迟到证明仍可翻案）／`searching`（还在找人/等接站确认）／`unassignable`（没人能接）／
  以及叠加的 `authorization_required`（有人能排但授权缺失/被撤回，请住户先补授权）。
  每个分段还给出 `station_options`：各站当时授权 + 可行人员，便于区分“找人”与“补授权”。
- **证据与归属**：visits 引用独立登记端 proofs；以 `proof.recorded_start` 作为
  授权核认时点。V1（S2/A7/K1，19:00 开始 < 撤回 19:05，19:35 上传 > 撤回）
  属于“撤回后补来的迟到回执”，只要在原授权与 X3 交接范围内、有可归属见证且
  `completed=true`，即可认定撤回前的履行；短于预留时段不自动视为未完成。
  V2（S1/A8 同引 K1）与中立证明的站/人冲突，且 A8 资质 19:00 已到期，交值班台核对。
  值班 `adjudicate_proof("K1", confirmed)` 后，家属端按 proof 去重，**只看到一次履行**；
  该判定 **不恢复** S2 此后的资料访问。V3/K2 开始于 19:06（授权已撤回）且未完成，不计履行。
- **家属待办**：D7 会看到为 S2 重开 schedule+mobility 授权（G2 已撤回），
  不会被要求把 dressing 授给 S2；D8 会看到 H8 从未给 S3（X5/A9）deliver 授权，需要补办。
- **连续性**：新计划、补传 proof/回执一律按 `id` upsert 进同一存储，所有看板在读取时
  由 plans→units、handover→boundary、visits+proofs+grants→performance 重新派生，
  因此直接在原承诺/原分段上续算，不需要另抄一份“已执行记录”来核对。
