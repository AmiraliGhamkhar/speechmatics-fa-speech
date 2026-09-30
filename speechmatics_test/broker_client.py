"""Client for the SwiftMedics demo token broker.

Exchanges the broker URL in the desktop configuration for a SHORT-LIVED
Speechmatics realtime JWT (see ``api/token.py``). The long-lived API key
never reaches the demo machine: only a token that expires within seconds is
held in memory and only for the duration of one dictation session.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_TIMEOUT_SECONDS = 10.0


class BrokerError(RuntimeError):
    """The broker could not issue a token (network, config, or refusal)."""


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
    url = (broker_url or "").strip()
    if not url:
        raise BrokerError("token_broker_url is empty")
    if not url.lower().startswith("https://") and "localhost" not in url and "127.0.0.1" not in url:
        raise BrokerError(
            "token_broker_url must use https (plain http is allowed only for localhost)"
        )

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
    return token


def broker_url_from_env() -> str | None:
    """Default broker URL: ``SWIFTMEDICS_BROKER_URL`` or the demo stamp.

    Environment variables do not survive into a distributed .exe, so demo
    builds fall back to the broker URL compiled in by
    ``scripts/set_demo_expiry.py`` (exposed through ``demo_license``).
    """
    value = (os.getenv("SWIFTMEDICS_BROKER_URL") or "").strip()
    if value:
        return value
    try:
        from .demo_license import DEMO_BROKER_URL
    except Exception:  # pragma: no cover - defensive
        return None
    return (DEMO_BROKER_URL or "").strip() or None
