#!/usr/bin/env python3
"""Build a codex-pets compatible package from the archived self-built pet.

The source package has 10 business-state rows. codex-pets expects a 9-row atlas
with action-state rows. This script rewrites orange-cat-v1 as a hybrid manifest:
valid for codex-pets while still readable by the current desktop fallback loader.
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

ROOT = Path("/Users/dianchi/DC-Agent")
DESKTOP_ROOT = Path("/Users/dianchi/dianchi-desktop")
SOURCE_SPRITESHEET = (
    ROOT
    / "archives"
    / "自研宠物系统_2026-06-18"
    / "dianchi-desktop"
    / "pets"
    / "orange-cat-v1"
    / "spritesheet.webp"
)
TARGET_DIRS = (
    ROOT / "data" / "pet_assets" / "orange-cat-v1",
    DESKTOP_ROOT / "assets" / "pets" / "orange-cat-v1",
)

CELL_WIDTH = 192
CELL_HEIGHT = 208
COLS = 8
CODEX_ROWS = 9

# target codex row -> source self-built row
ROW_MAP = {
    0: 0,  # idle <- idle
    1: 3,  # running-right <- working
    2: 3,  # running-left <- working
    3: 7,  # waving <- happy
    4: 4,  # jumping <- success
    5: 5,  # failed <- failed
    6: 1,  # waiting <- waiting
    7: 3,  # running <- working
    8: 6,  # review <- review
}

MANIFEST = {
    "id": "orange-cat-v1",
    "displayName": "小橘",
    "description": "巅池工作宠物，小助手身份闭环的桌面表现层资源。由自研 10 行 atlas 转换为 codex-pets 9 行 atlas。",
    "kind": "animal",
    "spritesheetPath": "spritesheet.webp",
    "name": "小橘",
    "scene": "desk",
    "spritesheet": "spritesheet.webp",
    "cell": {"width": CELL_WIDTH, "height": CELL_HEIGHT},
    "colors": {
        "body": "#f7b267",
        "head": "#f5a35c",
        "ground": "#d9eadf",
        "accent": "#0f766e",
    },
    "states": {
        "idle": {"row": 0, "frames": 6, "fps": 6, "label": "待机", "color": "#0f766e"},
        "running-right": {
            "row": 1,
            "frames": 8,
            "fps": 12,
            "label": "向右移动",
            "color": "#b45309",
        },
        "running-left": {
            "row": 2,
            "frames": 8,
            "fps": 12,
            "label": "向左移动",
            "color": "#b45309",
        },
        "waving": {
            "row": 3,
            "frames": 4,
            "fps": 5,
            "label": "回应",
            "color": "#be123c",
        },
        "jumping": {
            "row": 4,
            "frames": 5,
            "fps": 14,
            "label": "跃起",
            "color": "#15803d",
        },
        "failed": {
            "row": 5,
            "frames": 8,
            "fps": 6,
            "label": "遇到问题",
            "color": "#b91c1c",
        },
        "waiting": {
            "row": 6,
            "frames": 6,
            "fps": 4,
            "label": "等待",
            "color": "#1d4ed8",
        },
        "running": {
            "row": 7,
            "frames": 6,
            "fps": 12,
            "label": "工作中",
            "color": "#b45309",
        },
        "review": {
            "row": 8,
            "frames": 6,
            "fps": 6,
            "label": "待审核",
            "color": "#475569",
        },
    },
    "codexPets": {
        "compatible": True,
        "atlas": {
            "width": CELL_WIDTH * COLS,
            "height": CELL_HEIGHT * CODEX_ROWS,
            "columns": COLS,
            "rows": CODEX_ROWS,
        },
        "source": "archives/自研宠物系统_2026-06-18/dianchi-desktop/pets/orange-cat-v1/spritesheet.webp",
        "rowMap": ROW_MAP,
    },
}


def build_spritesheet() -> Image.Image:
    source = Image.open(SOURCE_SPRITESHEET).convert("RGBA")
    expected_width = CELL_WIDTH * COLS
    if source.width != expected_width or source.height < CELL_HEIGHT * max(
        ROW_MAP.values()
    ):
        raise SystemExit(f"unexpected source size: {source.size}")
    target = Image.new(
        "RGBA", (CELL_WIDTH * COLS, CELL_HEIGHT * CODEX_ROWS), (0, 0, 0, 0)
    )
    for target_row, source_row in ROW_MAP.items():
        box = (
            0,
            source_row * CELL_HEIGHT,
            CELL_WIDTH * COLS,
            (source_row + 1) * CELL_HEIGHT,
        )
        target.paste(source.crop(box), (0, target_row * CELL_HEIGHT))
    return target


def write_package() -> None:
    target = build_spritesheet()
    for directory in TARGET_DIRS:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "pet.json").write_text(
            json.dumps(MANIFEST, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        target.save(directory / "spritesheet.webp", "WEBP", lossless=True)
        print(f"wrote {directory}")


if __name__ == "__main__":
    write_package()
