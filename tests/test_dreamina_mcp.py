from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from dc_engines.dreamina_mcp.runner import run_dreamina


@pytest.mark.asyncio
async def test_run_dreamina_parses_downloads_and_records(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"submit_id":"sub_123","gen_status":"success",'
                '"result":"https://example.com/result.png"}'
            ),
            stderr="",
        )

    def fake_urlretrieve(url, filename):
        Path(filename).write_bytes(b"image")
        return filename, None

    monkeypatch.setattr(
        "dc_engines.dreamina_mcp.runner.resolve_dreamina_executable",
        lambda env: "/usr/local/bin/dreamina",
    )
    monkeypatch.setattr("dc_engines.dreamina_mcp.runner.subprocess.run", fake_run)
    monkeypatch.setattr(
        "dc_engines.dreamina_mcp.runner.urllib.request.urlretrieve", fake_urlretrieve
    )

    result = await run_dreamina(
        ["text2image", "--prompt", "端午海报", "--poll", "1"],
        output_dir=str(tmp_path),
        download=True,
        media_kind="image",
        prompt="端午海报",
    )

    assert result.ok is True
    assert result.submit_id == "sub_123"
    assert result.media_urls == ["https://example.com/result.png"]
    assert len(result.downloaded_files) == 1
    assert Path(result.downloaded_files[0]).read_bytes() == b"image"
    assert result.record is not None
    assert result.record["status"] == "succeeded"

    audit_path = tmp_path / "dreamina_mcp_tasks.jsonl"
    audit_payload = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert audit_payload["submit_id"] == "sub_123"


@pytest.mark.asyncio
async def test_run_dreamina_returns_actionable_missing_cli_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "dc_engines.dreamina_mcp.runner.resolve_dreamina_executable",
        lambda env: None,
    )

    result = await run_dreamina(["user_credit"], output_dir=str(tmp_path))

    assert result.ok is False
    assert "DREAMINA_CLI_PATH" in result.error_hint
    assert result.returncode is None


@pytest.mark.asyncio
async def test_run_dreamina_marks_async_submit_without_result_ready(
    tmp_path,
    monkeypatch,
):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout='{"submit_id":"video_pending","gen_status":"generating"}',
            stderr="",
        )

    monkeypatch.setattr(
        "dc_engines.dreamina_mcp.runner.resolve_dreamina_executable",
        lambda env: "/usr/local/bin/dreamina",
    )
    monkeypatch.setattr("dc_engines.dreamina_mcp.runner.subprocess.run", fake_run)

    result = await run_dreamina(
        ["text2video", "--prompt", "五菱产品展示视频", "--poll", "0"],
        output_dir=str(tmp_path),
        media_kind="video",
        prompt="五菱产品展示视频",
    )

    assert result.ok is True
    assert result.task_state == "submitted"
    assert result.result_ready is False
    assert "dreamina_query_result" in result.next_action
    assert "video_pending" in result.next_action
    assert result.record is None


@pytest.mark.asyncio
async def test_marketing_image_tool_compiles_dreamina_prompt(monkeypatch):
    from dc_engines.dreamina_mcp import server

    captured = {}

    async def fake_run_dreamina(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            to_dict=lambda: {
                "ok": True,
                "submit_id": "sub_456",
                "gen_status": "success",
                "media_urls": [],
                "downloaded_files": [],
                "output_dir": "/tmp/out",
            }
        )

    monkeypatch.setattr(server, "run_dreamina", fake_run_dreamina)

    response = await server.dreamina_generate_marketing_image(
        brief="五菱缤果夏至海报",
        aspect_ratio="portrait",
        poll_seconds=0,
        download=False,
    )

    payload = json.loads(response)
    assert payload["submit_id"] == "sub_456"
    assert captured["args"][:2] == ["text2image", "--prompt"]
    assert "Provider: Dreamina" in captured["args"][2]
    assert "五菱缤果夏至海报" in captured["args"][2]
    assert "--ratio" in captured["args"]
    assert captured["args"][captured["args"].index("--ratio") + 1] == "9:16"


@pytest.mark.asyncio
async def test_text2video_tool_uses_supported_cli_arguments(monkeypatch):
    from dc_engines.dreamina_mcp import server

    captured = {}

    async def fake_run_dreamina(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            to_dict=lambda: {
                "ok": True,
                "submit_id": "video_123",
                "gen_status": "success",
                "media_urls": ["https://example.com/result.mp4"],
                "downloaded_files": [],
                "output_dir": "/tmp/out",
            }
        )

    monkeypatch.setattr(server, "run_dreamina", fake_run_dreamina)

    response = await server.dreamina_text2video(
        prompt="五菱产品展示视频",
        duration=3,
        ratio="16:9",
        video_resolution="720p",
        poll_seconds=7,
        download=False,
    )

    payload = json.loads(response)
    assert payload["submit_id"] == "video_123"
    assert captured["args"] == [
        "text2video",
        "--prompt",
        "五菱产品展示视频",
        "--duration",
        "4",
        "--ratio",
        "16:9",
        "--video_resolution",
        "720p",
        "--model_version",
        "seedance2.0fast",
        "--poll",
        "7",
    ]
    assert captured["kwargs"]["timeout"] == 180
    assert captured["kwargs"]["media_kind"] == "video"


@pytest.mark.asyncio
async def test_run_dreamina_normalizes_concurrency_limit_error(
    tmp_path,
    monkeypatch,
):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"submit_id":"video_456","gen_status":"fail",'
                '"fail_reason":"api error: ret=1310, message=ExceedConcurrencyLimit"}'
            ),
            stderr="",
        )

    monkeypatch.setattr(
        "dc_engines.dreamina_mcp.runner.resolve_dreamina_executable",
        lambda env: "/usr/local/bin/dreamina",
    )
    monkeypatch.setattr("dc_engines.dreamina_mcp.runner.subprocess.run", fake_run)

    result = await run_dreamina(
        ["text2video", "--prompt", "五菱产品展示视频", "--poll", "1"],
        output_dir=str(tmp_path),
        media_kind="video",
        prompt="五菱产品展示视频",
    )

    assert result.ok is False
    assert result.submit_id == "video_456"
    assert result.gen_status == "fail"
    assert result.task_state == "failed"
    assert result.result_ready is False
    assert "并发超限" in result.error_hint
    assert "ExceedConcurrencyLimit" not in result.error_hint


@pytest.mark.asyncio
async def test_run_dreamina_normalizes_history_query_failure(
    tmp_path,
    monkeypatch,
):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="错误: get_history_by_ids failed: ret=1015, msg=",
        )

    monkeypatch.setattr(
        "dc_engines.dreamina_mcp.runner.resolve_dreamina_executable",
        lambda env: "/usr/local/bin/dreamina",
    )
    monkeypatch.setattr("dc_engines.dreamina_mcp.runner.subprocess.run", fake_run)

    result = await run_dreamina(
        ["image2video", "--image", "/tmp/input.jpg", "--prompt", "全息视频"],
        output_dir=str(tmp_path),
        media_kind="video",
        prompt="全息视频",
    )

    assert result.ok is False
    assert result.task_state == "failed"
    assert "历史查询接口返回异常" in result.error_hint
    assert "ret=1015" in result.error_hint
    assert "msg=" not in result.error_hint
