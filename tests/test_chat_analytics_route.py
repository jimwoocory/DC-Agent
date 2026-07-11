import json
import sqlite3
from pathlib import Path

import pytest
from quart import Quart

from astrbot.dashboard.routes.chat_analytics import ChatAnalyticsRoute
from astrbot.dashboard.routes.route import RouteContext


def _init_inbox_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE inbox_items (
                item_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                platform_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                text TEXT NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL,
                case_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE inbox_events (
                event_id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE feishu_ingress_audit (
                audit_id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL,
                platform_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                peer_kind TEXT NOT NULL,
                peer_id TEXT NOT NULL,
                text TEXT NOT NULL,
                allowed INTEGER NOT NULL,
                policy_reason TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                workspace TEXT NOT NULL,
                mentioned INTEGER NOT NULL,
                trusted_card_action INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE feishu_egress_audit (
                audit_id TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL,
                reply_message_id TEXT NOT NULL,
                receive_id TEXT NOT NULL,
                receive_id_type TEXT NOT NULL,
                msg_type TEXT NOT NULL,
                success INTEGER NOT NULL,
                response_code TEXT NOT NULL,
                response_message_id TEXT NOT NULL,
                content_chars INTEGER NOT NULL
            );
        """)


def _insert_inbox_item(
    db_path: Path,
    *,
    item_id: str,
    created_at: str,
    text: str,
    status: str = "in_progress",
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO inbox_items (
                item_id, session_id, conversation_id, platform_id,
                sender_id, sender_name, text, category, status, case_id,
                task_id, source, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                "lark:session",
                "lark:conversation",
                "巅池-Agent小助手",
                "ou_colleague",
                "同事A",
                text,
                "task",
                status,
                "case_1",
                "",
                "ai_inbox_plugin",
                json.dumps(
                    {
                        "is_group": False,
                        "message_id": "om_real",
                        "is_at_or_wake_command": True,
                    },
                    ensure_ascii=False,
                ),
                created_at,
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO inbox_events (
                event_id, item_id, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (f"evt_{item_id}", item_id, "item_created", "{}", created_at),
        )
        conn.commit()


def _insert_ingress_audit(
    db_path: Path,
    *,
    audit_id: str,
    created_at: str,
    sender_id: str,
    sender_name: str,
    text: str,
    allowed: bool,
    reason: str,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO feishu_ingress_audit (
                audit_id, received_at, platform_id, sender_id, sender_name,
                peer_kind, peer_id, text, allowed, policy_reason, agent_id,
                workspace, mentioned, trusted_card_action, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                created_at,
                "巅池-Agent小助手",
                sender_id,
                sender_name,
                "group",
                "oc_group",
                text,
                1 if allowed else 0,
                reason,
                "main",
                "",
                0,
                0,
                json.dumps({"message_id": f"om_{audit_id}"}, ensure_ascii=False),
            ),
        )
        conn.commit()


def _insert_egress_audit(
    db_path: Path,
    *,
    audit_id: str,
    sent_at: str,
    reply_message_id: str,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO feishu_egress_audit (
                audit_id, sent_at, reply_message_id, receive_id,
                receive_id_type, msg_type, success, response_code,
                response_message_id, content_chars
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                sent_at,
                reply_message_id,
                "",
                "",
                "post",
                1,
                "0",
                f"om_reply_{audit_id}",
                120,
            ),
        )
        conn.commit()


def _init_employee_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE employees (
                open_id TEXT PRIMARY KEY,
                platform_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                department TEXT NOT NULL,
                role TEXT NOT NULL,
                skill_tags TEXT NOT NULL,
                preferences TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                interaction_count INTEGER NOT NULL,
                relation_type TEXT NOT NULL,
                preferred_address TEXT NOT NULL,
                honorific_policy TEXT NOT NULL,
                personality_summary TEXT NOT NULL,
                communication_style TEXT NOT NULL,
                persona_evidence_count INTEGER NOT NULL,
                persona_updated_at TEXT NOT NULL
            );
        """)
        conn.execute(
            """
            INSERT INTO employees (
                open_id, platform_id, display_name, department, role,
                skill_tags, preferences, first_seen_at, last_seen_at,
                interaction_count, relation_type, preferred_address,
                honorific_policy, personality_summary, communication_style,
                persona_evidence_count, persona_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ou_colleague",
                "巅池-Agent小助手",
                "张三",
                "客户部",
                "专员",
                "[]",
                "{}",
                "",
                "",
                0,
                "employee",
                "张三",
                "formal",
                "",
                "",
                0,
                "",
            ),
        )
        conn.execute(
            """
            INSERT INTO employees (
                open_id, platform_id, display_name, department, role,
                skill_tags, preferences, first_seen_at, last_seen_at,
                interaction_count, relation_type, preferred_address,
                honorific_policy, personality_summary, communication_style,
                persona_evidence_count, persona_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ou_blocked",
                "巅池-Agent小助手",
                "李四",
                "售后部",
                "主管",
                "[]",
                "{}",
                "",
                "",
                0,
                "employee",
                "李四",
                "formal",
                "",
                "",
                0,
                "",
            ),
        )
        conn.commit()


def _init_chat_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE platform_message_history (
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                content TEXT NOT NULL,
                llm_checkpoint_id TEXT
            );
        """)


def _insert_chat_message(
    db_path: Path,
    *,
    created_at: str,
    user_id: str,
    sender_id: str,
    text: str,
) -> None:
    role = "bot" if sender_id == "bot" else "user"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO platform_message_history (
                created_at, updated_at, platform_id, user_id, sender_id,
                sender_name, content, llm_checkpoint_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                created_at,
                "巅池-Agent小助手",
                user_id,
                sender_id,
                sender_id,
                json.dumps(
                    {"type": role, "message": [{"type": "plain", "text": text}]},
                    ensure_ascii=False,
                ),
                None,
            ),
        )
        conn.commit()


@pytest.mark.asyncio
async def test_chat_analytics_uses_ai_inbox_inbound_requests(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "data" / "ai_inbox.db"
    employees_path = tmp_path / "data" / "employees.db"
    _init_inbox_db(inbox_path)
    _init_employee_db(employees_path)
    _insert_inbox_item(
        inbox_path,
        item_id="item_yesterday",
        created_at="2026-06-15T10:00:00+00:00",
        text="小助手，昨天同事发来的真实请求应该出现在看板里。",
    )

    app = Quart(__name__)
    ChatAnalyticsRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        messages = await (
            await client.get(
                "/api/chat-analytics/messages"
                "?from=2026-06-15T00:00:00%2B00:00"
                "&to=2026-06-16T00:00:00%2B00:00"
            )
        ).get_json()
        summary = await (
            await client.get(
                "/api/chat-analytics/summary"
                "?from=2026-06-15T00:00:00%2B00:00"
                "&to=2026-06-16T00:00:00%2B00:00"
            )
        ).get_json()

    assert messages["status"] == "ok"
    assert messages["data"]["total"] == 1
    assert messages["data"]["messages"][0]["text"].startswith("小助手，昨天同事")
    assert messages["data"]["messages"][0]["status"] == "in_progress"
    assert messages["data"]["messages"][0]["sender_display_name"] == "张三"
    assert messages["data"]["messages"][0]["sender_department"] == "客户部"
    assert summary["status"] == "ok"
    assert summary["data"]["metrics"]["inbox_items"] == 1
    assert summary["data"]["metrics"]["open_items"] == 1
    assert summary["data"]["top_senders"] == [{"name": "张三", "count": 1}]
    assert summary["data"]["departments"] == [{"name": "客户部", "count": 1}]


@pytest.mark.asyncio
async def test_chat_analytics_includes_blocked_ingress_audit(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "data" / "ai_inbox.db"
    employees_path = tmp_path / "data" / "employees.db"
    _init_inbox_db(inbox_path)
    _init_employee_db(employees_path)
    _insert_ingress_audit(
        inbox_path,
        audit_id="audit_blocked",
        created_at="2026-06-15T11:00:00+00:00",
        sender_id="ou_blocked",
        sender_name="ou_block",
        text="小助手，这条群消息没有进入正式处理，但也必须能分析。",
        allowed=False,
        reason="group_mention_required",
    )

    app = Quart(__name__)
    ChatAnalyticsRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        messages = await (
            await client.get(
                "/api/chat-analytics/messages"
                "?from=2026-06-15T00:00:00%2B00:00"
                "&to=2026-06-16T00:00:00%2B00:00"
            )
        ).get_json()
        summary = await (
            await client.get(
                "/api/chat-analytics/summary"
                "?from=2026-06-15T00:00:00%2B00:00"
                "&to=2026-06-16T00:00:00%2B00:00"
            )
        ).get_json()

    assert messages["status"] == "ok"
    assert messages["data"]["total"] == 1
    assert messages["data"]["messages"][0]["status"] == "blocked:group_mention_required"
    assert messages["data"]["messages"][0]["sender_display_name"] == "李四"
    assert messages["data"]["messages"][0]["sender_department"] == "售后部"
    assert summary["status"] == "ok"
    assert summary["data"]["metrics"]["messages"] == 1
    assert summary["data"]["metrics"]["inbox_items"] == 1
    assert summary["data"]["metrics"]["open_items"] == 0
    assert summary["data"]["top_senders"] == [{"name": "李四", "count": 1}]


@pytest.mark.asyncio
async def test_chat_analytics_uses_feishu_egress_as_reply_evidence(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "data" / "ai_inbox.db"
    employees_path = tmp_path / "data" / "employees.db"
    _init_inbox_db(inbox_path)
    _init_employee_db(employees_path)
    _insert_ingress_audit(
        inbox_path,
        audit_id="audit_replied",
        created_at="2026-06-15T11:00:00+00:00",
        sender_id="ou_replied",
        sender_name="同事A",
        text="帮我看一下这份方案",
        allowed=True,
        reason="dm_allowed",
    )
    _insert_egress_audit(
        inbox_path,
        audit_id="egress_replied",
        sent_at="2026-06-15T11:00:03+00:00",
        reply_message_id="om_audit_replied",
    )

    app = Quart(__name__)
    ChatAnalyticsRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        messages = await (
            await client.get(
                "/api/chat-analytics/messages"
                "?from=2026-06-15T00:00:00%2B00:00"
                "&to=2026-06-16T00:00:00%2B00:00"
            )
        ).get_json()

    assert messages["status"] == "ok"
    assert messages["data"]["total"] == 0


@pytest.mark.asyncio
async def test_chat_analytics_excludes_acknowledged_and_control_messages(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "data" / "ai_inbox.db"
    employees_path = tmp_path / "data" / "employees.db"
    _init_inbox_db(inbox_path)
    _init_employee_db(employees_path)
    _insert_inbox_item(
        inbox_path,
        item_id="item_acknowledged",
        created_at="2026-06-16T02:32:00+00:00",
        text="这条已经被系统接收，不应该进入问题看板。",
        status="acknowledged",
    )
    _insert_inbox_item(
        inbox_path,
        item_id="item_card_action",
        created_at="2026-06-16T02:33:00+00:00",
        text='__card_action__:{"value":{"action":"dismiss"}}',
        status="new",
    )
    _insert_inbox_item(
        inbox_path,
        item_id="item_waiting",
        created_at="2026-06-16T02:34:00+00:00",
        text="这条等材料，应该进入问题看板。",
        status="waiting_materials",
    )

    app = Quart(__name__)
    ChatAnalyticsRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        messages = await (
            await client.get(
                "/api/chat-analytics/messages"
                "?from=2026-06-16T00:00:00%2B00:00"
                "&to=2026-06-17T00:00:00%2B00:00"
            )
        ).get_json()
        summary = await (
            await client.get(
                "/api/chat-analytics/summary"
                "?from=2026-06-16T00:00:00%2B00:00"
                "&to=2026-06-17T00:00:00%2B00:00"
            )
        ).get_json()

    assert messages["status"] == "ok"
    assert messages["data"]["total"] == 1
    assert messages["data"]["messages"][0]["item_id"] == "item_waiting"
    assert summary["data"]["metrics"]["messages"] == 1
    assert summary["data"]["statuses"] == [{"name": "waiting_materials", "count": 1}]


@pytest.mark.asyncio
async def test_chat_analytics_excludes_new_message_when_bot_reply_exists(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "data" / "ai_inbox.db"
    chat_path = tmp_path / "data" / "data_v4.db"
    employees_path = tmp_path / "data" / "employees.db"
    _init_inbox_db(inbox_path)
    _init_chat_db(chat_path)
    _init_employee_db(employees_path)
    _insert_inbox_item(
        inbox_path,
        item_id="item_replied",
        created_at="2026-06-16T02:20:00+00:00",
        text="这条虽然还是 new，但后面已经有 bot 回复。",
        status="new",
    )
    _insert_chat_message(
        chat_path,
        created_at="2026-06-16 02:21:00",
        user_id="lark:conversation",
        sender_id="bot",
        text="已处理。",
    )

    app = Quart(__name__)
    ChatAnalyticsRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        messages = await (
            await client.get(
                "/api/chat-analytics/messages"
                "?from=2026-06-16T00:00:00%2B00:00"
                "&to=2026-06-17T00:00:00%2B00:00"
            )
        ).get_json()
        summary = await (
            await client.get(
                "/api/chat-analytics/summary"
                "?from=2026-06-16T00:00:00%2B00:00"
                "&to=2026-06-17T00:00:00%2B00:00"
            )
        ).get_json()

    assert messages["status"] == "ok"
    assert messages["data"]["total"] == 0
    assert summary["data"]["metrics"]["messages"] == 0
