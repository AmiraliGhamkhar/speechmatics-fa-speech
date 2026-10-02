"""Tests for the demo distribution machinery.

Covers the demo expiry gate, the token-broker client, the serverless broker
handler, and the broker-aware desktop config parsing. The realtime adapter's
JWT-in-place-of-API-key behavior is pinned by asserting the constructor stores
the credential and that the adapter never sends the long-lived key when a JWT
is present.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import json
import sys
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from speechmatics_test import broker_client, demo_license  # noqa: E402


def _load_api_token_module():
    spec = importlib.util.spec_from_file_location(
        "api_token_under_test", ROOT / "api" / "token.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["api_token_under_test"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- demo gate


@pytest.mark.skipif(
    demo_license.DEMO_EXPIRY is not None,
    reason="a demo_build_stamp.py exists locally; the no-stamp state cannot be tested",
)
def test_gate_is_inactive_without_a_stamp():
    assert demo_license.DEMO_EXPIRY is None
    assert demo_license.days_remaining() is None
    demo_license.check_demo_license()  # must not raise


def test_gate_is_inactive_for_a_future_stamp(monkeypatch):
    now = dt.datetime(2026, 11, 1, 12, 0, 0)
    monkeypatch.setattr(demo_license, "DEMO_EXPIRY", dt.datetime(2026, 11, 30, 23, 59, 59))
    assert demo_license.days_remaining(now) == 29
    demo_license.check_demo_license(now)  # must not raise


def test_expired_stamp_is_refused(monkeypatch):
    past = dt.datetime.now() - dt.timedelta(days=1)
    monkeypatch.setattr(demo_license, "DEMO_EXPIRY", past)
    with pytest.raises(demo_license.DemoLicenseExpired):
        demo_license.check_demo_license()


def test_expiry_day_is_still_valid_until_end_of_day(monkeypatch):
    expiry = dt.datetime.now().replace(microsecond=0) + dt.timedelta(hours=2)
    monkeypatch.setattr(demo_license, "DEMO_EXPIRY", expiry)
    demo_license.check_demo_license()  # same-day use must not raise


# ------------------------------------------------------------ broker client


def test_client_requires_https_outside_localhost():
    with pytest.raises(broker_client.BrokerError, match="https"):
        broker_client.fetch_realtime_token("http://insecure.example.com/api/token")


def test_client_allows_localhost_http():
    with mock.patch.object(urllib.request, "urlopen") as fake:
        fake.return_value.__enter__.return_value.read.return_value = json.dumps(
            {"token": "a.b.c", "ttl": 60}
        ).encode()
        token = broker_client.fetch_realtime_token("http://127.0.0.1:8787/token")
    assert token == "a.b.c"


def test_client_sends_demo_token_header():
    with mock.patch.object(urllib.request, "urlopen") as fake:
        fake.return_value.__enter__.return_value.read.return_value = json.dumps(
            {"token": "a.b.c", "ttl": 60}
        ).encode()
        broker_client.fetch_realtime_token(
            "https://broker.example.com/api/token", demo_token="secret-demo-token"
        )
    request = fake.call_args.args[0]
    assert request.headers.get("Authorization") == "Bearer secret-demo-token"


def test_client_surfaces_http_error_without_crashing():
    import urllib.error

    with mock.patch.object(urllib.request, "urlopen") as fake:
        fake.side_effect = urllib.error.HTTPError(
            url="https://broker.example.com/api/token",
            code=403,
            msg="forbidden",
            hdrs=None,
            fp=None,
        )
        with pytest.raises(broker_client.BrokerError, match="HTTP 403"):
            broker_client.fetch_realtime_token("https://broker.example.com/api/token")


def test_client_rejects_response_without_token():
    with mock.patch.object(urllib.request, "urlopen") as fake:
        fake.return_value.__enter__.return_value.read.return_value = b'{"nope": 1}'
        with pytest.raises(broker_client.BrokerError, match="token"):
            broker_client.fetch_realtime_token("https://broker.example.com/api/token")


# ------------------------------------------------------------- URL hardening


@pytest.mark.parametrize(
    "url",
    [
        "http://broker.example.com/api/token",  # non-loopback plain http
        "ftp://broker.example.com/api/token",  # wrong scheme entirely
        "https://user:pass@broker.example.com/api/token",  # embedded credentials
        "https://broker.example.com/api/token?x=1",  # query smuggling
        "https://broker.example.com/api/token#frag",  # fragment
        "https://broker.example.com:8443/api/token",  # non-standard https port
    ],
)
def test_client_rejects_unsafe_broker_urls(url):
    with pytest.raises(broker_client.BrokerError):
        broker_client.validate_broker_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://broker.example.com/api/token",
        "https://broker.example.com:443/api/token",  # explicit default is fine
        "http://localhost:8787/api/token",  # presenter-machine local mode
        "http://127.0.0.1:8787/api/token",
    ],
)
def test_client_accepts_safe_broker_urls(url):
    assert broker_client.validate_broker_url(url) == url


def test_client_rejects_empty_jwt_and_missing_segments():
    with pytest.raises(broker_client.BrokerError, match="empty"):
        broker_client.validate_token_shape("")
    with pytest.raises(broker_client.BrokerError, match="JWT"):
        broker_client.validate_token_shape("only-two.segments")
    broker_client.validate_token_shape("a.b.c")  # shape-valid


# ------------------------------------------------------- clock rollback gate


def test_clock_rollback_is_detected(monkeypatch, tmp_path):
    import time as time_module

    future = dt.datetime.now() + dt.timedelta(days=10)
    monkeypatch.setattr(demo_license, "DEMO_EXPIRY", future)
    monkeypatch.setattr(
        demo_license, "_state_path", lambda: tmp_path / "demo_state.json"
    )

    real_time = time_module.time()
    # First run records the watermark.
    monkeypatch.setattr(time_module, "time", lambda: real_time)
    demo_license.check_demo_license()
    # Clock moved back by a week: refused.
    monkeypatch.setattr(time_module, "time", lambda: real_time - 7 * 86400)
    with pytest.raises(demo_license.DemoLicenseExpired, match="clock"):
        demo_license.check_demo_license()
    # Small NTP-style correction (under tolerance): accepted and re-watermarked.
    monkeypatch.setattr(time_module, "time", lambda: real_time - 3600)
    demo_license.check_demo_license()


def test_clock_gate_is_inert_outside_demo_builds(monkeypatch, tmp_path):
    import time as time_module

    monkeypatch.setattr(demo_license, "DEMO_EXPIRY", None)
    monkeypatch.setattr(
        demo_license, "_state_path", lambda: tmp_path / "demo_state.json"
    )
    monkeypatch.setattr(time_module, "time", lambda: 1000.0)
    demo_license.check_demo_license()
    demo_license.check_demo_license()  # watermark never written, never trips
    assert not (tmp_path / "demo_state.json").exists()


# ------------------------------------------------------------------- broker


def test_broker_handler_refuses_bad_demo_token(monkeypatch):
    api = _load_api_token_module()
    monkeypatch.setenv("DEMO_TOKEN", "expected")
    monkeypatch.delenv("SPEECHMATICS_API_KEY", raising=False)
    response = api.handle_api_event({"httpMethod": "GET", "headers": {"authorization": "Bearer wrong"}})
    assert response["statusCode"] == 403


def test_broker_wsgi_application_is_exposed_for_vercel():
    """Current Vercel file-based /api contract: top-level WSGI ``application``."""
    api = _load_api_token_module()
    assert callable(api.application)

    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    import os as _os

    previous = _os.environ.pop("DEMO_TOKEN", None)
    _os.environ.pop("SPEECHMATICS_API_KEY", None)
    try:
        body = b"".join(
            api.application(
                {"REQUEST_METHOD": "GET", "HTTP_AUTHORIZATION": "Bearer x"},
                start_response,
            )
        )
    finally:
        if previous is not None:
            _os.environ["DEMO_TOKEN"] = previous
    assert captured["status"].startswith("500")  # no server-side key configured
    assert captured["headers"]["Cache-Control"] == "no-store"
    assert b"SPEECHMATICS_API_KEY" in body


def test_broker_handler_requires_server_side_api_key(monkeypatch):
    api = _load_api_token_module()
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    monkeypatch.delenv("SPEECHMATICS_API_KEY", raising=False)
    response = api.handle_api_event({"httpMethod": "GET", "headers": {}})
    assert response["statusCode"] == 500


def test_broker_handler_issues_a_short_lived_token(monkeypatch):
    api = _load_api_token_module()
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "private-key-never-shipped")
    monkeypatch.setenv("TOKEN_TTL", "60")

    captured = {}

    def fake_issue(api_key, ttl):
        captured["api_key"] = api_key
        captured["ttl"] = ttl
        return "minted-jwt"

    monkeypatch.setattr(api, "issue_realtime_token", fake_issue)
    response = api.handle_api_event({"httpMethod": "GET", "headers": {}})
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["token"] == "minted-jwt"
    assert body["ttl"] == 60
    assert captured == {"api_key": "private-key-never-shipped", "ttl": 60}


def _fake_jwt(exp: float) -> str:
    """A JWT-shaped token whose payload carries ``exp`` (signature unused)."""
    import base64

    def seg(obj) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg({'exp': exp})}.signature"


def _get_broker_token(api, headers=None) -> str:
    response = api.handle_api_event({"httpMethod": "GET", "headers": headers or {}})
    assert response["statusCode"] == 200
    return json.loads(response["body"])["token"]


def test_broker_reuses_a_key_across_starts(monkeypatch):
    """One upstream mint serves several Starts while the key still lives.

    A realtime temporary key can start any number of sessions within its TTL,
    so minting per Start is pure churn. Reuse is bounded by exp minus the
    margin, so a key is never handed out while it is close to expiring.
    """
    import time as time_module

    api = _load_api_token_module()
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "private-key-never-shipped")
    monkeypatch.setenv("TOKEN_TTL", "300")

    mints = []
    live = _fake_jwt(time_module.time() + 300)
    monkeypatch.setattr(
        api,
        "issue_realtime_token",
        lambda key, ttl: (mints.append((key, ttl)), live)[1],
    )

    first = _get_broker_token(api)
    second = _get_broker_token(api)
    third = _get_broker_token(api)

    assert first == second == third == live
    assert len(mints) == 1, "a still-valid key must be reused, not re-minted"


def test_broker_remints_when_the_cached_key_is_near_expiry(monkeypatch):
    """The reuse margin is a hard floor, not a hint."""
    import time as time_module

    api = _load_api_token_module()
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "private-key-never-shipped")

    barely_alive = _fake_jwt(time_module.time() + api.TOKEN_REUSE_MARGIN_SECONDS - 1)
    fresh = _fake_jwt(time_module.time() + 300)
    minted = [barely_alive, fresh]
    monkeypatch.setattr(api, "issue_realtime_token", lambda key, ttl: minted.pop(0))

    assert _get_broker_token(api) == barely_alive
    # Too little life left to hand out: mint again rather than risk a token
    # that dies mid-dictation.
    assert _get_broker_token(api) == fresh


def test_broker_never_reuses_a_token_from_a_rotated_api_key(monkeypatch):
    """A rotated SPEECHMATICS_API_KEY must not inherit the old key's token."""
    import time as time_module

    api = _load_api_token_module()
    live = _fake_jwt(time_module.time() + 300)
    monkeypatch.setattr(api, "issue_realtime_token", lambda key, ttl: live)

    monkeypatch.setenv("SPEECHMATICS_API_KEY", "old-key")
    assert _get_broker_token(api) == live

    # Different key, same reusable token value: still a fresh mint, because the
    # cache is keyed by the API key that produced the token.
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "new-key")
    assert _get_broker_token(api) == live
    assert set(api._TOKEN_CACHE) == {"old-key", "new-key"}


def test_broker_never_caches_an_unreadable_token(monkeypatch):
    """No readable ``exp`` means no reuse - expiry can never be guessed."""
    api = _load_api_token_module()
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "private-key-never-shipped")

    mints = []
    monkeypatch.setattr(
        api,
        "issue_realtime_token",
        lambda key, ttl: (mints.append(ttl), "not-a-jwt")[1],
    )

    assert _get_broker_token(api) == "not-a-jwt"
    assert _get_broker_token(api) == "not-a-jwt"
    assert len(mints) == 2
    assert api._TOKEN_CACHE == {}


def test_broker_reuse_does_not_enable_http_caching(monkeypatch):
    """Server-side reuse must never become a client/proxy HTTP cache."""
    api = _load_api_token_module()
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "private-key-never-shipped")
    monkeypatch.setattr(api, "issue_realtime_token", lambda key, ttl: _fake_jwt(1e12))

    response = api.handle_api_event({"httpMethod": "GET", "headers": {}})
    assert response["headers"]["Cache-Control"] == "no-store"


def test_broker_reuse_never_bypasses_authentication(monkeypatch):
    """The cache is consulted only after the demo token is accepted."""
    api = _load_api_token_module()
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "private-key-never-shipped")
    monkeypatch.setenv("DEMO_TOKEN", "the-shared-secret")
    monkeypatch.setattr(api, "issue_realtime_token", lambda key, ttl: _fake_jwt(1e12))

    authorized = {"Authorization": "Bearer the-shared-secret"}
    warm = _get_broker_token(api, authorized)
    assert _get_broker_token(api, authorized) == warm  # cache is now warm

    refused = api.handle_api_event({"httpMethod": "GET", "headers": {}})
    assert refused["statusCode"] == 403
    wrong = api.handle_api_event(
        {"httpMethod": "GET", "headers": {"Authorization": "Bearer wrong"}}
    )
    assert wrong["statusCode"] == 403


def test_broker_handler_never_returns_the_api_key(monkeypatch):
    api = _load_api_token_module()
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "private-key-never-shipped")
    monkeypatch.setattr(api, "issue_realtime_token", lambda key, ttl: "minted-jwt")
    response = api.handle_api_event({"httpMethod": "GET", "headers": {}})
    assert "private-key-never-shipped" not in response["body"]


def test_broker_clamps_nonsensical_ttl(monkeypatch):
    api = _load_api_token_module()
    monkeypatch.setenv("TOKEN_TTL", "999999")
    assert api._clamp_ttl("999999") == api.MAX_TTL
    assert api._clamp_ttl("garbage") == api.DEFAULT_TTL


# ------------------------------------------------------------------- config


def test_config_accepts_broker_url_without_api_key():
    from speechmatics_test.desktop_config import parse_config

    config = parse_config(
        {
            "speechmatics_api_key": "",
            "token_broker_url": "https://broker.example.com/api/token",
        }
    )
    assert config.speechmatics_api_key == ""
    assert config.token_broker_url == "https://broker.example.com/api/token"


def test_config_still_requires_something():
    from speechmatics_test.desktop_config import ConfigError, parse_config

    with pytest.raises(ConfigError, match="speechmatics_api_key is missing"):
        parse_config({"speechmatics_api_key": ""})


def test_config_rejects_non_string_broker_url():
    from speechmatics_test.desktop_config import ConfigError, parse_config

    with pytest.raises(ConfigError, match="token_broker_url"):
        parse_config(
            {
                "speechmatics_api_key": "",
                "token_broker_url": ["not", "a", "string"],
            }
        )


# ------------------------------------------------------- realtime passthrough


def test_realtime_stores_jwt_credential():
    from speechmatics_test.realtime import SpeechmaticsRealtime

    stt = SpeechmaticsRealtime(api_key="ignored", language="fa", auth_jwt="short-lived-jwt")
    assert stt.auth_jwt == "short-lived-jwt"


def test_realtime_without_jwt_keeps_api_key():
    from speechmatics_test.realtime import SpeechmaticsRealtime

    stt = SpeechmaticsRealtime(api_key="k", language="fa")
    assert stt.auth_jwt is None
    assert stt.api_key == "k"


def test_settings_flow_broker_fields_into_the_session():
    from speechmatics_test.desktop_config import DesktopConfig
    from speechmatics_test.session_controller import DictationSettings

    config = DesktopConfig(
        speechmatics_api_key="",
        token_broker_url="https://broker.example.com/api/token",
        demo_token="demo-secret",
    )
    settings = DictationSettings.from_desktop_config(config)
    assert settings.api_key == ""
    assert settings.token_broker_url == "https://broker.example.com/api/token"
    assert settings.demo_token == "demo-secret"


def test_session_fetches_broker_token_when_no_api_key():
    """The demo path: no local key -> short-lived JWT -> used as credential."""
    from speechmatics_test.session_controller import DictationSettings, SessionCallbacks

    settings = DictationSettings(
        api_key="",
        token_broker_url="https://broker.example.com/api/token",
        demo_token="demo-secret",
        inject=False,
        medical_layer=False,
        entity_guard=False,
    )
    calls: list[str] = []

    def fake_fetch(broker_url, *, demo_token=None, timeout=None):
        calls.append(broker_url)
        assert demo_token == "demo-secret"
        return "session-jwt"

    with (
        mock.patch.dict(sys.modules, {"injector": mock.MagicMock()}),
        mock.patch("speechmatics_test.broker_client.fetch_realtime_token", fake_fetch),
        mock.patch(
            "speechmatics_test.microphone.MicrophoneRecorder",
            side_effect=RuntimeError("stop before opening hardware"),
        ),
    ):
        session = __import__(
            "speechmatics_test.session_controller", fromlist=["DictationSession"]
        ).DictationSession(settings, SessionCallbacks())
        with pytest.raises(RuntimeError, match="stop before opening hardware"):
            asyncio.run(session._run_async())
    assert calls == ["https://broker.example.com/api/token"]
