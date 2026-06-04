"""Gemini OAuth (Google AI Pro · Code Assist Standard) AstrBot Provider。

通过 Google OAuth 凭证（用户已订阅 Google AI Pro，CLI 登录拿到 PKCE token）
调 ChatGPT 风格的 Cloud Code Assist API：

- Endpoint: `https://cloudcode-pa.googleapis.com/v1internal:generateContent`
- Envelope: `{project, model, user_prompt_id, request}`
- Response wrap: `{"response": {...gemini response...}}`
- Auth: Bearer access_token（自动 refresh，避免 401）

为什么不直接走 Hermes：AstrBot 是 router 入口，频次高 + 延迟敏感，
直接调省去 hermes 的额外开销，且日常 chat 不需要 hermes 的 multi-turn
agent 框架。

模型支持（实测可用）：
- gemini-3-flash-preview     · 默认中档（替代 aihubmix Flash）
- gemini-3.1-flash-lite-preview · 极速 lite（router LLM 判断用）
- gemini-3.1-pro-preview      · 高 / 超深档

不支持：function calling、image_urls 输入（先简化）
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Literal

import httpx

from astrbot.api import logger
from astrbot.core.provider.entities import LLMResponse, ToolCallsResult
from astrbot.core.provider.func_tool_manager import ToolSet
from astrbot.core.provider.provider import Provider
from astrbot.core.provider.register import register_provider_adapter

# ─────────────────── 常量 ───────────────────

CREDENTIALS_PATH = Path("/Users/dianchi/DC-Agent/hermes-config/auth/google_oauth.json")
CLOUDCODE_BASE = "https://cloudcode-pa.googleapis.com"
GENERATE_ENDPOINT = f"{CLOUDCODE_BASE}/v1internal:generateContent"
OAUTH_REFRESH_ENDPOINT = "https://oauth2.googleapis.com/token"

# Gemini OAuth client。client_secret 不进入源码；由环境变量提供。
CLIENT_ID = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
CLIENT_SECRET_ENV = "GEMINI_OAUTH_CLIENT_SECRET"
CLIENT_SECRET_FALLBACK_ENV = "HERMES_GEMINI_CLIENT_SECRET"

# 可用 model 白名单（实测过的）
SUPPORTED_MODELS = (
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-pro-preview",
)

# reasoning_effort → thinkingBudget 映射（按用户的 512/4096/8192 三档）
EFFORT_TO_BUDGET = {
    "minimal": 0,
    "low": 512,
    "medium": 512,  # 中 = 浅层推理
    "high": 4096,  # 高 = 标准推理
    "xhigh": 8192,  # 超深 = 深度推理
}

# token 距过期 < 60 秒视为即将过期，提前 refresh
REFRESH_BUFFER_MS = 60_000


# ─────────────────── OAuth 凭证 + Refresh ───────────────────


def _load_credentials() -> dict | None:
    if not CREDENTIALS_PATH.exists():
        return None
    try:
        return json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[gemini_oauth] 读凭证失败：%s", exc)
        return None


def _parse_refresh_field(refresh_raw: str) -> tuple[str, str]:
    """hermes 把 refresh_token 跟 project_id 拼在 refresh 字段里：
    格式 `{refresh_token}|{project_id}|{managed_project_id}|`
    """
    parts = refresh_raw.split("|")
    refresh_token = parts[0] if parts else ""
    project_id = parts[1] if len(parts) > 1 else ""
    return refresh_token, project_id


def _refresh_access_token(refresh_token: str) -> tuple[str, int] | None:
    """用 refresh_token 拿新 access_token。返 (new_access_token, expires_ms) 或 None。"""
    client_secret = (
        os.environ.get(CLIENT_SECRET_ENV)
        or os.environ.get(CLIENT_SECRET_FALLBACK_ENV)
        or ""
    ).strip()
    if not client_secret:
        logger.warning(
            "[gemini_oauth] client_secret 未配置，请设置 %s",
            CLIENT_SECRET_ENV,
        )
        return None
    req_data = urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        OAUTH_REFRESH_ENDPOINT,
        data=req_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        new_access = str(result.get("access_token", "")).strip()
        if not new_access:
            return None
        new_expires_ms = (
            int(time.time() * 1000) + int(result.get("expires_in", 3600)) * 1000
        )
        return new_access, new_expires_ms
    except Exception as exc:
        logger.warning("[gemini_oauth] refresh 失败：%s", exc)
        return None


def _get_valid_token_and_project() -> tuple[str, str] | None:
    """读凭证，必要时 refresh，返 (access_token, project_id)。"""
    creds = _load_credentials()
    if not creds:
        return None
    refresh_raw = str(creds.get("refresh", ""))
    refresh_token, project_id = _parse_refresh_field(refresh_raw)
    if not refresh_token:
        logger.warning("[gemini_oauth] 凭证缺 refresh_token")
        return None

    access_token = str(creds.get("access", "")).strip()
    expires_ms = int(creds.get("expires", 0))
    now_ms = int(time.time() * 1000)

    if access_token and expires_ms > now_ms + REFRESH_BUFFER_MS:
        return access_token, project_id

    # 过期 → refresh
    new = _refresh_access_token(refresh_token)
    if not new:
        return None
    new_access, new_expires_ms = new
    # 回写凭证
    creds["access"] = new_access
    creds["expires"] = new_expires_ms
    try:
        CREDENTIALS_PATH.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("[gemini_oauth] 写回凭证失败（不阻断）：%s", exc)
    logger.info("[gemini_oauth] access_token 已刷新，新过期时间 %s", new_expires_ms)
    return new_access, project_id


# ─────────────────── Message 翻译 ───────────────────


def _to_gemini_contents(
    contexts: list[dict] | None,
    prompt: str | None,
) -> tuple[list[dict], str | None]:
    """OpenAI messages → Gemini contents。

    Gemini 格式：
        [{"role": "user|model", "parts": [{"text": "..."}]}]
        system 角色单独抽出去 → systemInstruction
    """
    contents: list[dict] = []
    system_parts: list[str] = []

    for msg in contexts or []:
        role = msg.get("role")
        content = msg.get("content")

        # 多模态先简化：抽 text
        if isinstance(content, list):
            text_pieces = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_pieces.append(str(item.get("text", "")))
                elif isinstance(item, str):
                    text_pieces.append(item)
            text = "\n".join(text_pieces)
        elif isinstance(content, str):
            text = content
        else:
            text = ""
        text = text.strip()
        if not text:
            continue

        if role == "system":
            system_parts.append(text)
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": text}]})
        elif role == "assistant":
            contents.append({"role": "model", "parts": [{"text": text}]})
        elif role == "tool":
            contents.append(
                {
                    "role": "user",
                    "parts": [{"text": f"[tool result]\n{text}"}],
                }
            )

    if prompt:
        contents.append({"role": "user", "parts": [{"text": prompt}]})

    system_instruction = "\n\n".join(system_parts) if system_parts else None
    return contents, system_instruction


def _extract_text_from_gemini(resp_data: dict) -> str:
    """从 cloudcode-pa 返回里抽 assistant 文本。

    返回结构：{"response": {"candidates": [{"content": {"parts": [{"text": "..."}]}}]}}
    """
    # cloudcode 包了一层 response
    inner = (
        resp_data.get("response")
        if isinstance(resp_data.get("response"), dict)
        else resp_data
    )
    candidates = inner.get("candidates", []) if isinstance(inner, dict) else []
    if not candidates:
        return ""
    cand = candidates[0]
    content = cand.get("content", {}) if isinstance(cand, dict) else {}
    parts = content.get("parts", []) if isinstance(content, dict) else []
    text_pieces = []
    for p in parts:
        if isinstance(p, dict) and "text" in p:
            text_pieces.append(str(p["text"]))
    return "\n".join(text_pieces)


# ─────────────────── Provider 主体 ───────────────────


@register_provider_adapter(
    "gemini_oauth_chat",
    "Gemini OAuth Chat Provider · Google AI Pro Code Assist Standard tier (扁平 0 成本)",
)
class ProviderGeminiOAuth(Provider):
    """走 Google AI Pro Code Assist OAuth 调 Gemini 3.x 系列。"""

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)
        self.timeout = int(provider_config.get("timeout", 180))

        model_name = (
            provider_config.get("model_config", {}).get("model")
            or provider_config.get("model")
            or "gemini-3-flash-preview"
        )
        self.set_model(model_name)

        # reasoning_effort（控制 thinkingBudget）
        effort = str(provider_config.get("reasoning_effort", "medium")).lower()
        if effort not in EFFORT_TO_BUDGET:
            logger.warning(
                "[gemini_oauth] reasoning_effort=%s 不合法，回退到 medium", effort
            )
            effort = "medium"
        self.reasoning_effort = effort
        self.thinking_budget = EFFORT_TO_BUDGET[effort]

        self._http = httpx.AsyncClient(timeout=self.timeout)

        # 启动时验一次凭证
        tk = _get_valid_token_and_project()
        if not tk:
            logger.warning(
                "[gemini_oauth] 启动时凭证未就绪，首次 text_chat 再试。"
                "确认 %s 存在 + gemini CLI 已 auth。",
                CREDENTIALS_PATH,
            )

        logger.info(
            "[gemini_oauth] 启动 model=%s effort=%s budget=%d timeout=%ds",
            self.model_name,
            self.reasoning_effort,
            self.thinking_budget,
            self.timeout,
        )

    # ─── 必须实现 ───

    def get_current_key(self) -> str:
        tk = _get_valid_token_and_project()
        return tk[0] if tk else ""

    def set_key(self, key: str) -> None:
        # OAuth 不接受手动 key，忽略
        pass

    async def get_models(self) -> list[str]:
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
        if image_urls:
            logger.warning("[gemini_oauth] 图片输入暂未支持，已忽略")
        if audio_urls:
            logger.warning("[gemini_oauth] 音频输入暂未支持，已忽略")
        if func_tool and not func_tool.empty():
            logger.warning(
                "[gemini_oauth] func_tool 暂未支持（v1 简化版），将忽略 %d 个工具",
                len(func_tool.tools) if hasattr(func_tool.tools, "__len__") else 0,
            )

        tk = _get_valid_token_and_project()
        if not tk:
            return LLMResponse(
                role="err",
                completion_text=(
                    "Gemini OAuth 凭证缺失或失效（"
                    f"{CREDENTIALS_PATH}）。请确认 gemini CLI 已登录。"
                ),
            )
        access_token, project_id = tk
        if not project_id:
            return LLMResponse(
                role="err",
                completion_text=(
                    "凭证里没有 project_id（refresh 字段格式异常）。"
                    "请 gemini auth 重新登录。"
                ),
            )

        # 拼 Gemini contents
        contexts_dict = self._ensure_message_to_dicts(contexts) if contexts else []
        gemini_contents, ctx_system = _to_gemini_contents(contexts_dict, prompt)
        if not gemini_contents:
            return LLMResponse(
                role="err",
                completion_text="对话内容为空，无法调用 Gemini",
            )

        # systemInstruction
        system_pieces: list[str] = []
        if system_prompt:
            system_pieces.append(system_prompt)
        if ctx_system:
            system_pieces.append(ctx_system)
        system_text = "\n\n".join(p for p in system_pieces if p).strip()

        used_model = model or self.model_name

        # 构造 request body
        inner_request: dict[str, Any] = {"contents": gemini_contents}
        if system_text:
            inner_request["systemInstruction"] = {
                "parts": [{"text": system_text}],
                "role": "user",  # Gemini 推荐 system 用 user 角色（旧版兼容）
            }
        # thinking config（按 reasoning_effort → thinkingBudget）
        if self.thinking_budget > 0:
            inner_request["generationConfig"] = {
                "thinkingConfig": {"thinkingBudget": self.thinking_budget}
            }

        envelope = {
            "project": project_id,
            "model": used_model,
            "user_prompt_id": f"astrbot-{int(time.time() * 1000)}",
            "request": inner_request,
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        try:
            resp = await self._http.post(
                GENERATE_ENDPOINT,
                json=envelope,
                headers=headers,
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                err_text = resp.text[:300]
                logger.warning(
                    "[gemini_oauth] HTTP %s model=%s err=%s",
                    resp.status_code,
                    used_model,
                    err_text,
                )
                return LLMResponse(
                    role="err",
                    completion_text=f"Gemini 调用失败 HTTP {resp.status_code}: {err_text}",
                )
            data = resp.json()
        except Exception as exc:
            logger.warning("[gemini_oauth] 调用异常 model=%s err=%s", used_model, exc)
            return LLMResponse(
                role="err",
                completion_text=f"Gemini 调用异常: {exc}",
            )

        text = _extract_text_from_gemini(data).strip()
        if not text:
            logger.warning(
                "[gemini_oauth] 返回空文本 model=%s raw=%s",
                used_model,
                str(data)[:200],
            )
            return LLMResponse(
                role="err",
                completion_text="Gemini 没返回文本（可能 quota 或模型异常）",
            )

        logger.debug(
            "[gemini_oauth] 完成 model=%s effort=%s budget=%d len=%d",
            used_model,
            self.reasoning_effort,
            self.thinking_budget,
            len(text),
        )
        return LLMResponse(role="assistant", completion_text=text)
