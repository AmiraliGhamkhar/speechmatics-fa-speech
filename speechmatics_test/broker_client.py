"""Client for the SwiftMedics demo token broker.

Exchanges the broker URL in the desktop configuration for a SHORT-LIVED
Speechmatics realtime JWT (see ``api/token.py``). The long-lived API key
never reaches the demo machine: only a token that expires within seconds is
held in memory and only for the duration of one dictation session.

Hardening:

* HTTPS is enforced by parsed scheme, not substring matching (``https://``
  anywhere in a string does not count); plain http is allowed only for
  loopback hosts (localhost / 127.0.0.0/8 / ::1), where traffic never leaves
  the presenter machine.
* No userinfo (``user:pass@``), query or fragment in the broker URL, and
  https only on the standard port, so the URL in config.json cannot smuggle
  anything unexpected.
* The response token must be JWT-shaped (three dot-separated segments) — a
  wrong-shaped answer fails fast with a clear message instead of a confusing
  Speechmatics 401 mid-session.
* The websocket endpoint is NOT taken from the broker; the app always
  connects to Speechmatics directly, so even a fully compromised broker
  cannot redirect the audio stream.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT_SECONDS = 10.0


class BrokerError(RuntimeError):
    """The broker could not issue a token (network, config, or refusal)."""


def _host_allowed(url: urllib.parse.ParseResult) -> bool:
    """Loopback hosts may use plain http; everything else must be https."""
    host = (url.hostname or "").lower()
    if url.scheme == "https":
        return True
    return host == "localhost" or host.endswith(".localhost") or host in (
        "127.0.0.1",
        "::1",
    )


def validate_broker_url(url: str) -> str:
    """Return the cleaned broker URL or raise ``BrokerError``."""
    text = (url or "").strip()
    if not text:
        raise BrokerError("token_broker_url is empty")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in ("http", "https"):
        raise BrokerError("token_broker_url must start with http:// or https://")
    if not parsed.hostname:
        raise BrokerError("token_broker_url has no host")
    if not _host_allowed(parsed):
        raise BrokerError(
            "token_broker_url must use https (plain http is allowed only for localhost)"
        )
    if parsed.username or parsed.password:
        raise BrokerError("token_broker_url must not contain credentials")
    if parsed.scheme == "https" and parsed.port is not None and parsed.port != 443:
        raise BrokerError("token_broker_url must use the default https port 443")
    if parsed.query or parsed.fragment:
        raise BrokerError("token_broker_url must not contain a query or fragment")
    return text


def validate_token_shape(token: str) -> None:
    """Refuse obviously wrong credentials BEFORE a session starts.

    Speechmatics temporary keys are JWTs (three dot-separated base64url
    segments). A token that is not JWT-shaped means the endpoint answered
    with something else - failing fast beats a confusing 401 from
    Speechmatics mid-session.
    """
    text = (token or "").strip()
    if not text:
        raise BrokerError("broker returned an empty token")
    parts = text.split(".")
    if len(parts) != 3 or not all(parts):
        raise BrokerError(
            "broker returned a token that is not a valid JWT (expected three segments)"
        )


def fetch_realtime_token(
    broker_url: str,
    *,
    demo_token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Return a short-lived Speechmatics realtime JWT from the broker.

    ``demo_token`` is the optional shared secret the broker may require
    (``Authorization: Bearer ...``); it is NOT the Speechmatics API key.
    """
    url = validate_broker_url(broker_url)

    request = urllib.request.Request(url.strip(), method="GET")
    if demo_token:
        request.add_header("Authorization", f"Bearer {demo_token.strip()}")
    request.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str(exc.read().decode("utf-8", "replace"))[:200]
        except Exception:
            pass
        raise BrokerError(
            f"token broker returned HTTP {exc.code}" + (f": {detail}" if detail else "")
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BrokerError(f"could not reach the token broker: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BrokerError("token broker returned a non-JSON response") from exc

    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise BrokerError("token broker response did not contain 'token'")
    validate_token_shape(token)
    return token


def broker_url_from_env() -> str | None:
    """Default broker URL: ``SWIFTMEDICS_BROKER_URL`` or the demo stamp.

    Environment variables do not survive into a distributed .exe, so demo
    builds fall back to the broker URL compiled in by
    ``scripts/set_demo_expiry.py`` (exposed through ``demo_license``).
    """
    import os

    value = (os.getenv("SWIFTMEDICS_BROKER_URL") or "").strip()
    if value:
        return value
    try:
        from .demo_license import DEMO_BROKER_URL
    except Exception:  # pragma: no cover - defensive
        return None
    return (DEMO_BROKER_URL or "").strip() or None
