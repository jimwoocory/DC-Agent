"""日常对话卡片渲染（级别 3）。

LLM 长回复（≥300 字 / 含 markdown 标题/表格）自动渲染成飞书
interactive card —— 头部彩色 title bar + lark_md 富文本，比纯文本
markdown 更精致，跟杨总看截图觉得「漂亮」的那种排版一致。

短聊天（如"在吗"）保持纯文本，避免过度卡片化。

TODO 5/25 后：提取到 dc_engines/daily_renderer/ 引擎，让 Hermes
Agent 输出也能用同一套渲染规则。
"""
