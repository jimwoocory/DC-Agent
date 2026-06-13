"""Shared path helpers for the dc_router plugin."""

from __future__ import annotations

from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_data_path


def data_path(*parts: str) -> Path:
    return Path(get_astrbot_data_path()).joinpath(*parts)


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]
