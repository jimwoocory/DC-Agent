#!/usr/bin/env python3
"""Guard production runtime paths against fake employee-facing completions."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SCAN_ROOTS = (
    Path("data/plugins"),
    Path("dc_engines/dc_engines/harness"),
    Path("dc_engines/dc_engines/case"),
    Path("harness"),
)

EXCLUDED_PARTS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    "cache",
    ".cache",
    "docs",
    "dashboard",
    "tests",
    "test",
    "contracts",
}
EXCLUDED_RUNTIME_PREFIXES = (
    Path("data/temp"),
    Path("data/output"),
    Path("data/workspaces"),
)
EXCLUDED_FILE_NAME_MARKERS = ("_backup_",)
EXCLUDED_FILE_NAME_PREFIXES = ("_backup",)
EXCLUDED_FILE_NAME_SUFFIXES = (".bak.py", ".py.bak")

FAKE_WORD_RE = re.compile(
    r"\b(mock|demo|fake|sample|placeholder|stub|fabricated(?:\s+data)?)\b|"
    r"示例数据|随便生成|假数据",
    re.IGNORECASE,
)
STRING_LITERAL_RE = re.compile(r"([\"'])(?:\\.|(?!\1).)*\1")
FAKE_SEED_RE = re.compile(
    r"\b(?:MOCK|DEMO|FAKE|SAMPLE|PLACEHOLDER|STUB)_TASKS\b|"
    r"(?:^|[^A-Za-z0-9])_?seed(?:ed|ing)?[A-Za-z0-9_]*"
    r"(?:mock|demo|fake|sample|placeholder|stub)(?:\b|_)|"
    r"\b(?:mock|demo|fake|sample|placeholder|stub|fabricated(?:\s+data)?)\b"
    r".*\b(seed|seeded|seeding)\b|"
    r"\b(seed|seeded|seeding)\b.*"
    r"\b(?:mock|demo|fake|sample|placeholder|stub|fabricated(?:\s+data)?)\b",
    re.IGNORECASE,
)
COMPLETION_ANCHOR_RE = re.compile(
    r"\b("
    r"complete_task|gate\.complete|TaskCallbackPayload|status\s*=\s*[\"']completed[\"']|"
    r"_send_context_message|_send_context_chain|_send_card|_reply|"
    r"send_card(?:_via_runtime)?|finalize_card(?:_via_runtime)?|"
    r"event\.set_result|set_result|build_[A-Za-z0-9_]*card|"
    r"render_[A-Za-z0-9_]*text"
    r")\b"
)
TODO_FALLBACK_RE = re.compile(
    r"\b(TODO|FIXME|XXX|HACK)\b|"
    r"\b(fallback|fall back)\b.*\b(todo|temporary|for now|placeholder|stub|mock|fake|demo|sample)\b|"
    r"\b(todo|temporary|for now|placeholder|stub|mock|fake|demo|sample)\b.*\b(fallback|fall back)\b",
    re.IGNORECASE,
)
ACTION_ANCHOR_RE = re.compile(
    r"\b(return|send|complete_task|gate\.complete|_send_context_message|"
    r"_send_context_chain|_send_card|_reply|send_card(?:_via_runtime)?|"
    r"finalize_card(?:_via_runtime)?|event\.set_result|set_result)\b"
)
RUNTIME_HANDLER_RE = re.compile(
    r"^(on_message|dispatch|handle_.*response|complete.*|finalize.*|settle.*)$"
)

# Keep allowlisted exceptions exact and scarce. Each entry documents a line that
# contains guard vocabulary but is not an employee-facing fake completion path.
ALLOWLIST: dict[str, set[str]] = {}


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    rule_id: str
    message: str

    def format(self, root: Path = REPO_ROOT) -> str:
        display_path = self.path
        if self.path.is_absolute():
            display_path = self.path.relative_to(root)
        return f"{display_path}:{self.line}: {self.rule_id}: {self.message}"


def _relative(path: Path, root: Path) -> Path:
    if path.is_absolute():
        return path.relative_to(root)
    return path


def should_scan_path(path: Path, root: Path = REPO_ROOT) -> bool:
    relative = _relative(path, root)
    if path.suffix != ".py":
        return False
    if _is_excluded_file_name(relative.name):
        return False
    if any(relative.is_relative_to(prefix) for prefix in EXCLUDED_RUNTIME_PREFIXES):
        return False
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    return any(relative.is_relative_to(scan_root) for scan_root in SCAN_ROOTS)


def iter_scan_files(root: Path = REPO_ROOT) -> Iterable[Path]:
    for scan_root in SCAN_ROOTS:
        absolute_root = root / scan_root
        if not absolute_root.exists():
            continue
        for path in absolute_root.rglob("*.py"):
            if should_scan_path(path, root):
                yield path


def _is_allowlisted(path: Path, line_text: str, root: Path) -> bool:
    relative = str(_relative(path, root))
    return line_text.strip() in ALLOWLIST.get(relative, set())


def _is_excluded_file_name(name: str) -> bool:
    return (
        name.startswith(EXCLUDED_FILE_NAME_PREFIXES)
        or name.endswith(EXCLUDED_FILE_NAME_SUFFIXES)
        or any(marker in name for marker in EXCLUDED_FILE_NAME_MARKERS)
    )


def _window(lines: Sequence[str], index: int, radius: int = 2) -> str:
    start = max(0, index - radius)
    end = min(len(lines), index + radius + 1)
    return "\n".join(lines[start:end])


def _has_fake_word(line_text: str) -> bool:
    matches = list(FAKE_WORD_RE.finditer(line_text))
    if not matches:
        return False

    if any(match.group(0).lower() != "sample" for match in matches):
        return True

    visible_text = " ".join(
        _strip_fstring_expressions(string_match.group(0))
        for string_match in STRING_LITERAL_RE.finditer(line_text)
    )
    if "#" in line_text:
        visible_text = f"{visible_text} {line_text.split('#', 1)[1]}"
    return bool(FAKE_WORD_RE.search(visible_text))


def _strip_fstring_expressions(literal: str) -> str:
    return re.sub(r"\{[^{}]*\}", "", literal)


def scan_text(path: Path, text: str, root: Path = REPO_ROOT) -> list[Finding]:
    findings: list[Finding] = []
    lines = text.splitlines()
    for index, line_text in enumerate(lines):
        if _is_allowlisted(path, line_text, root):
            continue
        nearby = _window(lines, index)
        if FAKE_SEED_RE.search(line_text):
            findings.append(
                Finding(
                    path=path,
                    line=index + 1,
                    rule_id="fake-data-seed-path",
                    message=(
                        "fake/mock/demo/sample seed path appears in production "
                        "runtime code"
                    ),
                )
            )
        if _has_fake_word(line_text) and COMPLETION_ANCHOR_RE.search(nearby):
            findings.append(
                Finding(
                    path=path,
                    line=index + 1,
                    rule_id="fake-data-completion-anchor",
                    message=(
                        "fake/mock/demo/sample wording appears near a runtime "
                        "completion or reply anchor"
                    ),
                )
            )
        if TODO_FALLBACK_RE.search(line_text) and ACTION_ANCHOR_RE.search(nearby):
            findings.append(
                Finding(
                    path=path,
                    line=index + 1,
                    rule_id="todo-fallback-reply-anchor",
                    message=(
                        "TODO or temporary fallback wording appears near a return, "
                        "send, or completion path"
                    ),
                )
            )
    findings.extend(_scan_runtime_passes(path, text))
    return findings


def _scan_runtime_passes(path: Path, text: str) -> list[Finding]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [
            Finding(
                path=path,
                line=exc.lineno or 1,
                rule_id="parse-error",
                message="could not parse runtime file for fake-data guard",
            )
        ]

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if not RUNTIME_HANDLER_RE.match(node.name):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Pass):
                if _has_ancestor(child, parents, ast.ExceptHandler):
                    continue
                findings.append(
                    Finding(
                        path=path,
                        line=child.lineno,
                        rule_id="pass-runtime-handler",
                        message=(
                            f"runtime handler {node.name} contains pass; use explicit "
                            "blocking, error, or real completion behavior"
                        ),
                    )
                )
    return findings


def _has_ancestor(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
    ancestor_type: type[ast.AST],
) -> bool:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ancestor_type):
            return True
        current = parents.get(current)
    return False


def scan_paths(paths: Iterable[Path], root: Path = REPO_ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        absolute = path if path.is_absolute() else root / path
        if not should_scan_path(absolute, root):
            continue
        findings.extend(scan_text(absolute, absolute.read_text(encoding="utf-8"), root))
    return findings


def main() -> int:
    findings = scan_paths(iter_scan_files(REPO_ROOT), REPO_ROOT)
    if findings:
        for finding in sorted(findings, key=lambda item: (str(item.path), item.line)):
            print(finding.format(REPO_ROOT))
        return 1
    print("Fake-data runtime guard passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
