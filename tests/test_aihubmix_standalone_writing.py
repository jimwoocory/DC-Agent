import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "drafts" / "aihubmix_standalone" / "server.py"
GALLERY_PATH = (
    ROOT / "drafts" / "aihubmix_standalone" / "pages" / "gallery" / "index.html"
)
WRITING_PATH = (
    ROOT / "drafts" / "aihubmix_standalone" / "pages" / "writing" / "index.html"
)


def load_server_module():
    """Load the standalone server module directly from drafts.

    Returns:
        The imported module object.
    """
    spec = importlib.util.spec_from_file_location(
        "standalone_writing_server", SERVER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_writing_workspace_is_registered():
    server = load_server_module()
    gallery_html = GALLERY_PATH.read_text(encoding="utf-8")

    assert server.WRITING_HTML == WRITING_PATH
    assert server.WRITING_HTML.is_file()
    assert 'href="/writing/"' in gallery_html
    assert ">写作</a>" in gallery_html


def test_writing_workspace_has_complete_ui():
    html = WRITING_PATH.read_text(encoding="utf-8")

    for marker in (
        'id="outlinePanel"',
        'id="documentEditor"',
        'id="writingBrief"',
        'id="generateDraftButton"',
        'id="writingModelGrid"',
        'id="writingApiKey"',
        'id="writingResults"',
        'id="wordCount"',
        'id="copyDocumentButton"',
        'id="exportDocumentButton"',
        "生成初稿",
        "文章类型",
        "表达语气",
        "目标篇幅",
    ):
        assert marker in html


def test_writing_workspace_does_not_persist_secrets():
    html = WRITING_PATH.read_text(encoding="utf-8")

    assert "localStorage" not in html
    assert "sessionStorage" not in html
    assert ".env" not in html
    assert ".setItem(" not in html


def test_writing_model_catalog():
    server = load_server_module()

    assert server.CODEX_WRITING_MODEL == "gpt-5.6-sol"
    assert list(server.WRITING_MODELS) == [
        "gpt-5.6-sol",
        "qwen3.7-max",
        "doubao-seed-2-1-pro",
        "claude-fable-5",
        "gemini-3.5-flash",
        "grok-4.5",
    ]
    assert server.WRITING_MODELS["gpt-5.6-sol"]["provider"] == "codex-oauth"
    assert all(
        item["provider"] == "aihubmix"
        for model_id, item in server.WRITING_MODELS.items()
        if model_id != "gpt-5.6-sol"
    )


def test_writing_generation_compares_models_and_isolates_failures(monkeypatch):
    server = load_server_module()
    backend = server.AihubmixBackend()
    calls = []

    def fake_codex(context):
        calls.append(("codex", context["model_id"]))
        return {"title": "Sol 初稿", "content": "## 正文\nSol 内容"}

    def fake_aihubmix(context, api_key):
        calls.append((api_key, context["model_id"]))
        if context["model_id"] == "claude-fable-5":
            raise RuntimeError("渠道暂不可用")
        return {"title": "Qwen 初稿", "content": "## 正文\nQwen 内容"}

    monkeypatch.setattr(backend, "_generate_codex_writing", fake_codex)
    monkeypatch.setattr(backend, "_generate_aihubmix_writing", fake_aihubmix)

    result = backend.generate_writing(
        {
            "brief": "介绍巅池 Agent 工具箱的本地创作能力",
            "article_type": "公众号文章",
            "tone": "专业清晰",
            "target_length": 800,
            "models": ["gpt-5.6-sol", "qwen3.7-max", "claude-fable-5"],
            "api_key": "temporary-key",
        }
    )

    assert result["success_count"] == 2
    assert result["failure_count"] == 1
    assert [item["model_id"] for item in result["results"]] == [
        "gpt-5.6-sol",
        "qwen3.7-max",
        "claude-fable-5",
    ]
    assert [item["status"] for item in result["results"]] == [
        "success",
        "success",
        "error",
    ]
    assert ("temporary-key", "qwen3.7-max") in calls
    assert result["results"][2]["error"] == "渠道暂不可用"
    assert "temporary-key" not in str(result)


def test_writing_generation_reports_missing_remote_key_without_calling_provider(
    monkeypatch,
):
    server = load_server_module()
    backend = server.AihubmixBackend()
    monkeypatch.setattr(
        backend,
        "_generate_aihubmix_writing",
        lambda context, api_key: (_ for _ in ()).throw(
            AssertionError("provider should not be called")
        ),
    )

    result = backend.generate_writing(
        {
            "brief": "写一篇产品介绍",
            "models": ["gemini-3.5-flash"],
        }
    )

    assert result["success_count"] == 0
    assert result["failure_count"] == 1
    assert result["results"][0]["error"] == "需要临时 AIHUBMIX API Key"


def test_gemini_writing_uses_aihubmix_output_limit(monkeypatch):
    server = load_server_module()
    backend = server.AihubmixBackend()
    captured = {}

    def fake_post(url, body, api_key):
        captured.update(url=url, body=body, api_key=api_key)
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"title":"测试标题","content":"## 正文\\n测试内容"}'
                    }
                }
            ]
        }

    monkeypatch.setattr(backend, "_post_json", fake_post)
    result = backend._generate_aihubmix_writing(
        {
            "brief": "测试主题",
            "article_type": "自由写作",
            "tone": "专业清晰",
            "target_length": 800,
            "model_id": "gemini-3.5-flash",
        },
        "temporary-key",
    )

    assert captured["url"].endswith("/chat/completions")
    assert captured["body"]["model"] == "gemini-3.5-flash"
    assert captured["body"]["max_output_tokens"] == 2048
    assert "max_tokens" not in captured["body"]
    assert captured["api_key"] == "temporary-key"
    assert result == {"title": "测试标题", "content": "## 正文\n测试内容"}


def test_writing_workspace_uses_model_comparison_api():
    html = WRITING_PATH.read_text(encoding="utf-8")

    for model_id in (
        "gpt-5.6-sol",
        "qwen3.7-max",
        "doubao-seed-2-1-pro",
        "claude-fable-5",
        "gemini-3.5-flash",
        "grok-4.5",
    ):
        assert f'value="{model_id}"' in html
    assert "fetch('/api/writing-generate'" in html
    assert "采用此稿" in html
    assert 'id="localTemplateButton"' in html


def test_writing_workspace_preserves_template_and_retries_failures():
    html = WRITING_PATH.read_text(encoding="utf-8")

    assert "固定模板已生成" in html
    assert "刷新重试" in html
    assert "generateWriting([result.model_id], false)" in html
    assert "if (successful.length === 1) applyWritingResult" not in html
