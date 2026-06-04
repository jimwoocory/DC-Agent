"""Alert Channel —— DC-Agent 全栈告警推送引擎。

理念：
- 任何想发"告警 / 通知"的组件都走这里 —— 看门狗、codex 诊断、token 健康、
  LLM 异常、知识库同步失败 ……
- 一个引擎抹平 macOS / 飞书 / (未来) 微信 / 钉钉 / 短信 的差异
- 调用方只关心"发什么 / 多紧急"，不关心"用哪个通道"

新功能怎么用：

>>> from dc_engines.alert_channel import send_alert
>>> send_alert(
...     title="🚨 hermes_gateway 挂了",
...     body="codex 诊断: 进程崩溃, 建议 launchctl kickstart -k ai.hermes.gateway",
...     level="critical",
... )

或者从 bash 调（看门狗用）：

    .venv/bin/python -m dc_engines.alert_channel \
        --title "..." --body "..." --level critical

level 与渠道映射默认在 data/config/alert_channel.yaml 配置：
- info     → macos
- warning  → macos + lark
- critical → macos + lark
"""

from .client import (
    AlertLevel,
    AlertResult,
    send_alert,
    send_alert_async,
)
from .config import AlertChannelConfig, load_config

__all__ = [
    "AlertLevel",
    "AlertResult",
    "AlertChannelConfig",
    "load_config",
    "send_alert",
    "send_alert_async",
]
