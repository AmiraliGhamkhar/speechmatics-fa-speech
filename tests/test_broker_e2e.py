"""End-to-end smoke test of the demo token broker over real loopback HTTP.

Runs the actual ``api/token.py`` server (the ``--serve`` code path) in a
thread, then exercises: no API key configured, DEMO_TOKEN auth (missing,
wrong, correct), the mint path (Speechmatics call stubbed), and the client's
URL/token validation. No network beyond 127.0.0.1 is touched.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_api_module():
    spec = importlib.util.spec_from_file_location("api_token_e2e", ROOT / "api" / "token.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["api_token_e2e"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def broker_server(monkeypatch):
    """Run the real broker server on a random loopback port; yield its URL."""
    import socket
    from http.server import HTTPServer

    api = _load_api_module()

    # Build the handler exactly as _serve_locally does, but on port 0.
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self):
            response = api.handler(
                {"httpMethod": self.command, "headers": dict(self.headers)}
            )
            self.send_response(response["statusCode"])
            for key, value in response["headers"].items():
                self.send_header(key, value)
            body = response["body"].encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        do_GET = _dispatch
        do_POST = _dispatch
        do_OPTIONS = _dispatch

        def log_message(self, fmt, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api/token"
    server.shutdown()
    server.server_close()


def _get(url: str, demo_token: str | None = None):
    request = urllib.request.Request(url, method="GET")
    if demo_token:
        request.add_header("Authorization", f"Bearer {demo_token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def test_broker_missing_api_key_is_reported(broker_server, monkeypatch):
    monkeypatch.delenv("SPEECHMATICS_API_KEY", raising=False)
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    status, body = _get(broker_server)
    assert status == 500
    assert "SPEECHMATICS_API_KEY" in body["error"]


def test_broker_demo_token_auth_is_enforced(broker_server, monkeypatch):
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "private-key")
    monkeypatch.setenv("DEMO_TOKEN", "expected-secret")
    status, _ = _get(broker_server)  # no header
    assert status == 403
    status, _ = _get(broker_server, demo_token="wrong")
    assert status == 403


def test_broker_mints_and_client_accepts(broker_server, monkeypatch):
    from speechmatics_test.broker_client import fetch_realtime_token

    monkeypatch.setenv("SPEECHMATICS_API_KEY", "private-key")
    monkeypatch.delenv("DEMO_TOKEN", raising=False)

    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"
    monkeypatch.setattr(
        "api_token_e2e.issue_realtime_token", lambda api_key, ttl: jwt
    )

    status, body = _get(broker_server)
    assert status == 200
    assert body["token"] == jwt
    assert body["ttl"] >= 10

    # The real client must accept the real broker's response end-to-end.
    token = fetch_realtime_token(broker_server)
    assert token == jwt


def test_broker_response_is_never_cached(broker_server, monkeypatch):
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "private-key")
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    monkeypatch.setattr(
        "api_token_e2e.issue_realtime_token", lambda api_key, ttl: "a.b.c"
    )  # stub: this test must never touch the real Speechmatics API
    request = urllib.request.Request(broker_server, method="GET")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.headers.get("Cache-Control") == "no-store"


def test_client_rejects_wrong_jwt_shape_from_a_live_server(broker_server, monkeypatch):
    from speechmatics_test.broker_client import BrokerError, fetch_realtime_token

    monkeypatch.setenv("SPEECHMATICS_API_KEY", "private-key")
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    monkeypatch.setattr(
        "api_token_e2e.issue_realtime_token", lambda api_key, ttl: "not-a-jwt"
    )
    with pytest.raises(BrokerError, match="JWT"):
        fetch_realtime_token(broker_server)
