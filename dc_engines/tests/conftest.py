"""共享 pytest fixtures（tmp SQLite db / 测试隔离）。

每个测试用 ``tmp_path`` 自动建独立 db 文件，不污染 ``data/`` 下的真实业务库。
asyncio_mode=auto 在 pyproject.toml 设了，所以 ``async def test_xxx`` 直接当
test 用，不需要 @pytest_asyncio.fixture 装饰。
"""

from __future__ import annotations

from pathlib import Path

import pytest_asyncio

from dc_engines.case.case_store import CaseStore
from dc_engines.employee_directory.store import EmployeeStore


@pytest_asyncio.fixture
async def employee_store(tmp_path: Path) -> EmployeeStore:
    """空 EmployeeStore，每个 test 独立 db 文件。"""
    db = tmp_path / "employees.db"
    store = EmployeeStore(str(db))
    await store.initialize()
    return store


@pytest_asyncio.fixture
async def case_store(tmp_path: Path) -> CaseStore:
    """空 CaseStore，每个 test 独立 db 文件。"""
    db = tmp_path / "cases.db"
    store = CaseStore(str(db))
    await store.initialize()
    return store
