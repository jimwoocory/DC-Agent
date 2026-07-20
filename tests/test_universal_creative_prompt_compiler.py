import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "drafts" / "aihubmix_standalone" / "server.py"


def load_server_module():
    """Load the standalone prompt compiler directly from drafts.

    Returns:
        The imported standalone server module.
    """
    spec = importlib.util.spec_from_file_location(
        "universal_creative_prompt_server", SERVER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_copy_compiler_returns_three_distinct_production_prompts() -> None:
    server = load_server_module()
    backend = server.AihubmixBackend()
    source = "为巅池 Agent 写一篇公众号推文，介绍 NAS 本地创作能力，不要编造客户数据。"

    result = backend.prompt_candidates(
        {
            "task_type": "copy",
            "prompt": source,
            "copy_type": "official_account",
            "audience": "内部同事和合作伙伴",
            "source_material": "事实：支持文案、图片和视频三类任务。",
            "output_requirement": "约 800 字，专业但自然，含标题和正文",
        }
    )

    candidates = result["candidates"]
    assert result["framework"] == "writing-editorial-compiler-v1"
    assert len(candidates) == 3
    assert len({item["id"] for item in candidates}) == 3
    assert len({item["prompt"] for item in candidates}) == 3
    assert all(source in item["prompt"] for item in candidates)
    assert all("内部同事和合作伙伴" in item["prompt"] for item in candidates)
    assert all("约 800 字" in item["prompt"] for item in candidates)
    assert all("支持文案、图片和视频三类任务" in item["prompt"] for item in candidates)


def test_image_compiler_reuses_taxonomy_and_preserves_delivery_controls() -> None:
    server = load_server_module()
    backend = server.AihubmixBackend()
    source = "红色新能源车驶入春节灯笼街区，保留车身结构和品牌标志，不要额外文字。"

    result = backend.prompt_candidates(
        {
            "task_type": "image",
            "prompt": source,
            "request_mode": "codex",
            "api_model": "gpt-image-2",
            "aspect_ratio": "9:16",
            "quality": "high",
            "has_reference": True,
        }
    )

    candidates = result["candidates"]
    assert result["framework"] == "gpt-image-taxonomy-compiler-v2"
    assert server.AWESOME_GPT_IMAGE_2_SOURCE in result["prompt_pack_sources"]
    assert len(candidates) == 3
    assert len({item["prompt"] for item in candidates}) == 3
    assert all(source in item["prompt"] for item in candidates)
    assert all("9:16" in item["prompt"] for item in candidates)
    assert all("supplied reference image" in item["prompt"] for item in candidates)


def test_video_compiler_remains_available_through_universal_route() -> None:
    server = load_server_module()
    backend = server.AihubmixBackend()
    source = "一辆新能源车穿过雨夜街道，最后停在品牌门店前，不要字幕。"

    result = backend.prompt_candidates(
        {
            "task_type": "video",
            "prompt": source,
            "request_mode": "dreamina_mcp",
            "api_model": "dreamina-mcp-video",
            "seconds": 10,
            "aspect_ratio": "16:9",
            "resolution": "1080p",
            "shot_density": "sequence",
            "prompt_engine": "local",
        }
    )

    assert result["framework"] == "seedance-prompt-pack-v2"
    assert len(result["candidates"]) == 3
    assert len({item["prompt"] for item in result["candidates"]}) == 3
    assert all(source in item["prompt"] for item in result["candidates"])
    assert all("10 秒" in item["prompt"] for item in result["candidates"])
    assert all("1080p" in item["prompt"] for item in result["candidates"])


def test_universal_route_rejects_noncreative_task_types() -> None:
    server = load_server_module()
    backend = server.AihubmixBackend()

    with pytest.raises(server.UserFacingError, match="仅支持文案、图片和视频"):
        backend.prompt_candidates({"task_type": "research", "prompt": "分析市场"})


def test_standalone_server_registers_universal_prompt_route() -> None:
    source = SERVER_PATH.read_text(encoding="utf-8")

    assert 'if path == "/api/prompt-candidates":' in source
    assert "self.backend.prompt_candidates(payload)" in source
    assert 'self.headers.get("X-Dianchi-Toolbox-Token")' in source


@pytest.mark.parametrize("task_type", ["copy", "image", "video"])
def test_every_compiler_receives_obsidian_memory_with_review_boundaries(
    task_type: str,
) -> None:
    server = load_server_module()
    backend = server.AihubmixBackend()
    payload = {
        "task_type": task_type,
        "prompt": "为五菱之光EV制作春节返乡内容",
        "request_mode": "dreamina_mcp" if task_type == "video" else "codex",
        "api_model": ("dreamina-mcp-video" if task_type == "video" else "gpt-image-2"),
        "prompt_engine": "local",
        "company_memory": [
            {
                "title": "五菱之光EV春节传播文案",
                "source_path": "ObsidianVault/品牌文案/五菱之光EV春节传播.md",
                "doc_type": "文案素材",
                "excerpt": "历史内容以返乡路上的真实用户故事为核心，品牌表达克制温暖。",
                "source_status": "已复核",
                "usage_policy": "facts_and_style",
            },
            {
                "title": "春节内容草稿",
                "source_path": "ObsidianVault/品牌文案/春节内容草稿.md",
                "doc_type": "传播策略",
                "excerpt": "草稿建议从车主收拾年货的生活细节切入。",
                "source_status": "待复核",
                "usage_policy": "style_reference_only",
            },
        ],
    }

    result = backend.prompt_candidates(payload)

    assert len(result["company_memory_sources"]) == 2
    assert all(
        "公司 Obsidian 原文案参考" in item["prompt"] for item in result["candidates"]
    )
    assert all(
        "历史内容以返乡路上的真实用户故事为核心" in item["prompt"]
        for item in result["candidates"]
    )
    assert all(
        "待复核资料只能用于风格、结构和创意启发" in item["prompt"]
        for item in result["candidates"]
    )


def test_semantic_video_compiler_deterministically_preserves_obsidian_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = load_server_module()
    backend = server.AihubmixBackend()
    monkeypatch.setattr(backend, "_read_codex_access_token", lambda: "test-token")
    monkeypatch.setattr(
        backend,
        "_generate_codex_video_prompt_candidates",
        lambda _context: [
            {
                "id": f"semantic-{index}",
                "title": f"语义方向 {index}",
                "summary": f"摘要 {index}",
                "prompt": f"镜头方案：语义候选 {index}",
            }
            for index in range(1, 4)
        ],
    )

    result = backend.prompt_candidates(
        {
            "task_type": "video",
            "prompt": "为五菱之光EV制作春节返乡短片",
            "request_mode": "dreamina_mcp",
            "api_model": "dreamina-mcp-video",
            "company_memory": [
                {
                    "title": "五菱春节传播口径",
                    "source_path": "30_Entities/五菱/春节传播.md",
                    "doc_type": "文案素材",
                    "excerpt": "以真实返乡场景表达可靠陪伴。",
                    "source_status": "已复核",
                    "usage_policy": "facts_and_style",
                },
                {
                    "title": "旧版短片草稿",
                    "source_path": "00_Inbox/旧版短片草稿.md",
                    "doc_type": "传播策略",
                    "excerpt": "可借鉴清晨出发的结构。",
                    "source_status": "待复核",
                    "usage_policy": "style_reference_only",
                },
            ],
        }
    )

    assert result["framework"] == "seedance-semantic-compiler-v2"
    assert all(
        "公司 Obsidian 原文案参考" in item["prompt"] for item in result["candidates"]
    )
    assert all("五菱春节传播口径" in item["prompt"] for item in result["candidates"])
    assert all(
        "待复核资料只能用于风格、结构和创意启发" in item["prompt"]
        for item in result["candidates"]
    )
