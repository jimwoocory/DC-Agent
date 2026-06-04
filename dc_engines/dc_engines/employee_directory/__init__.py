"""员工目录引擎（P1）。

存档每个公司员工的飞书 ``open_id``、角色、偏好，以及跨会话的长期记忆。
"按人"维度的记忆区别于 Harness 的"按会话"记忆。

子模块：
- ``contracts`` — Employee / EmployeeMemory 数据模型
- ``store`` — aiosqlite 存储 (data/employees.db)
"""

from .bridge import EmployeeMemoryArchiveResult, EmployeeMemoryBridge
from .contracts import Employee, EmployeeMemory, RelationType
from .requester import requester_meta_from_event
from .store import EmployeeStore
from .sync import SyncReport, sync_from_feishu

__all__ = [
    "Employee",
    "EmployeeMemoryArchiveResult",
    "EmployeeMemoryBridge",
    "EmployeeMemory",
    "RelationType",
    "EmployeeStore",
    "SyncReport",
    "requester_meta_from_event",
    "sync_from_feishu",
]
