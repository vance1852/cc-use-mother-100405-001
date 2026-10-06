"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .review import ReviewService
from .service import DomainService
from .storage import Database


def _json_default(value: Any) -> Any:
    if hasattr(value, "__dict__"):
        return value.__dict__
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"不可序列化的类型: {type(value)!r}")


def dumps(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      default=_json_default).encode("utf-8")


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    review = service if isinstance(service, ReviewService) else None
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        status, payload = _review_routes(review, method, parsed.path, query, body, actor_id)
        if status is not None:
            return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _review_routes(review: ReviewService | None, method: str, path: str,
                   query: dict[str, list[str]], body: dict[str, Any],
                   actor_id: str) -> tuple[int | None, dict[str, Any]]:
    """分派研究组合审议领域的路由；非审议路径返回 None。"""

    if review is None:
        return None, {}

    def call(action: str, **extra: Any) -> tuple[int, dict[str, Any]]:
        receipt = getattr(review, action)(actor_id=actor_id, **{**body, **extra})
        return (200 if getattr(receipt, "replayed", False) else 201), receipt.__dict__

    if method == "POST":
        static_posts = {
            "/teams": "register_team",
            "/collaborations": "declare_collaboration",
            "/facilities": "register_facility",
            "/evidence": "register_evidence",
            "/evidence/versions": "new_evidence_version",
            "/questions": "register_question",
            "/rounds": "open_round",
            "/proposals": "submit_proposal",
            "/routes": "add_route",
            "/portfolios": "propose_portfolio",
        }
        if path in static_posts:
            return call(static_posts[path])
        match = re.fullmatch(r"/rounds/([^/]+)/close", path)
        if match:
            return call("close_round", round_id=match.group(1))
        match = re.fullmatch(r"/proposals/([^/]+)/withdraw", path)
        if match:
            return call("withdraw_proposal", proposal_id=match.group(1))
        match = re.fullmatch(r"/proposals/([^/]+)/merge", path)
        if match:
            return call("merge_routes", source_proposal_id=match.group(1))
        match = re.fullmatch(r"/proposals/([^/]+)/reviewers", path)
        if match:
            return call("assign_reviewer", proposal_id=match.group(1))
        match = re.fullmatch(r"/proposals/([^/]+)/recusals", path)
        if match:
            return call("recuse_reviewer", proposal_id=match.group(1))
        match = re.fullmatch(r"/proposals/([^/]+)/scores", path)
        if match:
            return call("submit_score", proposal_id=match.group(1))
        match = re.fullmatch(r"/proposals/([^/]+)/decisions", path)
        if match:
            return call("decide_proposal", proposal_id=match.group(1))
        match = re.fullmatch(r"/proposals/([^/]+)/reconsiderations", path)
        if match:
            return call("reconsider", proposal_id=match.group(1))
        match = re.fullmatch(r"/portfolios/([^/]+)/countersign/start", path)
        if match:
            return call("start_countersign", portfolio_id=match.group(1))
        match = re.fullmatch(r"/portfolios/([^/]+)/countersign", path)
        if match:
            return call("countersign", portfolio_id=match.group(1))
        return None, {}

    if method == "GET":
        match = re.fullmatch(r"/teams/([^/]+)", path)
        if match:
            return 200, review.get_team(match.group(1)).__dict__
        match = re.fullmatch(r"/evidence/([^/]+)", path)
        if match:
            return 200, {"items": [item.__dict__ for item in review.get_evidence(match.group(1))]}
        match = re.fullmatch(r"/questions/([^/]+)", path)
        if match:
            return 200, review.get_question(match.group(1)).__dict__
        match = re.fullmatch(r"/facilities/([^/]+)", path)
        if match:
            return 200, review.get_facility(match.group(1)).__dict__
        match = re.fullmatch(r"/rounds/([^/]+)", path)
        if match:
            return 200, review.get_round(match.group(1)).__dict__
        match = re.fullmatch(r"/proposals/([^/]+)", path)
        if match:
            return 200, review.get_proposal(match.group(1)).__dict__
        match = re.fullmatch(r"/proposals/([^/]+)/explanation", path)
        if match:
            return 200, review.explain_decision(match.group(1))
        match = re.fullmatch(r"/proposals/([^/]+)/decisions", path)
        if match:
            return 200, {"items": [item.__dict__ for item in review.list_decisions(match.group(1))]}
        match = re.fullmatch(r"/proposals/([^/]+)/assignments", path)
        if match:
            return 200, {"items": [item.__dict__ for item in review.list_assignments(match.group(1))]}
        match = re.fullmatch(r"/rounds/([^/]+)/snapshot", path)
        if match:
            return 200, review.get_snapshot(match.group(1)).__dict__
        match = re.fullmatch(r"/portfolios/([^/]+)", path)
        if match:
            return 200, review.get_portfolio(match.group(1)).__dict__
        match = re.fullmatch(r"/portfolios/([^/]+)/countersign", path)
        if match:
            return 200, review.get_countersign(match.group(1)).__dict__
        if path == "/dependency-graph":
            round_id = query.get("round_id", [None])[0]
            return 200, review.dependency_graph(round_id)
        if path == "/portfolio-comparison":
            a = query.get("a", [""])[0]
            b = query.get("b", [""])[0]
            if not a or not b:
                raise ValidationError("必须提供组合编号 a 与 b")
            return 200, review.compare_portfolios(a, b)
        return None, {}
    return None, {}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = dumps(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动原始创新研究组合审议服务")
    parser.add_argument("--database", default="review_service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = ReviewService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
