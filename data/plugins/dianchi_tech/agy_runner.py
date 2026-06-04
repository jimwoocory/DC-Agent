"""PTY 包装 agy --print，结构化 JSON 输出。

按 codex 2026-05-21 判定（DOC/Codex任务_agy卡点求助_2026-05-21.md §5.4）：
- 用 PTY（不是 shell 字符串拼接）启动 agy
- 超时 180-300s，不要 25 分钟
- 检测到 OAuth/authorization code/验证码/timeout 立刻失败，不等人工输入
- 返结构化 JSON，让上层 searcher.py 判断是否走兜底

调用：
    python -m data.plugins.dianchi_tech.agy_runner --prompt-file /path/to/prompt.txt --out /path/to/result.json
or
    python agy_runner.py "<inline prompt>"

输出 JSON 字段：
    ok: bool
    kind: 'success' | 'auth_required' | 'timeout' | 'error'
    text: str  (agy 返回的内容，剥 ANSI)
    elapsed_sec: float
    error: str (kind!=success 时填)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import pexpect

AGY_BIN = "/Users/dianchi/.local/bin/agy"
DEFAULT_TIMEOUT_SEC = 240  # 4 分钟，在 codex 建议的 180-300 区间

# 检测到这些字符串 → auth_required，立刻失败
AUTH_PATTERNS = [
    "Authentication required",
    "authorization code",
    "Visit the URL",
    "oauth2/auth",
    "Please visit",
]

# ANSI 转义序列剥离
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def run_agy(prompt: str, timeout: int = DEFAULT_TIMEOUT_SEC) -> dict:
    started = time.monotonic()
    args = [
        "--print",
        prompt,
        "--dangerously-skip-permissions",
        "--print-timeout",
        f"{timeout}s",
    ]

    try:
        child = pexpect.spawn(
            AGY_BIN, args, encoding="utf-8", timeout=timeout, codec_errors="replace"
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "kind": "error",
            "text": "",
            "elapsed_sec": round(time.monotonic() - started, 2),
            "error": f"pexpect spawn failed: {type(exc).__name__}: {exc}",
        }

    # 主循环：边读边检测；遇到 auth 关键字 → 立刻 kill
    buf = ""
    auth_hit = False
    try:
        while True:
            try:
                chunk = child.read_nonblocking(size=4096, timeout=2)
                if chunk:
                    buf += chunk
                    if any(p in buf for p in AUTH_PATTERNS):
                        auth_hit = True
                        break
            except pexpect.exceptions.TIMEOUT:
                # 2s 内没新输出；检查总耗时
                if time.monotonic() - started > timeout:
                    child.close(force=True)
                    return {
                        "ok": False,
                        "kind": "timeout",
                        "text": strip_ansi(buf).strip(),
                        "elapsed_sec": round(time.monotonic() - started, 2),
                        "error": f"timeout after {timeout}s, no auth seen but agy didn't finish",
                    }
                continue
            except pexpect.exceptions.EOF:
                break
    finally:
        if child.isalive():
            try:
                child.close(force=True)
            except Exception:  # noqa: BLE001
                pass

    elapsed = round(time.monotonic() - started, 2)
    clean = strip_ansi(buf).strip()

    if auth_hit:
        return {
            "ok": False,
            "kind": "auth_required",
            "text": clean,
            "elapsed_sec": elapsed,
            "error": "agy 要求重新 OAuth 认证",
        }

    if not clean:
        return {
            "ok": False,
            "kind": "error",
            "text": "",
            "elapsed_sec": elapsed,
            "error": "agy 返回空内容（无 auth 提示也无文本）",
        }

    return {
        "ok": True,
        "kind": "success",
        "text": clean,
        "elapsed_sec": elapsed,
        "error": "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="PTY 包装 agy --print")
    parser.add_argument(
        "prompt", nargs="?", default="", help="prompt 内容（与 --prompt-file 二选一）"
    )
    parser.add_argument("--prompt-file", help="从文件读 prompt")
    parser.add_argument("--out", help="结果 JSON 写到这个文件（不指定就 stdout）")
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_SEC, help="秒，默认 240"
    )
    args = parser.parse_args()

    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    else:
        prompt = args.prompt
    if not prompt.strip():
        print(json.dumps({"ok": False, "kind": "error", "error": "empty prompt"}))
        return 2

    result = run_agy(prompt, timeout=args.timeout)
    out = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
    else:
        print(out)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
