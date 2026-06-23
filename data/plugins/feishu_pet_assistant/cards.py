"""飞书 interactive card JSON builders。

只做卡片渲染，不读 DB、不操作 service。所有数据从入参拿。
按钮 value 约定（main.py 路由用）：
    {"action": "pet_view_tasks"}
    {"action": "pet_done_first"}
    {"action": "pet_done_task", "task_id": "<uuid>"}
"""

from __future__ import annotations

from typing import Any

NO_REAL_TASKS_TEXT = "当前没有从真实任务源同步到待办；请先接入 Harness 或业务任务源。"


def _btn(text: str, value: dict[str, Any], btn_type: str = "default") -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": btn_type,
        "value": value,
    }


def _url_btn(text: str, url: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": "default",
        "url": url,
    }


def _card_envelope(
    template: str, title: str, elements: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def _h5_button(h5_url: str | None) -> list[dict[str, Any]]:
    if not h5_url:
        return []
    return [_url_btn("打开小橘房间", h5_url)]


def _desktop_button(desktop_url: str | None) -> list[dict[str, Any]]:
    if not desktop_url:
        return []
    return [_url_btn("打开桌面端", desktop_url)]


def _external_buttons(
    h5_url: str | None,
    desktop_url: str | None,
) -> list[dict[str, Any]]:
    return [*_desktop_button(desktop_url), *_h5_button(h5_url)]


_LIVE_STATE_LABELS = {
    "idle": "待机",
    "waiting": "等你回应",
    "thinking": "理解中",
    "working": "工作中",
    "success": "完成",
    "failed": "遇到问题",
    "review": "待审核",
    "happy": "开心",
    "focused": "记忆命中",
    "sleeping": "休息",
}


def _live_name(pet: dict[str, Any], live_state: dict[str, Any] | None) -> str:
    if live_state:
        return str(live_state.get("name") or pet.get("pet_name") or "小橘")
    return str(pet.get("pet_name") or "小橘")


def _live_status_label(live_state: dict[str, Any] | None) -> str:
    if not live_state:
        return ""
    state = str(live_state.get("state") or "")
    return _LIVE_STATE_LABELS.get(state, state or "待机")


def _status_lines(
    pet: dict[str, Any],
    stats: dict[str, int],
    live_state: dict[str, Any] | None,
    desktop_bound: bool | None,
) -> list[str]:
    pending = stats.get("pending", 0)
    done = stats.get("done", 0)
    if live_state:
        lines = [
            f"**Live 状态**：{_live_status_label(live_state)}",
            f"**等级 / XP**：Lv.{int(live_state.get('level') or 1)} / {int(live_state.get('xp') or 0)}",
            f"**能量**：{int(live_state.get('energy') or 0)} / 100",
            f"**金币**：{int(live_state.get('coins') or 0)}",
            f"**今日待办**：{pending} 个",
            f"**今日已完成**：{done} 个",
        ]
        if desktop_bound is not None:
            lines.append(f"**桌面端**：{'已绑定' if desktop_bound else '待绑定'}")
        return lines
    return [
        f"**状态**：{pet.get('mood', '精神不错')}",
        f"**能量**：{int(pet.get('energy', 0))} / 100",
        f"**今日待办**：{pending} 个",
        f"**今日已完成**：{done} 个",
        f"**连续活跃**：{int(pet.get('streak_days', 0))} 天",
    ]


# ── 状态卡 ────────────────────────────────────────────────────────────────


def build_status_card(
    pet: dict[str, Any],
    stats: dict[str, int],
    h5_url: str | None = None,
    live_state: dict[str, Any] | None = None,
    desktop_url: str | None = None,
    desktop_bound: bool | None = None,
) -> dict[str, Any]:
    pending = stats.get("pending", 0)

    main_actions: list[dict[str, Any]] = [
        _btn("看看任务", {"action": "pet_view_tasks"}, btn_type="primary"),
    ]
    if pending > 0:
        main_actions.append(_btn("完成第 1 个", {"action": "pet_done_first"}))
    main_actions.extend(_external_buttons(h5_url, desktop_url))

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(
                    _status_lines(pet, stats, live_state, desktop_bound)
                ),
            },
        },
        {"tag": "hr"},
        {"tag": "action", "actions": main_actions},
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": "MVP 已接入飞书长连接；按钮和命令共用同一份状态。",
                }
            ],
        },
    ]
    title = f"{_live_name(pet, live_state)}今天在等你"
    return _card_envelope("blue", title, elements)


# ── 任务卡 ────────────────────────────────────────────────────────────────


def build_tasks_card(
    pet: dict[str, Any],
    tasks: list[dict[str, Any]],
    h5_url: str | None = None,
    desktop_url: str | None = None,
) -> dict[str, Any]:
    if not tasks:
        elements: list[dict[str, Any]] = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": NO_REAL_TASKS_TEXT,
                },
            },
        ]
        external_buttons = _external_buttons(h5_url, desktop_url)
        if external_buttons:
            elements.append({"tag": "hr"})
            elements.append({"tag": "action", "actions": external_buttons})
        return _card_envelope("orange", "待办未接入", elements)

    lines = "\n".join(f"{i}. {task['title']}" for i, task in enumerate(tasks, start=1))
    task_buttons = [
        _btn(
            f"完成第 {i} 个",
            {"action": "pet_done_task", "task_id": task["id"]},
            btn_type="default",
        )
        for i, task in enumerate(tasks, start=1)
    ]
    task_buttons.extend(_external_buttons(h5_url, desktop_url))

    elements = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**今日待办**\n{lines}"},
        },
        {"tag": "hr"},
        {"tag": "action", "actions": task_buttons},
    ]
    title = f"{pet.get('pet_name', '小橘')}叼来了今日事项"
    return _card_envelope("wathet", title, elements)


# ── 完成反馈卡 ────────────────────────────────────────────────────────────


def build_done_card(
    pet: dict[str, Any],
    task: dict[str, Any],
    stats: dict[str, int],
    h5_url: str | None = None,
    live_state: dict[str, Any] | None = None,
    desktop_url: str | None = None,
) -> dict[str, Any]:
    energy = int(
        (
            (live_state or {}).get("energy")
            if live_state is not None
            else pet.get("energy", 0)
        )
        or 0
    )
    xp_line = (
        f"**等级 / XP**：Lv.{int(live_state.get('level') or 1)} / {int(live_state.get('xp') or 0)}\n"
        if live_state
        else ""
    )
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**完成**：{task.get('title', '')}\n"
                    f"**能量**：+10\n"
                    f"**当前能量**：{energy} / 100\n"
                    f"{xp_line}"
                    f"**今日已完成**：{stats.get('done', 0)} 个\n"
                    f"**剩余待办**：{stats.get('pending', 0)} 个"
                ),
            },
        },
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                _btn("看看任务", {"action": "pet_view_tasks"}, btn_type="primary"),
                *_external_buttons(h5_url, desktop_url),
            ],
        },
    ]
    title = f"{_live_name(pet, live_state)}吃饱了一点"
    return _card_envelope("green", title, elements)


# ── 错误卡 ────────────────────────────────────────────────────────────────


def build_error_card(message: str) -> dict[str, Any]:
    return _card_envelope(
        "red",
        "出了点问题",
        [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": message},
            }
        ],
    )


# ── 文本兜底（无法发卡片时用） ─────────────────────────────────────────────


def render_status_text(
    pet: dict[str, Any],
    stats: dict[str, int],
    live_state: dict[str, Any] | None = None,
    desktop_bound: bool | None = None,
) -> str:
    desktop_line = ""
    if desktop_bound is not None:
        desktop_line = f"桌面端：{'已绑定' if desktop_bound else '待绑定'}\n"
    if live_state:
        return (
            f"{_live_name(pet, live_state)}今天在等你\n\n"
            f"Live 状态：{_live_status_label(live_state)}\n"
            f"等级 / XP：Lv.{int(live_state.get('level') or 1)} / {int(live_state.get('xp') or 0)}\n"
            f"能量：{int(live_state.get('energy') or 0)} / 100\n"
            f"金币：{int(live_state.get('coins') or 0)}\n"
            f"今日待办：{stats.get('pending', 0)} 个\n"
            f"今日已完成：{stats.get('done', 0)} 个\n\n"
            f"{desktop_line}"
            "可用操作：看看任务、/done 1"
        )
    return (
        f"{pet.get('pet_name', '小橘')}今天在等你\n\n"
        f"状态：{pet.get('mood', '')}\n"
        f"能量：{int(pet.get('energy', 0))} / 100\n"
        f"今日待办：{stats.get('pending', 0)} 个\n"
        f"今日已完成：{stats.get('done', 0)} 个\n"
        f"连续活跃：{int(pet.get('streak_days', 0))} 天\n\n"
        "可用操作：看看任务、/done 1"
    )


def render_tasks_text(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return NO_REAL_TASKS_TEXT
    lines = "\n".join(f"{i}. {task['title']}" for i, task in enumerate(tasks, start=1))
    return f"今天小橘帮你叼来了 {len(tasks)} 个事项\n\n{lines}\n\n回复 /done 1 可以完成第 1 个。"


def render_done_text(
    pet: dict[str, Any],
    task: dict[str, Any],
    stats: dict[str, int],
    live_state: dict[str, Any] | None = None,
) -> str:
    energy = int(
        (
            (live_state or {}).get("energy")
            if live_state is not None
            else pet.get("energy", 0)
        )
        or 0
    )
    xp_line = (
        f"等级 / XP：Lv.{int(live_state.get('level') or 1)} / {int(live_state.get('xp') or 0)}\n"
        if live_state
        else ""
    )
    return (
        f"{_live_name(pet, live_state)}吃饱了一点！\n\n"
        f"完成：{task.get('title', '')}\n"
        "能量 +10\n"
        f"当前能量：{energy} / 100\n"
        f"{xp_line}"
        f"剩余待办：{stats.get('pending', 0)} 个"
    )
