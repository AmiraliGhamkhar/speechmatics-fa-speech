"""Speechmatics short-lived token broker for the SwiftMedics demo.

The long-lived ``SPEECHMATICS_API_KEY`` lives ONLY in this service's
environment. A demo build of the desktop app calls this endpoint, receives a
short-lived realtime JWT (default 60 s, see ``TOKEN_TTL``), and connects to
Speechmatics directly. The API key never ships inside the .exe and never
touches the demo machine.

Response shape (HTTP 200)::

    {"token": "<jwt>", "ttl": 60}

Optional access control: set ``DEMO_TOKEN`` and the client must send
``Authorization: Bearer <DEMO_TOKEN>``. Rotating or deleting that value
revokes access for every demo build that was shipped with it.

Deployment (Vercel example, free Hobby tier):

    vercel            # from the repository root; api/token.py becomes /api/token
    # then set env vars in the dashboard:
    #   SPEECHMATICS_API_KEY  (required, the private key)
    #   DEMO_TOKEN            (optional, shared secret for demo revocation)
    #   TOKEN_TTL             (optional, seconds, default 60)

Local mode for an on-site demo (the key stays on the presenter machine):

    python api/token.py --serve 8787

This module is deliberately standard-library only.
"""
from __future__ import annotations

import hmac
import json
import os
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


def _response(
    status: int, payload: dict | None = None, *, extra: dict[str, str] | None = None
) -> dict:
    headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
    headers.update(_cors_headers())
    headers.update(extra or {})
    return {
        "statusCode": status,
        "headers": headers,
        "body": "" if payload is None else json.dumps(payload),
    }


def _authorized(headers: dict) -> bool:
    demo_token = (os.getenv("DEMO_TOKEN") or "").strip()
    if not demo_token:
        return True  # open endpoint; the issued token expires in seconds anyway
    supplied = (headers.get("authorization") or "").strip()
    return hmac.compare_digest(supplied, f"Bearer {demo_token}")


def handler(event: dict, context=None) -> dict:
    """HTTP entry point (Vercel-style). GET /api/token -> one short-lived JWT."""
    method = str(event.get("httpMethod") or "GET").upper()
    headers = {
        str(key).lower(): value
        for key, value in (event.get("headers") or {}).items()
    }
    if method == "OPTIONS":
        cors = _cors_headers()
        if not cors:
            return _response(204, extra={"Content-Length": "0"})
        return _response(204, extra={"Content-Length": "0", **cors})
    # Only the token mint is exposed; anything else is refused before auth so
    # the endpoint cannot be probed for behavior.
    if method != "GET":
        return _response(405, {"error": "method not allowed"})

    if not _authorized(headers):
        return _response(403, {"error": "invalid demo token"})

    api_key = (os.getenv("SPEECHMATICS_API_KEY") or "").strip()
    if not api_key:
        return _response(
            500, {"error": "SPEECHMATICS_API_KEY is not configured on the broker"}
        )

    ttl = _clamp_ttl(os.getenv("TOKEN_TTL"))
    try:
        token = issue_realtime_token(api_key, ttl)
    except urllib.error.HTTPError as exc:
        # Surface only the status code: the body could echo request context.
        return _response(
            502, {"error": f"Speechmatics rejected the token request (HTTP {exc.code})"}
        )
    except Exception as exc:  # defensive: never leak request details to the demo
        return _response(502, {"error": f"token request failed: {type(exc).__name__}"})

    return _response(200, {"token": token, "ttl": ttl})


def _serve_locally(port: int) -> None:  # pragma: no cover - interactive use
    """Minimal stdlib server so the broker can run on the presenter machine."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class BrokerHTTP(BaseHTTPRequestHandler):
        def _dispatch(self) -> None:
            response = handler(
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
