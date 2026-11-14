"""The local web UI: a 127.0.0.1 HTTP server that relays the user's view and actions to relayd."""

from __future__ import annotations

import hmac
import http.server
import json
import logging
import os
import secrets
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from mz_relay.errors import DaemonUnavailable, RelayError
from mz_relay.ids import new_id

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 65_536
# A comment line this often keeps proxies and the browser from dropping an idle stream.
KEEPALIVE_S = 15
STATE_LOG_LIMIT = 100
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}
ERROR_STATUS = {
    "not_found": HTTPStatus.NOT_FOUND,
    "not_held": HTTPStatus.CONFLICT,
    "not_allowed": HTTPStatus.FORBIDDEN,
}
NOT_SIGNED_IN = (
    "<!doctype html><meta charset=utf-8><title>mz-relay</title>"
    "<p>Open the address that <code>mz-relay ui</code> printed in its terminal.</p>"
)


class RelayCalls(Protocol):
    def call(
        self, op: str, args: Mapping[str, object], *, timeout_s: float | None = None
    ) -> dict[str, object]: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class UiConfig:
    """What the server needs: relayd connections, the session token and the static files."""

    connect: Callable[[], RelayCalls]
    token: str
    static: Traversable

    def __post_init__(self) -> None:
        if len(self.token) < 32:
            raise ValueError("token must be at least 32 characters")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def bundled_static() -> Traversable:
    return resources.files("mz_relay") / "ui"


class _Refused(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class UiServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, cfg: UiConfig, port: int = 0) -> None:
        if not 0 <= port <= 65535:
            raise ValueError(f"port must be in 0..65535, got {port}")
        self.cfg = cfg
        super().__init__(("127.0.0.1", port), _Handler)
        bound = self.server_address[1]
        # Anything else in Host is a DNS-rebinding page or a misdirected request.
        self.allowed_hosts = frozenset({f"127.0.0.1:{bound}", f"localhost:{bound}"})
        # Cookies ignore the port, so the name carries it; another local server gets no match.
        self.cookie = f"mzr_ui_{bound}"

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.cfg.token}"


class _Handler(http.server.BaseHTTPRequestHandler):
    server: UiServer
    server_version = "mz-relay-ui"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        # The default line holds the request target, and with it the token on sign-in.
        log.debug("%s %s", self.command, urlsplit(self.path).path)

    def do_GET(self) -> None:
        self._serve(self._get)

    def do_POST(self) -> None:
        self._serve(self._post)

    def _serve(self, route: Callable[[str, dict[str, list[str]]], None]) -> None:
        target = urlsplit(self.path)
        try:
            self._check_host()
            route(target.path, parse_qs(target.query))
        except _Refused as exc:
            log.info("refused %s %s: %s", self.command, target.path, exc.message)
            self._json(exc.status, {"error": {"code": "refused", "message": exc.message}})
        except DaemonUnavailable as exc:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, _error_body(exc))
        except RelayError as exc:
            self._json(ERROR_STATUS.get(exc.code, HTTPStatus.BAD_REQUEST), _error_body(exc))
        except (BrokenPipeError, ConnectionResetError):
            log.debug("browser left during %s %s", self.command, target.path)

    def _check_host(self) -> None:
        if self.headers.get("Host") not in self.server.allowed_hosts:
            raise _Refused(HTTPStatus.FORBIDDEN, "unexpected Host header")

    def _signed_in(self) -> bool:
        raw = self.headers.get("Cookie")
        if not raw:
            return False
        try:
            jar = SimpleCookie(raw)
        except CookieError:
            return False
        morsel = jar.get(self.server.cookie)
        return morsel is not None and hmac.compare_digest(
            morsel.value.encode(), self.server.cfg.token.encode()
        )

    def _get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/" and "token" in query:
            self._sign_in(query["token"][0])
            return
        if not self._signed_in():
            if path == "/":
                self._send(HTTPStatus.UNAUTHORIZED, NOT_SIGNED_IN.encode(), CONTENT_TYPES[".html"])
                return
            raise _Refused(HTTPStatus.UNAUTHORIZED, "not signed in")
        if path == "/":
            self._static("index.html")
        elif path.startswith("/static/"):
            self._static(path.removeprefix("/static/"))
        elif path == "/api/state":
            self._json(HTTPStatus.OK, self._state())
        elif path == "/api/log":
            args: dict[str, object] = {"limit": _limit(_one(query, "limit"))}
            context = _one(query, "context")
            if context:
                args["context_id"] = context
            self._json(HTTPStatus.OK, self._relay("log", args))
        elif path == "/api/message":
            source, msg_id = _one(query, "source"), _one(query, "id")
            self._json(HTTPStatus.OK, self._relay("message", {"source": source, "id": msg_id}))
        elif path == "/api/events":
            self._events()
        else:
            raise _Refused(HTTPStatus.NOT_FOUND, f"no route {path}")

    def _post(self, path: str, query: dict[str, list[str]]) -> None:
        if not self._signed_in():
            raise _Refused(HTTPStatus.UNAUTHORIZED, "not signed in")
        host = self.headers.get("Host")
        # Browsers send Origin on every cross-site POST; a page elsewhere cannot fake ours.
        if self.headers.get("Origin") != f"http://{host}":
            raise _Refused(HTTPStatus.FORBIDDEN, "missing or foreign Origin")
        body = self._read_json()
        if path == "/api/send":
            args = {
                "id": new_id(),
                "to": body.get("to"),
                "kind": body.get("kind", "request"),
                "body": body.get("body"),
            }
            if body.get("in_reply_to"):
                args["in_reply_to"] = body["in_reply_to"]
            self._json(HTTPStatus.OK, self._relay("send_as_user", args))
        elif path == "/api/decide":
            args = {k: body[k] for k in ("source", "id", "decision", "reason") if k in body}
            result = self._relay("decide", args)
            log.info("user decided %s on %s from the UI", args.get("decision"), args.get("id"))
            self._json(HTTPStatus.OK, result)
        elif path == "/api/inbox":
            self._json(HTTPStatus.OK, self._relay("user_inbox", {}))
        elif path == "/api/ack":
            self._json(HTTPStatus.OK, self._relay("user_ack", {"id": body.get("id")}))
        else:
            raise _Refused(HTTPStatus.NOT_FOUND, f"no route {path}")

    def _sign_in(self, offered: str) -> None:
        if not hmac.compare_digest(offered.encode(), self.server.cfg.token.encode()):
            raise _Refused(HTTPStatus.UNAUTHORIZED, "wrong token")
        log.info("browser signed in to the UI")
        # The redirect drops the token from the address bar and the history.
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header(
            "Set-Cookie",
            f"{self.server.cookie}={self.server.cfg.token}; HttpOnly; SameSite=Strict; Path=/",
        )
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()

    def _read_json(self) -> dict[str, object]:
        if self.headers.get_content_type() != "application/json":
            raise _Refused(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "body must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise _Refused(HTTPStatus.BAD_REQUEST, "bad Content-Length") from exc
        if not 0 <= length <= MAX_BODY_BYTES:
            raise _Refused(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"body over {MAX_BODY_BYTES} bytes")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise _Refused(HTTPStatus.BAD_REQUEST, "body is not JSON") from exc
        if not isinstance(body, dict):
            raise _Refused(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
        return body

    def _relay(self, op: str, args: Mapping[str, object]) -> dict[str, object]:
        with closing(self.server.cfg.connect()) as relay:
            return relay.call(op, args)

    def _state(self) -> dict[str, object]:
        with closing(self.server.cfg.connect()) as relay:
            return {
                "status": relay.call("status", {}),
                "held": relay.call("held", {}),
                "log": relay.call("log", {"limit": STATE_LOG_LIMIT}),
                "head": relay.call("watch", {})["last_seq"],
            }

    def _events(self) -> None:
        last = self.headers.get("Last-Event-ID")
        since = int(last) if last and last.isdigit() else None
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self._security_headers()
        self.end_headers()
        try:
            self._stream(since)
        except RelayError as exc:
            # The headers are out, so the failure goes down the stream and the browser retries.
            log.warning("event stream ended: %s", exc.code)
            self._event("error", _error_body(exc), since or 0)

    def _stream(self, since: int | None) -> None:
        # One relayd connection per stream: watch blocks it while nothing happens.
        with closing(self.server.cfg.connect()) as relay:
            if since is None:
                since = int(str(relay.call("watch", {})["last_seq"]))
                self._event("hello", {"last_seq": since}, since)
            while True:
                page = relay.call(
                    "watch", {"since": since, "timeout_s": KEEPALIVE_S}, timeout_s=KEEPALIVE_S + 5
                )
                if page.get("gap"):
                    since = int(str(page["last_seq"]))
                    self._event("reset", {"last_seq": since}, since)
                    continue
                events = page.get("events")
                if not isinstance(events, list) or not events:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                for event in events:
                    since = int(event["seq"])
                    self._event(str(event["kind"]), event, since)

    def _event(self, name: str, data: Mapping[str, object], seq: int) -> None:
        frame = f"id: {seq}\nevent: {name}\ndata: {json.dumps(data)}\n\n"
        self.wfile.write(frame.encode())
        self.wfile.flush()

    def _static(self, name: str) -> None:
        # Only plain names in the bundled folder; no slashes, so no path can climb out of it.
        suffix = name[name.rfind(".") :] if "." in name else ""
        if "/" in name or name.startswith(".") or suffix not in CONTENT_TYPES:
            raise _Refused(HTTPStatus.NOT_FOUND, f"no static file {name}")
        item = self.server.cfg.static / name
        if not item.is_file():
            raise _Refused(HTTPStatus.NOT_FOUND, f"no static file {name}")
        self._send(HTTPStatus.OK, item.read_bytes(), CONTENT_TYPES[suffix])

    def _json(self, status: HTTPStatus, payload: Mapping[str, object]) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)


def _one(query: Mapping[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None


def _limit(raw: str | None) -> int:
    if raw is None:
        return STATE_LOG_LIMIT
    if not raw.isdigit():
        raise _Refused(HTTPStatus.BAD_REQUEST, f"limit must be a whole number, got {raw!r}")
    return int(raw)


def _error_body(exc: RelayError) -> dict[str, object]:
    return {"error": {"code": exc.code, "message": exc.message}}


def write_launch_page(directory: Path, url: str) -> Path:
    """A 0600 page that forwards to url; opening it keeps the token out of the browser's argv."""
    page = directory / f"ui-{os.getpid()}.html"
    html = f'<!doctype html><meta charset=utf-8><meta http-equiv="refresh" content="0;url={url}">'
    fd = os.open(page, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(html)
    return page
