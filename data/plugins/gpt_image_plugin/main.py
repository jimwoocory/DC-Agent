"""GPT Image 2 (Codex OAuth) · AstrBot 生图主力工具。

调用方式：复刻 hermes-agent/plugins/image_gen/openai-codex 的协议，
独立运行，不依赖 hermes-agent 模块。

LLM 工具:
- generate_image(prompt, quality='medium', aspect_ratio='landscape')
  - quality: low (~15s) / medium (~40s · 默认) / high (~2min · 最强)
  - aspect_ratio: landscape / square / portrait

输出: 图片保存到 ~/DC-Agent/hermes-config/cache/images/，AstrBot 发回飞书

TODO: 5/25 后提取到 dc_engines/image_gen_engine/ 引擎，AstrBot + Hermes 共用
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import os
import re
import shlex
import subprocess
import urllib.request
import uuid
from pathlib import Path

from dc_engines.card_runtime import finalize_card_via_runtime
from dc_engines.feishu_card_streamer import (
    WaitingCardHandle,
    build_media_generation_card,
    start_waiting_card_for_event,
)
from dc_engines.media_sop import (
    build_media_generation_record,
    build_structured_media_prompt,
)

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# ─────────────────── 配置 ───────────────────

CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_CHAT_MODEL = "gpt-5.4"  # host 模型（实际生图用 gpt-image-2 工具）
API_MODEL = "gpt-image-2"

IMAGE_SIZES = {
    "landscape": "1536x1024",
    "square": "1024x1024",
    "portrait": "1024x1536",
}

IMAGE_CACHE_DIR = Path("/Users/dianchi/DC-Agent/hermes-config/cache/images")
IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_HARNESS_TERMINAL_STATUSES = {"completed", "cancelled", "failed"}
_IMAGE_TASK_DOMAINS = {"department_workflow:brand_publicity"}

INSTRUCTIONS = (
    "You are an assistant that must fulfill image generation requests by "
    "using the image_generation tool when provided."
)


# ─────────────────── Codex OAuth 辅助函数（独立复刻）───────────────────


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
        logger.debug("[gpt_image] 读取 codex token 失败: %s", exc)
    return None


def _codex_cloudflare_headers(access_token: str) -> dict:
    """Cloudflare 反爬 headers（复刻 hermes-agent/auxiliary_client.py:444）。"""
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


# ─────────────────── 核心生图调用 ───────────────────


def _save_b64_png(b64_data: str, model_id: str) -> Path:
    """保存 b64 PNG 到缓存目录，返回路径。"""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:8]
    safe_name = model_id.replace("/", "_").replace("-", "-")
    path = IMAGE_CACHE_DIR / f"astrbot_{safe_name}_{ts}_{uid}.png"
    path.write_bytes(base64.b64decode(b64_data))
    return path


def _call_codex_image_gen(
    prompt: str,
    quality: str,
    aspect_ratio: str,
) -> tuple[bool, str]:
    """同步调用 Codex Responses API 生图，返 (success, image_path_or_error)."""
    token = _read_codex_access_token()
    if not token:
        return False, "未找到 Codex OAuth token (~/.codex/auth.json)"

    try:
        import openai
    except ImportError:
        return False, "openai SDK 未装"

    size = IMAGE_SIZES.get(aspect_ratio, IMAGE_SIZES["landscape"])

    image_b64: str | None = None
    try:
        # 保留 OS 代理（用户机器需要走科学上网到 chatgpt.com）
        client = openai.OpenAI(
            api_key=token,
            base_url=CODEX_BASE_URL,
            default_headers=_codex_cloudflare_headers(token),
            timeout=300.0,
        )

        with client.responses.stream(
            model=CODEX_CHAT_MODEL,
            store=False,
            instructions=INSTRUCTIONS,
            input=[
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
            tools=[
                {
                    "type": "image_generation",
                    "model": API_MODEL,
                    "size": size,
                    "quality": quality,
                    "output_format": "png",
                    "background": "opaque",
                }
            ],
            tool_choice={
                "type": "allowed_tools",
                "mode": "required",
                "tools": [{"type": "image_generation"}],
            },
        ) as stream:
            for event in stream:
                event_type = getattr(event, "type", "")
                if event_type == "response.output_item.done":
                    item = getattr(event, "item", None)
                    if getattr(item, "type", None) == "image_generation_call":
                        result = getattr(item, "result", None)
                        if isinstance(result, str) and result:
                            image_b64 = result

        if not image_b64:
            return False, "Codex 没返回图片数据（可能 quota 用完或模型异常）"

        path = _save_b64_png(image_b64, f"{API_MODEL}-{quality}")
        return True, str(path)

    except Exception as exc:
        if image_b64:
            logger.warning("[gpt_image] Codex 尾部异常但已收到图片: %s", exc)
            path = _save_b64_png(image_b64, f"{API_MODEL}-{quality}")
            return True, str(path)
        logger.warning("[gpt_image] Codex 调用失败: %s", exc)
        return False, f"Codex 调用失败: {exc}"


# ─────────────────── Dreamina 即梦自动降级 ───────────────────


# 即梦 aspect_ratio 映射
DREAMINA_RATIOS = {
    "landscape": "16:9",
    "square": "1:1",
    "portrait": "9:16",
}


def _dreamina_text2image_sync(prompt: str, aspect_ratio: str) -> tuple[bool, str]:
    """同步调用 dreamina CLI 生图，返 (success, image_local_path_or_error)。

    用 subprocess 直接调，不依赖 dreamina_plugin（避免插件耦合）。
    """
    ratio = DREAMINA_RATIOS.get(aspect_ratio, "1:1")
    command = [
        "dreamina",
        "text2image",
        "--prompt",
        prompt,
        "--ratio",
        ratio,
        "--resolution_type",
        "2k",
        "--poll",
        "600",
    ]
    shell_command = " ".join(shlex.quote(arg) for arg in command)

    try:
        result = subprocess.run(
            shell_command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ},
            cwd=os.getcwd(),
        )
    except subprocess.TimeoutExpired:
        return False, "Dreamina 执行超时（600s）"
    except FileNotFoundError:
        return False, "未找到 dreamina CLI（npm i -g @ai-tools/dreamina）"
    except Exception as exc:
        return False, f"Dreamina 执行异常: {exc}"

    output = result.stdout or ""
    if result.stderr:
        output += f"\n错误：{result.stderr}"

    if result.returncode != 0:
        # 常见登陆 / 积分问题
        return False, f"Dreamina CLI 失败 (rc={result.returncode}): {output[:300]}"

    # 检查 gen_status（CLI 0 但任务可能失败）
    try:
        m = re.search(r'\{.*"gen_status".*\}', output, re.DOTALL)
        if m:
            data = json.loads(m.group())
            if data.get("gen_status") == "fail":
                return False, f"Dreamina 生成失败: {data.get('fail_reason', '?')}"
    except Exception:
        pass

    # 提取图片 URL
    url_match = re.search(r'https?://[^\s<>"]+\.(?:jpg|png)[^\s<>"]*', output)
    if not url_match:
        return False, "Dreamina 输出未找到图片 URL"

    image_url = url_match.group()
    # 下载到本地缓存
    try:
        suffix = ".png" if ".png" in image_url else ".jpg"
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = uuid.uuid4().hex[:8]
        local_path = IMAGE_CACHE_DIR / f"astrbot_dreamina_fallback_{ts}_{uid}{suffix}"
        urllib.request.urlretrieve(image_url, str(local_path))
        return True, str(local_path)
    except Exception as exc:
        return False, f"Dreamina 图片下载失败: {exc}"


# ─────────────────── AstrBot Plugin ───────────────────


@register(
    "gpt_image_plugin",
    "dc_agent",
    "GPT Image 2 生图（Codex OAuth · 主用 · dreamina 备用）",
    "1.0.0",
)
class GPTImagePlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)

    async def _load_image_harness_tasks(self, event: AstrMessageEvent):
        harness_engine = getattr(self.context, "harness_engine", None)
        store = getattr(harness_engine, "store", None)
        if harness_engine is None or store is None:
            return []

        task_ids: list[str] = []
        raw_task_id = event.get_extra("department_workflow_task_id")
        raw_task_ids = (
            raw_task_id
            if isinstance(raw_task_id, (list, tuple, set))
            else [raw_task_id]
        )
        for value in raw_task_ids:
            if isinstance(value, str) and value.strip():
                task_ids.append(value.strip())

        if task_ids:
            tasks = []
            for task_id in dict.fromkeys(task_ids):
                try:
                    task = await store.get_task(task_id)
                except Exception:  # noqa: BLE001
                    task = None
                if (
                    task is not None
                    and task.status not in _HARNESS_TERMINAL_STATUSES
                    and task.domain in _IMAGE_TASK_DOMAINS
                ):
                    tasks.append(task)
            return tasks

        try:
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
            if not conv_id:
                return []
            recent = await store.list_tasks_for_conversation(conv_id, limit=5)
        except Exception:  # noqa: BLE001
            return []
        return [
            task
            for task in recent
            if task.status not in _HARNESS_TERMINAL_STATUSES
            and task.domain in _IMAGE_TASK_DOMAINS
        ]

    async def _settle_image_harness_tasks(
        self,
        event: AstrMessageEvent,
        *,
        success: bool,
        prompt: str,
        provider: str,
        quality: str,
        aspect_ratio: str,
        detail: str,
    ) -> None:
        tasks = await self._load_image_harness_tasks(event)
        if not tasks:
            return

        harness_engine = getattr(self.context, "harness_engine", None)
        if harness_engine is None:
            return

        result = {
            "summary": "图片已生成并返回。"
            if success
            else "图片生成失败，已记录失败原因。",
            "response_preview": detail[:500],
            "source": "gpt_image_plugin",
            "quality": "success" if success else "error",
            "image_provider": provider,
            "image_quality": quality,
            "aspect_ratio": aspect_ratio,
            "prompt": prompt[:1000],
        }
        if success:
            result["image_path"] = detail
        else:
            result["error"] = detail[:1000]

        for task in tasks:
            try:
                if success:
                    await harness_engine.complete_task(task.task_id, result=result)
                else:
                    await harness_engine.set_status(
                        task.task_id,
                        "failed",
                        result=result,
                        event_payload={"reason": detail[:200]},
                    )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[gpt_image] harness settle failed task=%s",
                    task.task_id,
                    exc_info=True,
                )

    async def _start_image_waiting_card(
        self,
        event: AstrMessageEvent,
        *,
        prompt: str,
        quality: str,
        aspect_ratio: str,
    ) -> WaitingCardHandle | None:
        return await start_waiting_card_for_event(
            self.context,
            event,
            title="生图任务",
            brief=prompt,
            reasoning_tier="high" if quality == "high" else "medium",
            current_stage=f"GPT Image 2 正在生成 · {quality} · {aspect_ratio}",
            interval_sec=5.0,
        )

    async def _finish_image_waiting_card(
        self,
        card: WaitingCardHandle | None,
        *,
        prompt: str,
        success: bool,
        detail: str,
    ) -> bool:
        if card is None:
            return False
        stream = card.streamer.get_stream(card.message_id)
        elapsed_sec = stream.elapsed_sec if stream else 0
        record = build_media_generation_record(
            media_kind="image",
            prompt=prompt,
            engine="GPT Image 2",
            status="succeeded" if success else "failed",
            output_path=detail if success else "",
            error_hint="" if success else detail,
        )
        if success:
            final_card = build_media_generation_card(
                task_title="生图任务",
                media_type="image",
                status="已完成",
                prompt=record.to_card_detail(),
                engine="GPT Image 2",
                task_id=record.record_id,
                output_url=detail,
                elapsed_sec=elapsed_sec,
            )
        else:
            final_card = build_media_generation_card(
                task_title="生图任务",
                media_type="image",
                status="失败",
                prompt=record.to_card_detail(),
                engine="GPT Image 2",
                task_id=record.record_id,
                error_hint=detail,
                elapsed_sec=elapsed_sec,
            )
        return await finalize_card_via_runtime(
            card.streamer,
            card_type="media_generation",
            message_id=card.message_id,
            card=final_card,
            platform_id="",
            detail=f"gpt image generation finalized record={record.record_id}",
        )

    @filter.llm_tool(name="generate_image")
    async def tool_generate_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
        quality: str = "medium",
        aspect_ratio: str = "landscape",
    ):
        """生成图片（主用 · GPT Image 2）。当用户要画图、生成图片、做插画、海报、视觉素材时调用。

        Args:
            prompt(string): 图片内容描述。优先使用内容 SOP 生成的结构化 prompt；若传入原始描述，工具会先包装为结构化媒体 prompt。
            quality(string): 质量档位 - low(~15秒,快速试草稿) / medium(~40秒,默认/平衡) / high(~2分钟,最高质量) 。默认 medium。
            aspect_ratio(string): 画幅 - landscape(横版1536x1024) / square(方版1024x1024) / portrait(竖版1024x1536)。默认 landscape。
        """
        yield
        # 验证参数
        if quality not in ("low", "medium", "high"):
            quality = "medium"
        if aspect_ratio not in IMAGE_SIZES:
            aspect_ratio = "landscape"
        prompt = build_structured_media_prompt(
            prompt,
            media_kind="image",
            aspect_ratio=aspect_ratio,
        )

        waiting_card = await self._start_image_waiting_card(
            event,
            prompt=prompt,
            quality=quality,
            aspect_ratio=aspect_ratio,
        )
        yield event.plain_result(
            f"🎨 正在用 GPT Image 2 ({quality}) 生图: {prompt[:80]}\n"
            f"画幅 {aspect_ratio} ({IMAGE_SIZES[aspect_ratio]})，请稍候..."
        )

        # 异步执行（避免阻塞事件循环）
        loop = asyncio.get_event_loop()
        try:
            success, result = await loop.run_in_executor(
                None,
                _call_codex_image_gen,
                prompt,
                quality,
                aspect_ratio,
            )
        except asyncio.CancelledError:
            detail = "生图工具被 AstrBot 外层超时取消，未能返回图片。"
            await self._settle_image_harness_tasks(
                event,
                success=False,
                prompt=prompt,
                provider="gpt-image-2",
                quality=quality,
                aspect_ratio=aspect_ratio,
                detail=detail,
            )
            await self._finish_image_waiting_card(
                waiting_card,
                prompt=prompt,
                success=False,
                detail=detail,
            )
            raise

        if success:
            image_path = result
            logger.info(
                "[gpt_image] 生图成功 %s (quality=%s prompt=%r)",
                image_path,
                quality,
                prompt[:30],
            )
            await self._finish_image_waiting_card(
                waiting_card,
                prompt=prompt,
                success=True,
                detail=f"图片已生成，会在下一条消息里发送（GPT Image 2 · {quality}）。",
            )
            await self._settle_image_harness_tasks(
                event,
                success=True,
                prompt=prompt,
                provider="gpt-image-2",
                quality=quality,
                aspect_ratio=aspect_ratio,
                detail=image_path,
            )
            yield event.image_result(image_path)
            yield event.plain_result(f"✅ 已生成 (GPT Image 2 · {quality})")
            return

        # ─── gpt-image-2 失败 → 自动降级到即梦 dreamina ───
        gpt_error = result
        logger.warning(
            "[gpt_image] GPT Image 2 失败，自动降级到 Dreamina: %s",
            gpt_error,
        )
        yield event.plain_result(
            "⚠️ GPT Image 2 暂时不可用（多半是网络/代理），自动切换即梦继续生图..."
        )
        if waiting_card:
            await waiting_card.update_stage(
                "GPT Image 2 暂不可用，Dreamina 即梦兜底生成中"
            )

        try:
            d_success, d_result = await loop.run_in_executor(
                None,
                _dreamina_text2image_sync,
                prompt,
                aspect_ratio,
            )
        except asyncio.CancelledError:
            detail = "Dreamina 降级生成被 AstrBot 外层超时取消，未能返回图片。"
            await self._settle_image_harness_tasks(
                event,
                success=False,
                prompt=prompt,
                provider="dreamina",
                quality=quality,
                aspect_ratio=aspect_ratio,
                detail=detail,
            )
            await self._finish_image_waiting_card(
                waiting_card,
                prompt=prompt,
                success=False,
                detail=detail,
            )
            raise

        if d_success:
            logger.info(
                "[gpt_image] Dreamina 降级生图成功 %s prompt=%r",
                d_result,
                prompt[:30],
            )
            await self._finish_image_waiting_card(
                waiting_card,
                prompt=prompt,
                success=True,
                detail="图片已生成，会在下一条消息里发送（Dreamina 即梦 · 自动降级）。",
            )
            await self._settle_image_harness_tasks(
                event,
                success=True,
                prompt=prompt,
                provider="dreamina",
                quality=quality,
                aspect_ratio=aspect_ratio,
                detail=d_result,
            )
            yield event.image_result(d_result)
            yield event.plain_result("✅ 已生成 (Dreamina 即梦 · 自动降级)")
        else:
            logger.warning("[gpt_image] Dreamina 降级也失败: %s", d_result)
            failure = (
                f"❌ GPT Image 2 + Dreamina 都失败了\n"
                f"GPT Image 2: {gpt_error}\n"
                f"Dreamina: {d_result}\n"
                f"请检查网络代理 / Dreamina 登陆状态"
            )
            await self._finish_image_waiting_card(
                waiting_card,
                prompt=prompt,
                success=False,
                detail=failure,
            )
            await self._settle_image_harness_tasks(
                event,
                success=False,
                prompt=prompt,
                provider="gpt-image-2+dreamina",
                quality=quality,
                aspect_ratio=aspect_ratio,
                detail=failure,
            )
            yield event.plain_result(failure)
