"""Chat content analytics APIs for assistant conversations."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot.dashboard.asgi_runtime import request

from .route import Response, Route, RouteContext

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class ChatAnalyticsRoute(Route):
    def __init__(self, context: RouteContext, dc_root: Path | None = None) -> None:
        super().__init__(context)
        self.dc_root = dc_root or PROJECT_ROOT
        self.routes = {
            "/chat-analytics/summary": ("GET", self.summary),
            "/chat-analytics/messages": ("GET", self.messages),
        }
        self.register_routes()

    async def summary(self):
        inbox_path = self._inbox_db_path()
        chat_path = self._chat_db_path()
        if not inbox_path.exists():
            return Response().error("ai inbox database not found").__dict__
        window = _event_window_from_request()
        with _connect(inbox_path) as conn:
            items = _fetch_inbox_items(conn, window=window, limit=2000)
            audits = _fetch_ingress_audits(conn, window=window, limit=4000)
            total_events = _count_inbox_events(conn, window)
        reply_evidence = _load_reply_evidence(chat_path, window)
        merged_items = _problem_messages(
            _merge_ingress_and_inbox(audits, items),
            reply_evidence=reply_evidence,
        )
        total_items = len(merged_items)
        latest = _latest_item_time(merged_items)
        if not latest:
            with _connect(inbox_path) as conn:
                latest = _latest_inbox_time(conn)
        employees = _load_employee_directory(self._employees_db_path())
        items = [_enrich_sender(item, employees) for item in merged_items]

        chat_history_messages = 0
        assistant_messages = 0
        if chat_path.exists():
            with _connect(chat_path) as conn:
                chat_messages = _fetch_messages(conn, window=window, limit=2000)
                chat_history_messages = _count_messages(conn, window)
                assistant_messages = len(
                    [msg for msg in chat_messages if msg["role"] == "bot"]
                )

        active_colleagues = len({item["sender_id"] for item in items})
        total_conversations = len({item["conversation_id"] for item in items})
        platform_counts = Counter(item["platform_id"] for item in items)
        sender_counts = Counter(
            item["sender_display_name"] or item["sender_name"] or "unknown"
            for item in items
        )
        department_counts = Counter(
            item["sender_department"] or "未标注部门" for item in items
        )
        keyword_counts = _keyword_counts([item["text"] for item in items])
        status_counts = Counter(item["status"] for item in items)
        category_counts = Counter(item["category"] for item in items)
        open_items = sum(1 for item in items if _is_open_problem_status(item["status"]))
        actionable_items = sum(
            1 for item in items if item["category"] in _ACTIONABLE_CATEGORIES
        )
        avg_user_length = (
            round(
                sum(len(item["text"]) for item in items) / len(items),
                1,
            )
            if items
            else 0
        )

        return (
            Response()
            .ok(
                {
                    "window": window,
                    "latest_message_at": latest,
                    "metrics": {
                        "messages": total_items,
                        "user_messages": total_items,
                        "assistant_messages": assistant_messages,
                        "active_colleagues": active_colleagues,
                        "conversations": total_conversations,
                        "avg_user_length": avg_user_length,
                        "inbox_items": total_items,
                        "open_items": open_items,
                        "actionable_items": actionable_items,
                        "inbox_events": total_events,
                        "chat_history_messages": chat_history_messages,
                    },
                    "platforms": _counter_rows(platform_counts),
                    "top_senders": _counter_rows(sender_counts, limit=10),
                    "departments": _counter_rows(department_counts, limit=20),
                    "keywords": _counter_rows(keyword_counts, limit=20),
                    "statuses": _counter_rows(status_counts, limit=20),
                    "categories": _counter_rows(category_counts, limit=20),
                }
            )
            .__dict__
        )

    async def messages(self):
        db_path = self._inbox_db_path()
        if not db_path.exists():
            return Response().error("ai inbox database not found").__dict__
        window = _event_window_from_request()
        limit = _positive_int(request.args.get("limit"), default=80, max_value=300)
        offset = _positive_int(request.args.get("offset"), default=0)
        with _connect(db_path) as conn:
            inbox_rows = _fetch_inbox_items(conn, window=window, limit=2000)
            audit_rows = _fetch_ingress_audits(conn, window=window, limit=4000)
        reply_evidence = _load_reply_evidence(self._chat_db_path(), window)
        merged_rows = _problem_messages(
            _merge_ingress_and_inbox(audit_rows, inbox_rows),
            reply_evidence=reply_evidence,
        )
        total = len(merged_rows)
        rows = merged_rows[offset : offset + limit]
        employees = _load_employee_directory(self._employees_db_path())
        rows = [_enrich_sender(row, employees) for row in rows]
        return (
            Response().ok({"window": window, "messages": rows, "total": total}).__dict__
        )

    def _chat_db_path(self) -> Path:
        return self.dc_root / "data" / "data_v4.db"

    def _inbox_db_path(self) -> Path:
        return self.dc_root / "data" / "ai_inbox.db"

    def _employees_db_path(self) -> Path:
        return self.dc_root / "data" / "employees.db"


_PROBLEM_INBOX_STATUSES = {
    "new",
    "waiting_materials",
    "in_progress",
}

_CLOSED_INBOX_STATUSES = {
    "delivered",
    "confirmed",
    "closed",
    "ignored",
}

_NON_PROBLEM_TEXT_PREFIXES = (
    "__card_action__:",
    "/employees",
    "employees ",
    "/case ",
    "/task ",
    "/tasks",
    "tasks",
)

_ACTIONABLE_CATEGORIES = {
    "request",
    "task",
    "feedback",
    "material",
    "escalation",
    "question",
}


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_messages(
    conn: sqlite3.Connection,
    *,
    window: dict[str, str],
    limit: int,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses, params = _time_clauses(window)
    params.extend([int(limit), int(offset)])
    rows = conn.execute(
        f"""
        SELECT id, created_at, platform_id, user_id, sender_id, sender_name, content
        FROM platform_message_history
        WHERE {" AND ".join(clauses)}
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()
    return [_message_row_to_dict(row) for row in rows]


def _fetch_inbox_items(
    conn: sqlite3.Connection,
    *,
    window: dict[str, str],
    limit: int,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses, params = _time_clauses(window, converter=_iso_time)
    params.extend([int(limit), int(offset)])
    rows = conn.execute(
        f"""
        SELECT item_id, created_at, updated_at, session_id, conversation_id,
               platform_id, sender_id, sender_name, text, category, status,
               case_id, task_id, source, payload_json
        FROM inbox_items
        WHERE {" AND ".join(clauses)}
        ORDER BY created_at DESC, item_id DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()
    return [_inbox_row_to_dict(row) for row in rows]


def _fetch_ingress_audits(
    conn: sqlite3.Connection,
    *,
    window: dict[str, str],
    limit: int,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "feishu_ingress_audit"):
        return []
    clauses, params = _time_clauses(
        window,
        column="received_at",
        converter=_iso_time,
    )
    params.append(int(limit))
    rows = conn.execute(
        f"""
        SELECT audit_id, received_at, platform_id, sender_id, sender_name,
               peer_kind, peer_id, text, allowed, policy_reason, agent_id,
               workspace, mentioned, trusted_card_action, payload_json
        FROM feishu_ingress_audit
        WHERE {" AND ".join(clauses)}
        ORDER BY received_at DESC, audit_id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_audit_row_to_dict(row) for row in rows]


def _merge_ingress_and_inbox(
    audits: list[dict[str, Any]],
    inbox_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    inbox_by_message_id = {
        item["message_id"]: item for item in inbox_items if item.get("message_id")
    }
    used_inbox_ids: set[str] = set()
    merged: list[dict[str, Any]] = []
    for audit in audits:
        inbox = inbox_by_message_id.get(str(audit.get("message_id") or ""))
        if inbox:
            used_inbox_ids.add(str(inbox["id"]))
            merged.append(_merge_audit_with_inbox(audit, inbox))
        else:
            merged.append(audit)
    for item in inbox_items:
        if str(item["id"]) not in used_inbox_ids:
            merged.append(item)
    return sorted(
        merged,
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")),
        reverse=True,
    )


def _merge_audit_with_inbox(
    audit: dict[str, Any],
    inbox: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(audit)
    merged.update(
        {
            "item_id": inbox["item_id"],
            "updated_at": inbox["updated_at"],
            "session_id": inbox["session_id"],
            "conversation_id": inbox["conversation_id"],
            "category": inbox["category"],
            "status": inbox["status"],
            "case_id": inbox["case_id"],
            "task_id": inbox["task_id"],
            "source": "feishu_ingress_audit+ai_inbox_plugin",
            "processing_status": inbox["status"],
            "inbox_item_id": inbox["item_id"],
        }
    )
    if inbox.get("sender_name") and not merged.get("sender_name"):
        merged["sender_name"] = inbox["sender_name"]
    return merged


def _count_inbox_items(conn: sqlite3.Connection, window: dict[str, str]) -> int:
    clauses, params = _time_clauses(window, converter=_iso_time)
    row = conn.execute(
        f"SELECT COUNT(*) AS total FROM inbox_items WHERE {' AND '.join(clauses)}",
        params,
    ).fetchone()
    return int(row["total"] if row else 0)


def _count_inbox_events(conn: sqlite3.Connection, window: dict[str, str]) -> int:
    clauses, params = _time_clauses(window, converter=_iso_time)
    row = conn.execute(
        f"SELECT COUNT(*) AS total FROM inbox_events WHERE {' AND '.join(clauses)}",
        params,
    ).fetchone()
    return int(row["total"] if row else 0)


def _latest_inbox_time(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(created_at) AS latest FROM inbox_items").fetchone()
    return str(row["latest"] or "") if row else ""


def _latest_item_time(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    return max(str(item.get("created_at") or "") for item in items)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return bool(row)


def _count_messages(conn: sqlite3.Connection, window: dict[str, str]) -> int:
    clauses, params = _time_clauses(window)
    row = conn.execute(
        f"SELECT COUNT(*) AS total FROM platform_message_history WHERE {' AND '.join(clauses)}",
        params,
    ).fetchone()
    return int(row["total"] if row else 0)


def _count_conversations(conn: sqlite3.Connection, window: dict[str, str]) -> int:
    clauses, params = _time_clauses(window, column="updated_at")
    row = conn.execute(
        f"SELECT COUNT(*) AS total FROM conversations WHERE {' AND '.join(clauses)}",
        params,
    ).fetchone()
    return int(row["total"] if row else 0)


def _latest_message_time(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT MAX(created_at) AS latest FROM platform_message_history"
    ).fetchone()
    return str(row["latest"] or "") if row else ""


def _load_reply_evidence(
    chat_path: Path,
    window: dict[str, str],
) -> dict[str, list[str]]:
    """Load assistant reply timestamps keyed by likely conversation/session ids."""
    if not chat_path.exists():
        return {}
    try:
        with _connect(chat_path) as conn:
            clauses, params = _time_clauses(window)
            rows = conn.execute(
                f"""
                SELECT created_at, platform_id, user_id, sender_id, content
                FROM platform_message_history
                WHERE {" AND ".join(clauses)}
                ORDER BY created_at ASC, id ASC
                """,
                params,
            ).fetchall()
    except sqlite3.Error:
        return {}

    replies: dict[str, list[str]] = {}
    for row in rows:
        content = _json_loads(row["content"])
        role = str(content.get("type") or "").strip()
        sender_id = str(row["sender_id"] or "").strip().lower()
        if role != "bot" and sender_id != "bot":
            continue
        created_at = _normalize_time_string(str(row["created_at"] or ""))
        keys = {
            str(row["user_id"] or "").strip(),
            f"{row['platform_id']}:{row['user_id']}",
        }
        for key in keys:
            if key:
                replies.setdefault(key, []).append(created_at)
    return replies


def _time_clauses(
    window: dict[str, str],
    *,
    column: str = "created_at",
    converter: Any = None,
) -> tuple[list[str], list[Any]]:
    clauses = ["1 = 1"]
    params: list[Any] = []
    converter = converter or _sqlite_time
    if window["from"]:
        clauses.append(f"{column} >= ?")
        params.append(converter(window["from"]))
    if window["to"]:
        clauses.append(f"{column} < ?")
        params.append(converter(window["to"]))
    return clauses, params


def _message_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    content = _json_loads(row["content"])
    role = str(content.get("type") or "").strip() or "unknown"
    return {
        "id": int(row["id"]),
        "created_at": str(row["created_at"]),
        "platform_id": str(row["platform_id"] or ""),
        "user_id": str(row["user_id"] or ""),
        "sender_id": str(row["sender_id"] or ""),
        "sender_name": str(row["sender_name"] or ""),
        "role": role,
        "text": _message_text(content),
    }


def _inbox_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    payload = _json_loads(row["payload_json"])
    return {
        "id": str(row["item_id"]),
        "item_id": str(row["item_id"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "session_id": str(row["session_id"] or ""),
        "conversation_id": str(row["conversation_id"] or ""),
        "platform_id": str(row["platform_id"] or ""),
        "sender_id": str(row["sender_id"] or ""),
        "sender_name": str(row["sender_name"] or ""),
        "role": "user",
        "text": str(row["text"] or ""),
        "category": str(row["category"] or ""),
        "status": str(row["status"] or ""),
        "case_id": str(row["case_id"] or ""),
        "task_id": str(row["task_id"] or ""),
        "source": str(row["source"] or ""),
        "message_id": str(payload.get("message_id") or ""),
        "is_group": bool(payload.get("is_group")),
        "is_at_or_wake_command": bool(payload.get("is_at_or_wake_command")),
    }


def _audit_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    payload = _json_loads(row["payload_json"])
    allowed = bool(row["allowed"])
    status = "received" if allowed else f"blocked:{row['policy_reason'] or 'policy'}"
    return {
        "id": str(row["audit_id"]),
        "item_id": "",
        "audit_id": str(row["audit_id"]),
        "created_at": str(row["received_at"]),
        "updated_at": str(row["received_at"]),
        "session_id": str(row["peer_id"] or ""),
        "conversation_id": str(row["peer_id"] or ""),
        "platform_id": str(row["platform_id"] or ""),
        "sender_id": str(row["sender_id"] or ""),
        "sender_name": str(row["sender_name"] or ""),
        "role": "user",
        "text": str(row["text"] or ""),
        "category": "request",
        "status": status,
        "case_id": "",
        "task_id": "",
        "source": "feishu_ingress_audit",
        "message_id": str(payload.get("message_id") or ""),
        "is_group": str(row["peer_kind"] or "") == "group",
        "is_at_or_wake_command": bool(row["mentioned"]),
        "ingress_allowed": allowed,
        "ingress_reason": str(row["policy_reason"] or ""),
        "processing_status": "",
    }


def _problem_messages(
    items: list[dict[str, Any]],
    *,
    reply_evidence: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    return [
        _with_problem_reason(item)
        for item in items
        if _is_problem_message(item, reply_evidence=reply_evidence or {})
    ]


def _is_problem_message(
    item: dict[str, Any],
    *,
    reply_evidence: dict[str, list[str]],
) -> bool:
    status = str(item.get("status") or "").strip()
    if status in _CLOSED_INBOX_STATUSES:
        return False
    if _is_non_problem_control_message(item):
        return False
    if status.startswith("blocked:"):
        return True
    if status == "received":
        # Ingress audit allowed the message but no downstream inbox item exists.
        # That is exactly the "not formally handled / no visible reply" bucket.
        return not item.get("inbox_item_id")
    if status in {"waiting_materials", "in_progress"}:
        return True
    if status == "new":
        return not _has_assistant_reply(item, reply_evidence)
    if status == "acknowledged":
        return False
    return status in _PROBLEM_INBOX_STATUSES


def _is_non_problem_control_message(item: dict[str, Any]) -> bool:
    text = str(item.get("text") or "").strip()
    if not text:
        return True
    lowered = text.lower()
    return any(lowered.startswith(prefix) for prefix in _NON_PROBLEM_TEXT_PREFIXES)


def _has_assistant_reply(
    item: dict[str, Any],
    reply_evidence: dict[str, list[str]],
) -> bool:
    if not reply_evidence:
        return False
    created_at = _normalize_time_string(str(item.get("created_at") or ""))
    keys = {
        str(item.get("conversation_id") or "").strip(),
        str(item.get("session_id") or "").strip(),
        str(item.get("sender_id") or "").strip(),
        f"{item.get('platform_id')}:{item.get('conversation_id')}",
        f"{item.get('platform_id')}:{item.get('session_id')}",
        f"{item.get('platform_id')}:{item.get('sender_id')}",
    }
    for key in keys:
        for replied_at in reply_evidence.get(key, []):
            if replied_at and replied_at >= created_at:
                return True
    return False


def _is_open_problem_status(status: str) -> bool:
    return status in {"new", "waiting_materials", "in_progress"}


def _with_problem_reason(item: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(item)
    status = str(enriched.get("status") or "").strip()
    if status.startswith("blocked:"):
        reason = "channel_blocked"
    elif status == "received":
        reason = "no_formal_processing"
    elif status == "new":
        reason = "no_formal_reply"
    elif status == "waiting_materials":
        reason = "waiting_materials"
    elif status == "in_progress":
        reason = "processing_not_closed"
    else:
        reason = "problem"
    enriched["problem_reason"] = reason
    return enriched


def _load_employee_directory(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        with _connect(path) as conn:
            rows = conn.execute(
                """
                SELECT open_id, display_name, department, role, preferred_address,
                       relation_type
                FROM employees
                """
            ).fetchall()
    except sqlite3.Error:
        return {}
    employees: dict[str, dict[str, str]] = {}
    for row in rows:
        open_id = str(row["open_id"] or "").strip()
        if not open_id:
            continue
        employees[open_id] = {
            "display_name": str(row["display_name"] or "").strip(),
            "department": str(row["department"] or "").strip(),
            "role": str(row["role"] or "").strip(),
            "preferred_address": str(row["preferred_address"] or "").strip(),
            "relation_type": str(row["relation_type"] or "").strip(),
        }
    return employees


def _enrich_sender(
    item: dict[str, Any],
    employees: dict[str, dict[str, str]],
) -> dict[str, Any]:
    sender_id = str(item.get("sender_id") or "").strip()
    employee = employees.get(sender_id, {})
    display_name = (
        employee.get("display_name")
        or employee.get("preferred_address")
        or _readable_sender_name(str(item.get("sender_name") or ""), sender_id)
    )
    enriched = dict(item)
    enriched["sender_display_name"] = display_name
    enriched["sender_department"] = employee.get("department", "")
    enriched["sender_role"] = employee.get("role", "")
    enriched["sender_relation_type"] = employee.get("relation_type", "")
    enriched["sender_directory_matched"] = bool(employee)
    return enriched


def _readable_sender_name(sender_name: str, sender_id: str) -> str:
    name = sender_name.strip()
    if name and not (name.startswith("ou_") and sender_id.startswith(name)):
        return name
    return sender_id[:10] + "..." if sender_id else "unknown"


def _message_text(content: dict[str, Any]) -> str:
    parts = content.get("message")
    if isinstance(parts, list):
        texts = []
        for part in parts:
            if isinstance(part, dict):
                texts.append(str(part.get("text") or part.get("content") or ""))
            else:
                texts.append(str(part))
        return "\n".join(text for text in texts if text).strip()
    if isinstance(parts, str):
        return parts.strip()
    return str(content.get("text") or "").strip()


def _keyword_counts(texts: list[str]) -> Counter[str]:
    stopwords = {
        "一个",
        "一下",
        "这个",
        "可以",
        "怎么",
        "什么",
        "我们",
        "你们",
        "他们",
        "需要",
        "帮我",
        "进行",
        "以及",
        "如果",
        "the",
        "and",
        "for",
        "with",
    }
    counter: Counter[str] = Counter()
    for text in texts:
        normalized = "".join(
            ch if ch.isalnum() or "\u4e00" <= ch <= "\u9fff" else " " for ch in text
        )
        for token in normalized.split():
            cleaned = token.strip().lower()
            if len(cleaned) < 2 or cleaned in stopwords:
                continue
            counter[cleaned[:24]] += 1
    return counter


def _counter_rows(counter: Counter[str], limit: int = 20) -> list[dict[str, Any]]:
    return [
        {"name": name, "count": count} for name, count in counter.most_common(limit)
    ]


def _json_loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _event_window_from_request() -> dict[str, str]:
    return {
        "from": _iso_datetime_param(request.args.get("from")),
        "to": _iso_datetime_param(request.args.get("to")),
    }


def _iso_datetime_param(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _normalize_time_string(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(raw.replace(" ", "T"))
        except ValueError:
            return raw
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.isoformat(sep=" ")


def _sqlite_time(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=None).isoformat(sep=" ")


def _iso_time(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()


def _positive_int(
    value: str | None,
    *,
    default: int,
    max_value: int | None = None,
) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    parsed = max(0, parsed)
    if max_value is not None:
        parsed = min(parsed, max_value)
    return parsed
