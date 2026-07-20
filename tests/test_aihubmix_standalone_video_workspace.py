import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STANDALONE_ROOT = ROOT / "drafts" / "aihubmix_standalone"
SERVER_PATH = STANDALONE_ROOT / "server.py"
GALLERY_PATH = STANDALONE_ROOT / "pages" / "gallery" / "index.html"
WRITING_PATH = STANDALONE_ROOT / "pages" / "writing" / "index.html"
VIDEO_PATH = STANDALONE_ROOT / "pages" / "video" / "index.html"


def load_server_module():
    """Load the standalone server module directly from drafts.

    Returns:
        The imported module object.
    """
    spec = importlib.util.spec_from_file_location(
        "standalone_video_workspace_server", SERVER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_primary_navigation_uses_video_workspace():
    for page in (GALLERY_PATH, WRITING_PATH):
        html = page.read_text(encoding="utf-8")
        assert 'href="/video/"' in html
        assert 'href="/dashboard/"' not in html


def test_video_workspace_is_registered():
    server = load_server_module()

    assert server.VIDEO_HTML == VIDEO_PATH
    assert server.VIDEO_HTML.is_file()


def test_video_workspace_has_complete_ui():
    html = VIDEO_PATH.read_text(encoding="utf-8")

    for marker in (
        'id="videoFrame"',
        'id="videoModel"',
        'id="creationMode"',
        'id="referenceInput"',
        'id="durationSelect"',
        'id="resolutionSelect"',
        'id="ratioSelect"',
        'id="videoPrompt"',
        'id="inputDock"',
        'class="input-dock"',
        'id="promptAssistant"',
        'id="naturalInput"',
        'id="candidateList"',
        'id="makeCandidatesButton"',
        'id="generateVideoButton"',
        'id="downloadVideoButton"',
        'href="/video/"',
        "生成视频",
    ):
        assert marker in html


def test_video_workspace_does_not_persist_secrets():
    html = VIDEO_PATH.read_text(encoding="utf-8")

    assert "localStorage" not in html
    assert ".env" not in html


def test_video_prompt_candidates_are_materially_distinct():
    server = load_server_module()
    backend = server.AihubmixBackend()

    result = backend.video_prompt_candidates(
        {
            "prompt": "五菱新能源春节拜年视频，车辆驶入灯笼街区，最后全家人在车旁拜年。保留红色车身和品牌标志，不要字幕。",
            "api_model": "dreamina-mcp-video",
            "request_mode": "dreamina_mcp",
            "use_case": "品牌短片",
            "shot_density": "sequence",
            "style": "commercial",
            "seconds": 15,
            "aspect_ratio": "9:16",
            "resolution": "1080p",
            "has_reference": True,
            "generate_audio": True,
            "prompt_engine": "local",
        }
    )

    candidates = result["candidates"]
    assert result["provider"] == "local-prompt-pack"
    assert len(result["prompt_pack"]) == 3
    assert "车身结构" in result["reference_slots"][0]["role"]
    assert "包装" not in result["reference_slots"][0]["role"]
    assert len({item["id"] for item in candidates}) == 3
    assert len({item["prompt"] for item in candidates}) == 3
    assert all("五菱新能源春节拜年视频" in item["prompt"] for item in candidates)
    assert all("@图片1" in item["prompt"] for item in candidates)
    assert all(
        "9:16" in item["prompt"] and "15 秒" in item["prompt"] for item in candidates
    )
    assert all("镜头方案：" in item["prompt"] for item in candidates)
    assert len({item["prompt"].split("镜头方案：", 1)[1] for item in candidates}) == 3

    submitted = backend._with_optimized_dreamina_video_prompt(
        {
            "prompt": candidates[1]["prompt"],
            "api_model": "dreamina-mcp-video",
            "request_mode": "dreamina_mcp",
            "seconds": 15,
        }
    )
    assert submitted["prompt"] == candidates[1]["prompt"]
    assert "_prompt_optimized" not in submitted


def test_video_prompt_candidates_preserve_long_brief_and_single_take():
    server = load_server_module()
    backend = server.AihubmixBackend()
    source = (
        "一镜到底展示一个人在清晨工作室完成产品设计。"
        + "保持自然光、桌面布局和人物服装连续。" * 45
        + "尾部硬约束：最后停在未点亮的屏幕前，绝对不要切镜头。"
    )

    result = backend.video_prompt_candidates(
        {
            "prompt": source,
            "request_mode": "dreamina_mcp",
            "api_model": "dreamina-mcp-video",
            "shot_density": "single",
            "seconds": 5,
            "prompt_engine": "local",
        }
    )

    assert all("尾部硬约束" in item["prompt"] for item in result["candidates"])
    assert all("一镜到底" in item["prompt"] for item in result["candidates"])
    assert all("[00-" not in item["prompt"] for item in result["candidates"])


def test_video_prompt_candidates_prefer_codex_analysis(monkeypatch):
    server = load_server_module()
    backend = server.AihubmixBackend()
    generated = [
        {
            "id": f"smart-{index}",
            "title": f"智能方向 {index}",
            "summary": f"摘要 {index}",
            "prompt": f"创作方向：智能方向 {index}\n镜头方案：场景化方案 {index}",
        }
        for index in range(1, 4)
    ]
    monkeypatch.setattr(backend, "_read_codex_access_token", lambda: "token")
    monkeypatch.setattr(
        backend,
        "_generate_codex_video_prompt_candidates",
        lambda context: generated,
        raising=False,
    )

    result = backend.video_prompt_candidates(
        {
            "prompt": "一个雨夜汽车品牌短片",
            "request_mode": "dreamina_mcp",
            "api_model": "dreamina-mcp-video",
            "prompt_engine": "auto",
        }
    )

    assert result["provider"] == "codex-oauth"
    assert result["candidates"] == generated


def test_dreamina_1080p_uses_supported_vip_model(monkeypatch):
    server = load_server_module()
    backend = server.AihubmixBackend()
    captured = {}

    monkeypatch.setattr(backend, "_dreamina_reference_files", lambda raw: [])

    def fake_run(args, *, timeout, media_kind, prompt):
        captured["args"] = args
        return {"ok": True, "submit_id": "video-test"}

    monkeypatch.setattr(backend, "_run_dreamina_mcp", fake_run)
    result = backend._generate_dreamina_video(
        {
            "prompt": "一辆车驶过雨夜街道",
            "seconds": 5,
            "aspect_ratio": "16:9",
            "resolution": "1080p",
        }
    )

    args = captured["args"]
    assert args[args.index("--model_version") + 1] == "seedance2.0_vip"
    assert args[args.index("--video_resolution") + 1] == "1080p"
    assert result["request"]["model_version"] == "seedance2.0_vip"


def test_video_workspace_uses_prompt_candidate_api():
    html = VIDEO_PATH.read_text(encoding="utf-8")

    assert "apiPost('video-prompt-candidates'" in html
    assert "function buildCandidate(" not in html
