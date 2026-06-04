"""
新员工引导插件 (onboarding_guide)

首次对话时通过 ABC 选择引导员工完成角色配置，自动绑定对应人格。
完成后进入正常模式，不再拦截消息。

入口 class 定义在 ``onboarding_guide.py``，由 AstrBot 通过
``@register`` 装饰器加载（AstrBot 找 ``main.py`` 优先，
否则找跟目录同名的 ``<dir_name>.py``）。

历史问题：之前这里也有一份 ``OnboardingGuidePlugin`` class 定义，
跟 ``onboarding_guide.py`` 完全重复，但都没 ``@register`` 装饰器，
导致 plugin metadata=None（日志显示 "Plugin None (None) by None"）。
"""

from .onboarding_guide import OnboardingGuidePlugin

__all__ = ["OnboardingGuidePlugin"]
