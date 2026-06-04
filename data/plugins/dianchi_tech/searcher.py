"""巅池-技术 日报阶段 A：抓硅谷 AI 当日动态 → raw_news.md。

2026-05-26 蔡挺决策（agy 已复活，切回 agy 主通道）：
1. **主路径**：agy CLI（Antigravity）via PTY 包装（agy_runner.py）→ Gemini 搜索 + 初步分析
2. **兜底**：aihubmix 原生 Gemini 路径 + google_search grounding（不依赖 agy 登录态）
3. **二级兜底**：aihubmix OpenAI 兼容路径（无实时网搜，仅模型知识）

raw_news.md 顶部明确标"数据源"，便于回溯哪条路出来的。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date as _date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from agy_runner import run_agy  # noqa: E402

CMD_CONFIG = Path("/Users/dianchi/DC-Agent/data/cmd_config.json")
GEMINI_MODEL = "gemini-3.5-flash"
NATIVE_BASE = "https://aihubmix.com/gemini/v1beta/models"
OPENAI_COMPAT_BASE = "https://aihubmix.com/v1/chat/completions"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [searcher] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─────────────────────── 凭证 ───────────────────────


def _read_json_loose(path: Path) -> dict:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return json.loads(raw.decode("utf-8"))


def _load_aihubmix_key() -> str:
    try:
        d = _read_json_loose(CMD_CONFIG)
    except Exception as exc:  # noqa: BLE001
        log.warning("读 cmd_config.json 失败：%s", exc)
        return ""
    for p in d.get("provider", []):
        if "aihubmix.com" in json.dumps(p):
            return p.get("embedding_api_key") or p.get("api_key") or ""
    return ""


# ─────────────────────── Prompt ───────────────────────


def _prompt(date_str: str) -> str:
    """给 agy 和 Gemini 共用的硅谷科技圈 prompt。"""
    return f"""你是『巅池-技术』日报阶段 A 的**硅谷科技圈**资讯采集助手。今天 {date_str}（北京时间）。

请用你的搜索能力**实时搜索**过去 24-48 小时硅谷 AI 科技圈的最新动态。

## 优先收（按重要性递减）

- **新模型 / 新版本发布**（参数、benchmark、能力变化、context window、token 价）
- **API / SDK 变更**（新 endpoint、新参数、deprecation、breaking change、SDK 版本号）
- **新功能 / 新工具**（Claude Code / Agent SDK / Gemini CLI / Antigravity / Codex 等开发者工具更新）
- **研究论文 / 技术博客**（官方 research blog、arXiv 重要论文、技术深度文章）
- **开源项目动态**（重要 GitHub release、上游 commit、社区流行 fork）
- **架构 / 训练方法创新**（新训练范式、新推理优化、benchmark 突破）
- **多模态 / 智能体 / 工具调用**方向的真实进展
- **技术性收购 / 合作 / 产品路线**（影响 SDK / API / 生态格局的那种）
- **安全 / 漏洞 / 滥用事件**（CVE、隐私泄漏、模型滥用 case）

## 严格排除（看到就跳过）

- ❌ IPO / 上市 / SEC 文件 / 招股书 / 路演 / 估值
- ❌ 季度财报 / 营收 / 利润率 / EPS / 现金流 / 烧钱速度
- ❌ 融资轮次金额（除非融资目的是某项明确的技术资产）
- ❌ 股价 / 期权 / 二级市场动态
- ❌ 纯人事 CEO 离职 / 跳槽 / 高管八卦
- ❌ 用户数 / DAU / MAU / 市占率排行

## 四大目标厂商

OpenAI / Anthropic / Google DeepMind (Gemini) / xAI (Grok)

## 输出格式

严格 Markdown 中文（不要前言、不要 ```markdown 包裹）：

# 硅谷 AI 技术动态 {date_str}

## OpenAI
- **中文标题** (`YYYY-MM-DD`, [原文](url)) — 1-2 句要点，带具体技术细节
- ...

## Anthropic
- ...

## Google / Gemini
- ...

## xAI / Grok
- ...

## 开源 / 社区技术亮点
- 重要开源 release、上游 commit、值得关注的 demo
- 带 GitHub / arXiv 链接

## 硬要求

- **硅谷科技圈，剔除金融/财报**
- **新鲜度**：优先今天+昨天；3-7 天的标"背景延展"；超 7 天不写
- **每条标真实发布日期**：格式 `- **标题** (YYYY-MM-DD, [原文](url)) — 要点`
- 每家至少 **3 条**；24-72h 内确实无新动态写 `- 近 3 天无新动态`
- 中文标题，原文 URL 保留
- 不要瞎编，没搜到就写 "无新动态"
"""


# ─────────────────────── 主路径：agy ───────────────────────


def try_agy(date_str: str) -> tuple[bool, str, dict]:
    log.info("主路径：agy CLI（Antigravity / Gemini）")
    result = run_agy(_prompt(date_str), timeout=300)
    meta = {
        "channel": "agy",
        "kind": result["kind"],
        "elapsed_sec": result["elapsed_sec"],
        "error": result.get("error", ""),
    }
    if result["ok"] and result["text"]:
        log.info("agy 成功 %.1fs %d chars", result["elapsed_sec"], len(result["text"]))
        return True, result["text"], meta
    log.warning("agy 失败 kind=%s err=%s", result["kind"], result.get("error", ""))
    return False, "", meta


# ─────────────────────── 兜底 1：aihubmix Gemini grounding ───────────────────────


def gemini_grounding(api_key: str, prompt: str) -> tuple[bool, str, dict]:
    url = f"{NATIVE_BASE}/{GEMINI_MODEL}:generateContent?key={api_key}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"maxOutputTokens": 8192, "temperature": 0.4},
    }
    started = time.monotonic()
    try:
        r = httpx.post(url, json=body, timeout=180)
    except Exception as exc:  # noqa: BLE001
        return (
            False,
            "",
            {
                "error": f"http exc: {type(exc).__name__}: {exc}",
                "elapsed_sec": round(time.monotonic() - started, 2),
            },
        )
    elapsed = round(time.monotonic() - started, 2)
    if r.status_code != 200:
        return (
            False,
            "",
            {"error": f"HTTP {r.status_code}: {r.text[:300]}", "elapsed_sec": elapsed},
        )
    d = r.json()
    candidates = d.get("candidates") or []
    if not candidates:
        return False, "", {"error": "no candidates", "elapsed_sec": elapsed}
    text = "".join(
        p.get("text", "") for p in candidates[0].get("content", {}).get("parts", [])
    )
    meta = {
        "elapsed_sec": elapsed,
        "finish_reason": candidates[0].get("finishReason"),
        "usage_metadata": d.get("usageMetadata"),
        "grounding_chunks": len(
            candidates[0].get("groundingMetadata", {}).get("groundingChunks", []) or []
        ),
    }
    if not text.strip():
        return False, "", {**meta, "error": "empty text"}
    return True, text.strip(), meta


# ─────────────────────── 兜底 2：OpenAI 兼容 Gemini（无网搜） ───────────────────────


def gemini_openai_compat(api_key: str, prompt: str) -> tuple[bool, str, dict]:
    started = time.monotonic()
    try:
        r = httpx.post(
            OPENAI_COMPAT_BASE,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": GEMINI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 8000,
            },
            timeout=180,
        )
    except Exception as exc:  # noqa: BLE001
        return (
            False,
            "",
            {
                "error": f"http exc: {type(exc).__name__}: {exc}",
                "elapsed_sec": round(time.monotonic() - started, 2),
            },
        )
    elapsed = round(time.monotonic() - started, 2)
    if r.status_code != 200:
        return (
            False,
            "",
            {"error": f"HTTP {r.status_code}: {r.text[:300]}", "elapsed_sec": elapsed},
        )
    d = r.json()
    text = d["choices"][0]["message"]["content"]
    return True, text.strip(), {"elapsed_sec": elapsed, "usage": d.get("usage")}


# ─────────────────────── main ───────────────────────


def header(channel: str) -> str:
    return f"> 数据源：{channel} · 生成于 {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"


def run(date_str: str, out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path = out_path.parent / "searcher_meta.json"

    # 1) 主路径：agy
    ok, text, agy_meta = try_agy(date_str)
    if ok:
        out_path.write_text(
            header("agy CLI / Antigravity（主通道）") + text + "\n", encoding="utf-8"
        )
        meta_path.write_text(
            json.dumps(
                {"channel": "agy", "agy": agy_meta}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        return 0

    # 2) 兜底 1：aihubmix Gemini grounding
    key = _load_aihubmix_key()
    if not key:
        out_path.write_text(
            header("ALL FAILED")
            + f"# 硅谷 AI 资讯 {date_str}\n\n⚠️ agy 失败 + 缺 aihubmix key。\n\nagy: {agy_meta}\n",
            encoding="utf-8",
        )
        meta_path.write_text(
            json.dumps(
                {
                    "channel": "ALL FAILED",
                    "agy": agy_meta,
                    "error": "missing aihubmix key",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return 2

    log.info("兜底 1：aihubmix native gemini-3.5-flash + google_search grounding")
    ok2, text2, gemini_meta = gemini_grounding(key, _prompt(date_str))
    if ok2:
        out_path.write_text(
            header(
                f"aihubmix native {GEMINI_MODEL} + google_search grounding（agy 降级，{gemini_meta.get('grounding_chunks', 0)} 个来源）"
            )
            + text2
            + "\n",
            encoding="utf-8",
        )
        meta_path.write_text(
            json.dumps(
                {
                    "channel": "gemini_grounding_fallback",
                    "agy": agy_meta,
                    "gemini": gemini_meta,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return 1

    log.warning(
        "Gemini grounding 失败 %s，二级兜底 OpenAI 兼容路径", gemini_meta.get("error")
    )

    # 3) 二级兜底：OpenAI 兼容（无网搜）
    ok3, text3, oai_meta = gemini_openai_compat(key, _prompt(date_str))
    if ok3:
        out_path.write_text(
            header(
                f"aihubmix OpenAI 兼容 {GEMINI_MODEL}（agy + grounding 都挂，无实时网搜）"
            )
            + text3
            + "\n",
            encoding="utf-8",
        )
        meta_path.write_text(
            json.dumps(
                {
                    "channel": "openai_compat_2nd_fallback",
                    "agy": agy_meta,
                    "gemini_grounding": gemini_meta,
                    "oai": oai_meta,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return 1

    # 全挂
    out_path.write_text(
        header("ALL FAILED")
        + f"# 硅谷 AI 资讯 {date_str}\n\n⚠️ agy / Gemini grounding / OpenAI 兼容三路全失败。\n\nagy: {agy_meta}\n\ngrounding: {gemini_meta}\n\noai: {oai_meta}\n",
        encoding="utf-8",
    )
    meta_path.write_text(
        json.dumps(
            {
                "channel": "ALL FAILED",
                "agy": agy_meta,
                "gemini_grounding": gemini_meta,
                "oai": oai_meta,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=_date.today().isoformat())
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    out = (
        Path(args.out)
        if args.out
        else Path(f"/Users/dianchi/DC-Agent/data/dianchi_tech/{args.date}/raw_news.md")
    )
    return run(args.date, out)


if __name__ == "__main__":
    sys.exit(main())
