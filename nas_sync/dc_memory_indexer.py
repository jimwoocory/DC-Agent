#!/usr/bin/env python3
"""DC-Agent NAS memory indexer.

This is the primary NAS knowledge path for DC-Agent. It reads files directly
from NAS, extracts searchable text, writes a local SQLite/FTS index, and records
document memories into harness memory. AstrBot KB mirroring is intentionally
optional and no longer required for DC-Agent recall.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import yaml

DC_ROOT = Path("/Users/dianchi/DC-Agent")
CONFIG_PATH = DC_ROOT / "nas_sync" / "config.yaml"
NAS_MEMORY_DB = DC_ROOT / "data" / "nas_memory.db"
HARNESS_MEMORY_DB = DC_ROOT / "data" / "harness_memory.db"
EMPLOYEE_DB = DC_ROOT / "data" / "employees.db"
OVERRIDES_PATH = DC_ROOT / "data" / "config" / "nas_memory_overrides.json"
STATE_PATH = DC_ROOT / "data" / "watchdog" / "dc_memory_state.json"
LOCK_PATH = DC_ROOT / "data" / "watchdog" / "dc_memory_indexer.lock"
LOG_PATH = DC_ROOT / "data" / "watchdog" / "dc_memory_indexer.log"
STAGING_DIR = DC_ROOT / "data" / "staging" / "memory_indexer"

SUPPORTED_EXTENSIONS = {
    ".md",
    ".txt",
    ".json",
    ".csv",
    ".docx",
    ".pptx",
    ".xlsx",
    ".pdf",
    ".zip",
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
}

KNOWN_PERSON_NAMES = {
    "谭媛尹",
    "郁学爽",
    "谭庆秋",
    "韦欢",
    "潘彩月",
    "黄丽清",
    "胡洁",
    "李卓",
    "王爱萍",
    "曾荣林",
    "黎德强",
    "涂筱妍",
    "韦婕",
    "覃春丝",
    "玉晓莉",
    "周钰岚",
}


@dataclass(slots=True)
class ExtractedDocument:
    text: str
    parser: str
    warning: str = ""


@dataclass(slots=True)
class EmployeeProfile:
    open_id: str
    name: str
    department: str
    role: str


@dataclass(slots=True)
class DocumentGraph:
    project_id: str
    project_name: str
    doc_type: str
    initiator: str
    owner: str
    participants: list[EmployeeProfile]
    departments: list[str]
    confidence: float
    review_status: str
    evidence: list[str]


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as file:
        file.write(f"{now_iso()} {message}\n")


@contextmanager
def process_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("SKIP already_running")
            print(
                json.dumps(
                    {"skipped": 1, "reason": "already_running"}, ensure_ascii=False
                )
            )
            raise SystemExit(0)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}


def load_overrides() -> dict:
    if not OVERRIDES_PATH.exists():
        return {"people": {}, "projects": {}, "departments": {}, "ownership_rules": []}
    try:
        data = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"people": {}, "projects": {}, "departments": {}, "ownership_rules": []}
    data.setdefault("people", {})
    data.setdefault("projects", {})
    data.setdefault("departments", {})
    data.setdefault("ownership_rules", [])
    return data


def save_overrides(data: dict) -> None:
    data.setdefault("people", {})
    data.setdefault("projects", {})
    data.setdefault("departments", {})
    data.setdefault("ownership_rules", [])
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OVERRIDES_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    tmp.replace(OVERRIDES_PATH)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"files": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"files": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def staged_index_path(path: Path, mount: Path) -> Path:
    """Copy NAS files to local disk before parsing to avoid SMB read instability."""
    try:
        path.relative_to(mount)
    except ValueError:
        return path
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
    target = STAGING_DIR / f"{path.stem}__{token}{path.suffix}"
    source_stat = path.stat()
    if target.exists():
        try:
            if target.stat().st_size == source_stat.st_size:
                return target
        except OSError:
            pass
    tmp = target.with_name(f".{target.name}.tmp")
    attempts = 0
    while tmp.exists() and tmp.stat().st_size > source_stat.st_size:
        tmp.unlink(missing_ok=True)
    while not tmp.exists() or tmp.stat().st_size < source_stat.st_size:
        attempts += 1
        if attempts > 12:
            raise OSError(f"staging copy failed after retries: {path}")
        offset = tmp.stat().st_size if tmp.exists() else 0
        try:
            with path.open("rb") as source, tmp.open("ab") as dest:
                source.seek(offset)
                for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                    dest.write(chunk)
        except OSError as exc:
            log(f"STAGING retry path={path} offset={offset} reason={exc}")
            time.sleep(min(15, 2 * attempts))
            continue
    tmp.replace(target)
    return target


def file_signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def same_file_signature(existing: dict, signature: dict[str, int]) -> bool:
    return (
        existing.get("size") == signature["size"]
        and existing.get("mtime_ns") == signature["mtime_ns"]
    )


def safe_relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def normalize_space(text: str) -> str:
    text = sanitize_text(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sanitize_text(text: str) -> str:
    """Remove invalid Unicode that SQLite/JSON cannot encode safely."""
    text = re.sub(r"[\ud800-\udfff]", "", text)
    return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def extract_office_xml(
    path: Path, members: list[str], parser: str
) -> ExtractedDocument:
    texts: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for member in members:
            if member not in zf.namelist():
                continue
            root = ET.fromstring(zf.read(member))
            for node in root.iter():
                if node.tag.endswith("}t") and node.text:
                    texts.append(node.text)
    return ExtractedDocument(normalize_space("\n".join(texts)), parser)


def extract_docx(path: Path) -> ExtractedDocument:
    return extract_office_xml(path, ["word/document.xml"], "docx")


def extract_pptx(path: Path) -> ExtractedDocument:
    members: list[str] = []
    with zipfile.ZipFile(path) as zf:
        members = sorted(
            name
            for name in zf.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        )
    return extract_office_xml(path, members, "pptx")


def extract_xlsx(path: Path) -> ExtractedDocument:
    texts: list[str] = []
    warnings: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            try:
                root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in root:
                    item = "".join(
                        node.text or "" for node in si.iter() if node.tag.endswith("}t")
                    )
                    shared.append(item)
            except (ET.ParseError, zipfile.BadZipFile, KeyError, RuntimeError) as exc:
                warnings.append(f"sharedStrings skipped: {exc}")
        for name in sorted(
            n
            for n in names
            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
        ):
            try:
                root = ET.fromstring(zf.read(name))
            except (ET.ParseError, zipfile.BadZipFile, KeyError, RuntimeError) as exc:
                warnings.append(f"{name} skipped: {exc}")
                continue
            for cell in root.iter():
                if not cell.tag.endswith("}c"):
                    continue
                ctype = cell.attrib.get("t")
                value = ""
                for child in cell:
                    if child.tag.endswith("}v") and child.text:
                        value = child.text
                        break
                if not value:
                    continue
                if ctype == "s":
                    try:
                        texts.append(shared[int(value)])
                    except (ValueError, IndexError):
                        continue
                else:
                    texts.append(value)
    return ExtractedDocument(
        normalize_space("\n".join(texts)), "xlsx", "; ".join(warnings)
    )


def ensure_indexable_extracted(
    source_path: Path, parsed_path: Path, extracted: ExtractedDocument
) -> ExtractedDocument:
    if len(extracted.text) >= 20:
        return extracted
    if extracted.parser != "xlsx":
        return extracted
    try:
        size = parsed_path.stat().st_size
    except OSError:
        size = 0
    lines = [
        f"表格文件: {source_path.name}",
        "说明: 该 Excel 表格正文文本较少或部分工作表 XML 无法解析，已按表格元数据入库。",
        f"文件大小: {size} bytes",
        f"路径: {source_path}",
    ]
    if extracted.warning:
        lines.append(f"解析警告: {extracted.warning}")
    return ExtractedDocument(
        normalize_space("\n".join(lines)), "xlsx_metadata", extracted.warning
    )


def extract_pdf(path: Path) -> ExtractedDocument:
    try:
        from pypdf import PdfReader  # type: ignore
    except ModuleNotFoundError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ModuleNotFoundError:
            return ExtractedDocument("", "pdf", "pypdf/PyPDF2 unavailable")

    reader = PdfReader(str(path))
    texts = [(page.extract_text() or "") for page in reader.pages]
    return ExtractedDocument(normalize_space("\n".join(texts)), "pdf")


def extract_zip(path: Path) -> ExtractedDocument:
    lines: list[str] = [f"压缩包文件: {path.name}"]
    suffix_counts: dict[str, int] = {}
    suffix_sizes: dict[str, int] = {}
    top_dirs: dict[str, int] = {}
    entries: list[str] = []
    skipped_dirs = 0
    listed_files = 0
    total_files = 0
    total_uncompressed = 0
    total_compressed = 0
    max_entries = 1200

    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                skipped_dirs += 1
                continue
            total_files += 1
            total_uncompressed += int(info.file_size or 0)
            total_compressed += int(info.compress_size or 0)
            name = sanitize_text(info.filename)
            suffix = Path(name).suffix.lower() or "<无扩展名>"
            suffix_counts[suffix] = suffix_counts.get(suffix, 0) + 1
            suffix_sizes[suffix] = suffix_sizes.get(suffix, 0) + int(
                info.file_size or 0
            )
            first_part = name.split("/", 1)[0] if "/" in name else "<根目录>"
            top_dirs[first_part] = top_dirs.get(first_part, 0) + 1
            if listed_files < max_entries:
                entries.append(f"- {name} ({info.file_size} bytes)")
                listed_files += 1

    lines.append(f"文件数量: {total_files}")
    lines.append(f"目录数量: {skipped_dirs}")
    lines.append(f"压缩后总大小: {total_compressed} bytes")
    lines.append(f"解压后总大小: {total_uncompressed} bytes")
    lines.append("扩展名统计:")
    for suffix, count in sorted(
        suffix_counts.items(), key=lambda item: (-item[1], item[0])
    )[:80]:
        lines.append(f"- {suffix}: {count} 个, {suffix_sizes.get(suffix, 0)} bytes")
    lines.append("一级目录统计:")
    for dirname, count in sorted(
        top_dirs.items(), key=lambda item: (-item[1], item[0])
    )[:120]:
        lines.append(f"- {dirname}: {count} 个文件")
    lines.append("文件清单:")
    lines.extend(entries)
    if total_files > listed_files:
        lines.append(f"... 还有 {total_files - listed_files} 个文件未在本次清单展开")
    return ExtractedDocument(normalize_space("\n".join(lines)), "zip_manifest")


def extract_media_metadata(path: Path) -> ExtractedDocument:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    lines = [
        f"媒体文件: {path.name}",
        f"文件类型: {path.suffix.lower().lstrip('.') or 'unknown'}",
        f"文件大小: {size} bytes",
        f"路径: {path}",
        "说明: 该文件为视频或媒体素材，当前记忆系统索引文件元数据，不解析媒体画面或音轨。",
    ]
    return ExtractedDocument(normalize_space("\n".join(lines)), "media_metadata")


def extract_text(path: Path) -> ExtractedDocument:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt", ".json", ".csv"}:
        return ExtractedDocument(
            normalize_space(path.read_text(encoding="utf-8", errors="replace")),
            suffix.lstrip("."),
        )
    if suffix == ".docx":
        return extract_docx(path)
    if suffix == ".pptx":
        return extract_pptx(path)
    if suffix == ".xlsx":
        return extract_xlsx(path)
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".zip":
        return extract_zip(path)
    if suffix in {".mp4", ".mov", ".m4v", ".avi", ".mkv"}:
        return extract_media_metadata(path)
    return ExtractedDocument("", "unsupported", f"unsupported extension: {suffix}")


def build_summary(text: str, limit: int = 260) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean[:limit]


def extract_tags(name: str, text: str) -> list[str]:
    candidates = [
        "项目管理",
        "五菱",
        "传播",
        "执行方案",
        "策划案",
        "SOP",
        "舆情",
        "KOW",
        "KOS",
        "视频",
        "排期",
        "负责人",
        "活动",
        "复盘",
        "预算",
        "文案",
    ]
    blob = f"{name}\n{text[:4000]}"
    return [tag for tag in candidates if tag.lower() in blob.lower()]


def safe_path_part(value: str, fallback: str = "未归类") -> str:
    value = re.sub(r"[\\/:*?\"<>|]+", "_", value).strip().strip(".")
    value = re.sub(r"\s+", " ", value)
    return (value or fallback)[:80]


def load_employee_profiles() -> list[EmployeeProfile]:
    overrides = load_overrides()
    if not EMPLOYEE_DB.exists():
        profiles = []
    else:
        with sqlite3.connect(EMPLOYEE_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT open_id, display_name, department, role
                FROM employees
                WHERE COALESCE(display_name, '') != ''
                """
            ).fetchall()
        profiles = []
        for row in rows:
            name = str(row["display_name"] or "").strip()
            if not name or name in {"老大"}:
                continue
            profile_override = overrides["people"].get(name, {})
            profiles.append(
                EmployeeProfile(
                    open_id=str(row["open_id"] or ""),
                    name=name,
                    department=str(
                        profile_override.get("department") or row["department"] or ""
                    ),
                    role=str(profile_override.get("role") or row["role"] or ""),
                )
            )
    known_names = {profile.name for profile in profiles}
    for name, profile_override in overrides["people"].items():
        if name not in known_names:
            profiles.append(
                EmployeeProfile(
                    open_id=f"override:{hashlib.sha1(name.encode('utf-8')).hexdigest()[:12]}",
                    name=name,
                    department=str(profile_override.get("department") or ""),
                    role=str(profile_override.get("role") or ""),
                )
            )
            known_names.add(name)
    profiles.sort(key=lambda item: len(item.name), reverse=True)
    return profiles


def infer_doc_type(name: str, text: str) -> str:
    blob = f"{name}\n{text[:3000]}"
    rules = [
        ("项目总表", ("项目管理总表", "方案进度记录", "项目管理")),
        ("复盘结算", ("复盘", "结算")),
        ("执行方案", ("执行策划", "执行方案", "活动执行", "策划案")),
        ("SOP", ("SOP", "标准流程", "操作规范")),
        ("排期分工", ("排期", "负责人", "分工", "进度记录")),
        ("文案素材", ("文案", "脚本", "话术", "素材")),
        ("预算报价", ("预算", "报价", "费用")),
        ("传播策略", ("传播策略", "传播方案", "运营规划", "规划方案")),
    ]
    for doc_type, markers in rules:
        if any(marker in blob for marker in markers):
            return doc_type
    return "资料文档"


def infer_project_name(
    name: str, text: str, doc_type: str
) -> tuple[str, float, list[str]]:
    stem = Path(name).stem
    evidence: list[str] = []
    cleaned = re.sub(r"__[a-zA-Z0-9_-]+$", "", stem)
    cleaned = re.sub(r"^【[^】]+】", "", cleaned)
    cleaned = re.sub(r"^\d+[_-]", "", cleaned)
    cleaned = re.sub(
        r"(执行策划案|执行方案|传播策略|传播方案|运营规划|项目规划|内容规划|执行SOP|SOP|复盘报告|方案|规划|\.docx)$",
        "",
        cleaned,
    )
    cleaned = cleaned.strip(" _-：:（）()")
    is_token_name = bool(re.fullmatch(r"[A-Za-z0-9_-]{16,}", cleaned))
    if 2 <= len(cleaned) <= 40 and not is_token_name:
        evidence.append(f"filename:{stem}")
        return cleaned, 0.78, evidence

    for line in text.splitlines()[:20]:
        line = line.strip()
        if 4 <= len(line) <= 40 and any(
            marker in line for marker in ("项目", "方案", "规划", "传播", "活动")
        ):
            evidence.append(f"heading:{line}")
            return re.sub(r"(执行策划案|执行方案|方案|规划)$", "", line), 0.62, evidence

    fallback = "待确认项目"
    if doc_type == "项目总表":
        fallback = stem or "项目总表"
    evidence.append("fallback:low_confidence")
    return fallback, 0.3, evidence


def find_people(text: str, employees: list[EmployeeProfile]) -> list[EmployeeProfile]:
    seen: set[str] = set()
    found: list[EmployeeProfile] = []
    employee_names = {employee.name for employee in employees}
    for employee in employees:
        if employee.name and employee.name in text and employee.open_id not in seen:
            seen.add(employee.open_id)
            found.append(employee)
    for name in sorted(KNOWN_PERSON_NAMES - employee_names, key=len, reverse=True):
        if name in text:
            open_id = f"unknown:{hashlib.sha1(name.encode('utf-8')).hexdigest()[:12]}"
            if open_id not in seen:
                seen.add(open_id)
                found.append(
                    EmployeeProfile(open_id=open_id, name=name, department="", role="")
                )
    return found


def apply_project_override(graph: DocumentGraph) -> DocumentGraph:
    overrides = load_overrides()
    project_override = overrides["projects"].get(graph.project_name, {})
    if not project_override:
        return graph
    project_name = str(project_override.get("project_name") or graph.project_name)
    project_id = hashlib.sha1(project_name.encode("utf-8")).hexdigest()[:16]
    departments = graph.departments
    if project_override.get("departments"):
        departments = list(project_override["departments"])
    return DocumentGraph(
        project_id=project_id,
        project_name=project_name,
        doc_type=str(project_override.get("doc_type") or graph.doc_type),
        initiator=str(project_override.get("initiator") or graph.initiator),
        owner=str(project_override.get("owner") or graph.owner),
        participants=graph.participants,
        departments=departments,
        confidence=max(
            graph.confidence, float(project_override.get("confidence") or 0.96)
        ),
        review_status=str(project_override.get("review_status") or "confirmed"),
        evidence=graph.evidence + ["override:nas_memory_overrides.json"],
    )


def infer_role_name(
    patterns: list[str], text: str, candidates: list[EmployeeProfile]
) -> str:
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            window = text[match.end() : match.end() + 80]
            for person in candidates:
                if person.name in window:
                    return person.name
    return ""


def build_document_graph(name: str, extracted: ExtractedDocument) -> DocumentGraph:
    employees = load_employee_profiles()
    text = extracted.text
    doc_type = infer_doc_type(name, text)
    project_name, project_confidence, evidence = infer_project_name(
        name, text, doc_type
    )
    participants = find_people(f"{name}\n{text[:20000]}", employees)
    initiator = infer_role_name(
        [r"发起人[:：]?", r"需求方[:：]?", r"提出[:：]?"], text, participants
    )
    owner = infer_role_name(
        [r"负责人[:：]?", r"项目负责人[:：]?", r"负责[:：]?"], text, participants
    )
    if doc_type == "项目总表":
        owner = ""
        evidence.append("owner:summary_table_requires_row_level_review")
    if (
        not owner
        and participants
        and doc_type != "项目总表"
        and participants[0].department
    ):
        owner = participants[0].name
        evidence.append(f"owner_candidate:first_mentioned:{owner}")
    departments = sorted(
        {person.department for person in participants if person.department}
    )
    confidence = project_confidence
    if participants:
        confidence += 0.1
    if owner:
        confidence += 0.08
    if initiator:
        confidence += 0.08
    confidence = min(confidence, 0.95)
    has_unknown_people = any(
        person.open_id.startswith("unknown:") for person in participants
    )
    review_status = (
        "confirmed"
        if confidence >= 0.82 and owner and initiator and not has_unknown_people
        else "need_review"
    )
    project_id = hashlib.sha1(project_name.encode("utf-8")).hexdigest()[:16]
    graph = DocumentGraph(
        project_id=project_id,
        project_name=project_name,
        doc_type=doc_type,
        initiator=initiator,
        owner=owner,
        participants=participants,
        departments=departments,
        confidence=round(confidence, 2),
        review_status=review_status,
        evidence=evidence,
    )
    return apply_project_override(graph)


def chunk_text(text: str, size: int = 1600, overlap: int = 160) -> list[str]:
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + size])
        start += max(1, size - overlap)
    return chunks


def init_nas_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            doc_key TEXT PRIMARY KEY,
            rel_path TEXT NOT NULL,
            source_path TEXT NOT NULL,
            archive_path TEXT DEFAULT '',
            sha256 TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            parser TEXT NOT NULL,
            project_id TEXT DEFAULT '',
            project_name TEXT DEFAULT '',
            doc_type TEXT DEFAULT '',
            initiator TEXT DEFAULT '',
            owner TEXT DEFAULT '',
            departments_json TEXT DEFAULT '[]',
            participants_json TEXT DEFAULT '[]',
            confidence REAL DEFAULT 0,
            review_status TEXT DEFAULT 'need_review',
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            indexed_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id TEXT PRIMARY KEY,
            doc_key TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            FOREIGN KEY(doc_key) REFERENCES documents(doc_key)
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
        USING fts5(chunk_id UNINDEXED, doc_key UNINDEXED, title, text);

        CREATE INDEX IF NOT EXISTS idx_documents_indexed_at
        ON documents(indexed_at DESC);

        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            project_name TEXT NOT NULL,
            owner TEXT DEFAULT '',
            initiator TEXT DEFAULT '',
            departments_json TEXT DEFAULT '[]',
            confidence REAL DEFAULT 0,
            review_status TEXT DEFAULT 'need_review',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS people (
            open_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            department TEXT DEFAULT '',
            role TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS document_people (
            doc_key TEXT NOT NULL,
            open_id TEXT NOT NULL,
            person_name TEXT NOT NULL,
            relation_role TEXT NOT NULL,
            department TEXT DEFAULT '',
            PRIMARY KEY (doc_key, open_id, relation_role)
        );

        CREATE TABLE IF NOT EXISTS document_projects (
            doc_key TEXT NOT NULL,
            project_id TEXT NOT NULL,
            relation_role TEXT NOT NULL,
            PRIMARY KEY (doc_key, project_id, relation_role)
        );

        CREATE TABLE IF NOT EXISTS review_queue (
            review_id TEXT PRIMARY KEY,
            doc_key TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS project_items (
            item_id TEXT PRIMARY KEY,
            doc_key TEXT NOT NULL,
            project_id TEXT NOT NULL,
            project_name TEXT NOT NULL,
            project_type TEXT DEFAULT '',
            project_status TEXT DEFAULT '',
            owner TEXT DEFAULT '',
            owner_department TEXT DEFAULT '',
            source_rel_path TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        """
    )
    for column, ddl in {
        "archive_path": "ALTER TABLE documents ADD COLUMN archive_path TEXT DEFAULT ''",
        "project_id": "ALTER TABLE documents ADD COLUMN project_id TEXT DEFAULT ''",
        "project_name": "ALTER TABLE documents ADD COLUMN project_name TEXT DEFAULT ''",
        "doc_type": "ALTER TABLE documents ADD COLUMN doc_type TEXT DEFAULT ''",
        "initiator": "ALTER TABLE documents ADD COLUMN initiator TEXT DEFAULT ''",
        "owner": "ALTER TABLE documents ADD COLUMN owner TEXT DEFAULT ''",
        "departments_json": "ALTER TABLE documents ADD COLUMN departments_json TEXT DEFAULT '[]'",
        "participants_json": "ALTER TABLE documents ADD COLUMN participants_json TEXT DEFAULT '[]'",
        "confidence": "ALTER TABLE documents ADD COLUMN confidence REAL DEFAULT 0",
        "review_status": "ALTER TABLE documents ADD COLUMN review_status TEXT DEFAULT 'need_review'",
    }.items():
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        if column not in existing:
            conn.execute(ddl)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id, doc_type, indexed_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_owner ON documents(owner, indexed_at DESC)"
    )


def init_harness_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS harness_memories (
            memory_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            domain TEXT NOT NULL,
            memory_kind TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_harness_memories_task_kind
        ON harness_memories(task_id, memory_kind);
        """
    )


def employee_department_by_name() -> dict[str, str]:
    return {profile.name: profile.department for profile in load_employee_profiles()}


def extract_project_items_from_summary(
    rel_path: str,
    doc_key: str,
    text: str,
) -> list[dict]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    dept_by_name = employee_department_by_name()
    items: list[dict] = []
    project_type_markers = ("项目类", "汇报类", "结算类", "其他")
    for index, line in enumerate(lines):
        if not line.isdigit():
            continue
        if index + 4 >= len(lines):
            continue
        project_type = lines[index + 1]
        if not any(marker in project_type for marker in project_type_markers):
            continue
        project_name = lines[index + 2]
        project_status = lines[index + 3]
        owner = lines[index + 4]
        if len(project_name) < 2 or project_name in {"方案名", "负责人"}:
            continue
        if owner in {"/", "无", "待定"}:
            owner = ""
        first_owner = re.split(r"[、,，/ ]+", owner)[0].strip() if owner else ""
        item_id = hashlib.sha1(f"{doc_key}:{line}:{project_name}".encode()).hexdigest()
        items.append(
            {
                "item_id": item_id,
                "project_name": project_name[:120],
                "project_type": project_type[:80],
                "project_status": project_status[:40],
                "owner": owner[:80],
                "owner_department": dept_by_name.get(first_owner, ""),
                "evidence": {
                    "row_no": line,
                    "source_rel_path": rel_path,
                    "raw": lines[index : min(index + 8, len(lines))],
                },
            }
        )
    return items


def write_document_memory(
    rel_path: str,
    source_path: Path,
    file_hash: str,
    extracted: ExtractedDocument,
    *,
    archive_path: Path | None = None,
    graph: DocumentGraph | None = None,
    file_size: int | None = None,
) -> str:
    doc_key = hashlib.sha1(f"{rel_path}:{file_hash}".encode()).hexdigest()
    title = source_path.stem
    summary = build_summary(extracted.text)
    tags = extract_tags(source_path.name, extracted.text)
    graph = graph or build_document_graph(source_path.name, extracted)
    chunks = chunk_text(extracted.text)
    indexed_at = now_iso()
    metadata = {
        "source": "nas",
        "source_path": str(source_path),
        "archive_path": str(archive_path or ""),
        "rel_path": rel_path,
        "warning": extracted.warning,
        "text_chars": len(extracted.text),
        "chunk_count": len(chunks),
        "graph_evidence": graph.evidence,
    }

    NAS_MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(NAS_MEMORY_DB) as conn:
        init_nas_db(conn)
        conn.execute("DELETE FROM chunks_fts WHERE doc_key = ?", (doc_key,))
        conn.execute("DELETE FROM chunks WHERE doc_key = ?", (doc_key,))
        conn.execute("DELETE FROM document_people WHERE doc_key = ?", (doc_key,))
        conn.execute("DELETE FROM document_projects WHERE doc_key = ?", (doc_key,))
        conn.execute("DELETE FROM project_items WHERE doc_key = ?", (doc_key,))
        conn.execute(
            """
            INSERT OR REPLACE INTO documents (
                doc_key, rel_path, source_path, archive_path, sha256, file_size, parser,
                project_id, project_name, doc_type, initiator, owner,
                departments_json, participants_json, confidence, review_status,
                title, summary, tags_json, indexed_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_key,
                rel_path,
                str(source_path),
                str(archive_path or ""),
                file_hash,
                int(file_size if file_size is not None else source_path.stat().st_size),
                extracted.parser,
                graph.project_id,
                graph.project_name,
                graph.doc_type,
                graph.initiator,
                graph.owner,
                json.dumps(graph.departments, ensure_ascii=False),
                json.dumps(
                    [
                        {
                            "open_id": person.open_id,
                            "name": person.name,
                            "department": person.department,
                            "role": person.role,
                        }
                        for person in graph.participants
                    ],
                    ensure_ascii=False,
                ),
                graph.confidence,
                graph.review_status,
                title,
                summary,
                json.dumps(tags, ensure_ascii=False),
                indexed_at,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO projects (
                project_id, project_name, owner, initiator, departments_json,
                confidence, review_status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                graph.project_id,
                graph.project_name,
                graph.owner,
                graph.initiator,
                json.dumps(graph.departments, ensure_ascii=False),
                graph.confidence,
                graph.review_status,
                indexed_at,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO document_projects (doc_key, project_id, relation_role) VALUES (?, ?, ?)",
            (doc_key, graph.project_id, "primary"),
        )
        for person in graph.participants:
            conn.execute(
                """
                INSERT OR REPLACE INTO people (open_id, name, department, role, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    person.open_id,
                    person.name,
                    person.department,
                    person.role,
                    indexed_at,
                ),
            )
            relation_role = "participant"
            if person.name == graph.owner:
                relation_role = "owner"
            elif person.name == graph.initiator:
                relation_role = "initiator"
            conn.execute(
                """
                INSERT OR REPLACE INTO document_people (
                    doc_key, open_id, person_name, relation_role, department
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    doc_key,
                    person.open_id,
                    person.name,
                    relation_role,
                    person.department,
                ),
            )
        if graph.review_status == "need_review":
            review_id = hashlib.sha1(f"{doc_key}:metadata".encode()).hexdigest()
            conn.execute(
                """
                INSERT OR REPLACE INTO review_queue (
                    review_id, doc_key, reason, status, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    doc_key,
                    "项目/发起人/负责人识别置信度不足",
                    "open",
                    json.dumps(
                        {
                            "project_name": graph.project_name,
                            "doc_type": graph.doc_type,
                            "initiator": graph.initiator,
                            "owner": graph.owner,
                            "participants": [
                                person.name for person in graph.participants
                            ],
                            "departments": graph.departments,
                            "confidence": graph.confidence,
                            "evidence": graph.evidence,
                        },
                        ensure_ascii=False,
                    ),
                    indexed_at,
                ),
            )
        else:
            conn.execute(
                "UPDATE review_queue SET status = 'resolved' WHERE doc_key = ? AND status = 'open'",
                (doc_key,),
            )
        for item in extract_project_items_from_summary(
            rel_path, doc_key, extracted.text
        ):
            item_project_id = hashlib.sha1(
                item["project_name"].encode("utf-8")
            ).hexdigest()[:16]
            conn.execute(
                """
                INSERT OR REPLACE INTO project_items (
                    item_id, doc_key, project_id, project_name, project_type,
                    project_status, owner, owner_department, source_rel_path,
                    evidence_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["item_id"],
                    doc_key,
                    item_project_id,
                    item["project_name"],
                    item["project_type"],
                    item["project_status"],
                    item["owner"],
                    item["owner_department"],
                    rel_path,
                    json.dumps(item["evidence"], ensure_ascii=False),
                    indexed_at,
                ),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO projects (
                    project_id, project_name, owner, initiator, departments_json,
                    confidence, review_status, updated_at
                ) VALUES (?, ?, ?, '', ?, ?, 'need_review', ?)
                """,
                (
                    item_project_id,
                    item["project_name"],
                    item["owner"],
                    json.dumps(
                        [item["owner_department"]] if item["owner_department"] else [],
                        ensure_ascii=False,
                    ),
                    0.86 if item["owner"] else 0.62,
                    indexed_at,
                ),
            )
        for index, chunk in enumerate(chunks):
            chunk_id = f"{doc_key}:{index:04d}"
            conn.execute(
                "INSERT INTO chunks (chunk_id, doc_key, chunk_index, text) VALUES (?, ?, ?, ?)",
                (chunk_id, doc_key, index, chunk),
            )
            conn.execute(
                "INSERT INTO chunks_fts (chunk_id, doc_key, title, text) VALUES (?, ?, ?, ?)",
                (chunk_id, doc_key, title, chunk),
            )

    with sqlite3.connect(HARNESS_MEMORY_DB) as conn:
        init_harness_db(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO harness_memories (
                memory_id, session_id, conversation_id, task_id, domain,
                memory_kind, title, summary, payload_json, created_at
            ) VALUES (
                COALESCE((
                    SELECT memory_id FROM harness_memories
                    WHERE task_id = ? AND memory_kind = 'nas_document'
                ), lower(hex(randomblob(16)))),
                'nas-memory', 'nas-memory', ?, 'company_knowledge',
                'nas_document', ?, ?, ?, ?
            )
            """,
            (
                f"nas:{doc_key}",
                f"nas:{doc_key}",
                title,
                summary,
                json.dumps(
                    {
                        "doc_key": doc_key,
                        "rel_path": rel_path,
                        "source_path": str(source_path),
                        "tags": tags,
                        "project_id": graph.project_id,
                        "project_name": graph.project_name,
                        "doc_type": graph.doc_type,
                        "initiator": graph.initiator,
                        "owner": graph.owner,
                        "departments": graph.departments,
                        "participants": [person.name for person in graph.participants],
                        "confidence": graph.confidence,
                        "review_status": graph.review_status,
                        "parser": extracted.parser,
                        "text_chars": len(extracted.text),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                indexed_at,
            ),
        )
    return doc_key


def is_hidden_or_excluded(path: Path, root: Path, exclude_dirs: set[str]) -> bool:
    rel_parts = path.relative_to(root).parts
    return any(part.startswith(".") or part in exclude_dirs for part in rel_parts[:-1])


def quarantine(path: Path, root: Path, failed_root: Path, reason: str) -> Path:
    rel = path.relative_to(root)
    dest = failed_root / rel
    if dest.exists():
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        dest = dest.with_name(f"{dest.stem}.{stamp}{dest.suffix}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest))
    dest.with_name(f"{dest.name}.error.txt").write_text(
        f"failed_at={datetime.now().isoformat(timespec='seconds')}\n"
        f"source={path}\nreason={reason}\n",
        encoding="utf-8",
    )
    return dest


def should_quarantine_error(reason: str) -> bool:
    transient_markers = (
        "Input/output error",
        "Operation timed out",
        "Device not configured",
        "Bad file descriptor",
        "No such file or directory",
        "Resource temporarily unavailable",
        "Stale file handle",
    )
    if any(marker in reason for marker in transient_markers):
        return False

    non_retryable_markers = (
        "File is not a zip file",
        "extracted text too short",
        "unsupported extension",
        "文档解析失败",
        "任务完成但无上传记录",
        "文件格式受支持",
        "文件内容未损坏",
    )
    return any(marker in reason for marker in non_retryable_markers)


def archive_destination(
    mount: Path, inbox: Path, path: Path, graph: DocumentGraph
) -> Path:
    filename = path.name
    project_part = safe_path_part(graph.project_name, "待确认项目")
    type_part = safe_path_part(graph.doc_type, "资料文档")
    if graph.review_status == "need_review":
        return mount / "projects" / "_待确认" / project_part / type_part / filename
    return mount / "projects" / project_part / type_part / filename


def scan_files(root: Path, exclude_dirs: set[str]) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            item
            for item in dirnames
            if not item.startswith(".") and item not in exclude_dirs
        ]
        current = Path(dirpath)
        for filename in filenames:
            if filename.startswith("."):
                continue
            path = current / filename
            if (
                path.suffix.lower() in SUPPORTED_EXTENSIONS
                and not is_hidden_or_excluded(path, root, exclude_dirs)
            ):
                files.append(path)
    return sorted(files)


def run_scan(*, inbox_only: bool, move_success: bool, full_scan: bool) -> int:
    cfg = load_config()
    mount = Path(cfg["nas"]["mount_point"])
    watch = cfg.get("watch", {})
    inbox = mount / watch.get("inbox_dir", "inbox")
    processed = mount / watch.get("processed_dir", "processed")
    failed = mount / watch.get("failed_dir", "failed")
    exclude_dirs = set(watch.get("exclude_dirs", [])) | {"failed", "archive"}
    root = inbox if inbox_only else mount
    if full_scan:
        root = mount
        move_success = False
        exclude_dirs.discard("processed")
        exclude_dirs.discard(watch.get("processed_dir", "processed"))

    state = load_state()
    files_state = state.setdefault("files", {})
    stats = {"scanned": 0, "indexed": 0, "skipped": 0, "failed": 0, "quarantined": 0}

    if not root.exists():
        log(f"SCAN root_missing path={root}")
        return 1

    for path in scan_files(root, exclude_dirs):
        stats["scanned"] += 1
        rel_path = safe_relative(path, root)
        try:
            if not path.exists():
                stats["skipped"] += 1
                log(f"SKIP vanished rel={rel_path}")
                continue
            existing = files_state.get(rel_path)
            signature = file_signature(path)
            if existing and same_file_signature(existing, signature):
                stats["skipped"] += 1
                existing["source_path"] = str(path)
                if move_success and path.is_relative_to(inbox):
                    dest = processed / path.relative_to(inbox)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(dest))
                continue

            file_hash = sha256_file(path)
            if existing and existing.get("sha256") == file_hash:
                stats["skipped"] += 1
                existing.update(signature)
                existing["source_path"] = str(path)
                if move_success and path.is_relative_to(inbox):
                    dest = processed / path.relative_to(inbox)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(dest))
                continue

            extracted = ensure_indexable_extracted(path, path, extract_text(path))
            if len(extracted.text) < 20:
                raise RuntimeError(extracted.warning or "extracted text too short")

            graph = build_document_graph(path.name, extracted)
            archive_path = (
                archive_destination(mount, inbox, path, graph)
                if move_success and path.is_relative_to(inbox)
                else None
            )
            doc_key = write_document_memory(
                rel_path,
                path,
                file_hash,
                extracted,
                archive_path=archive_path,
                graph=graph,
            )
            files_state[rel_path] = {
                "sha256": file_hash,
                "doc_key": doc_key,
                "indexed_at": now_iso(),
                "source_path": str(path),
                "parser": extracted.parser,
                **signature,
            }
            stats["indexed"] += 1
            log(f"INDEX ok rel={rel_path} doc_key={doc_key} parser={extracted.parser}")

            if move_success and path.is_relative_to(inbox):
                dest = archive_path or (processed / path.relative_to(inbox))
                if dest.exists():
                    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    dest = dest.with_name(f"{dest.stem}.{stamp}{dest.suffix}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(dest))
                log(f"ARCHIVE rel={rel_path} dest={dest}")
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            log(f"INDEX fail rel={rel_path} error={exc}")
            failed_state = files_state.setdefault(rel_path, {})
            failed_state["last_error"] = str(exc)
            failed_state["last_failed_at"] = now_iso()
            if (
                path.exists()
                and path.is_relative_to(inbox)
                and should_quarantine_error(str(exc))
            ):
                dest = quarantine(path, inbox, failed, str(exc))
                stats["quarantined"] += 1
                failed_state["quarantined_at"] = now_iso()
                failed_state["quarantine_path"] = str(dest)
                log(f"QUARANTINE rel={rel_path} dest={dest}")

    state["updated_at"] = now_iso()
    state["last_stats"] = stats
    save_state(state)
    print(json.dumps(stats, ensure_ascii=False))
    return 0 if stats["failed"] == stats["quarantined"] else 1


def run_one_path(path: Path, *, no_move: bool) -> int:
    cfg = load_config()
    mount = Path(cfg["nas"]["mount_point"])
    watch = cfg.get("watch", {})
    inbox = mount / watch.get("inbox_dir", "inbox")
    if not path.exists():
        print(json.dumps({"failed": 1, "reason": "path_missing"}, ensure_ascii=False))
        return 1
    index_path: Path | None = None
    try:
        source_size = path.stat().st_size
        index_path = staged_index_path(path, mount)
        file_hash = sha256_file(index_path)
        extracted = ensure_indexable_extracted(
            path, index_path, extract_text(index_path)
        )
        if len(extracted.text) < 20:
            raise RuntimeError(extracted.warning or "extracted text too short")
        graph = build_document_graph(path.name, extracted)
        archive_path = None
        if not no_move and path.is_relative_to(inbox):
            archive_path = archive_destination(mount, inbox, path, graph)
        rel_path = safe_relative(path, inbox if path.is_relative_to(inbox) else mount)
        doc_key = write_document_memory(
            rel_path,
            path,
            file_hash,
            extracted,
            archive_path=archive_path,
            graph=graph,
            file_size=source_size,
        )
        if archive_path:
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(archive_path))
        print(
            json.dumps(
                {
                    "indexed": 1,
                    "doc_key": doc_key,
                    "project_name": graph.project_name,
                    "doc_type": graph.doc_type,
                    "owner": graph.owner,
                    "initiator": graph.initiator,
                    "departments": graph.departments,
                    "participants": [person.name for person in graph.participants],
                    "review_status": graph.review_status,
                    "archive_path": str(archive_path or ""),
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"failed": 1, "reason": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        if index_path is not None and index_path.parent == STAGING_DIR:
            index_path.unlink(missing_ok=True)


def run_repair_state_signatures(*, inbox_only: bool) -> int:
    cfg = load_config()
    mount = Path(cfg["nas"]["mount_point"])
    watch = cfg.get("watch", {})
    inbox = mount / watch.get("inbox_dir", "inbox")
    root = inbox if inbox_only else mount

    state = load_state()
    files_state = state.setdefault("files", {})
    stats = {"scanned": 0, "updated": 0, "unchanged": 0, "missing": 0, "failed": 0}

    for rel_path, item in files_state.items():
        stats["scanned"] += 1
        raw_source_path = str(item.get("source_path") or "")
        candidates = [Path(raw_source_path)] if raw_source_path else []
        candidates.append(root / rel_path)
        path = next((candidate for candidate in candidates if candidate.exists()), None)
        if path is None:
            stats["missing"] += 1
            continue
        try:
            signature = file_signature(path)
        except OSError as exc:
            stats["failed"] += 1
            item["last_signature_error"] = str(exc)
            continue
        if same_file_signature(item, signature):
            stats["unchanged"] += 1
            continue
        item.update(signature)
        item["source_path"] = str(path)
        stats["updated"] += 1

    state["updated_at"] = now_iso()
    state["last_signature_repair"] = stats
    save_state(state)
    print(json.dumps(stats, ensure_ascii=False))
    return 0 if stats["failed"] == 0 else 1


DOMAIN_QUERY_TERMS = (
    "Method",
    "Method C",
    "openclaw",
    "KOW",
    "KOS",
    "AI",
    "API",
    "飞书",
    "文档",
    "同步",
    "机器人",
    "知识库",
    "权限",
    "舆情",
    "公私域",
    "私域",
    "社群",
    "六一",
    "五菱",
    "联盟",
    "鉴宝",
    "执行",
    "策划",
    "方案",
    "SOP",
)


def query_terms(query: str) -> list[str]:
    """Build lightweight fallback terms for Chinese and mixed-language queries."""
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        cleaned = term.strip()
        if len(cleaned) < 2:
            return
        key = cleaned.lower()
        if key in seen:
            return
        seen.add(key)
        terms.append(cleaned)

    add(query)
    for match in re.findall(r"[A-Za-z][A-Za-z0-9_+-]*|[0-9]+|[\u4e00-\u9fff]+", query):
        add(match)
        if re.fullmatch(r"[\u4e00-\u9fff]+", match):
            for term in DOMAIN_QUERY_TERMS:
                if term in match:
                    add(term)
            if 3 <= len(match) <= 10:
                for size in (2, 3):
                    for index in range(0, len(match) - size + 1):
                        add(match[index : index + size])
    query_lower = query.lower()
    for term in DOMAIN_QUERY_TERMS:
        if term.lower() in query_lower:
            add(term)
    return terms[:24]


def query_row_score(row: dict, terms: list[str], original_query: str) -> float:
    title = str(row.get("title") or "")
    project_name = str(row.get("project_name") or "")
    doc_type = str(row.get("doc_type") or "")
    parser = str(row.get("parser") or "")
    summary = str(row.get("summary") or "")
    text = str(row.get("text") or "")
    searchable = {
        "title": title.lower(),
        "project_name": project_name.lower(),
        "doc_type": doc_type.lower(),
        "summary": summary.lower(),
        "text": text.lower(),
    }

    score = 0.0
    full = original_query.strip().lower()
    if full:
        if full in searchable["title"]:
            score += 80
        if full in searchable["project_name"]:
            score += 40
        if full in searchable["summary"]:
            score += 18
        if full in searchable["text"]:
            score += 8

    for term in terms:
        term_lower = term.lower()
        if term_lower in searchable["title"]:
            score += 40
        if term_lower in searchable["project_name"]:
            score += 24
        if term_lower in searchable["doc_type"]:
            score += 12
        if term_lower in searchable["summary"]:
            score += 8
        if term_lower in searchable["text"]:
            score += 3

    if doc_type in {"SOP", "执行方案"}:
        score += 10
    if parser in {"docx", "md"}:
        score += 6
    return score


def fetch_fts_rows(
    conn: sqlite3.Connection, query: str, fetch_limit: int
) -> list[dict]:
    try:
        rows = conn.execute(
            """
            SELECT d.doc_key, d.title, d.rel_path, d.source_path, d.summary, d.tags_json,
                   d.parser, c.chunk_index, c.text,
                   d.project_name, d.doc_type, d.initiator, d.owner,
                   d.departments_json, d.participants_json, d.review_status,
                   bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
            JOIN documents d ON d.doc_key = chunks_fts.doc_key
            WHERE chunks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, fetch_limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    result = []
    for row in rows:
        item = dict(row)
        item["match_source"] = "fts"
        result.append(item)
    return result


def fetch_like_rows(
    conn: sqlite3.Connection, terms: list[str], fetch_limit: int
) -> list[dict]:
    if not terms:
        return []
    clauses: list[str] = []
    args: list[str | int] = []
    for term in terms:
        like = f"%{term}%"
        clauses.append(
            """
            (
                c.text LIKE ? OR d.title LIKE ? OR d.summary LIKE ?
                OR d.project_name LIKE ? OR d.doc_type LIKE ?
            )
            """
        )
        args.extend([like, like, like, like, like])
    args.append(fetch_limit)
    rows = conn.execute(
        f"""
        SELECT d.doc_key, d.title, d.rel_path, d.source_path, d.summary, d.tags_json,
               d.parser, c.chunk_index, c.text,
               d.project_name, d.doc_type, d.initiator, d.owner,
               d.departments_json, d.participants_json, d.review_status,
               0.0 AS rank
        FROM chunks c
        JOIN documents d ON d.doc_key = c.doc_key
        WHERE {" OR ".join(clauses)}
        ORDER BY d.indexed_at DESC, c.chunk_index
        LIMIT ?
        """,
        args,
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["match_source"] = "like"
        result.append(item)
    return result


def dedupe_query_rows(
    rows: list[dict], terms: list[str], query: str, limit: int
) -> list[dict]:
    for row in rows:
        row["score"] = round(query_row_score(row, terms, query), 3)
        if row.get("match_source") == "fts":
            row["score"] += 4
    rows.sort(
        key=lambda item: (
            -float(item.get("score") or 0),
            str(item.get("title") or ""),
            int(item.get("chunk_index") or 0),
        )
    )

    selected: list[dict] = []
    seen_titles: set[str] = set()
    seen_doc_keys: set[str] = set()
    for row in rows:
        doc_key = str(row.get("doc_key") or "")
        title = str(row.get("title") or doc_key)
        title_key = re.sub(r"\s+", " ", title).strip().lower()
        if doc_key in seen_doc_keys or title_key in seen_titles:
            continue
        seen_doc_keys.add(doc_key)
        seen_titles.add(title_key)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def run_query(query: str, limit: int) -> int:
    if not NAS_MEMORY_DB.exists():
        print("nas memory db not found", file=sys.stderr)
        return 1
    terms = query_terms(query)
    fetch_limit = max(limit * 80, limit, 1000)
    with sqlite3.connect(NAS_MEMORY_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = fetch_fts_rows(conn, query, fetch_limit)
        rows.extend(fetch_like_rows(conn, terms, fetch_limit))
    print(
        json.dumps(
            dedupe_query_rows(rows, terms, query, limit), ensure_ascii=False, indent=2
        )
    )
    return 0


def run_project_query(query: str, owner: str, limit: int) -> int:
    if not NAS_MEMORY_DB.exists():
        print("nas memory db not found", file=sys.stderr)
        return 1
    clauses: list[str] = []
    args: list[str | int] = []
    if query:
        like = f"%{query}%"
        clauses.append(
            "(project_name LIKE ? OR project_type LIKE ? OR project_status LIKE ?)"
        )
        args.extend([like, like, like])
    if owner:
        clauses.append("owner LIKE ?")
        args.append(f"%{owner}%")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    args.append(limit)
    with sqlite3.connect(NAS_MEMORY_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT project_name, project_type, project_status, owner,
                   owner_department, source_rel_path, evidence_json, updated_at
            FROM project_items
            {where}
            ORDER BY updated_at DESC, project_name
            LIMIT ?
            """,
            args,
        ).fetchall()
    print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))
    return 0


def run_review_list(limit: int) -> int:
    if not NAS_MEMORY_DB.exists():
        print("nas memory db not found", file=sys.stderr)
        return 1
    with sqlite3.connect(NAS_MEMORY_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT q.review_id, q.reason, q.status, q.payload_json,
                   d.title, d.project_name, d.doc_type, d.owner, d.initiator
            FROM review_queue q
            JOIN documents d ON d.doc_key = q.doc_key
            WHERE q.status = 'open'
            ORDER BY q.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))
    return 0


def run_unknown_people(limit: int) -> int:
    if not NAS_MEMORY_DB.exists():
        print("nas memory db not found", file=sys.stderr)
        return 1
    with sqlite3.connect(NAS_MEMORY_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT p.person_name, COUNT(*) AS doc_count,
                   GROUP_CONCAT(DISTINCT d.project_name) AS projects
            FROM document_people p
            JOIN documents d ON d.doc_key = p.doc_key
            WHERE COALESCE(p.department, '') = ''
            GROUP BY p.person_name
            ORDER BY doc_count DESC, p.person_name
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))
    return 0


def split_departments(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in re.split(r"[、,，/]+", value) if item.strip()]


def run_set_person(name: str, department: str, role: str) -> int:
    if not name or not department:
        print(
            json.dumps(
                {"failed": 1, "reason": "name_and_department_required"},
                ensure_ascii=False,
            )
        )
        return 1
    overrides = load_overrides()
    people = overrides.setdefault("people", {})
    existing = people.get(name, {})
    people[name] = {
        "department": department,
        "role": role or existing.get("role", ""),
    }
    save_overrides(overrides)
    print(
        json.dumps(
            {"updated": 1, "person": name, "department": department}, ensure_ascii=False
        )
    )
    return 0


def run_confirm_review(
    review_id: str,
    *,
    owner: str,
    initiator: str,
    doc_type: str,
    project_name: str,
    departments: str,
) -> int:
    if not NAS_MEMORY_DB.exists():
        print("nas memory db not found", file=sys.stderr)
        return 1
    with sqlite3.connect(NAS_MEMORY_DB) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT q.review_id, q.payload_json, d.project_name, d.doc_type,
                   d.owner, d.initiator, d.source_path, d.archive_path
            FROM review_queue q
            JOIN documents d ON d.doc_key = q.doc_key
            WHERE q.review_id = ?
            """,
            (review_id,),
        ).fetchone()
    if not row:
        print(
            json.dumps({"failed": 1, "reason": "review_not_found"}, ensure_ascii=False)
        )
        return 1

    payload = json.loads(row["payload_json"] or "{}")
    original_project_name = str(
        payload.get("project_name") or row["project_name"] or ""
    )
    if not original_project_name:
        print(
            json.dumps(
                {"failed": 1, "reason": "project_name_missing"}, ensure_ascii=False
            )
        )
        return 1

    overrides = load_overrides()
    project_overrides = overrides.setdefault("projects", {})
    existing = project_overrides.get(original_project_name, {})
    merged = {
        "project_name": project_name
        or existing.get("project_name")
        or original_project_name,
        "doc_type": doc_type
        or existing.get("doc_type")
        or row["doc_type"]
        or payload.get("doc_type", ""),
        "initiator": initiator or existing.get("initiator") or row["initiator"] or "",
        "owner": owner or existing.get("owner") or row["owner"] or "",
        "departments": split_departments(departments)
        or existing.get("departments")
        or payload.get("departments", []),
        "confidence": 0.98,
        "review_status": "confirmed",
    }
    project_overrides[original_project_name] = merged
    save_overrides(overrides)

    reindex_path = Path(row["archive_path"] or row["source_path"] or "")
    if not reindex_path.exists() and row["source_path"]:
        reindex_path = Path(row["source_path"])
    if reindex_path.exists():
        with process_lock():
            result = run_one_path(reindex_path, no_move=True)
        return result

    with sqlite3.connect(NAS_MEMORY_DB) as conn:
        conn.execute(
            "UPDATE review_queue SET status = 'resolved' WHERE review_id = ?",
            (review_id,),
        )
    print(
        json.dumps(
            {
                "updated": 1,
                "review_id": review_id,
                "project": original_project_name,
                "reindexed": 0,
                "reason": "source_file_missing",
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="DC-Agent NAS memory indexer")
    parser.add_argument("--once", action="store_true", help="scan once")
    parser.add_argument("--inbox-only", action="store_true", help="scan NAS inbox")
    parser.add_argument(
        "--full-scan", action="store_true", help="scan all NAS without moving files"
    )
    parser.add_argument(
        "--no-move",
        action="store_true",
        help="do not move successfully indexed inbox files to processed",
    )
    parser.add_argument("--query", help="search local NAS memory")
    parser.add_argument("--project-query", default="", help="search project item rows")
    parser.add_argument("--owner", default="", help="filter project item rows by owner")
    parser.add_argument(
        "--review-list", action="store_true", help="list open review queue"
    )
    parser.add_argument(
        "--unknown-people",
        action="store_true",
        help="list people without department mapping",
    )
    parser.add_argument(
        "--set-person", default="", help="add or update a person department override"
    )
    parser.add_argument(
        "--set-department", default="", help="department for --set-person"
    )
    parser.add_argument("--set-role", default="", help="role for --set-person")
    parser.add_argument(
        "--confirm-review",
        default="",
        help="confirm one review queue item by review_id",
    )
    parser.add_argument(
        "--set-owner", default="", help="owner value for --confirm-review"
    )
    parser.add_argument(
        "--set-initiator", default="", help="initiator value for --confirm-review"
    )
    parser.add_argument(
        "--set-doc-type", default="", help="doc type value for --confirm-review"
    )
    parser.add_argument(
        "--set-project-name", default="", help="project name value for --confirm-review"
    )
    parser.add_argument(
        "--set-departments",
        default="",
        help="comma-separated departments for --confirm-review",
    )
    parser.add_argument("--path", help="index one explicit file path")
    parser.add_argument(
        "--repair-state-signatures",
        action="store_true",
        help="backfill lightweight file signatures for already indexed files",
    )
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    if args.set_person:
        return run_set_person(args.set_person, args.set_department, args.set_role)
    if args.confirm_review:
        return run_confirm_review(
            args.confirm_review,
            owner=args.set_owner,
            initiator=args.set_initiator,
            doc_type=args.set_doc_type,
            project_name=args.set_project_name,
            departments=args.set_departments,
        )
    if args.query:
        return run_query(args.query, args.limit)
    if args.project_query or args.owner:
        return run_project_query(args.project_query, args.owner, args.limit)
    if args.review_list:
        return run_review_list(args.limit)
    if args.unknown_people:
        return run_unknown_people(args.limit)
    if args.repair_state_signatures:
        with process_lock():
            return run_repair_state_signatures(
                inbox_only=args.inbox_only or not args.full_scan,
            )
    if args.path:
        with process_lock():
            return run_one_path(Path(args.path), no_move=args.no_move)
    if not args.once:
        parser.error("--once or --query is required")
    with process_lock():
        return run_scan(
            inbox_only=args.inbox_only or not args.full_scan,
            move_success=not args.no_move,
            full_scan=args.full_scan,
        )


if __name__ == "__main__":
    raise SystemExit(main())
