"""Codex OAuth Chat Provider · 通过 Claude Code Codex OAuth 调 ChatGPT GPT-5.x。

特点:
- 直接复用 ~/.codex/auth.json 的 OAuth access_token（无需另搞 API key）
- 走 ChatGPT 后台 Responses API，与官方 codex CLI 相同
- 支持 reasoning_effort: minimal / low / medium / high / xhigh
- 用 user 的 ChatGPT Pro / Plus 订阅额度（不烧 API 钱）

protocol 注意:
- 走 https://chatgpt.com/backend-api/codex（不是 api.openai.com）
- 必须带 Cloudflare 反爬 headers (originator + ChatGPT-Account-ID)
- 用 client.responses.create()，输入是 input=[{type:message, role, content:[...]}]

不支持: function calling / image_urls 输入 / audio_urls / stream
（这些 DevOps_Console persona 都不需要）

TODO 5/25 后:
- 提取到 dc_engines/llm_oauth_proxy/ 引擎，AstrBot + Hermes 共用
- 支持 function calling（codex Responses API 是支持的，只是没接）
- 支持 stream（responses.stream）
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Literal

import httpx
from openai import AsyncOpenAI

from astrbot.api import logger
from astrbot.core.provider.entities import LLMResponse, ToolCallsResult
from astrbot.core.provider.func_tool_manager import ToolSet
from astrbot.core.provider.provider import Provider
from astrbot.core.provider.register import register_provider_adapter

# ─────────────────── Codex OAuth 常量 ───────────────────

CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

# 支持的模型（hermes_cli/codex_models.py 列表）
SUPPORTED_MODELS = [
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.2-codex",
    "gpt-5",
]

# 合法 reasoning effort
VALID_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}


# ─────────────────── OAuth 辅助函数 ───────────────────


def _read_codex_access_token() -> str | None:
    """从 ~/.codex/auth.json 读 access_token。"""
    try:
        if not CODEX_AUTH_PATH.exists():
            return None
        with CODEX_AUTH_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        tokens = data.get("tokens") if isinstance(data, dict) else None
        if not isinstance(tokens, dict):
            return None
        access_token = tokens.get("access_token")
        if isinstance(access_token, str) and access_token.strip():
            return access_token.strip()
    except Exception as exc:
        logger.debug("[codex_oauth] 读 token 失败: %s", exc)
    return None


def _codex_cloudflare_headers(access_token: str) -> dict:
    """Cloudflare 反爬 headers（hermes-agent/auxiliary_client.py 同款）。"""
    headers = {
        "User-Agent": "codex_cli_rs/0.0.0 (DC-Agent AstrBot)",
        "originator": "codex_cli_rs",
    }
    if not access_token:
        return headers
    try:
        parts = access_token.split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload_b64))
            acct_id = claims.get("https://api.openai.com/auth", {}).get(
                "chatgpt_account_id"
            )
            if isinstance(acct_id, str) and acct_id:
                headers["ChatGPT-Account-ID"] = acct_id
    except Exception:
        pass
    return headers


# ─────────────────── Message 翻译 ───────────────────


def _build_codex_input(
    contexts: list[dict] | None,
    prompt: str | None,
) -> tuple[list[dict], str | None]:
    """把 OpenAI 风格 messages 翻译成 codex input 格式。

    OpenAI 标准:
        [{"role": "system|user|assistant", "content": "text"}]

    Codex Responses API:
        instructions: "system text"
        input: [
            {"type": "message", "role": "user|assistant",
             "content": [{"type": "input_text"|"output_text", "text": "..."}]}
        ]

    Returns:
        (codex_input, instructions_text)
    """
    codex_input: list[dict] = []
    instructions_parts: list[str] = []

    contexts = contexts or []
    for msg in contexts:
        role = msg.get("role")
        content = msg.get("content")

        # 复杂 content（multimodal）暂取 text 部分
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")))
                elif isinstance(item, str):
                    text_parts.append(item)
            text = "\n".join(text_parts)
        elif isinstance(content, str):
            text = content
        else:
            text = ""

        if not text.strip():
            continue

        if role == "system":
            instructions_parts.append(text)
        elif role == "user":
            codex_input.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                }
            )
        elif role == "assistant":
            codex_input.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            )
        elif role == "tool":
            # 工具结果以 user 角色注入（简化版）
            codex_input.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": f"[tool result]\n{text}"}
                    ],
                }
            )

    # 最新 prompt 拼到尾
    if prompt:
        codex_input.append(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        )

    instructions = "\n\n".join(instructions_parts) if instructions_parts else None
    return codex_input, instructions


def _extract_text_from_response(resp: Any) -> str:
    """从 Responses API 返回里抽 assistant 文本。"""
    # 1) 优先用 output_text 便捷字段（如果有）
    direct = getattr(resp, "output_text", None)
    if isinstance(direct, str) and direct.strip():
        return direct

    # 2) 遍历 output items
    parts: list[str] = []
    output = getattr(resp, "output", None) or []
    for item in output:
        item_type = getattr(item, "type", None) or (
            item.get("type") if isinstance(item, dict) else None
        )
        if item_type != "message":
            continue
        content = getattr(item, "content", None) or (
            item.get("content") if isinstance(item, dict) else None
        )
        if not content:
            continue
        for blk in content:
            blk_type = getattr(blk, "type", None) or (
                blk.get("type") if isinstance(blk, dict) else None
            )
            if blk_type in ("output_text", "text"):
                txt = getattr(blk, "text", None) or (
                    blk.get("text") if isinstance(blk, dict) else None
                )
                if isinstance(txt, str) and txt:
                    parts.append(txt)
    return "\n".join(parts)


# ─────────────────── Provider 主体 ───────────────────


@register_provider_adapter(
    "codex_oauth_chat",
    "Codex OAuth Chat Provider · 用 ChatGPT Pro 订阅调 GPT-5.x（响应式 API）",
)
class ProviderCodexOAuth(Provider):
    """走 Claude Code Codex OAuth 调 ChatGPT 后台 GPT-5.x 模型。"""

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)

        self.timeout = int(provider_config.get("timeout", 300))
        model_name = provider_config.get("model_config", {}).get(
            "model"
        ) or provider_config.get("model", "gpt-5.5")
        self.set_model(model_name)

        # 读取 reasoning_effort（可写在 provider_config["reasoning_effort"]）
        effort = str(provider_config.get("reasoning_effort", "high")).lower()
        if effort not in VALID_EFFORTS:
            logger.warning(
                "[codex_oauth] reasoning_effort=%s 不合法，回退到 high", effort
            )
            effort = "high"
        self.reasoning_effort = effort

        # OAuth token 启动时校验一次
        self.token = _read_codex_access_token()
        if not self.token:
            logger.warning(
                "[codex_oauth] 启动时 ~/.codex/auth.json 不存在或无 token。"
                "首次 text_chat 会再读一次。"
            )

        # AsyncOpenAI client（每次 chat 都会刷 token，避免 OAuth 过期）
        self._client: AsyncOpenAI | None = None
        self._rebuild_client()

        logger.info(
            "[codex_oauth] provider 启动 model=%s effort=%s timeout=%ds",
            self.model_name,
            self.reasoning_effort,
            self.timeout,
        )

    def _rebuild_client(self) -> None:
        """根据最新 token 重建 client（OAuth 可能 refresh，需要拿新值）。"""
        token = _read_codex_access_token() or self.token or ""
        self.token = token
        headers = _codex_cloudflare_headers(token)
        self._client = AsyncOpenAI(
            api_key=token or "dummy",  # SDK 要求非空，OAuth 走 headers
            base_url=CODEX_BASE_URL,
            default_headers=headers,
            timeout=self.timeout,
            http_client=httpx.AsyncClient(timeout=self.timeout),
        )

    # ─── 必须实现的抽象方法 ───

    def get_current_key(self) -> str:
        return self.token or ""

    def set_key(self, key: str) -> None:
        # OAuth 不接受手动 key，但留个 hook 防 AstrBot 主动调
        self.token = key or ""
        self._rebuild_client()

    async def get_models(self) -> list[str]:
        # codex 没有 /v1/models 接口，返回硬编码列表
        return list(SUPPORTED_MODELS)

    # ─── 核心：text_chat ───

    async def text_chat(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool: ToolSet | None = None,
        contexts: list[dict] | None = None,
        system_prompt: str | None = None,
        tool_calls_result: ToolCallsResult | list[ToolCallsResult] | None = None,
        model: str | None = None,
        extra_user_content_parts: list[Any] | None = None,
        tool_choice: Literal["auto", "required"] = "auto",
        **kwargs,
    ) -> LLMResponse:
        # 多模态暂未支持
        if image_urls:
            logger.warning("[codex_oauth] 图片输入暂未支持，已忽略")
        if audio_urls:
            logger.warning("[codex_oauth] 音频输入暂未支持，已忽略")

        # 确保 token 是最新的（codex CLI 会定期 refresh auth.json）
        latest_token = _read_codex_access_token()
        if latest_token and latest_token != self.token:
            logger.debug("[codex_oauth] 检测到 token 更新，重建 client")
            self.set_key(latest_token)

        if not self.token:
            return LLMResponse(
                role="err",
                completion_text="Codex OAuth token 缺失（~/.codex/auth.json）。"
                "请在终端跑 `codex login` 重新授权。",
            )

        # 拼 input + instructions
        contexts_dict = self._ensure_message_to_dicts(contexts) if contexts else []
        codex_input, ctx_instructions = _build_codex_input(contexts_dict, prompt)

        # ─── 处理 tool_calls_result（上一轮工具结果，回写给 codex）───
        if tool_calls_result:
            tcr_list = (
                tool_calls_result
                if isinstance(tool_calls_result, list)
                else [tool_calls_result]
            )
            for tcr in tcr_list:
                # tool_calls_info: AssistantMessageSegment(tool_calls=list[ToolCall])
                info = getattr(tcr, "tool_calls_info", None)
                calls = getattr(info, "tool_calls", None) if info else None
                if calls:
                    for tc in calls:
                        if hasattr(tc, "id"):  # ToolCall pydantic
                            call_id = tc.id
                            fn = tc.function
                            fn_name = getattr(fn, "name", "") if fn else ""
                            fn_args = getattr(fn, "arguments", "{}") if fn else "{}"
                        else:  # dict 形式
                            call_id = tc.get("id", "")
                            fn = tc.get("function", {})
                            fn_name = fn.get("name", "")
                            fn_args = fn.get("arguments", "{}")
                        if not isinstance(fn_args, str):
                            fn_args = json.dumps(fn_args, ensure_ascii=False)
                        codex_input.append(
                            {
                                "type": "function_call",
                                "call_id": call_id,
                                "name": fn_name,
                                "arguments": fn_args or "{}",
                            }
                        )
                # tool_calls_result: list[ToolCallMessageSegment(content, tool_call_id)]
                for tres in getattr(tcr, "tool_calls_result", []) or []:
                    content = getattr(tres, "content", None)
                    tcid = getattr(tres, "tool_call_id", "") or ""
                    if not isinstance(content, str):
                        content = "" if content is None else str(content)
                    codex_input.append(
                        {
                            "type": "function_call_output",
                            "call_id": tcid,
                            "output": content,
                        }
                    )

        # system_prompt（来自 persona）拼到 instructions 头部
        instructions_pieces: list[str] = []
        if system_prompt:
            instructions_pieces.append(system_prompt)
        if ctx_instructions:
            instructions_pieces.append(ctx_instructions)
        instructions = "\n\n".join(p for p in instructions_pieces if p) or None

        if not codex_input:
            return LLMResponse(
                role="err",
                completion_text="对话内容为空，无法调用 codex",
            )

        # 实际用的 model（允许 runtime 覆盖）
        used_model = model or self.model_name

        request_kwargs: dict[str, Any] = {
            "model": used_model,
            "input": codex_input,
            "store": False,
            "reasoning": {"effort": self.reasoning_effort, "summary": "auto"},
            "include": ["reasoning.encrypted_content"],
        }
        if instructions:
            request_kwargs["instructions"] = instructions

        # ─── 翻译 AstrBot ToolSet → codex tools 字段 ───
        codex_tools: list[dict] = []
        if func_tool and not func_tool.empty():
            for ft in func_tool.tools:
                if hasattr(ft, "active") and not ft.active:
                    continue
                params = ft.parameters or {"type": "object", "properties": {}}
                codex_tools.append(
                    {
                        "type": "function",
                        "name": ft.name,
                        "description": ft.description or "",
                        "strict": False,
                        "parameters": params,
                    }
                )
        if codex_tools:
            request_kwargs["tools"] = codex_tools
            # tool_choice 透传，但只承认 codex 已知值
            if isinstance(tool_choice, str) and tool_choice in ("auto", "required"):
                request_kwargs["tool_choice"] = tool_choice
            else:
                request_kwargs["tool_choice"] = "auto"
            request_kwargs["parallel_tool_calls"] = True
            logger.debug(
                "[codex_oauth] 启用 function calling tools=%d choice=%s",
                len(codex_tools),
                request_kwargs["tool_choice"],
            )

        # Codex Responses API 强制 stream=true，必须用 stream() 聚合输出
        text_parts: list[str] = []
        tool_calls_collected: list[dict] = []  # {id, name, arguments}
        response_id: str | None = None

        try:
            assert self._client is not None
            async with self._client.responses.stream(**request_kwargs) as stream:
                async for event in stream:
                    event_type = getattr(event, "type", "")
                    # ─ 文本 delta ─
                    if event_type == "response.output_text.delta":
                        delta = getattr(event, "delta", "") or ""
                        if delta:
                            text_parts.append(delta)
                    elif event_type == "response.output_text.done":
                        full = getattr(event, "text", "") or ""
                        if full and not text_parts:
                            text_parts.append(full)
                    # ─ 工具调用 item 完成 ─
                    elif event_type == "response.output_item.done":
                        item = getattr(event, "item", None)
                        item_type = getattr(item, "type", None) or (
                            item.get("type") if isinstance(item, dict) else None
                        )
                        if item_type == "function_call":
                            call_id = (
                                getattr(item, "call_id", None)
                                or (
                                    item.get("call_id")
                                    if isinstance(item, dict)
                                    else None
                                )
                                or getattr(item, "id", None)
                                or (item.get("id") if isinstance(item, dict) else None)
                                or ""
                            )
                            name = (
                                getattr(item, "name", None)
                                or (
                                    item.get("name") if isinstance(item, dict) else None
                                )
                                or ""
                            )
                            args = (
                                getattr(item, "arguments", None)
                                or (
                                    item.get("arguments")
                                    if isinstance(item, dict)
                                    else None
                                )
                                or "{}"
                            )
                            if not isinstance(args, str):
                                args = json.dumps(args, ensure_ascii=False)
                            if name:
                                tool_calls_collected.append(
                                    {
                                        "id": call_id,
                                        "name": name,
                                        "arguments": args,
                                    }
                                )
                    elif event_type == "response.completed":
                        resp = getattr(event, "response", None)
                        response_id = getattr(resp, "id", None) if resp else None
        except Exception as exc:
            if text_parts or tool_calls_collected:
                logger.warning(
                    "[codex_oauth] responses.stream 尾部异常但已收到输出 "
                    "model=%s err=%s",
                    used_model,
                    exc,
                )
            else:
                logger.warning(
                    "[codex_oauth] responses.stream 失败 model=%s err=%s",
                    used_model,
                    exc,
                )
                return LLMResponse(
                    role="err",
                    completion_text=f"Codex 调用失败: {exc}",
                )

        text = "".join(text_parts).strip()

        # ─── 优先返回工具调用 ───
        if tool_calls_collected:
            args_dicts: list[dict] = []
            for tc in tool_calls_collected:
                try:
                    parsed = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except Exception:
                    parsed = {}
                if not isinstance(parsed, dict):
                    parsed = {"_raw": parsed}
                args_dicts.append(parsed)
            logger.info(
                "[codex_oauth] 返回工具调用 model=%s tools=%s",
                used_model,
                [tc["name"] for tc in tool_calls_collected],
            )
            return LLMResponse(
                role="assistant",
                completion_text=text or None,
                tools_call_args=args_dicts,
                tools_call_name=[tc["name"] for tc in tool_calls_collected],
                tools_call_ids=[tc["id"] for tc in tool_calls_collected],
            )

        # ─── 纯文本 ───
        if not text:
            logger.warning(
                "[codex_oauth] codex 返回空文本 model=%s response_id=%s",
                used_model,
                response_id,
            )
            return LLMResponse(
                role="err",
                completion_text="Codex 没返回文本（可能 quota / 模型异常）",
            )

        logger.debug(
            "[codex_oauth] 完成 model=%s effort=%s len=%d",
            used_model,
            self.reasoning_effort,
            len(text),
        )

        return LLMResponse(
            role="assistant",
            completion_text=text,
        )
