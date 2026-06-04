"""
即梦 AI CLI 插件 - 让 AstrBot 可以调用即梦 CLI 进行图片和视频生成
"""

import asyncio
import json
import os
import re
import shlex
import subprocess
import tempfile
import urllib.request
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
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Star, register


@register(
    "dreamina_plugin",
    "dreamina_plugin",
    "即梦 AI CLI 插件，支持文生图、文生视频、配音生成等功能",
    "0.0.2",
)
class DreaminaPlugin(Star):
    def __init__(self, context) -> None:
        super().__init__(context)
        self.context = context
        self.last_image_path: str | None = None  # 最近生成图片的本地临时文件路径

    async def initialize(self) -> None:
        """插件初始化"""
        # 验证 dreamina CLI 是否可用
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["dreamina", "-h"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                logger.info("✅ 即梦 CLI 已安装并可用")
            else:
                logger.warning("❌ 即梦 CLI 安装异常")
        except FileNotFoundError:
            logger.error("❌ 未找到 dreamina 命令，请先安装即梦 CLI")
        except Exception as e:
            logger.error(f"❌ 即梦 CLI 验证失败：{e}")

    async def _start_waiting_card(
        self,
        event: AstrMessageEvent,
        *,
        title: str,
        prompt: str,
        stage: str,
    ) -> WaitingCardHandle | None:
        return await start_waiting_card_for_event(
            self.context,
            event,
            title=title,
            brief=prompt,
            reasoning_tier="xhigh",
            current_stage=stage,
            interval_sec=5.0,
        )

    async def _finish_waiting_card(
        self,
        card: WaitingCardHandle | None,
        *,
        title: str,
        prompt: str,
        success: bool,
        detail: str,
    ) -> bool:
        if card is None:
            return False
        stream = card.streamer.get_stream(card.message_id)
        elapsed_sec = stream.elapsed_sec if stream else 0
        media_kind = (
            "image2video"
            if "图片转视频" in title
            else ("video" if "视频" in title or "配音" in title else "image")
        )
        record = build_media_generation_record(
            media_kind=media_kind,
            prompt=prompt,
            engine="Dreamina 即梦",
            status="succeeded" if success else "failed",
            output_url=detail if success else "",
            error_hint="" if success else detail,
        )
        if success:
            final_card = build_media_generation_card(
                task_title=title,
                media_type=media_kind,
                status="已完成",
                prompt=record.to_card_detail(),
                engine="Dreamina 即梦",
                task_id=record.record_id,
                output_url=detail,
                elapsed_sec=elapsed_sec,
            )
        else:
            final_card = build_media_generation_card(
                task_title=title,
                media_type=media_kind,
                status="失败",
                prompt=record.to_card_detail(),
                engine="Dreamina 即梦",
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
            detail=f"dreamina media generation finalized record={record.record_id}",
        )

    async def _execute_dreamina(
        self, command: list, timeout: int = 300, _retry: int = 3
    ) -> tuple[bool, str]:
        """执行即梦 CLI 命令

        Args:
            command: 命令参数列表
            timeout: 超时时间（秒）
            _retry: ExceedConcurrencyLimit 时的最大重试次数

        Returns:
            (success, output)
        """
        for attempt in range(1, _retry + 1):
            try:
                full_command = ["dreamina"] + command
                logger.info(f"执行命令（第 {attempt} 次）：{' '.join(full_command)}")

                shell_command = " ".join(shlex.quote(arg) for arg in full_command)

                result = await asyncio.to_thread(
                    subprocess.run,
                    shell_command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env={**os.environ},
                    cwd=os.getcwd(),
                )

                logger.info(f"返回码：{result.returncode}")
                logger.info(
                    f"stdout: {result.stdout[:500] if result.stdout else '(空)'}"
                )
                if result.stderr:
                    logger.warning(f"stderr: {result.stderr[:500]}")

                output = result.stdout
                if result.stderr:
                    output += f"\n错误：{result.stderr}"

                if not output and result.returncode != 0:
                    output = f"命令执行失败 (返回码：{result.returncode})，可能原因：\n1. 需要登录 (请先运行：dreamina login)\n2. 积分不足\n3. 参数错误"
                elif not output:
                    output = f"命令执行成功但无输出 (返回码：{result.returncode})"

                # 检测并发限制错误，等待后重试
                if "ExceedConcurrencyLimit" in output and attempt < _retry:
                    wait = 10 * attempt
                    logger.warning(
                        f"触发并发限制，{wait} 秒后重试（{attempt}/{_retry}）"
                    )
                    await asyncio.sleep(wait)
                    continue

                return result.returncode == 0, output

            except subprocess.TimeoutExpired:
                return False, f"命令执行超时（{timeout}秒）"
            except Exception as e:
                logger.error(f"执行异常：{e}")
                return False, f"执行失败：{str(e)}"

        return False, "多次重试后仍触发并发限制，请稍后再试"

    async def _execute_voiceover(
        self,
        *,
        prompt: str,
        voice: str = "明媚女声",
        timeout: int = 240,
    ) -> tuple[bool, dict | str]:
        """Run the browser-backed Dreamina voiceover helper.

        The official Dreamina CLI does not expose audio generation yet, so this
        helper uses the dedicated Playwright Chrome profile created under
        data/temp/playwright/dreamina-profile.
        """
        repo_root = Path.cwd()
        script_path = repo_root / "scripts-tools" / "dreamina_voiceover_generate.mjs"
        node_path = (
            Path.home()
            / ".cache"
            / "codex-runtimes"
            / "codex-primary-runtime"
            / "dependencies"
            / "node"
            / "bin"
            / "node"
        )
        node_cmd = str(node_path) if node_path.exists() else "node"
        env = {
            **os.environ,
            "DREAMINA_VOICE_NAME": voice,
        }
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [node_cmd, str(script_path), prompt],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd=str(repo_root),
            )
        except subprocess.TimeoutExpired:
            return False, f"配音生成超时（{timeout}秒）"
        except Exception as e:
            logger.error(f"配音生成脚本执行异常：{e}")
            return False, f"配音生成脚本执行失败：{e}"

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if stderr:
            logger.warning(f"voiceover stderr: {stderr[:500]}")
        if result.returncode != 0:
            return False, (stdout + "\n" + stderr).strip() or "配音生成脚本返回失败"

        try:
            json_start = stdout.find("{")
            payload = json.loads(stdout[json_start:] if json_start >= 0 else stdout)
        except Exception as e:
            logger.error(f"配音生成脚本输出解析失败：{e}; stdout={stdout[:500]}")
            return False, f"配音生成脚本输出解析失败：{stdout[:500]}"

        if not payload.get("ok"):
            return False, payload
        saved_paths = payload.get("savedPaths") or []
        if not saved_paths:
            return False, payload
        return True, payload

    def _check_gen_status(self, output: str) -> tuple[bool, str]:
        """检查生成任务的实际状态（CLI 返回码可能为 0 但任务本身失败）

        Returns:
            (is_success, fail_reason_or_empty)
        """
        try:
            json_match = re.search(r'\{.*"gen_status".*\}', output, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                gen_status = data.get("gen_status", "")
                if gen_status == "fail":
                    fail_reason = data.get("fail_reason", "未知原因")
                    return False, fail_reason
        except Exception:
            pass
        return True, ""

    def _extract_prompt(self, message: str, intent: str = "image") -> str:
        """从自然语言消息中提取生成 prompt

        去掉请求性前缀和意图动词，保留内容描述部分。
        """
        text = message.strip()
        # 去掉开头的礼貌词和请求前缀
        text = re.sub(r"^(帮我|请|麻烦|能|可以|帮).{0,2}", "", text)
        # 去掉动词
        text = re.sub(r"^(生成|画|制作|做|创作|绘制|创建|来|给我)", "", text)
        # 去掉量词
        text = re.sub(r"^(一张|一幅|一个|一段|个|张|幅|段)", "", text)
        if intent == "image":
            # 去掉结尾的意图名词
            text = re.sub(r"(图片|照片|插画|壁纸|图)$", "", text)
        elif intent == "video":
            text = re.sub(r"(视频|动画|短片|影片|动效)$", "", text)
        text = text.strip("，。！？,.!? \t")
        # 提取结果为空则回退到原始消息
        return text if text else message.strip()

    def _parse_submit_id(self, output: str) -> str | None:
        """从输出中提取 submit_id"""
        # 尝试匹配 JSON 格式
        try:
            # 查找 JSON 部分
            json_match = re.search(r'\{[^{}]*"submit_id"[^{}]*\}', output, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                return data.get("submit_id")
        except Exception:
            pass

        # 尝试直接匹配 submit_id
        match = re.search(r'submit_id["\s:=]+([a-zA-Z0-9]+)', output)
        if match:
            return match.group(1)

        return None

    @filter.command("生成图片")
    async def text2image(self, event: AstrMessageEvent, prompt: str = ""):
        """文生图功能"""
        if not prompt:
            yield event.plain_result("请提供图片描述，例如：/生成图片 一只可爱的橘猫")
            return
        prompt = build_structured_media_prompt(prompt, media_kind="image")

        # 构建命令
        command = [
            "text2image",
            "--prompt",
            prompt,
            "--ratio",
            "1:1",
            "--resolution_type",
            "2k",
            "--poll",
            "600",  # 轮询最多 600 秒（10 分钟）
        ]

        waiting_card = await self._start_waiting_card(
            event,
            title="Dreamina 生图",
            prompt=prompt,
            stage="Dreamina 正在生成图片",
        )
        yield event.plain_result(f"正在生成图片：{prompt}\n这可能需要几分钟时间...")

        # 执行命令
        success, output = await self._execute_dreamina(command, timeout=600)

        if success:
            gen_ok, fail_reason = self._check_gen_status(output)
            if not gen_ok:
                await self._finish_waiting_card(
                    waiting_card,
                    title="Dreamina 生图",
                    prompt=prompt,
                    success=False,
                    detail=f"生成失败：{fail_reason}",
                )
                yield event.plain_result(f"生成失败：{fail_reason}")
                return
            url_match = re.search(r'https?://[^\s<>"]+\.(?:jpg|png)[^\s<>"]*', output)
            if url_match:
                image_url = url_match.group()
                # 下载图片到本地，供后续 image2video 使用
                try:
                    suffix = ".png" if ".png" in image_url else ".jpg"
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                    tmp.close()
                    await asyncio.to_thread(
                        urllib.request.urlretrieve, image_url, tmp.name
                    )
                    self.last_image_path = tmp.name
                    logger.info(f"图片已下载到：{tmp.name}")
                    await self._finish_waiting_card(
                        waiting_card,
                        title="Dreamina 生图",
                        prompt=prompt,
                        success=True,
                        detail="图片已生成，会在下一条消息里发送。",
                    )
                    yield event.image_result(image_url)
                    yield event.plain_result(
                        "图片已保存，可用 /图片转视频 <描述> 生成动画"
                    )
                except Exception as e:
                    logger.warning(f"图片下载失败：{e}")
                    await self._finish_waiting_card(
                        waiting_card,
                        title="Dreamina 生图",
                        prompt=prompt,
                        success=True,
                        detail="图片已生成，但本地下载失败，已发送远程图片。",
                    )
                    yield event.image_result(image_url)
            else:
                await self._finish_waiting_card(
                    waiting_card,
                    title="Dreamina 生图",
                    prompt=prompt,
                    success=True,
                    detail="生成成功，但未解析到图片 URL，已回传 CLI 输出。",
                )
                yield event.plain_result(f"生成成功！\n{output[:500]}")
        else:
            await self._finish_waiting_card(
                waiting_card,
                title="Dreamina 生图",
                prompt=prompt,
                success=False,
                detail=f"生成失败：{output}",
            )
            yield event.plain_result(f"生成失败：{output}")

    @filter.command("生成视频")
    async def text2video(self, event: AstrMessageEvent, prompt: str = ""):
        """文生视频功能"""
        if not prompt:
            yield event.plain_result(
                "请提供视频描述，例如：/生成视频 海浪拍打礁石，慢动作"
            )
            return
        prompt = build_structured_media_prompt(prompt, media_kind="video")

        # 构建命令
        command = [
            "text2video",
            "--prompt",
            prompt,
            "--duration",
            "5",
            "--ratio",
            "16:9",
            "--video_resolution",
            "720p",
            "--poll",
            "900",  # 轮询最多 900 秒（15 分钟）
        ]

        waiting_card = await self._start_waiting_card(
            event,
            title="Dreamina 文生视频",
            prompt=prompt,
            stage="Dreamina 正在生成视频",
        )
        yield event.plain_result(f"正在生成视频：{prompt}\n这可能需要较长时间...")

        # 执行命令
        success, output = await self._execute_dreamina(command, timeout=900)

        if success:
            gen_ok, fail_reason = self._check_gen_status(output)
            if not gen_ok:
                await self._finish_waiting_card(
                    waiting_card,
                    title="Dreamina 文生视频",
                    prompt=prompt,
                    success=False,
                    detail=f"生成失败：{fail_reason}",
                )
                yield event.plain_result(f"生成失败：{fail_reason}")
                return
            url_match = re.search(r'https?://[^\s<>"]+\.mp4[^\s<>"]*', output)
            if url_match:
                await self._finish_waiting_card(
                    waiting_card,
                    title="Dreamina 文生视频",
                    prompt=prompt,
                    success=True,
                    detail="视频已生成，链接会在下一条消息里发送。",
                )
                yield event.plain_result(f"视频生成成功：{url_match.group()}")
            else:
                await self._finish_waiting_card(
                    waiting_card,
                    title="Dreamina 文生视频",
                    prompt=prompt,
                    success=True,
                    detail="生成成功，但未解析到 mp4 链接，已回传 CLI 输出。",
                )
                yield event.plain_result(f"生成成功！\n{output[:500]}")
        else:
            await self._finish_waiting_card(
                waiting_card,
                title="Dreamina 文生视频",
                prompt=prompt,
                success=False,
                detail=f"生成失败：{output}",
            )
            yield event.plain_result(f"生成失败：{output}")

    @filter.command("图片转视频")
    async def image2video(self, event: AstrMessageEvent, prompt: str = ""):
        """将最近生成的图片动画化为视频"""
        if not self.last_image_path or not os.path.exists(self.last_image_path):
            yield event.plain_result(
                "没有找到最近生成的图片，请先用 /生成图片 生成一张图片"
            )
            return

        prompt_text = build_structured_media_prompt(
            prompt or "默认动效",
            media_kind="image2video",
        )
        command = [
            "image2video",
            "--image",
            self.last_image_path,
            "--prompt",
            prompt_text,
            "--duration",
            "5",
            "--poll",
            "900",
        ]

        waiting_card = await self._start_waiting_card(
            event,
            title="Dreamina 图片转视频",
            prompt=prompt_text,
            stage="Dreamina 正在把图片动画化",
        )
        yield event.plain_result(
            f"正在将图片动画化：{prompt or '（默认动效）'}\n这可能需要较长时间..."
        )

        success, output = await self._execute_dreamina(command, timeout=900)

        if success:
            gen_ok, fail_reason = self._check_gen_status(output)
            if not gen_ok:
                await self._finish_waiting_card(
                    waiting_card,
                    title="Dreamina 图片转视频",
                    prompt=prompt_text,
                    success=False,
                    detail=f"生成失败：{fail_reason}",
                )
                yield event.plain_result(f"生成失败：{fail_reason}")
                return
            url_match = re.search(r'https?://[^\s<>"]+\.mp4[^\s<>"]*', output)
            if url_match:
                await self._finish_waiting_card(
                    waiting_card,
                    title="Dreamina 图片转视频",
                    prompt=prompt_text,
                    success=True,
                    detail="视频已生成，链接会在下一条消息里发送。",
                )
                yield event.plain_result(f"视频生成成功：{url_match.group()}")
            else:
                await self._finish_waiting_card(
                    waiting_card,
                    title="Dreamina 图片转视频",
                    prompt=prompt_text,
                    success=True,
                    detail="生成成功，但未解析到 mp4 链接，已回传 CLI 输出。",
                )
                yield event.plain_result(f"生成成功！\n{output[:500]}")
        else:
            await self._finish_waiting_card(
                waiting_card,
                title="Dreamina 图片转视频",
                prompt=prompt_text,
                success=False,
                detail=f"生成失败：{output}",
            )
            yield event.plain_result(f"生成失败：{output}")

    @filter.command("即梦余额")
    async def check_credit(self, event: AstrMessageEvent):
        """查询账户余额"""
        command = ["user_credit"]

        success, output = await self._execute_dreamina(command, timeout=30)

        if success:
            try:
                # 尝试解析 JSON
                json_match = re.search(r"\{[^{}]*\}", output, re.DOTALL)
                if json_match:
                    data = json.loads(json_match.group())
                    credit = data.get("credit", "未知")
                    yield event.plain_result(f"即梦账户余额：{credit} 积分")
                else:
                    yield event.plain_result(output)
            except Exception:
                yield event.plain_result(output)
        else:
            yield event.plain_result(f"查询失败：{output}")

    @filter.command("生成配音")
    async def voiceover(self, event: AstrMessageEvent, prompt: str = ""):
        """配音生成功能，默认使用收藏音色「明媚女声」"""
        if not prompt:
            yield event.plain_result("请提供配音文案，例如：/生成配音 这是一段旁白文案")
            return

        voice = "明媚女声"
        waiting_card = await self._start_waiting_card(
            event,
            title="Dreamina 配音",
            prompt=prompt,
            stage=f"Dreamina 正在用「{voice}」生成配音",
        )
        yield event.plain_result(
            f"正在生成配音（音色：{voice}），会默认保存第一个结果的高码率版本..."
        )

        success, payload_or_error = await self._execute_voiceover(
            prompt=prompt,
            voice=voice,
        )
        if not success:
            detail = (
                json.dumps(payload_or_error, ensure_ascii=False)[:1000]
                if isinstance(payload_or_error, dict)
                else str(payload_or_error)
            )
            await self._finish_waiting_card(
                waiting_card,
                title="Dreamina 配音",
                prompt=prompt,
                success=False,
                detail=f"配音生成失败：{detail}",
            )
            yield event.plain_result(f"配音生成失败：{detail}")
            return

        payload = payload_or_error
        audio_path = str((payload.get("savedPaths") or [""])[0])
        await self._finish_waiting_card(
            waiting_card,
            title="Dreamina 配音",
            prompt=prompt,
            success=True,
            detail=f"配音已生成：{audio_path}",
        )
        yield event.chain_result(
            [
                Record(file=audio_path, url=audio_path, text=prompt),
                Plain(f"配音已生成（{voice}）：{audio_path}"),
            ]
        )

    @filter.command("即梦任务列表")
    async def list_tasks(self, event: AstrMessageEvent, status: str = ""):
        """查询任务列表"""
        command = ["list_task"]

        if status:
            command.extend(["--gen_status", status])

        success, output = await self._execute_dreamina(command, timeout=60)

        if success:
            yield event.plain_result(output[:2000] if len(output) > 2000 else output)
        else:
            yield event.plain_result(f"查询失败：{output}")

    @filter.command("查询即梦任务")
    async def query_task(self, event: AstrMessageEvent, submit_id: str):
        """查询特定任务结果"""
        if not submit_id:
            yield event.plain_result(
                "请提供任务 ID，例如：/查询即梦任务 3f6eb41f425d23a3"
            )
            return

        command = ["query_result", "--submit_id", submit_id]

        success, output = await self._execute_dreamina(command, timeout=60)

        if success:
            yield event.plain_result(output)
        else:
            yield event.plain_result(f"查询失败：{output}")

    # ── LLM Tool：让 LLM 自己决定何时调用，彻底避免与 LLM 管道冲突 ──

    @filter.llm_tool(name="dreamina_generate_image_fallback")
    async def tool_text2image(self, event: AstrMessageEvent, prompt: str):
        """【备用】Dreamina 即梦生图（备选工具）。优先使用 generate_image (GPT Image 2)。

        只在以下情况调用本工具:
        - 用户明确说"用 dreamina / 即梦 / 国风 / 中文海报"
        - GPT Image 2 (generate_image) 失败时降级
        - 中文文字海报、国潮节日（端午 / 中秋 / 春节）等中文场景

        Args:
            prompt(string): 图片内容描述。优先使用内容 SOP 生成的结构化 prompt；若传入原始描述，工具会先包装为结构化媒体 prompt。
        """
        event.stop_event()
        yield
        prompt = build_structured_media_prompt(prompt, media_kind="image")
        command = [
            "text2image",
            "--prompt",
            prompt,
            "--ratio",
            "1:1",
            "--resolution_type",
            "2k",
            "--poll",
            "600",
        ]
        waiting_card = await self._start_waiting_card(
            event,
            title="Dreamina 生图",
            prompt=prompt,
            stage="Dreamina 正在生成图片",
        )
        yield event.plain_result(f"正在生成图片：{prompt}\n这可能需要几分钟时间...")
        success, output = await self._execute_dreamina(command, timeout=600)
        if success:
            gen_ok, fail_reason = self._check_gen_status(output)
            if not gen_ok:
                await self._finish_waiting_card(
                    waiting_card,
                    title="Dreamina 生图",
                    prompt=prompt,
                    success=False,
                    detail=f"生成失败：{fail_reason}",
                )
                yield event.plain_result(f"生成失败：{fail_reason}")
                return
            url_match = re.search(r'https?://[^\s<>"]+\.(?:jpg|png)[^\s<>"]*', output)
            if url_match:
                image_url = url_match.group()
                try:
                    suffix = ".png" if ".png" in image_url else ".jpg"
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                    tmp.close()
                    await asyncio.to_thread(
                        urllib.request.urlretrieve, image_url, tmp.name
                    )
                    self.last_image_path = tmp.name
                    await self._finish_waiting_card(
                        waiting_card,
                        title="Dreamina 生图",
                        prompt=prompt,
                        success=True,
                        detail="图片已生成，会在下一条消息里发送。",
                    )
                    yield event.image_result(image_url)
                    yield event.plain_result(
                        "图片已保存，可以直接说「把这张图做成视频」"
                    )
                except Exception as e:
                    logger.warning(f"图片下载失败：{e}")
                    await self._finish_waiting_card(
                        waiting_card,
                        title="Dreamina 生图",
                        prompt=prompt,
                        success=True,
                        detail="图片已生成，但本地下载失败，已发送远程图片。",
                    )
                    yield event.image_result(image_url)
            else:
                await self._finish_waiting_card(
                    waiting_card,
                    title="Dreamina 生图",
                    prompt=prompt,
                    success=True,
                    detail="生成成功，但未解析到图片 URL，已回传 CLI 输出。",
                )
                yield event.plain_result(f"生成成功！\n{output[:500]}")
        else:
            await self._finish_waiting_card(
                waiting_card,
                title="Dreamina 生图",
                prompt=prompt,
                success=False,
                detail=f"生成失败：{output}",
            )
            yield event.plain_result(f"生成失败：{output}")

    @filter.llm_tool(name="dreamina_animate_image")
    async def tool_image2video(self, event: AstrMessageEvent, prompt: str):
        """将最近生成的图片动画化为视频。当用户想把已有图片做成动画、视频、动效时调用。

        Args:
            prompt(string): 动画描述。优先使用内容 SOP 生成的结构化 prompt；若传入原始描述，工具会先包装为结构化媒体 prompt。
        """
        event.stop_event()
        yield
        if not self.last_image_path or not os.path.exists(self.last_image_path):
            yield event.plain_result("没有找到最近生成的图片，请先生成一张图片")
            return
        prompt_text = build_structured_media_prompt(
            prompt or "默认动效",
            media_kind="image2video",
        )
        command = [
            "image2video",
            "--image",
            self.last_image_path,
            "--prompt",
            prompt_text,
            "--duration",
            "5",
            "--poll",
            "900",
        ]
        waiting_card = await self._start_waiting_card(
            event,
            title="Dreamina 图片转视频",
            prompt=prompt_text,
            stage="Dreamina 正在把图片动画化",
        )
        yield event.plain_result(f"正在将图片动画化：{prompt}\n这可能需要较长时间...")
        success, output = await self._execute_dreamina(command, timeout=900)
        if success:
            gen_ok, fail_reason = self._check_gen_status(output)
            if not gen_ok:
                await self._finish_waiting_card(
                    waiting_card,
                    title="Dreamina 图片转视频",
                    prompt=prompt_text,
                    success=False,
                    detail=f"生成失败：{fail_reason}",
                )
                yield event.plain_result(f"生成失败：{fail_reason}")
                return
            url_match = re.search(r'https?://[^\s<>"]+\.mp4[^\s<>"]*', output)
            if url_match:
                await self._finish_waiting_card(
                    waiting_card,
                    title="Dreamina 图片转视频",
                    prompt=prompt_text,
                    success=True,
                    detail="视频已生成，链接会在下一条消息里发送。",
                )
                yield event.plain_result(f"视频生成成功：{url_match.group()}")
            else:
                await self._finish_waiting_card(
                    waiting_card,
                    title="Dreamina 图片转视频",
                    prompt=prompt_text,
                    success=True,
                    detail="生成成功，但未解析到 mp4 链接，已回传 CLI 输出。",
                )
                yield event.plain_result(f"生成成功！\n{output[:500]}")
        else:
            await self._finish_waiting_card(
                waiting_card,
                title="Dreamina 图片转视频",
                prompt=prompt_text,
                success=False,
                detail=f"生成失败：{output}",
            )
            yield event.plain_result(f"生成失败：{output}")

    @filter.llm_tool(name="dreamina_generate_video")
    async def tool_text2video(self, event: AstrMessageEvent, prompt: str):
        """根据用户描述生成视频。当用户想生成视频、动画、短片时调用（不依赖已有图片）。

        Args:
            prompt(string): 视频内容描述。优先使用内容 SOP 生成的结构化 prompt；若传入原始描述，工具会先包装为结构化媒体 prompt。
        """
        event.stop_event()
        yield
        prompt = build_structured_media_prompt(prompt, media_kind="video")
        command = [
            "text2video",
            "--prompt",
            prompt,
            "--duration",
            "5",
            "--ratio",
            "16:9",
            "--video_resolution",
            "720p",
            "--poll",
            "900",
        ]
        waiting_card = await self._start_waiting_card(
            event,
            title="Dreamina 文生视频",
            prompt=prompt,
            stage="Dreamina 正在生成视频",
        )
        yield event.plain_result(f"正在生成视频：{prompt}\n这可能需要较长时间...")
        success, output = await self._execute_dreamina(command, timeout=900)
        if success:
            gen_ok, fail_reason = self._check_gen_status(output)
            if not gen_ok:
                await self._finish_waiting_card(
                    waiting_card,
                    title="Dreamina 文生视频",
                    prompt=prompt,
                    success=False,
                    detail=f"生成失败：{fail_reason}",
                )
                yield event.plain_result(f"生成失败：{fail_reason}")
                return
            url_match = re.search(r'https?://[^\s<>"]+\.mp4[^\s<>"]*', output)
            if url_match:
                await self._finish_waiting_card(
                    waiting_card,
                    title="Dreamina 文生视频",
                    prompt=prompt,
                    success=True,
                    detail="视频已生成，链接会在下一条消息里发送。",
                )
                yield event.plain_result(f"视频生成成功：{url_match.group()}")
            else:
                await self._finish_waiting_card(
                    waiting_card,
                    title="Dreamina 文生视频",
                    prompt=prompt,
                    success=True,
                    detail="生成成功，但未解析到 mp4 链接，已回传 CLI 输出。",
                )
                yield event.plain_result(f"生成成功！\n{output[:500]}")
        else:
            await self._finish_waiting_card(
                waiting_card,
                title="Dreamina 文生视频",
                prompt=prompt,
                success=False,
                detail=f"生成失败：{output}",
            )
            yield event.plain_result(f"生成失败：{output}")

    @filter.llm_tool(name="dreamina_generate_voiceover")
    async def tool_voiceover(
        self,
        event: AstrMessageEvent,
        prompt: str,
        voice: str = "明媚女声",
    ):
        """使用 Dreamina 即梦网页配音生成音频。

        当用户想生成配音、旁白、口播音频、人声配音时调用。默认使用收藏音色「明媚女声」，
        并只保存第一个生成结果的高码率版本到 NAS inbox/AIVoiceover。

        Args:
            prompt(string): 需要朗读的配音文案。
            voice(string): 即梦收藏音色名称。默认「明媚女声」。
        """
        event.stop_event()
        yield
        if not prompt.strip():
            yield event.plain_result("请提供配音文案。")
            return

        waiting_card = await self._start_waiting_card(
            event,
            title="Dreamina 配音",
            prompt=prompt,
            stage=f"Dreamina 正在用「{voice}」生成配音",
        )
        yield event.plain_result(
            f"正在生成配音（音色：{voice}），会默认保存第一个结果的高码率版本..."
        )

        success, payload_or_error = await self._execute_voiceover(
            prompt=prompt,
            voice=voice or "明媚女声",
        )
        if not success:
            detail = (
                json.dumps(payload_or_error, ensure_ascii=False)[:1000]
                if isinstance(payload_or_error, dict)
                else str(payload_or_error)
            )
            await self._finish_waiting_card(
                waiting_card,
                title="Dreamina 配音",
                prompt=prompt,
                success=False,
                detail=f"配音生成失败：{detail}",
            )
            yield event.plain_result(f"配音生成失败：{detail}")
            return

        payload = payload_or_error
        audio_path = str((payload.get("savedPaths") or [""])[0])
        await self._finish_waiting_card(
            waiting_card,
            title="Dreamina 配音",
            prompt=prompt,
            success=True,
            detail=f"配音已生成：{audio_path}",
        )
        yield event.chain_result(
            [
                Record(file=audio_path, url=audio_path, text=prompt),
                Plain(f"配音已生成（{voice}）：{audio_path}"),
            ]
        )
