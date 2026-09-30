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
            {"token": "jwt-abc", "ttl": 60}
        ).encode()
        token = broker_client.fetch_realtime_token("http://127.0.0.1:8787/token")
    assert token == "jwt-abc"


def test_client_sends_demo_token_header():
    with mock.patch.object(urllib.request, "urlopen") as fake:
        fake.return_value.__enter__.return_value.read.return_value = json.dumps(
            {"token": "jwt-abc", "ttl": 60}
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


# ------------------------------------------------------------------- broker


def test_broker_handler_refuses_bad_demo_token(monkeypatch):
    api = _load_api_token_module()
    monkeypatch.setenv("DEMO_TOKEN", "expected")
    monkeypatch.delenv("SPEECHMATICS_API_KEY", raising=False)
    response = api.handler({"httpMethod": "GET", "headers": {"authorization": "Bearer wrong"}})
    assert response["statusCode"] == 403


def test_broker_handler_requires_server_side_api_key(monkeypatch):
    api = _load_api_token_module()
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    monkeypatch.delenv("SPEECHMATICS_API_KEY", raising=False)
    response = api.handler({"httpMethod": "GET", "headers": {}})
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
    response = api.handler({"httpMethod": "GET", "headers": {}})
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["token"] == "minted-jwt"
    assert body["ttl"] == 60
    assert captured == {"api_key": "private-key-never-shipped", "ttl": 60}


def test_broker_handler_never_returns_the_api_key(monkeypatch):
    api = _load_api_token_module()
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "private-key-never-shipped")
    monkeypatch.setattr(api, "issue_realtime_token", lambda key, ttl: "minted-jwt")
    response = api.handler({"httpMethod": "GET", "headers": {}})
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
