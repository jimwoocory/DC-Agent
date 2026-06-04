"""飞书 interactive card JSON builders。

只做卡片渲染，不读 DB、不操作 service。所有数据从入参拿。
按钮 value 约定（main.py 路由用）：
    {"action": "pet_view_tasks"}
    {"action": "pet_done_first"}
    {"action": "pet_done_task", "task_id": "<uuid>"}
"""

from __future__ import annotations

from typing import Any


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


# ── 状态卡 ────────────────────────────────────────────────────────────────


def build_status_card(
    pet: dict[str, Any],
    stats: dict[str, int],
    h5_url: str | None = None,
) -> dict[str, Any]:
    pending = stats.get("pending", 0)
    done = stats.get("done", 0)

    main_actions: list[dict[str, Any]] = [
        _btn("看看任务", {"action": "pet_view_tasks"}, btn_type="primary"),
    ]
    if pending > 0:
        main_actions.append(_btn("完成第 1 个", {"action": "pet_done_first"}))
    main_actions.extend(_h5_button(h5_url))

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**状态**：{pet.get('mood', '精神不错')}\n"
                    f"**能量**：{int(pet.get('energy', 0))} / 100\n"
                    f"**今日待办**：{pending} 个\n"
                    f"**今日已完成**：{done} 个\n"
                    f"**连续活跃**：{int(pet.get('streak_days', 0))} 天"
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
    title = f"{pet.get('pet_name', '小橘')}今天在等你"
    return _card_envelope("blue", title, elements)


# ── 任务卡 ────────────────────────────────────────────────────────────────


def build_tasks_card(
    pet: dict[str, Any],
    tasks: list[dict[str, Any]],
    h5_url: str | None = None,
) -> dict[str, Any]:
    if not tasks:
        elements: list[dict[str, Any]] = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "今天没有待办了，小橘可以躺着晒太阳 🌞",
                },
            },
        ]
        if h5_url:
            elements.append({"tag": "hr"})
            elements.append({"tag": "action", "actions": _h5_button(h5_url)})
        return _card_envelope("green", "今日清空", elements)

    lines = "\n".join(f"{i}. {task['title']}" for i, task in enumerate(tasks, start=1))
    task_buttons = [
        _btn(
            f"完成第 {i} 个",
            {"action": "pet_done_task", "task_id": task["id"]},
            btn_type="default",
        )
        for i, task in enumerate(tasks, start=1)
    ]
    task_buttons.extend(_h5_button(h5_url))

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
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**完成**：{task.get('title', '')}\n"
                    f"**能量**：+10\n"
                    f"**当前能量**：{int(pet.get('energy', 0))} / 100\n"
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
                *_h5_button(h5_url),
            ],
        },
    ]
    title = f"{pet.get('pet_name', '小橘')}吃饱了一点"
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


def render_status_text(pet: dict[str, Any], stats: dict[str, int]) -> str:
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
        return "今天没有待办了，小橘可以躺着晒太阳 🌞"
    lines = "\n".join(f"{i}. {task['title']}" for i, task in enumerate(tasks, start=1))
    return f"今天小橘帮你叼来了 {len(tasks)} 个事项\n\n{lines}\n\n回复 /done 1 可以完成第 1 个。"


def render_done_text(
    pet: dict[str, Any], task: dict[str, Any], stats: dict[str, int]
) -> str:
    return (
        "小橘吃饱了一点！\n\n"
        f"完成：{task.get('title', '')}\n"
        "能量 +10\n"
        f"当前能量：{int(pet.get('energy', 0))} / 100\n"
        f"剩余待办：{stats.get('pending', 0)} 个"
    )
