"""W3+ 飞书写操作引擎（P2 接待机器人配套）。

子模块：
- ``contracts`` — ChatCreationRequest / ChatCreationResult
- ``chat_creator`` — 创建临时飞书群（im.v1.chat.create + 邀请成员）
- ``private_message_sender`` — 给试点员工 open_id 发送一对一文本私聊

复用 ``dc_engines.feishu_reader.FeishuClient`` 的凭证机制（同一个 ``feishu_whitelist.yaml``），
所以写操作 = ops 在飞书后台多勾两个权限：
  - im:chat                创建/编辑群
  - im:chat.member         管理成员
"""

from .chat_creator import ChatCreator
from .contracts import ChatCreationRequest, ChatCreationResult
from .private_message_sender import FeishuPrivateMessageSender

__all__ = [
    "ChatCreationRequest",
    "ChatCreationResult",
    "ChatCreator",
    "FeishuPrivateMessageSender",
]
