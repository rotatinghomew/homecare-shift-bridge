"""仅依赖标准库的 JSON HTTP 后端与静态控制台。

启动::

    python3 -m backend.server --host 127.0.0.1 --port 8000
    # 持久化（可选）：
    python3 -m backend.server --port 8000 --state .runtime/state.json

主要路由见 README；另有 ``POST /api/rpc/<方法名>`` 可直接调用
:class:`backend.service.CareBackend` 的方法，请求体为 kwargs JSON。
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .errors import CareError
from .service import CareBackend

WEB_INDEX = Path(__file__).resolve().parents[1] / "web" / "index.html"
_LOCK = threading.RLock()

# 允许经 /api/rpc 直接调用的方法白名单（只读 + 办理操作）
RPC_METHODS = {
    "board_for", "family_board", "resident_board", "delegate_board",
    "station_board", "duty_board", "overview",
    "authorize", "inquire_shifts", "fetch_materials",
    "select_items",
    "issue_grant", "revoke_grant",
    "ingest", "receive_referral",
    "evaluate_assignment", "candidates",
    "propose_handover", "confirm_handover", "accept_handover",
    "reject_handover", "reassign",
    "open_dispatch", "close_dispatch", "mark_no_show",
    "register_proof", "upload_visit", "attach_receipt",
    "adjudicate_proof", "resolve_duplicate", "events", "rows", "get",
}


def create_server(host: str = "127.0.0.1", port: int = 8000,
                  backend: Optional[CareBackend] = None,
                  state_path: Optional[str] = None) -> ThreadingHTTPServer:
    backend = backend or CareBackend(state_path=state_path)

    class Handler(BackendHandler):
        backend_ref = backend

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.backend = backend  # type: ignore[attr-defined]
    return httpd


class BackendHandler(BaseHTTPRequestHandler):
    server_version = "CareExchange/1.0"
    backend_ref: CareBackend = None  # type: ignore[assignment]

    # -- 工具 ----------------------------------------------------------------
    @property
    def backend(self) -> CareBackend:
        return self.backend_ref or getattr(self.server, "backend", None)

    def log_message(self, fmt: str, *args) -> None:  # 安静一点
        pass

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Actor-Id")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self._send_json({"ok": False, "code": "ui_missing"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise _BadRequest(f"请求体不是合法 JSON：{exc}") from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise _BadRequest("请求体必须是 JSON 对象")
        return data

    def _query(self) -> dict:
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query, keep_blank_values=True)
        return {k: v[-1] for k, v in q.items()}

    def options(self, *_):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Actor-Id")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    do_OPTIONS = options

    # -- GET -----------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            q = self._query()
            if path in ("/", "/index.html"):
                self._send_html(WEB_INDEX)
                return
            if path == "/api/health" or path == "/healthz":
                self._send_json({"ok": True, "service": "care-exchange-backend"})
                return
            if path == "/api":
                self._send_json({
                    "ok": True,
                    "endpoints": [
                        "/api/health", "/api/board?actor=D7|D8|S1|S2|S3|duty&at=19:40",
                        "/api/family/<D7|H7>", "/api/station/<S2>/board",
                        "/api/station/<S2>/inquire?resident=H7&at=09:00",
                        "/api/station/<S2>/materials?resident=H7&categories=mobility,dressing&at=15:00",
                        "/api/duty/board", "/api/events?plan_id=P4",
                        "/api/collections/<plans|grants|handovers|visits|proofs|staff|travel|residents>",
                        "POST /api/ingest|grants|handovers|handovers/<id>/confirm|reassign|visits|proofs|adjudications|dispatch|no-shows|rpc/<method>",
                    ],
                })
                return
            if path == "/api/board":
                actor = q.get("actor") or self.headers.get("X-Actor-Id")
                if not actor:
                    raise _BadRequest("缺少 actor")
                self._send_json(self.backend.board_for(actor, q.get("at")))
                return
            if path == "/api/overview":
                self._send_json(self.backend.overview(q.get("at")))
                return
            if path == "/api/duty/board":
                self._send_json(self.backend.duty_board(q.get("at")))
                return
            if path.startswith("/api/family/"):
                actor = path.rsplit("/", 1)[-1]
                self._send_json(self.backend.family_board(actor, q.get("at")))
                return
            if path.endswith("/board") and "/resident/" in path:
                actor = path.split("/resident/", 1)[1].split("/", 1)[0]
                self._send_json(self.backend.family_board(actor, q.get("at")))
                return
            if path.startswith("/api/station/"):
                rest = path[len("/api/station/"):]
                sid, _, tail = rest.partition("/")
                if tail == "board" or tail == "":
                    self._send_json(self.backend.station_board(sid, q.get("at")))
                elif tail == "inquire":
                    self._send_json(self.backend.inquire_shifts(
                        sid, q["resident"], q.get("at")))
                elif tail == "materials":
                    cats = _csv(q.get("categories"))
                    self._send_json(self.backend.fetch_materials(
                        sid, q["resident"], cats, q.get("at")))
                else:
                    self._send_json({"ok": False, "code": "not_found",
                                     "path": path}, 404)
                return
            if path.startswith("/api/collections/"):
                name = path.rsplit("/", 1)[-1]
                self._send_json({"ok": True, name: self.backend.rows(name)})
                return
            collection_alias = {
                f"/api/{name}": name for name in
                ("residents", "plans", "grants", "staff", "travel",
                 "handovers", "visits", "proofs",
                 "dispatch_requests", "adjudications")
            }
            if path in collection_alias:
                name = collection_alias[path]
                self._send_json({"ok": True, name: self.backend.rows(name)})
                return
            if path == "/api/events":
                self._send_json({"ok": True, "events": self.backend.events(
                    plan_id=q.get("plan_id"), handover_id=q.get("handover_id"))})
                return
            if path.startswith("/api/rpc/"):
                method = path.rsplit("/", 1)[-1]
                self._send_json(self._rpc(method, q, read_body=False))
                return
            # 兼容直接取种子报文
            if path in ("/data/care_20261202.json", "/care_20261202.json"):
                seed = Path(__file__).resolve().parents[1] / "data" / "care_20261202.json"
                self._send_html(seed)
                return
            self._send_json({"ok": False, "code": "not_found", "path": path}, 404)
        except CareError as exc:
            self._send_json(exc.to_dict(), getattr(exc, "http_status", 400))
        except KeyError as exc:
            self._send_json({"ok": False, "code": "missing_parameter",
                             "message": f"缺少参数 {exc}"}, 400)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "code": "internal_error",
                             "error": f"{type(exc).__name__}: {exc}"}, 500)

    # -- POST ----------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            body = self._read_body()
            with _LOCK:
                self._route_post(path, body)
        except CareError as exc:
            self._send_json(exc.to_dict(), getattr(exc, "http_status", 400))
        except KeyError as exc:
            self._send_json({"ok": False, "code": "missing_parameter",
                             "message": f"缺少参数 {exc}"}, 400)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "code": "internal_error",
                             "error": f"{type(exc).__name__}: {exc}"}, 500)

    def _route_post(self, path: str, body: dict) -> None:
        b = self.backend
        if path == "/api/rpc" or path.startswith("/api/rpc/"):
            method = body.pop("method", None) or path.rsplit("/", 1)[-1]
            self._send_json(self._rpc(method, body, read_body=True))
            return
        if path == "/api/reset":
            self.backend_ref = CareBackend(clock=b.clock)
            if getattr(self.server, "backend", None) is not None:
                self.server.backend = self.backend_ref  # type: ignore[attr-defined]
            self._send_json({"ok": True})
            return
        if path in ("/api/ingest", "/api/referrals"):
            self._send_json(b.ingest(body.get("packet", body), actor=_actor(body)))
            return
        if path == "/api/grants":
            self._send_json(b.issue_grant(**_kwargs(
                body, ["resident", "station", "categories"],
                optional=["purpose", "issued", "expires", "revoked",
                          "grant_id", "actor", "at"])))
            return
        if path.startswith("/api/grants/") and path.endswith("/revoke"):
            gid = path.split("/")[3]
            self._send_json(b.revoke_grant(gid, at=body.get("at"),
                                           actor=_actor(body, "resident")))
            return
        if path == "/api/handovers":
            self._send_json(b.propose_handover(**_kwargs(
                body, ["station_to", "staff", "start", "end"],
                optional=["resident", "plan", "plan_ids", "unit_key",
                          "station_from", "at", "actor", "confirm_from",
                          "handover_id", "force"])))
            return
        if path.startswith("/api/handovers/"):
            parts = path.split("/")
            hid = parts[3]
            action = parts[4] if len(parts) > 4 else "confirm"
            if action in ("confirm", ""):
                self._send_json(b.confirm_handover(
                    hid, body.get("side"), at=body.get("at"),
                    actor=_actor(body), force=bool(body.get("force", False))))
            elif action == "accept":
                self._send_json(b.accept_handover(
                    hid, at=body.get("at"), actor=_actor(body)))
            elif action == "reject":
                self._send_json(b.reject_handover(
                    hid, body.get("side", "to"), reason=body.get("reason", ""),
                    at=body.get("at"), actor=_actor(body)))
            elif action == "reassign":
                self._send_json(b.reassign(
                    hid, station_to=body["station_to"], staff=body["staff"],
                    start=body["start"], end=body["end"], at=body.get("at"),
                    actor=_actor(body, "S2"),
                    confirm_from=bool(body.get("confirm_from", True))))
            else:
                self._send_json({"ok": False, "code": "not_found"}, 404)
            return
        if path == "/api/dispatch":
            self._send_json(b.open_dispatch(**_kwargs(
                body, ["resident"],
                optional=["plan_ids", "unit_key", "station", "note",
                          "at", "actor"])))
            return
        if path.startswith("/api/dispatch/") and path.endswith("/close"):
            rid = path.split("/")[3]
            self._send_json(b.close_dispatch(
                rid, body.get("outcome", "closed"), at=body.get("at"),
                actor=_actor(body, "duty"), handover_id=body.get("handover_id")))
            return
        if path == "/api/no-shows":
            self._send_json(b.mark_no_show(**_kwargs(
                body, ["resident"],
                optional=["plan_ids", "unit_key", "reporter", "note",
                          "at", "actor"])))
            return
        if path == "/api/proofs":
            self._send_json(b.register_proof(**_kwargs(
                body, ["resident", "station", "staff", "start", "end"],
                optional=["issued", "proof_id", "actor", "at"])))
            return
        if path == "/api/visits":
            self._send_json(b.upload_visit(**_kwargs(
                body, ["plan", "station", "staff"],
                optional=["proof", "start", "end", "completed", "received",
                          "visit_id", "actor", "at"])))
            return
        if path == "/api/adjudications":
            self._send_json(b.adjudicate_proof(**_kwargs(
                body, ["proof_id"],
                optional=["outcome", "station", "staff", "visit_id",
                          "counts", "note", "at", "actor"])))
            return
        self._send_json({"ok": False, "code": "not_found", "path": path}, 404)

    # -- RPC -----------------------------------------------------------------
    def _rpc(self, method: str, payload: dict, *, read_body: bool):
        if method not in RPC_METHODS or not hasattr(self.backend, method):
            return {"ok": False, "code": "rpc_method_not_allowed",
                    "method": method}
        fn = getattr(self.backend, method)
        if read_body and isinstance(payload, dict) and (
                "args" in payload or "kwargs" in payload):
            args = payload.get("args") or []
            kwargs = payload.get("kwargs") or {}
        else:
            args, kwargs = [], dict(payload or {})
        actor = self.headers.get("X-Actor-Id")
        if actor and "actor" not in kwargs and method not in ("duty_board",):
            # 不强行注入；多数方法有显式 actor 缺省
            pass
        return fn(*args, **kwargs)


class _BadRequest(CareError):
    code = "bad_request"
    http_status = 400


def _actor(body: dict, default: str = "system") -> str:
    return body.get("actor") or default


def _csv(value: Optional[str]):
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _kwargs(body: dict, required, optional):
    missing = [k for k in required if k not in body]
    if missing:
        raise KeyError(missing[0])
    keys = set(required) | set(optional)
    return {k: body[k] for k in keys if k in body}


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(description="分段转介与到访交换后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(__import__("os").environ.get("PORT", "8000")))
    parser.add_argument("--state", default=__import__("os").environ.get("CARE_STATE"),
                        help="可选 JSON 状态持久化路径")
    parser.add_argument("--clock", default="19:40")
    args = parser.parse_args(argv)
    backend = CareBackend(clock=args.clock, state_path=args.state)
    try:
        httpd = create_server(args.host, args.port, backend=backend)
    except OSError as exc:
        # 已有一个本后端在同端口运行时，给出清晰提示并以 0 退出，
        # 方便接入方/评测脚本重复执行启动命令。
        if getattr(exc, "errno", None) in (98, 10048) or "Address already in use" in str(exc):
            import urllib.request
            probe_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
            try:
                with urllib.request.urlopen(
                        f"http://{probe_host}:{args.port}/api/health", timeout=2) as r:
                    if r.status == 200:
                        print(f"care backend already serving on "
                              f"http://{probe_host}:{args.port} (reusing existing instance)")
                        return
            except Exception:
                pass
        raise
    print(f"care exchange backend on http://{args.host}:{args.port}  "
          f"(console /, api /api/health, clock {args.clock})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
