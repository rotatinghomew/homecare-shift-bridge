"""有状态存储：以交换报文的八个集合为核心，追加值班台处置等运行期集合。

设计要点：
* 新计划、补传回执一律按 ``id`` upsert 到原集合，随后在“原承诺”上重算视图，
  不复制已执行记录；
* 所有写操作追加一条 ``events``，作为两站答复/改派/判定的凭据链；
* 默认纯内存、从种子报文确定性启动；需要跨进程保留时可传 ``state_path``
  或调用 :meth:`Store.save`。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "care_20261202.json"

#: 交换约定中的原始集合（顺序也是报文顺序）
PACKET_COLLECTIONS = [
    "residents", "plans", "grants", "staff", "travel",
    "handover", "visits", "proofs",
]
#: 后端运行期追加的集合
RUNTIME_COLLECTIONS = [
    "dispatch_requests", "no_shows", "adjudications", "events",
]
ALL_COLLECTIONS = PACKET_COLLECTIONS + RUNTIME_COLLECTIONS


class Store:
    def __init__(self, source: Any = None, *, state_path: Optional[str | Path] = None):
        self.state_path = Path(state_path) if state_path else None
        self.data: Dict[str, List[dict]] = {name: [] for name in ALL_COLLECTIONS}
        packet = self._resolve_source(source)
        self.load_packet(packet)
        if self.state_path and self.state_path.exists():
            self.load_state_file(self.state_path)

    # -- 加载 ----------------------------------------------------------------
    def _resolve_source(self, source: Any) -> Optional[dict]:
        if source is None:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if isinstance(source, dict):
            return source
        path = Path(source)
        return json.loads(path.read_text(encoding="utf-8"))

    def load_packet(self, packet: dict) -> "Store":
        for name in PACKET_COLLECTIONS:
            rows = packet.get(name)
            if isinstance(rows, list):
                self.data[name] = [dict(row) for row in rows]
        return self

    def load_state_file(self, path: Path) -> None:
        saved = json.loads(path.read_text(encoding="utf-8"))
        for name in ALL_COLLECTIONS:
            if name in saved and isinstance(saved[name], list):
                self.data[name] = saved[name]

    def save(self, path: Optional[str | Path] = None) -> Path:
        target = Path(path) if path else self.state_path
        if target is None:
            raise ValueError("未配置 state_path，无法持久化")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(target)
        self.state_path = target
        return target

    # -- 集合访问 -------------------------------------------------------------
    def rows(self, collection: str) -> List[dict]:
        return self.data.setdefault(collection, [])

    def get(self, collection: str, row_id: str) -> Optional[dict]:
        return next((r for r in self.rows(collection) if r.get("id") == row_id), None)

    def require(self, collection: str, row_id: str) -> dict:
        row = self.get(collection, row_id)
        if row is None:
            from .errors import NotFoundError
            raise NotFoundError(f"{collection} 中找不到 {row_id}",
                                collection=collection, id=row_id)
        return row

    def upsert(self, collection: str, row: dict) -> tuple[dict, bool]:
        """按 id 插入或合并更新，返回 ``(row, inserted)``。"""
        if not row.get("id"):
            row["id"] = self.new_id(self._prefix_for(collection), collection)
        existing = self.get(collection, row["id"])
        if existing is None:
            self.rows(collection).append(row)
            return row, True
        existing.update({k: v for k, v in row.items() if v is not None or k not in existing})
        return existing, False

    @staticmethod
    def _prefix_for(collection: str) -> str:
        return {
            "residents": "H", "plans": "P", "grants": "G", "staff": "A",
            "travel": "T", "handover": "X", "visits": "V", "proofs": "K",
            "dispatch_requests": "R", "no_shows": "N",
            "adjudications": "J", "events": "E",
        }.get(collection, "ID")

    def new_id(self, prefix: str, collection: str) -> str:
        max_n = 0
        pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
        for row in self.rows(collection):
            m = pattern.match(str(row.get("id", "")))
            if m:
                max_n = max(max_n, int(m.group(1)))
        # 事件可能高频，附加两位避免同分钟冲突
        candidate = f"{prefix}{max_n + 1}"
        while self.get(collection, candidate):
            max_n += 1
            candidate = f"{prefix}{max_n + 1}"
        return candidate

    # -- 事件凭据 -------------------------------------------------------------
    def event(self, action: str, *, at: Optional[str] = None, actor: str = "system",
              target: Optional[str] = None, **detail) -> dict:
        row = {
            "id": self.new_id("E", "events"),
            "at": at,
            "actor": actor,
            "action": action,
            "target": target,
            "detail": detail,
        }
        self.rows("events").append(row)
        if self.state_path:
            self.save()
        return row

    def query_events(self, target: Optional[str] = None,
                    plan_id: Optional[str] = None,
                    handover_id: Optional[str] = None) -> List[dict]:
        result = self.rows("events")
        if target:
            result = [e for e in result if e.get("target") == target]
        if plan_id:
            result = [e for e in result
                      if (e.get("detail") or {}).get("plan") == plan_id
                      or plan_id in ((e.get("detail") or {}).get("plan_ids") or [])]
        if handover_id:
            result = [e for e in result
                      if (e.get("detail") or {}).get("handover_id") == handover_id
                      or e.get("target") == handover_id]
        return list(result)

    # -- 批量汇入 -------------------------------------------------------------
    def ingest(self, packet: Dict[str, Iterable[dict]]) -> Dict[str, Any]:
        summary: Dict[str, Any] = {"inserted": {}, "updated": {}}
        for collection, rows in packet.items():
            if collection not in ALL_COLLECTIONS or not isinstance(rows, list):
                continue
            ins = upd = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                _, inserted = self.upsert(collection, dict(row))
                ins += inserted
                upd += not inserted
            summary["inserted"][collection] = ins
            summary["updated"][collection] = upd
        if self.state_path:
            self.save()
        return summary
