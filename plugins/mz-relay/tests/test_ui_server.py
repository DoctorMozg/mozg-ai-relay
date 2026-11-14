"""The local web UI server: its guards, its JSON routes onto relayd and its event stream."""

from __future__ import annotations

import http.client
import json
import logging
import os
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from mz_relay.cli import Terminal, main
from mz_relay.client import RelayClient
from mz_relay.errors import RelayError
from mz_relay.ids import new_id
from mz_relay.paths import Paths
from mz_relay.roster import USER_NAME
from mz_relay.ui_server import (
    MAX_BODY_BYTES,
    UiConfig,
    UiServer,
    bundled_static,
    new_token,
    write_launch_page,
)
from relay_rig import InThread, ServiceRig

BODY = "please look at src/app.py"
STREAM_S = 5.0

Headers = dict[str, str]


@dataclass
class Ui:
    """A UI server on a free port in front of an in-process relayd, plus agents kept online."""

    server: UiServer
    paths: Paths
    clients: list[RelayClient] = field(default_factory=list)

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server.port}"

    @property
    def cookie(self) -> str:
        return f"{self.server.cookie}={self.server.cfg.token}"

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Headers | None = None,
    ) -> http.client.HTTPResponse:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=STREAM_S)
        conn.request(method, path, body=body, headers={"Host": self.host, **(headers or {})})
        return conn.getresponse()

    def get(self, path: str, headers: Headers | None = None) -> http.client.HTTPResponse:
        return self.request("GET", path, headers={"Cookie": self.cookie, **(headers or {})})

    def post(
        self, path: str, payload: object, headers: dict[str, str | None] | None = None
    ) -> http.client.HTTPResponse:
        """A same-origin JSON POST; a header set to None is left out."""
        merged: dict[str, str | None] = {
            "Cookie": self.cookie,
            "Origin": f"http://{self.host}",
            "Content-Type": "application/json",
            **(headers or {}),
        }
        sent = {name: value for name, value in merged.items() if value is not None}
        return self.request("POST", path, json.dumps(payload).encode(), sent)

    def agent(self, name: str) -> tuple[RelayClient, str]:
        client = RelayClient(self.paths.socket)
        client.connect(client="mcp")
        self.clients.append(client)
        reg = client.call(
            "register",
            {"name": name, "harness": "opencode", "cwd": str(self.paths.config_dir), "pid": 1},
        )
        return client, str(reg["token"])

    def agent_send(self, name: str, to: str) -> str:
        client, token = self.agent(name)
        args = {"token": token, "id": new_id(), "to": to, "kind": "request", "body": BODY}
        return str(client.call("send", args)["id"])

    def query(self, op: str, args: dict[str, object]) -> dict[str, object]:
        with closing(cli_client(self.paths)) as client:
            return client.call(op, args)


def cli_client(paths: Paths) -> RelayClient:
    client = RelayClient(paths.socket)
    client.connect(client="cli")
    return client


def read_json(resp: http.client.HTTPResponse) -> dict[str, object]:
    data = json.loads(resp.read())
    assert isinstance(data, dict)
    return data


def read_event(resp: http.client.HTTPResponse) -> dict[str, str]:
    """The next SSE frame as its fields; comment lines (keepalives) are skipped."""
    frame: dict[str, str] = {}
    while True:
        line = resp.fp.readline().decode().rstrip("\n")
        if not line:
            if frame:
                return frame
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(": ")
        frame[name] = value


@pytest.fixture
def ui(daemon_paths: Paths) -> Iterator[Ui]:
    cfg = UiConfig(
        connect=lambda: cli_client(daemon_paths), token=new_token(), static=bundled_static()
    )
    server = UiServer(cfg)
    serving = InThread(server.serve_forever)
    live = Ui(server=server, paths=daemon_paths)
    try:
        yield live
    finally:
        for client in live.clients:
            client.close()
        server.shutdown()
        serving.result(STREAM_S)
        server.server_close()


class TestSignIn:
    def test_the_token_trades_for_a_strict_httponly_cookie_and_leaves_the_address(
        self, ui: Ui
    ) -> None:
        resp = ui.request("GET", f"/?token={ui.server.cfg.token}")

        cookie = resp.getheader("Set-Cookie") or ""
        assert resp.status == 303
        assert resp.getheader("Location") == "/"
        assert cookie.startswith(f"mzr_ui_{ui.server.port}=")
        assert {"HttpOnly", "SameSite=Strict", "Path=/"} <= {
            part.strip() for part in cookie.split(";")
        }

    def test_a_wrong_token_is_refused(self, ui: Ui) -> None:
        resp = ui.request("GET", "/?token=" + "x" * 43)

        assert resp.status == 401
        assert resp.getheader("Set-Cookie") is None

    def test_without_the_cookie_the_page_says_where_to_sign_in_and_the_api_refuses(
        self, ui: Ui
    ) -> None:
        page = ui.request("GET", "/")
        api = ui.request("GET", "/api/state")

        assert page.status == 401
        assert b"mz-relay ui" in page.read()
        assert api.status == 401

    def test_a_cookie_named_for_another_port_does_not_sign_in(self, ui: Ui) -> None:
        other = f"mzr_ui_{ui.server.port + 1}={ui.server.cfg.token}"

        assert ui.request("GET", "/api/state", headers={"Cookie": other}).status == 401

    def test_the_token_is_never_logged(self, ui: Ui, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)

        ui.request("GET", f"/?token={ui.server.cfg.token}").read()
        ui.get("/api/state").read()

        assert ui.server.cfg.token not in caplog.text


class TestRequestGuards:
    def test_a_foreign_host_header_is_refused_even_with_the_cookie(self, ui: Ui) -> None:
        resp = ui.get("/api/state", headers={"Host": f"evil.example:{ui.server.port}"})

        assert resp.status == 403

    @pytest.mark.parametrize("origin", [None, "http://evil.example", "null"])
    def test_a_post_without_our_origin_is_refused(self, ui: Ui, origin: str | None) -> None:
        resp = ui.post("/api/send", {"to": "bob", "body": BODY}, {"Origin": origin})

        assert resp.status == 403
        assert ui.query("log", {})["messages"] == []

    def test_a_post_that_is_not_json_is_refused(self, ui: Ui) -> None:
        resp = ui.post("/api/send", {"to": "bob"}, {"Content-Type": "text/plain"})

        assert resp.status == 415

    def test_an_oversized_body_is_refused(self, ui: Ui) -> None:
        resp = ui.post("/api/send", {"to": "bob", "body": "x" * MAX_BODY_BYTES})

        assert resp.status == 413

    def test_every_answer_carries_the_csp_and_no_cors(self, ui: Ui) -> None:
        resp = ui.get("/api/state")

        assert "frame-ancestors 'none'" in (resp.getheader("Content-Security-Policy") or "")
        assert resp.getheader("X-Content-Type-Options") == "nosniff"
        assert resp.getheader("Access-Control-Allow-Origin") is None

    @pytest.mark.parametrize("name", ["..%2Fservice.py", ".hidden.js", "service.py", "nope.js"])
    def test_only_bundled_static_files_are_served(self, ui: Ui, name: str) -> None:
        assert ui.get(f"/static/{name}").status == 404

    def test_the_index_page_is_served_once_signed_in(self, ui: Ui) -> None:
        resp = ui.get("/")

        assert resp.status == 200
        assert resp.getheader("Content-Type") == "text/html; charset=utf-8"


class TestActions:
    def test_state_lists_agents_held_messages_the_log_and_the_event_head(self, ui: Ui) -> None:
        held_id = ui.agent_send("carol", "alice")

        state = read_json(ui.get("/api/state"))

        held = state["held"]
        assert isinstance(held, dict)
        assert [m["id"] for m in held["messages"]] == [held_id]
        assert isinstance(state["head"], int)
        assert state["head"] > 0
        status = state["status"]
        assert isinstance(status, dict)
        assert "agents" in status

    def test_send_reaches_the_agent_as_the_user(self, ui: Ui) -> None:
        resp = ui.post("/api/send", {"to": "bob", "body": BODY})

        sent = read_json(resp)
        detail = ui.query("message", {"source": USER_NAME, "id": sent["id"]})
        assert resp.status == 200
        assert detail["envelope"]["body"] == BODY  # type: ignore[index]

    def test_approve_releases_a_held_message(self, ui: Ui) -> None:
        held_id = ui.agent_send("carol", "alice")

        resp = ui.post("/api/decide", {"source": "carol", "id": held_id, "decision": "approve"})

        assert (resp.status, read_json(resp)["status"]) == (200, "queued")

    def test_deciding_an_unknown_message_answers_404_with_the_relay_code(self, ui: Ui) -> None:
        resp = ui.post("/api/decide", {"source": "carol", "id": new_id(), "decision": "deny"})

        assert resp.status == 404
        assert read_json(resp)["error"]["code"] == "not_found"  # type: ignore[index]

    def test_the_message_route_shows_one_message_whole(self, ui: Ui) -> None:
        held_id = ui.agent_send("carol", "alice")

        detail = read_json(ui.get(f"/api/message?source=carol&id={held_id}"))

        assert detail["status"] == "held"
        assert detail["envelope"]["body"] == BODY  # type: ignore[index]

    def test_inbox_then_ack_clear_a_message_to_the_user(self, ui: Ui) -> None:
        msg_id = ui.agent_send("bob", USER_NAME)

        inbox = read_json(ui.post("/api/inbox", {}))
        ui.post("/api/ack", {"id": msg_id}).read()
        after = read_json(ui.post("/api/inbox", {}))

        messages = inbox["messages"]
        assert isinstance(messages, list)
        assert [m["id"] for m in messages] == [msg_id]
        assert after == {"messages": [], "awaiting_ack": []}

    def test_a_bad_log_limit_is_a_400(self, ui: Ui) -> None:
        assert ui.get("/api/log?limit=lots").status == 400


class TestEventStream:
    def test_the_stream_opens_at_the_head_then_carries_a_new_send(self, ui: Ui) -> None:
        stream = ui.get("/api/events")
        hello = read_event(stream)

        sent = read_json(ui.post("/api/send", {"to": "bob", "body": BODY}))
        event = read_event(stream)

        assert stream.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        assert hello["event"] == "hello"
        data = json.loads(event["data"])
        assert (event["event"], data["source"], data["id"]) == ("message", USER_NAME, sent["id"])
        assert int(event["id"]) > int(hello["id"])
        assert BODY not in event["data"]
        stream.close()

    def test_last_event_id_replays_what_happened_since(self, ui: Ui) -> None:
        head = ui.query("watch", {})["last_seq"]
        first = ui.agent_send("alice", "bob")
        ui.agent_send("alice", "bob")

        stream = ui.get("/api/events", headers={"Last-Event-ID": str(head)})
        replayed = [read_event(stream) for _ in range(3)]

        messages = [json.loads(e["data"]) for e in replayed if e["event"] == "message"]
        assert messages[0]["id"] == first
        assert [int(e["id"]) for e in replayed] == sorted(int(e["id"]) for e in replayed)
        stream.close()


class TestMessageOp:
    def test_it_returns_the_body_to_the_cli(self, rig: ServiceRig) -> None:
        conn, token = rig.connect("alice")
        sent = rig.send(conn, token, "bob", BODY)

        detail = rig.call(rig.hello(client="cli"), "message", source="alice", id=sent["id"])

        assert (detail["status"], detail["envelope"]["body"]) == ("queued", BODY)

    def test_it_refuses_a_non_cli_connection(self, rig: ServiceRig) -> None:
        conn, token = rig.connect("alice")
        sent = rig.send(conn, token, "bob", BODY)

        with pytest.raises(RelayError) as caught:
            rig.call(rig.hello(client="mcp"), "message", source="alice", id=sent["id"])

        assert caught.value.code == "not_allowed"


class TestUiCommand:
    def test_an_agent_session_is_refused(self, agent_free_env: dict[str, str]) -> None:
        terminal = Terminal(isatty=lambda: True, ask=lambda _: "", env={"CLAUDECODE": "1"})

        assert main(["ui", "--no-open"], terminal=terminal) == 2

    def test_a_port_out_of_range_is_a_usage_error(self) -> None:
        with pytest.raises(SystemExit) as caught:
            main(["ui", "--port", "70000"])

        assert caught.value.code == 2

    def test_the_launch_page_is_owner_only_and_forwards_to_the_address(
        self, tmp_path: Path
    ) -> None:
        url = "http://127.0.0.1:5/?token=abc"

        page = write_launch_page(tmp_path, url)

        assert os.stat(page).st_mode & 0o777 == 0o600
        assert f'content="0;url={url}"' in page.read_text()
