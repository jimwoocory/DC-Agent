"""Pull recent group messages via the platform-native history API.

Why not ``platform_message_history_manager``? That table is only populated
by the WebChat adapter — QQ / Lark group messages are never written there.
So the entry point dispatches to a platform-specific fetcher based on the
adapter type, and each branch returns the same normalized GroupMessage list.

Platform fetchers:

- **QQ (aiocqhttp)**: ``bot.call_action("get_group_msg_history", ...)`` on
  the OneBot-V11 endpoint. Requires the group_id parsed out of the umo.
- **Lark**: ``lark_api.im.v1.message.alist(request)`` with container_id =
  chat_id, start_time / end_time as unix seconds (str).
- **WebChat**: read from ``platform_message_history`` table via
  ``PlatformMessageHistoryManager.get(platform_id="webchat",
  user_id=<conversation_id parsed from umo>)``.

All three branches skip messages where ``is_bot is True`` (the bot's own
posts) and the triggering message itself (matched on message_id).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from astrbot.core import logger

from .contracts import GroupMessage
from .time_range import TimeRange

if TYPE_CHECKING:
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.platform import Platform
    from astrbot.core.platform_message_history_mgr import (
        PlatformMessageHistoryManager,
    )


class UnsupportedPlatformError(RuntimeError):
    """Raised when no fetcher branch matches the adapter type.

    Surfaced back to the user so they see *why* the summary failed rather
    than getting a silent empty result.
    """


# ── 公共工具 ─────────────────────────────────────────────────────────────────


def _ensure_aware(dt: datetime) -> datetime:
    """Force aware datetime in UTC (matches GroupMessage.timestamp contract)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _to_aware(value: Any) -> datetime | None:
    """Best-effort timestamp coercion from platform-native shapes."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_aware(value)
    if isinstance(value, (int, float)):
        # OneBot 给秒，Lark 也是秒，msec 也用同一字段名也见过；先按秒解
        try:
            if value > 10**12:  # 毫秒级
                return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        # 1) ISO 格式
        try:
            return _ensure_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            pass
        # 2) 数字字符串
        try:
            return _to_aware(int(value))
        except (TypeError, ValueError):
            return None
    return None


def _filter_and_cap(
    messages: list[GroupMessage], time_range: TimeRange
) -> list[GroupMessage]:
    """Filter to window + drop bots + cap to max_count (keep tail)."""
    start = _ensure_aware(time_range.start)
    end = _ensure_aware(time_range.end)

    kept: list[GroupMessage] = []
    for msg in messages:
        if msg.get("is_bot"):
            continue
        ts = msg.get("timestamp")
        if not isinstance(ts, datetime):
            continue
        ts = _ensure_aware(ts)
        if ts < start or ts > end:
            continue
        kept.append(msg)
    # 按时间升序
    kept.sort(key=lambda m: _ensure_aware(m["timestamp"]))
    if len(kept) > time_range.max_count:
        kept = kept[-time_range.max_count :]
    return kept


# ── QQ / aiocqhttp ───────────────────────────────────────────────────────────


def _looks_like_aiocqhttp(platform_inst: Any) -> bool:
    bot = getattr(platform_inst, "bot", None)
    return bot is not None and hasattr(bot, "call_action")


def _aiocqhttp_extract_text(message_field: Any) -> str:
    """OneBot message field may be string or list of segments."""
    if isinstance(message_field, str):
        return message_field
    if isinstance(message_field, list):
        parts: list[str] = []
        for seg in message_field:
            if isinstance(seg, dict):
                t = seg.get("type", "")
                data = seg.get("data", {}) or {}
                if t == "text":
                    parts.append(str(data.get("text", "")))
                elif t == "image":
                    parts.append("[图片]")
                elif t == "file":
                    parts.append(
                        f"[文件:{data.get('file_name') or data.get('file') or ''}]"
                    )
                elif t == "record":
                    parts.append("[语音]")
                elif t == "video":
                    parts.append("[视频]")
                elif t == "at":
                    parts.append(f"@{data.get('qq', '?')}")
                elif t == "reply":
                    parts.append("[引用]")
                else:
                    parts.append(f"[{t}]")
        return "".join(parts)
    return str(message_field) if message_field is not None else ""


async def _fetch_qq(
    *,
    platform_inst: Any,
    group_id: str,
    self_bot_id: str | None,
    skip_message_id: str | None,
) -> list[GroupMessage]:
    try:
        ret = await platform_inst.bot.call_action(
            action="get_group_msg_history",
            group_id=int(group_id),
            count=200,
        )
    except Exception as exc:  # noqa: BLE001
        raise UnsupportedPlatformError(
            f"QQ get_group_msg_history 调用失败: {exc}"
        ) from exc

    raw = ret.get("messages", []) if isinstance(ret, dict) else []
    out: list[GroupMessage] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        msg_id = str(item.get("message_id") or item.get("id") or "")
        if skip_message_id and msg_id == skip_message_id:
            continue
        sender = item.get("sender") or {}
        sender_id = str(sender.get("user_id") or item.get("user_id") or "")
        sender_name = str(sender.get("nickname") or sender.get("card") or sender_id)
        is_bot = bool(self_bot_id and sender_id == str(self_bot_id))
        ts = _to_aware(item.get("time") or item.get("timestamp"))
        if ts is None:
            continue
        out.append(
            {
                "sender": sender_name,
                "sender_id": sender_id,
                "content": _aiocqhttp_extract_text(item.get("message")),
                "timestamp": ts,
                "is_bot": is_bot,
            }
        )
    return out


# ── Lark ────────────────────────────────────────────────────────────────────


def _looks_like_lark(platform_inst: Any) -> bool:
    return hasattr(platform_inst, "lark_api")


def _lark_extract_text(item: Any) -> str:
    """Lark message body 'content' 是 JSON 字符串，简化抽 text。"""
    body = getattr(item, "body", None) or {}
    content = (
        getattr(body, "content", None)
        if not isinstance(body, dict)
        else body.get("content")
    )
    if isinstance(content, str):
        # 尝试 JSON 解析
        try:
            import json as _json

            parsed = _json.loads(content)
            if isinstance(parsed, dict) and "text" in parsed:
                return str(parsed["text"])
            return content
        except Exception:
            return content
    return ""


async def _fetch_lark(
    *,
    platform_inst: Any,
    chat_id: str,
    time_range: TimeRange,
    self_bot_id: str | None,
    skip_message_id: str | None,
) -> list[GroupMessage]:
    try:
        from lark_oapi.api.im.v1 import ListMessageRequest  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise UnsupportedPlatformError(f"lark_oapi 未安装或版本不符: {exc}") from exc

    try:
        req = (
            ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(chat_id)
            .start_time(str(int(_ensure_aware(time_range.start).timestamp())))
            .end_time(str(int(_ensure_aware(time_range.end).timestamp())))
            .sort_type("ByCreateTimeAsc")
            .page_size(min(50, max(20, time_range.max_count)))
            .build()
        )
        resp = await platform_inst.lark_api.im.v1.message.alist(req)
    except Exception as exc:  # noqa: BLE001
        raise UnsupportedPlatformError(f"Lark message.list 调用失败: {exc}") from exc

    items = []
    if hasattr(resp, "success") and resp.success():
        data = getattr(resp, "data", None)
        items = getattr(data, "items", None) or []

    out: list[GroupMessage] = []
    for item in items:
        msg_id = str(getattr(item, "message_id", "") or "")
        if skip_message_id and msg_id == skip_message_id:
            continue
        sender = getattr(item, "sender", None)
        sender_id = str(getattr(sender, "id", "") or "") if sender else ""
        sender_name = str(getattr(sender, "id_type", "") or sender_id)
        is_bot = bool(self_bot_id and sender_id == str(self_bot_id))
        ts_raw = getattr(item, "create_time", None) or getattr(
            item, "update_time", None
        )
        ts = _to_aware(ts_raw)
        if ts is None:
            continue
        out.append(
            {
                "sender": sender_name,
                "sender_id": sender_id,
                "content": _lark_extract_text(item),
                "timestamp": ts,
                "is_bot": is_bot,
            }
        )
    return out


# ── WebChat ────────────────────────────────────────────────────────────────


async def _fetch_webchat(
    *,
    message_history_manager: PlatformMessageHistoryManager,
    platform_id: str,
    user_id: str,
    self_bot_id: str | None,
    skip_message_id: str | None,
) -> list[GroupMessage]:
    try:
        records = await message_history_manager.get(
            platform_id=platform_id,
            user_id=user_id,
            page=1,
            page_size=200,
        )
    except Exception as exc:  # noqa: BLE001
        raise UnsupportedPlatformError(f"WebChat history get 失败: {exc}") from exc

    out: list[GroupMessage] = []
    for rec in records or []:
        rec_id = str(getattr(rec, "id", "") or getattr(rec, "message_id", "") or "")
        if skip_message_id and rec_id == skip_message_id:
            continue
        sender_id = str(getattr(rec, "sender_id", "") or "")
        sender_name = str(getattr(rec, "sender_name", "") or sender_id or "?")
        content_field = getattr(rec, "content", None)
        if isinstance(content_field, dict):
            content = str(
                content_field.get("text") or content_field.get("content") or ""
            )
        else:
            content = str(content_field or "")
        ts = _to_aware(
            getattr(rec, "created_at", None) or getattr(rec, "timestamp", None)
        )
        if ts is None:
            continue
        is_bot = bool(self_bot_id and sender_id == str(self_bot_id))
        out.append(
            {
                "sender": sender_name,
                "sender_id": sender_id,
                "content": content,
                "timestamp": ts,
                "is_bot": is_bot,
            }
        )
    return out


# ── Dispatcher ──────────────────────────────────────────────────────────────


def _group_id_from_event(event: AstrMessageEvent) -> str | None:
    """Best-effort: 从 event.message_obj.group_id 拿 group_id（QQ/aiocqhttp）。"""
    obj = getattr(event, "message_obj", None)
    if obj is None:
        return None
    gid = getattr(obj, "group_id", None)
    if gid:
        return str(gid)
    # 兜底：从 umo 解
    umo = getattr(event, "unified_msg_origin", "") or ""
    parts = umo.split(":")
    if len(parts) >= 3 and "Group" in parts[1]:
        return parts[2].split("_")[0]
    return None


def _chat_id_from_event(event: AstrMessageEvent) -> str | None:
    """Lark chat_id 通常在 message_obj 的 raw_event 或 group_id 里。"""
    obj = getattr(event, "message_obj", None)
    if obj is None:
        return None
    return (
        getattr(obj, "group_id", None)
        or getattr(obj, "chat_id", None)
        or getattr(obj, "session_id", None)
    )


def _trigger_message_id(event: AstrMessageEvent) -> str | None:
    obj = getattr(event, "message_obj", None)
    if obj is None:
        return None
    return str(getattr(obj, "message_id", "") or "") or None


async def fetch_group_messages(
    *,
    platform_inst: Platform,
    message_history_manager: PlatformMessageHistoryManager,
    event: AstrMessageEvent,
    time_range: TimeRange,
    self_bot_id: str | None = None,
) -> list[GroupMessage]:
    """Dispatch to the platform-specific fetcher and return a normalized list.

    ``self_bot_id`` is the bot's own platform-native id; when provided it's
    used to set ``GroupMessage.is_bot`` and to filter the result.
    """
    skip_id = _trigger_message_id(event)
    raw: list[GroupMessage] = []

    try:
        if _looks_like_aiocqhttp(platform_inst):
            group_id = _group_id_from_event(event)
            if not group_id:
                raise UnsupportedPlatformError("QQ 平台未取到 group_id")
            raw = await _fetch_qq(
                platform_inst=platform_inst,
                group_id=group_id,
                self_bot_id=self_bot_id,
                skip_message_id=skip_id,
            )
        elif _looks_like_lark(platform_inst):
            chat_id = _chat_id_from_event(event)
            if not chat_id:
                raise UnsupportedPlatformError("Lark 平台未取到 chat_id")
            raw = await _fetch_lark(
                platform_inst=platform_inst,
                chat_id=chat_id,
                time_range=time_range,
                self_bot_id=self_bot_id,
                skip_message_id=skip_id,
            )
        else:
            # WebChat fallback: 用 message_history_manager
            platform_id = (
                event.get_platform_id() if hasattr(event, "get_platform_id") else ""
            )
            umo = getattr(event, "unified_msg_origin", "") or ""
            user_id = umo.rsplit(":", 1)[-1] if ":" in umo else umo
            if not platform_id or not user_id:
                raise UnsupportedPlatformError(
                    f"无法识别的平台 / session: platform_id={platform_id!r}"
                )
            raw = await _fetch_webchat(
                message_history_manager=message_history_manager,
                platform_id=platform_id,
                user_id=user_id,
                self_bot_id=self_bot_id,
                skip_message_id=skip_id,
            )
    except UnsupportedPlatformError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[group_summary] 历史拉取异常: %s", exc, exc_info=True)
        raise UnsupportedPlatformError(f"未预期错误: {exc}") from exc

    return _filter_and_cap(raw, time_range)
