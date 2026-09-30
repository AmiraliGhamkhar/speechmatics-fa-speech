#!/usr/bin/env python3
"""Stage-by-stage smoke test of the hospital demo authentication chain.

Runs exactly the stages the compiled demo exe performs on Start, one at a
time, and reports the FIRST stage that fails with the provider's own reason:

    1. broker URL validation          (broker_client rules)
    2. GET /api/token                 (broker reachable? DEMO_TOKEN accepted?)
    3. temporary JWT inspection       (decodes claims WITHOUT trusting them,
                                       shows the token's own ttl/expiry)
    4. websocket handshake to         (real Speechmatics realtime endpoint,
       Speechmatics realtime           the same URL the app uses)

Usage:

    python scripts/smoke_demo_chain.py --broker-url https://HOST/api/token \
        --demo-token VALUE [--api-key FALLBACK_API_KEY]

Nothing secret is printed: the JWT is never echoed, only decoded claim
names and numbers.
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from speechmatics_test.broker_client import (  # noqa: E402
    BrokerError,
    fetch_realtime_token,
    validate_broker_url,
)

RT_URL = os.getenv("SPEECHMATICS_RT_URL", "wss://global.rt.speechmatics.com/v2")


def _load_api_module():
    spec = importlib.util.spec_from_file_location(
        "smoke_api_token", ROOT / "api" / "token.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decode_jwt_untrusted(token: str) -> dict:
    """Decode payload claims for display only - no signature verification."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def stage_websocket(token: str) -> None:
    """Minimal realtime handshake: connect, send StartRecognition, read one
    message. Reports the server's own close/reject reason verbatim."""
    import asyncio

    async def run() -> tuple[bool, str]:
        try:
            import websockets
        except ImportError:
            return False, "websockets package not installed in this venv"

        api = _load_api_module()
        try:
            ws_kwargs = {"additional_headers": {"Authorization": f"Bearer {token}"}}
            async with websockets.connect(RT_URL, **ws_kwargs) as ws:
                start_msg = {
                    "message": "StartRecognition",
                    "transcription_config": {"language": "fa"},
                }
                await ws.send(json.dumps(start_msg))
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
                msg = json.loads(raw)
                return True, f"handshake OK - first server message: {msg.get('message')}"
        except Exception as exc:
            detail = str(exc).strip()
            if hasattr(exc, "rcvd") and exc.rcvd is not None:
                detail = (
                    f"server closed: code={exc.rcvd.code} reason={exc.rcvd.reason!r}"
                )
            return False, f"{type(exc).__name__}: {detail}"

    ok, message = asyncio.run(run())
    print(f"  {'PASS' if ok else 'FAIL'}: {message}")
    if not ok:
        raise SystemExit(4)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker-url", required=True)
    parser.add_argument("--demo-token", default="")
    parser.add_argument(
        "--api-key",
        default="",
        help="fallback: test Speechmatics directly with the long-lived key",
    )
    parser.add_argument("--skip-websocket", action="store_true")
    args = parser.parse_args()

    print("Stage 1 - broker URL validation")
    try:
        validate_broker_url(args.broker_url)
        print("  PASS")
    except BrokerError as exc:
        print(f"  FAIL: {exc}")
        return 1

    print("Stage 2 - GET /api/token")
    try:
        token = fetch_realtime_token(args.broker_url, demo_token=args.demo_token)
        print(f"  PASS: got a JWT-shaped credential ({len(token)} chars)")
    except BrokerError as exc:
        print(f"  FAIL: {exc}")
        print("  -> 403 here means the DEMO_TOKEN does not match the broker.")
        print("  -> other errors mean reachability/server-side config.")
        return 2

    print("Stage 3 - temporary JWT claims (untrusted decode)")
    claims = decode_jwt_untrusted(token)
    if not claims:
        print("  WARN: payload not decodable; continuing anyway")
    else:
        print(f"  claim names: {sorted(claims)}")
        for key in ("exp", "ttl", "iat"):
            if key in claims and isinstance(claims[key], (int, float)):
                if key == "exp":
                    remaining = int(claims[key] - time.time())
                    print(f"  exp: {remaining}s remaining")
                else:
                    print(f"  {key}: {claims[key]}")

    if args.skip_websocket:
        print("Websocket stage skipped.")
        return 0

    print(f"Stage 4 - realtime websocket handshake ({RT_URL})")
    stage_websocket(token)

    print("\nALL STAGES PASSED - the compiled demo should be able to start.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
