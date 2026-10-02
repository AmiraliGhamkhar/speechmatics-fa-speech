"""Speechmatics short-lived token broker for the SwiftMedics demo.

The long-lived ``SPEECHMATICS_API_KEY`` lives ONLY in this service's
environment. A demo build of the desktop app calls this endpoint, receives a
short-lived realtime JWT (default 60 s, see ``TOKEN_TTL``), and connects to
Speechmatics directly. The API key never ships inside the .exe and never
touches the demo machine.

Response shape (HTTP 200)::

    {"token": "<jwt>", "ttl": 60}

Vercel deployment contract (verified against
https://vercel.com/docs/functions/runtimes/python and
.../python/api-directory, updated 2026-08): a file-based Python function in
``/api`` must expose a top-level ``app`` (ASGI/WSGI), ``application`` (WSGI)
or ``handler`` (``BaseHTTPRequestHandler`` subclass). The legacy AWS-Lambda
``handler(event, context)`` signature is NOT supported by the current
runtime. This module therefore exposes the WSGI ``application`` below; the
``handler``/``event``-dict surface is kept only as ``handle_api_event`` for
tests and reuse.

Optional access control: set ``DEMO_TOKEN`` and the client must send
``Authorization: Bearer <DEMO_TOKEN>``. Rotating or deleting that value
revokes access for every demo build that was shipped with it.

HTTPS in production is enforced by the platform (all Vercel traffic is
terminated on TLS at the edge) and by the client (``broker_client.py``
refuses non-loopback plain-http broker URLs); this function additionally
marks every response ``no-store`` and never logs credentials.

A realtime temporary key can start "any number of Realtime transcription
sessions" within its TTL (Speechmatics documentation), so minting a fresh
key on every Start is pure churn: one extra upstream call and one more live
key per dictation session. The broker therefore reuses a still-valid key
across Starts (see ``realtime_token``). Reuse is strictly an optimisation -
on a cold serverless instance the cache is empty and a key is minted exactly
as before, so correctness never depends on it.

Local mode for an on-site demo (the key stays on the presenter machine):

    python api/token.py --serve 8787

This module is deliberately standard-library only (see api/requirements.txt).
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request

#: Speechmatics endpoint that mints temporary keys. ``type=rt`` keys work with
#: any realtime endpoint, including ``global.rt.speechmatics.com``.
SPEECHMATICS_KEYS_URL = os.getenv(
    "SPEECHMATICS_KEYS_URL", "https://mp.speechmatics.com/v1/api_keys?type=rt"
)

MIN_TTL = 10
MAX_TTL = 300
DEFAULT_TTL = 60

#: A cached key is reused only while at least this many seconds of life remain,
#: so a client never receives a token that could expire mid-dictation.
TOKEN_REUSE_MARGIN_SECONDS = 30

#: Best-effort per-instance cache of minted keys, keyed by the API key that
#: minted them so a rotated ``SPEECHMATICS_API_KEY`` never serves a token
#: derived from the old one. Guarded by a lock so two Starts arriving together
#: cannot stampede the upstream endpoint.
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_CACHE_LOCK = threading.Lock()

_STATUS_REASONS = {
    200: "OK",
    204: "No Content",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    500: "Internal Server Error",
    502: "Bad Gateway",
}


def _clamp_ttl(raw: str | None) -> int:
    try:
        ttl = int((raw or "").strip() or DEFAULT_TTL)
    except ValueError:
        return DEFAULT_TTL
    return max(MIN_TTL, min(MAX_TTL, ttl))


def issue_realtime_token(api_key: str, ttl: int) -> str:
    """Mint one short-lived realtime key. Never logs or stores anything."""
    body = json.dumps({"ttl": ttl}).encode("utf-8")
    request = urllib.request.Request(
        SPEECHMATICS_KEYS_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    token = payload.get("key_value") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("Speechmatics response did not contain 'key_value'")
    return token


def _jwt_expires_at(token: str) -> float | None:
    """Return a JWT's ``exp`` claim as a UTC epoch, or ``None`` if unreadable.

    The signature is deliberately NOT verified: this token was just minted by
    us with our own key, and all we need from the claim is the lifetime that
    decides reuse. An unreadable claim means "do not cache" - never a guess.
    """
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(
            base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
        )
    except Exception:
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None


def realtime_token(api_key: str, ttl: int) -> str:
    """Return a usable realtime key, reusing a cached one when it still lives.

    Reuse is bounded by the key's own ``exp`` claim minus
    ``TOKEN_REUSE_MARGIN_SECONDS``, so the returned key always outlives a short
    dictation burst. Anything unreadable, expired, or close to expiry falls
    through to a fresh mint.
    """
    with _TOKEN_CACHE_LOCK:
        entry = _TOKEN_CACHE.get(api_key)
    if entry is not None:
        token, expires_at = entry
        if time.time() <= expires_at - TOKEN_REUSE_MARGIN_SECONDS:
            return token

    token = issue_realtime_token(api_key, ttl)
    expires_at = _jwt_expires_at(token)
    if expires_at is not None:
        with _TOKEN_CACHE_LOCK:
            _TOKEN_CACHE[api_key] = (token, expires_at)
    return token


def _cors_headers() -> dict[str, str]:
    """CORS is OFF by default. This endpoint serves native Windows clients
    (Tkinter/urllib), which are not subject to browser same-origin policy, so
    a wide-open ``Access-Control-Allow-Origin: *`` only invites browser-based
    abuse of the token mint. Set ``ALLOWED_ORIGIN`` to a specific origin only
    if you also serve a web client from the same broker."""
    allowed_origin = (os.getenv("ALLOWED_ORIGIN") or "").strip()
    if not allowed_origin:
        return {}
    return {
        "Access-Control-Allow-Origin": allowed_origin,
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Vary": "Origin",
    }


def _authorized(headers: dict) -> bool:
    demo_token = (os.getenv("DEMO_TOKEN") or "").strip()
    if not demo_token:
        return True  # open endpoint; the issued token expires in seconds anyway
    supplied = (headers.get("authorization") or "").strip()
    return hmac.compare_digest(supplied, f"Bearer {demo_token}")


def _dispatch(
    method: str, headers: dict | None, path: str | None = None
) -> tuple[int, dict[str, str], bytes]:
    """Core ``GET /api/token`` logic shared by every entry point below.

    Returns ``(status, headers, body)`` and never logs the request: the
    ``Authorization`` header carries a credential. When ``path`` is given
    (WSGI and the local server always provide it), any route other than
    ``/api/token`` is refused with 404 - with Vercel's entrypoint mode the
    app receives every request, and only the token mint may be reachable.
    Event-style shims that carry no path keep working.
    """
    headers = {
        str(key).lower(): value for key, value in (headers or {}).items()
    }
    method = str(method or "GET").upper()
    response_headers = {"Cache-Control": "no-store"}

    if path:
        route = path.split("?", 1)[0].rstrip("/") or "/"
        if route != "/api/token":
            response_headers["Content-Type"] = "application/json"
            return 404, response_headers, json.dumps(
                {"error": "not found; use /api/token"}
            ).encode("utf-8")

    if method == "OPTIONS":
        response_headers.update(_cors_headers())
        response_headers["Content-Length"] = "0"
        return 204, response_headers, b""
    # Only the token mint is exposed; anything else is refused before auth so
    # the endpoint cannot be probed for behavior.
    if method != "GET":
        response_headers["Content-Type"] = "application/json"
        return 405, response_headers, json.dumps({"error": "method not allowed"}).encode("utf-8")

    if not _authorized(headers):
        response_headers["Content-Type"] = "application/json"
        return 403, response_headers, json.dumps({"error": "invalid demo token"}).encode("utf-8")

    api_key = (os.getenv("SPEECHMATICS_API_KEY") or "").strip()
    response_headers["Content-Type"] = "application/json"
    if not api_key:
        return 500, response_headers, json.dumps(
            {"error": "SPEECHMATICS_API_KEY is not configured on the broker"}
        ).encode("utf-8")

    ttl = _clamp_ttl(os.getenv("TOKEN_TTL"))
    try:
        token = realtime_token(api_key, ttl)
    except urllib.error.HTTPError as exc:
        # Surface only the status code: the body could echo request context.
        return 502, response_headers, json.dumps(
            {"error": f"Speechmatics rejected the token request (HTTP {exc.code})"}
        ).encode("utf-8")
    except Exception as exc:  # defensive: never leak request details to the demo
        return 502, response_headers, json.dumps(
            {"error": f"token request failed: {type(exc).__name__}"}
        ).encode("utf-8")

    return 200, response_headers, json.dumps({"token": token, "ttl": ttl}).encode("utf-8")


# ---------------------------------------------------------------------------
# Vercel entry point: WSGI application (current file-based /api contract).
# ---------------------------------------------------------------------------

def application(environ, start_response):  # noqa: ANN001 - WSGI signature
    """WSGI entry point loaded by Vercel for ``api/token.py``.

    HTTPS is guaranteed by the platform edge (Vercel terminates TLS and only
    serves the function over TLS); behind the edge the request arrives with
    whatever internal scheme the platform uses, so the scheme is not checked
    here. Client-side, ``broker_client.py`` refuses non-loopback plain http.
    """
    headers = {
        key[5:].replace("_", "-"): value
        for key, value in environ.items()
        if isinstance(key, str) and key.startswith("HTTP_")
    }
    if isinstance(environ.get("AUTHORIZATION"), str):
        # Some WSGI servers expose Authorization directly rather than as
        # HTTP_AUTHORIZATION; accept both.
        headers.setdefault("Authorization", environ["AUTHORIZATION"])
    status, response_headers, body = _dispatch(
        environ.get("REQUEST_METHOD", "GET"),
        headers,
        path=str(environ.get("PATH_INFO") or environ.get("SCRIPT_NAME") or ""),
    )
    start_response(
        f"{status} {_STATUS_REASONS.get(status, 'OK')}",
        list(response_headers.items()),
    )
    return [body]


# ---------------------------------------------------------------------------
# Local mode + event-style shim (tests / other hosts)
# ---------------------------------------------------------------------------

def handle_api_event(event: dict, context=None) -> dict:
    """Adapter for event/dict-style hosts and tests: ``{"httpMethod", "headers"}``
    in, ``{"statusCode", "headers", "body"}`` out. The current Vercel Python
    runtime does NOT load this shape; use ``application`` there."""
    status, response_headers, body = _dispatch(
        event.get("httpMethod") if isinstance(event, dict) else None,
        event.get("headers") if isinstance(event, dict) else None,
    )
    return {
        "statusCode": status,
        "headers": response_headers,
        "body": body.decode("utf-8"),
    }


def _serve_locally(port: int) -> None:  # pragma: no cover - interactive use
    """Minimal stdlib server so the broker can run on the presenter machine."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class BrokerHTTP(BaseHTTPRequestHandler):
        def _dispatch(self) -> None:
            status, response_headers, body = _dispatch(
                self.command, dict(self.headers), path=self.path
            )
            self.send_response(status)
            for key, value in response_headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        do_GET = _dispatch
        do_POST = _dispatch
        do_OPTIONS = _dispatch

        def log_message(self, fmt: str, *args) -> None:  # keep tokens out of logs
            pass

    server = HTTPServer(("0.0.0.0", port), BrokerHTTP)
    print(f"SwiftMedics token broker listening on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SwiftMedics token broker")
    parser.add_argument("--serve", type=int, metavar="PORT", help="run a local HTTP server")
    args = parser.parse_args()
    if not args.serve:
        parser.error("use --serve PORT to run the broker locally")
    _serve_locally(args.serve)
