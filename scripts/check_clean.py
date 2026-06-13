#!/usr/bin/env python3
"""Check that generated cache, backup, and runtime output files stay out of git."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "python cache/build artifact",
        re.compile(
            r"(^|/)(__pycache__|\.pytest_cache|\.ruff_cache)(/|$)"
            r"|\.py[cod]$|\$py\.class$|\.so$"
        ),
    ),
    (
        "backup file",
        re.compile(
            r"(^|/)_backup_[^/]+|(^|/)[^/]*_backup_[^/]+|\.py\.bak$|\.bak($|\.)"
        ),
    ),
    (
        "runtime output directory",
        re.compile(
            r"(^|/)(tmp|output|logs)(/|$)"
            r"|^data/(temp|output|knowledge_base|harness_intake|case_archives|workspaces|feishu-rpa-persistent-profile)(/|$)"
            r"|^nas(/|$)"
            r"|^\.qoder(/|$)"
        ),
    ),
    (
        "runtime event/state file",
        re.compile(
            r"^data/[^/]+(_events\.jsonl|_state\.json|\.log)$"
            r"|^nas_sync/([^/]+\.log|state\.json|sync_mtime_cache)$"
            r"|^nas_sync/\.cache(/|$)"
            r"|^nas_sync/[^/]+\.failstate$"
            r"|^h_send_[^/]*_cron[^/]*\.json$"
        ),
    ),
)

REVIEWED_LIVE_CONFIGS = {
    "data/config/knowledge_cycle.env",
    "data/config/openclaw_on_demand_config.json",
    "data/config/system_entries_config.json",
}

CONFIG_TEMPLATE_PATTERN = re.compile(r"^data/config/[^/]+\.example\.(json|ya?ml)$")

SENSITIVE_CONFIG_PATTERN = re.compile(
    r"^data/config/[^/]+\.(env|json|ya?ml|txt|token|secret|key)$"
)

EXCLUDED_PREFIXES = (
    ".git/",
    ".venv/",
    ".uv-cache/",
    "hermes-agent/",
    "hermes-config/",
    "hermes-webui/",
    "hermes-webui-state/",
)


def git_lines(args: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def git_status_lines() -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def staged_paths() -> list[str]:
    return git_lines(["diff", "--cached", "--name-only", "--diff-filter=ACMR"])


def tracked_paths() -> list[str]:
    return git_lines(["ls-files"])


def visible_status_paths() -> list[str]:
    paths: list[str] = []
    for line in git_status_lines():
        status = line[:2]
        if "D" in status:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return paths


def is_excluded(path: str) -> bool:
    return path.startswith(EXCLUDED_PREFIXES)


def violation_for(path: str, *, source: str = "tracked") -> str | None:
    if is_excluded(path):
        return None
    for label, pattern in FORBIDDEN_PATTERNS:
        if pattern.search(path):
            return label
    if source == "staged":
        if path in REVIEWED_LIVE_CONFIGS:
            return None
        if SENSITIVE_CONFIG_PATTERN.search(path) and not CONFIG_TEMPLATE_PATTERN.search(
            path
        ):
            return "sensitive local config change"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--staged",
        action="store_true",
        help="check only staged paths, for pre-commit hooks",
    )
    args = parser.parse_args()

    if args.staged:
        paths_with_source = [(path, "staged") for path in staged_paths()]
    else:
        paths_with_source = [
            *[(path, "tracked") for path in tracked_paths()],
            *[(path, "status") for path in visible_status_paths()],
        ]
    violations = sorted(
        {
            (path, reason)
            for path, source in paths_with_source
            if (reason := violation_for(path, source=source))
        }
    )

    if not violations:
        print("Repository hygiene check passed.")
        return 0

    print(
        "Repository hygiene check failed; remove or ignore these files:",
        file=sys.stderr,
    )
    for path, reason in violations[:80]:
        print(f"- {path} ({reason})", file=sys.stderr)
    if len(violations) > 80:
        print(f"... and {len(violations) - 80} more", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
