"""Harness 任务状态约束注入器。

每次 LLM 调用前，把当前 session 的 active Harness task 状态注入到
system_prompt 里，并加硬约束防止 LLM 假装『已完成』『已分析』。

为什么需要：之前 hermes_escalation 派发失败但回了"已交给 Hermes"，
用户后续问"分析好了吗"，LLM 看 context 以为做了，就编出"已完成
2.2.2 部分分析"——典型 hallucination。Harness 是真相源，LLM 必须基于
task 真实状态说话，不能凭 context 想象。

TODO 5/25 后：提取到 dc_engines/harness_guard/ 引擎，让 Hermes Agent
对话流也共用同一份约束。
"""
