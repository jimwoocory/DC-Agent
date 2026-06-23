#!/usr/bin/env python3
"""Smoke-test concurrent first messages against the local AstrBot OpenAPI.

This script is intentionally runtime-facing rather than a unit test: it checks
the real local API server, plugin chain, provider path, conversation creation,
and first-turn streaming response for multiple distinct users.

It does not send Feishu messages. All traffic goes to localhost.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_DB = ROOT / "data" / "data_v4.db"
DEFAULT_API_BASE = "http://127.0.0.1:6185"


@dataclass(frozen=True)
class SmokeResult:
    index: int
    ok: bool
    status_code: int | None
    seconds: float
    response_bytes: int = 0
    error: str = ""


def _hash_api_key(raw_key: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        raw_key.encode("utf-8"),
        b"astrbot_api_key",
        100_000,
    ).hex()


def _configured_api_key() -> str:
    return (
        os.environ.get("ASTRBOT_API_KEY") or os.environ.get("ASTRBOT_APIKEY") or ""
    ).strip()


@contextmanager
def _api_key() -> Iterator[str]:
    configured = _configured_api_key()
    if configured:
        yield configured
        return

    if not DATA_DB.exists():
        raise RuntimeError(f"Cannot create temporary API key; DB missing: {DATA_DB}")

    raw_key = f"abk_multi_user_smoke_{secrets.token_urlsafe(24)}"
    key_id = str(uuid4())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expires_at = now + timedelta(minutes=20)
    with sqlite3.connect(DATA_DB) as conn:
        conn.execute(
            """
            INSERT INTO api_keys (
                created_at, updated_at, key_id, name, key_hash, key_prefix,
                scopes, created_by, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now.strftime("%Y-%m-%d %H:%M:%S.%f"),
                now.strftime("%Y-%m-%d %H:%M:%S.%f"),
                key_id,
                "multi-user-first-message-smoke",
                _hash_api_key(raw_key),
                raw_key[:12],
                json.dumps(["chat"]),
                "multi_user_first_message_smoke.py",
                expires_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
            ),
        )
    try:
        yield raw_key
    finally:
        with sqlite3.connect(DATA_DB) as conn:
            conn.execute("DELETE FROM api_keys WHERE key_id = ?", (key_id,))


def _send_first_message(
    *,
    api_base: str,
    api_key: str,
    index: int,
    timeout: float,
    message: str,
) -> SmokeResult:
    started = time.monotonic()
    payload = {
        "username": f"ou_smoke_user_{index}",
        "session_id": f"multi-user-first-message-{int(time.time())}-{index}",
        "enable_streaming": True,
        "message": f"{message} ({index})",
    }
    try:
        response = requests.post(
            f"{api_base.rstrip('/')}/api/v1/chat",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
            },
            json=payload,
            timeout=(5, timeout),
        )
    except Exception as exc:  # noqa: BLE001
        return SmokeResult(
            index=index,
            ok=False,
            status_code=None,
            seconds=round(time.monotonic() - started, 2),
            error=f"{type(exc).__name__}: {exc}",
        )

    text = response.text
    ok = (
        response.status_code == 200
        and '"session_id"' in text
        and ('"complete"' in text or '"end"' in text)
    )
    return SmokeResult(
        index=index,
        ok=ok,
        status_code=response.status_code,
        seconds=round(time.monotonic() - started, 2),
        response_bytes=len(text),
        error="" if ok else text[:300],
    )


def run_smoke(
    *,
    api_base: str,
    users: int,
    timeout: float,
    message: str,
) -> tuple[bool, list[SmokeResult]]:
    with _api_key() as api_key:
        with concurrent.futures.ThreadPoolExecutor(max_workers=users) as executor:
            futures = [
                executor.submit(
                    _send_first_message,
                    api_base=api_base,
                    api_key=api_key,
                    index=index,
                    timeout=timeout,
                    message=message,
                )
                for index in range(1, users + 1)
            ]
            results = [future.result() for future in futures]
    return all(result.ok for result in results), results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--users", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--message",
        default="并发首句烟测：你好，请用一句话回复收到。",
    )
    args = parser.parse_args()

    if args.users < 1:
        print("--users must be >= 1", file=sys.stderr)
        return 2

    ok, results = run_smoke(
        api_base=args.api_base,
        users=args.users,
        timeout=args.timeout,
        message=args.message,
    )
    print(
        json.dumps(
            {
                "ok": ok,
                "api_base": args.api_base,
                "users": args.users,
                "results": [asdict(result) for result in results],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
