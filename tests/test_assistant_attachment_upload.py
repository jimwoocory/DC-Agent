from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image
from starlette.datastructures import Headers, UploadFile

from astrbot.dashboard.api.assistant_attachments import (
    AttachmentDraftStore,
    CloudDocsRequest,
    CreateDraftRequest,
    PromptCandidatesRequest,
    WorkspaceRequest,
    _connector_post,
    _enqueue_workspace_task,
    build_attachment_upload_page,
    build_jsapi_signature,
    build_media_player_page,
    build_task_workspace_page,
    confirm_attachments,
    create_attachment_draft,
    download_ai_cdr_result,
    download_quotation_deliverable,
    draft_store,
    finish_cloud_docs_oauth,
    generate_prompt_candidates,
    get_attachment_upload_page,
    get_media_player_page,
    link_cloud_documents,
    list_ai_cdr_jobs,
    list_cloud_documents,
    save_supplier_prices,
    save_task_workspace,
    search_quotation_prices,
    start_cloud_docs_oauth,
    submit_ai_cdr_jobs,
    sync_quotation_to_feishu_doc,
    upload_attachment,
    upload_quotation_item_image,
)


@pytest.mark.asyncio
async def test_remote_draft_administration_requires_the_shared_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.draft_store",
        store,
    )
    monkeypatch.setenv("DC_ASSISTANT_H5_ADMIN_TOKEN", "shared-test-token")
    payload = CreateDraftRequest(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://192.168.1.35:6185",
        task_type="file",
        session_id="lark:FriendMessage:ou_test",
        message_type="FriendMessage",
        sender_id="ou_test",
        sender_name="测试员工",
        group_id="",
    )
    rejected_request = SimpleNamespace(
        client=SimpleNamespace(host="192.168.2.162"),
        headers={"X-DC-Assistant-Admin-Token": "wrong-token"},
    )

    with pytest.raises(HTTPException) as error:
        await create_attachment_draft(payload, rejected_request)

    assert error.value.status_code == 403
    accepted_request = SimpleNamespace(
        client=SimpleNamespace(host="192.168.2.162"),
        headers={"X-DC-Assistant-Admin-Token": "shared-test-token"},
    )
    result = await create_attachment_draft(payload, accepted_request)
    draft = store.get(result["token"])

    assert draft is not None
    assert draft.upload_url.startswith(
        "http://192.168.1.35:6185/api/v1/assistant-attachments/"
    )
    assert draft.session_id == "lark:FriendMessage:ou_test"
    assert draft.sender_id == "ou_test"
    assert draft.sender_name == "测试员工"


@pytest.mark.asyncio
async def test_expired_workspace_renders_recovery_page() -> None:
    expired_store = AttachmentDraftStore(ttl_seconds=60)

    with patch(
        "astrbot.dashboard.api.assistant_attachments.draft_store",
        expired_store,
    ):
        response = await get_attachment_upload_page("expired-token")

    assert response.status_code == 410
    assert response.headers["cache-control"] == "no-store"
    page = response.body.decode()
    assert "这张旧卡片的入口已失效" in page
    assert "选择任务类型" in page


@pytest.mark.asyncio
async def test_quotation_workspace_csp_allows_blob_image_previews() -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="quotation",
    )

    response = await get_attachment_upload_page(draft.token)

    assert response.status_code == 200
    assert "img-src 'self' data: blob:" in response.headers["content-security-policy"]


def test_media_player_page_has_cinematic_custom_controls() -> None:
    page = build_media_player_page(
        source_url="https://cdn.example.com/result.mp4?token=signed",
        title="新品发布短片",
        poster_url="https://cdn.example.com/poster.jpg",
        engine="Dreamina 即梦",
        aspect_ratio="16:9",
        duration="5 秒",
        task_id="video-123",
    )

    assert "Dianchi Screening Room" in page
    assert '<video id="video" playsinline preload="metadata"></video>' in page
    assert "requestPictureInPicture" in page
    assert "requestFullscreen" in page
    assert "LarkAPI?.webview?.close" in page
    assert "Space 播放" in page
    assert "https://cdn.example.com/result.mp4?token=signed" in page
    assert "新品发布短片" in page
    assert "var(--accent)" in page


@pytest.mark.asyncio
async def test_media_player_route_is_non_cacheable_and_rejects_unsafe_source() -> None:
    response = await get_media_player_page(
        src="https://cdn.example.com/result.mp4",
        title="横版视频预览",
        poster="https://cdn.example.com/poster.jpg",
        engine="Dreamina 即梦",
        ratio="16:9",
        duration="5 秒",
        task_id="video-123",
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "media-src http: https: blob:" in response.headers["content-security-policy"]
    assert "横版视频预览" in response.body.decode()
    with pytest.raises(Exception) as error:
        await get_media_player_page(src="javascript:alert(1)")
    assert getattr(error.value, "status_code", None) == 400


def test_attachment_draft_store_uses_expiring_capability_tokens(tmp_path) -> None:
    storage_path = tmp_path / "drafts.json"
    store = AttachmentDraftStore(ttl_seconds=60, storage_path=storage_path)
    draft = store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
    )

    assert len(draft.token) >= 32
    assert draft.upload_url.endswith(draft.token)
    assert store.get(draft.token) is draft

    store.bind_message(draft.token, "om_test")
    assert store.get(draft.token).message_id == "om_test"
    restored = AttachmentDraftStore(ttl_seconds=60, storage_path=storage_path)
    assert restored.get(draft.token).message_id == "om_test"

    draft.expires_at = time.time() - 1
    store.save()
    assert store.get(draft.token) is None


def test_attachment_upload_page_supports_two_sources_and_confirmation() -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    draft = store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
    )

    page = build_attachment_upload_page(draft)

    assert 'type="file"' in page
    assert 'name="files"' in page
    assert "multiple" in page
    assert "上传本地文件" in page
    assert "选择飞书云文档" in page
    assert page.count('class="source-panel') == 1
    assert page.count('class="source-button"') == 2
    assert page.count("<button") == 3
    assert "确认关联" in page
    assert "source-card" not in page
    assert "source-grid" not in page
    assert "fetch(base" in page
    assert "Microsoft YaHei" in page
    assert "h5sdk.config" not in page
    assert "tt.docsPicker" not in page
    assert "/oauth/start" in page
    assert "/cloud-files" in page
    assert "/confirm" in page
    assert "LarkAPI?.webview?.close" in page
    assert "grid-template-columns:172px minmax(0,1fr)" in page
    assert "grid-template-rows:auto minmax(0,1fr)" in page
    assert ".lead,.footer-note { display:none; }" in page
    assert "height:100%" in page
    assert "把资料放进同一个任务里" not in page
    assert "ATTACHMENT INTAKE" not in page


def test_sidebar_h5_pages_share_the_material_quotation_visual_system() -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    creative_drafts = tuple(
        store.create(
            platform_id="巅池-Agent小助手",
            upload_base_url="http://127.0.0.1:6185",
            task_type=task_type,
        )
        for task_type in ("copy", "image", "video")
    )
    attachment_draft = store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
    )
    quotation_draft = store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="quotation",
    )
    ai_cdr_draft = store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="ai_cdr",
    )

    player = build_media_player_page(source_url="https://cdn.example.com/result.mp4")
    creative_pages = tuple(
        build_task_workspace_page(draft) for draft in creative_drafts
    )
    quotation = build_task_workspace_page(quotation_draft)
    attachments = build_attachment_upload_page(attachment_draft)
    ai_cdr = build_task_workspace_page(ai_cdr_draft)

    pages = (player, *creative_pages, quotation, attachments, ai_cdr)
    for page in pages:
        assert 'data-ui-system="material-quotation"' in page
        assert "--olive:#46513a" in page
        assert "--signal:#d96b32" in page
        assert "--ink:#25281f" in page
        assert "Songti SC" in page
        assert "linear-gradient" not in page
        assert "radial-gradient" not in page

    assert '<meta name="theme-color" content="#46513a">' in player
    assert "var(--signal)" in player
    assert all("var(--signal)" in page for page in creative_pages)
    assert "var(--signal)" in quotation
    assert "var(--signal)" in attachments
    assert "var(--signal)" in ai_cdr


def test_ai_cdr_workspace_has_batch_color_and_progress_controls() -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    draft = store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="ai_cdr",
    )

    page = build_task_workspace_page(draft)

    assert 'type="file" accept=".ai,application/postscript" multiple' in page
    assert "一次最多 20 份" in page
    assert "复制当前规则到全部" in page
    assert "工具自动识别原稿 CMYK" in page
    assert "原稿颜色（自动识别，只读）" in page
    assert "目标 CMYK（设计师填写）" in page
    assert "extractCmykColors" in page
    assert "data-source-select" in page
    assert 'data-group="source"' not in page
    assert "新增 CMYK 映射" in page
    assert "载入 ICC 颜色文件…" in page
    assert "不会未经确认自动转换颜色空间" in page
    assert "转换进度" in page
    assert "overallPercent" in page
    assert "预计剩余" in page
    assert 'id="batchForm"' in page
    assert 'id="submitBatch" type="submit"' in page
    assert "addEventListener('submit',submitBatch)" in page
    assert "addEventListener('pointerup',submitBatch)" in page
    assert "addEventListener('touchend',submitBatch" in page
    assert page.count("addEventListener(eventName,event=>") == 2
    assert "addEventListener(event=>" not in page
    assert "页面执行失败" in page
    assert "showSaveFilePicker" not in page
    assert "当前飞书版本无法打开" not in page
    assert "电脑“下载”文件夹" in page
    assert "下载已开始" in page
    assert "link.download=suggestedName" in page
    assert '<button class="download"' in page
    assert '<a class="download"' not in page
    assert "single_worker_sequential" not in page


@pytest.mark.asyncio
async def test_ai_cdr_h5_uploads_batch_and_reads_mini4_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    queue_root = tmp_path / "AI转CDR共享"
    for directory in (
        "inbox",
        "processing",
        "outbox",
        "failed",
        "reports",
        "color_profiles",
        "progress",
    ):
        (queue_root / directory).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DC_AI_CDR_QUEUE_ROOT", str(queue_root))
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="ai_cdr",
    )
    draft_store.bind_message(draft.token, "om_ai_cdr")
    uploads = [
        UploadFile(
            file=BytesIO(b"illustrator-one"),
            filename="货架一.ai",
            size=15,
            headers=Headers({"content-type": "application/postscript"}),
        ),
        UploadFile(
            file=BytesIO(b"illustrator-two"),
            filename="货架二.ai",
            size=15,
            headers=Headers({"content-type": "application/postscript"}),
        ),
    ]
    profile_bytes = bytearray(128)
    profile_bytes[36:40] = b"acsp"
    icc_upload = UploadFile(
        file=BytesIO(profile_bytes),
        filename="印刷机.icc",
        size=len(profile_bytes),
        headers=Headers({"content-type": "application/vnd.iccprofile"}),
    )
    calibration = json.dumps(
        {
            "files": [
                {
                    "name": "货架一.ai",
                    "mappings": [
                        {
                            "label": "企业蓝",
                            "source": {"c": 100, "m": 70, "y": 0, "k": 0},
                            "target": {"c": 100, "m": 68, "y": 0, "k": 12},
                            "apply_to": "all",
                            "tolerance": 0.1,
                        }
                    ],
                },
                {"name": "货架二.ai", "mappings": []},
            ]
        },
        ensure_ascii=False,
    )

    submitted = await submit_ai_cdr_jobs(
        draft.token,
        uploads,
        calibration,
        icc_upload,
    )

    assert submitted["queue_mode"] == "single_worker_sequential"
    assert submitted["icc_mode"] == "registered_only"
    assert [job["source_name"] for job in submitted["jobs"]] == [
        "货架一.ai",
        "货架二.ai",
    ]
    assert sorted(path.name for path in (queue_root / "inbox").glob("*.ai")) == [
        "货架一.ai",
        "货架二.ai",
    ]
    first_sidecar = json.loads(
        (queue_root / "inbox" / "货架一.color.json").read_text(encoding="utf-8")
    )
    second_sidecar = json.loads(
        (queue_root / "inbox" / "货架二.color.json").read_text(encoding="utf-8")
    )
    assert first_sidecar["mappings"][0]["target"]["k"] == 12.0
    assert first_sidecar["icc_profile"].startswith("color_profiles/")
    assert second_sidecar["mappings"] == []
    assert (queue_root / first_sidecar["icc_profile"]).is_file()

    submitted_at = datetime.fromisoformat(submitted["jobs"][0]["submitted_at"])
    stale_progress = {
        "job_id": "previous-job-one",
        "source_name": "货架一.ai",
        "status": "succeeded",
        "stage": "completed",
        "percentage": 100,
        "message": "上一次同名文件已完成",
        "updated_at": (submitted_at - timedelta(seconds=1)).isoformat(),
        "elapsed_seconds": 48.0,
        "error": None,
    }
    (queue_root / "progress" / "previous-job-one.json").write_text(
        json.dumps(stale_progress, ensure_ascii=False),
        encoding="utf-8",
    )

    queued = await list_ai_cdr_jobs(draft.token)
    assert [job["percentage"] for job in queued["jobs"]] == [8, 8]
    assert queued["jobs"][0]["message"] == "排队中，马上开始"
    assert "download_url" not in queued["jobs"][0]
    with pytest.raises(HTTPException, match="CDR 尚未转换完成") as error:
        await download_ai_cdr_result(draft.token, 0)
    assert error.value.status_code == 409
    progress = {
        "job_id": "job-one",
        "source_name": "货架一.ai",
        "status": "in_progress",
        "stage": "pdf_bridge",
        "percentage": 55,
        "message": "PDF 矢量桥接已生成",
        "updated_at": (submitted_at + timedelta(seconds=1)).isoformat(),
        "elapsed_seconds": 32.5,
        "error": None,
    }
    (queue_root / "progress" / "job-one.json").write_text(
        json.dumps(progress, ensure_ascii=False),
        encoding="utf-8",
    )

    current = await list_ai_cdr_jobs(draft.token)

    assert current["jobs"][0]["percentage"] == 55
    assert current["jobs"][0]["message"] == "PDF 矢量桥接已生成"

    output = queue_root / "outbox" / "货架一.cdr"
    output.write_bytes(b"RIFFcdr")
    progress.update(
        {
            "status": "succeeded",
            "stage": "completed",
            "percentage": 100,
            "message": "CDR 已生成并通过重新打开校验",
            "updated_at": (submitted_at + timedelta(seconds=2)).isoformat(),
        }
    )
    (queue_root / "progress" / "job-one.json").write_text(
        json.dumps(progress, ensure_ascii=False),
        encoding="utf-8",
    )
    (queue_root / "reports" / "job-one.json").write_text(
        json.dumps({"output_cdr": "/Volumes/knowledge/outbox/货架一.cdr"}),
        encoding="utf-8",
    )

    completed = await list_ai_cdr_jobs(draft.token)
    response = await download_ai_cdr_result(draft.token, 0)

    assert completed["jobs"][0]["download_url"].endswith("/0/download")
    assert completed["jobs"][0]["download_name"] == "货架一.cdr"
    assert response.path == output
    assert response.filename == "货架一.cdr"


@pytest.mark.parametrize(
    ("task_type", "required_text"),
    [
        ("copy", ("内容类型", "受众 / 渠道", "篇幅 / 语气 / 结构")),
        ("image", ("尺寸 / 比例", "图片格式", "生成质量")),
        ("video", ("时长", "清晰度", "镜头", "声音")),
        ("research", ("AI 分析方法", "内容结构", "交付文件")),
        ("file", ("处理方式", "处理范围", "交付文件")),
        ("codex", ("分析深度", "交付要求")),
    ],
)
def test_task_workspace_unifies_prompt_material_and_settings(
    task_type: str,
    required_text: tuple[str, ...],
) -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    draft = store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type=task_type,
    )

    page = build_task_workspace_page(draft)

    assert 'data-tab="prompt"' in page
    assert 'data-tab="material"' in page
    assert 'data-tab="settings"' in page
    assert "整理 3 个提词版本" in page
    assert "上传本地文件" in page
    assert "飞书云文档" in page
    assert "模型选择" in page
    assert "智能选择" in page
    assert "直接交付" in page
    assert "sidebar-semi" not in page
    assert "api key" not in page.lower()
    assert all(text in page for text in required_text)


def test_task_workspace_adapts_for_web_and_returns_to_assistant() -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    page = build_task_workspace_page(
        store.create(
            platform_id="巅池-Agent小助手",
            upload_base_url="http://127.0.0.1:6185",
            task_type="copy",
        )
    )

    assert "@media (min-width:760px)" in page
    assert "document.documentElement.dataset.displayMode" in page
    assert 'id="returnToAssistant"' in page
    assert 'id="completion"' in page
    assert 'id="completionReturn"' in page
    assert "window.opener.focus()" in page
    assert "window.close();setTimeout(()=>{const note=" in page
    assert "window.LarkAPI?.webview?.close" in page
    assert "window.location.assign" not in page
    assert "window.location.replace" not in page
    assert "原飞书小助手仍在上一页" in page
    assert "任务已经交给小助手" in page
    assert "setTimeout(()=>returnToAssistant(),900)" in page


def test_creative_workspace_uses_nas_professional_prompt_candidates() -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    creative_page = build_task_workspace_page(
        store.create(
            platform_id="巅池-Agent小助手",
            upload_base_url="http://127.0.0.1:6185",
            task_type="image",
        )
    )
    office_page = build_task_workspace_page(
        store.create(
            platform_id="巅池-Agent小助手",
            upload_base_url="http://127.0.0.1:6185",
            task_type="research",
        )
    )

    assert "fetch(`${base}/prompt-candidates`" in creative_page
    assert "正在调用 NAS 高级提词器" in creative_page
    assert "公司 Obsidian 原文案" in creative_page
    assert "company_memory_sources" in creative_page
    assert "candidate.prompt" in creative_page
    assert "const endings=" not in creative_page
    assert "!['copy','image','video'].includes(config.taskType)" in office_page


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_type", "required_field"),
    [
        ("copy", "task_request"),
        ("image", "visual_prompt"),
        ("video", "video_prompt"),
    ],
)
async def test_opening_creative_revision_h5_only_refills_data_and_never_executes(
    task_type: str,
    required_field: str,
) -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type=task_type,
    )
    draft_store.save_task_data(
        draft.token,
        {required_field: "保留旧资料，等待用户修改后确认"},
    )
    enqueue_task = AsyncMock()

    with patch(
        "astrbot.dashboard.api.assistant_attachments._enqueue_workspace_task",
        enqueue_task,
    ):
        response = await get_attachment_upload_page(draft.token)

    page = response.body.decode()
    assert response.status_code == 200
    assert '"canRevise":true' in page
    assert "const revisionMode=config.canRevise" in page
    assert "确认修改并再次生成" in page
    assert "打开页面不会自动执行" in page
    assert "保留旧资料，等待用户修改后确认" in page
    enqueue_task.assert_not_awaited()


@pytest.mark.parametrize("task_type", ["research", "file", "codex"])
def test_office_and_executor_h5_cannot_enter_revision_mode(task_type: str) -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    page = build_task_workspace_page(
        store.create(
            platform_id="巅池-Agent小助手",
            upload_base_url="http://127.0.0.1:6185",
            task_type=task_type,
        )
    )

    assert '"canRevise":false' in page


@pytest.mark.asyncio
async def test_prompt_candidate_proxy_maps_whitelisted_video_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    draft = store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="video",
    )
    draft.attachments.append({"filename": "reference.png"})
    captured: dict[str, object] = {}
    service_candidates = [
        {
            "id": f"candidate-{index}",
            "title": f"方向 {index}",
            "summary": f"摘要 {index}",
            "prompt": f"专业提示词 {index}",
        }
        for index in range(1, 4)
    ]
    company_memory = [
        {
            "title": "雨夜汽车品牌短片",
            "source_path": "ObsidianVault/品牌文案/雨夜汽车品牌短片.md",
            "doc_type": "文案素材",
            "excerpt": "历史短片采用克制的城市雨夜光影。",
            "review_status": "confirmed",
            "source_status": "已复核",
            "usage_policy": "facts_and_style",
        }
    ]

    class FakeClient:
        """Capture one standalone prompt-service request."""

        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, url, *, json, headers):
            captured.update(url=url, json=json, headers=headers)
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "status": "ok",
                    "data": {
                        "framework": "seedance-prompt-pack-v2",
                        "candidates": service_candidates,
                    },
                },
            )

    monkeypatch.setenv("DIANCHI_CREATIVE_PROMPT_SERVICE_URL", "http://nas-prompt:8765/")
    monkeypatch.setenv("DIANCHI_CREATIVE_PROMPT_SERVICE_TOKEN", "internal-token")
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.draft_store", store
    )
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.search_company_creative_memory",
        lambda query, *, db_path, limit: company_memory,
    )
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.httpx.AsyncClient", FakeClient
    )

    result = await generate_prompt_candidates(
        draft.token,
        PromptCandidatesRequest(
            prompt="车辆穿过雨夜街道",
            task_data={
                "duration": "10",
                "aspect_ratio": "9:16",
                "video_quality": "1080p",
                "camera_motion": "free",
                "audio_mode": "off",
            },
        ),
    )

    assert result["candidates"] == service_candidates
    assert captured["url"] == "http://nas-prompt:8765/api/prompt-candidates"
    assert captured["headers"] == {"X-Dianchi-Toolbox-Token": "internal-token"}
    assert captured["json"] == {
        "task_type": "video",
        "prompt": "车辆穿过雨夜街道",
        "request_mode": "dreamina_mcp",
        "api_model": "dreamina-mcp-video",
        "use_case": "品牌短片",
        "seconds": "10",
        "aspect_ratio": "9:16",
        "resolution": "1080p",
        "shot_density": "sequence",
        "style": "commercial",
        "generate_audio": False,
        "has_reference": True,
        "company_memory": company_memory,
    }
    assert result["company_memory_status"] == "matched"
    assert result["company_memory_sources"][0]["source_status"] == "已复核"


@pytest.mark.asyncio
async def test_prompt_candidate_proxy_uses_exact_backend_in_process_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    draft = store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="image",
    )
    source = "红色新能源车驶入灯笼街区，保留车身结构，不要额外文字。"
    company_memory = [
        {
            "title": "春节灯笼街区历史主视觉",
            "source_path": "ObsidianVault/品牌文案/春节灯笼街区.md",
            "doc_type": "文案素材",
            "excerpt": "历史视觉以暖红灯笼和克制留白形成返乡氛围。",
            "review_status": "rule_confirmed",
            "source_status": "已复核",
            "usage_policy": "facts_and_style",
        }
    ]
    monkeypatch.delenv("DIANCHI_CREATIVE_PROMPT_SERVICE_URL", raising=False)
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.draft_store", store
    )
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments._creative_prompt_backend", None
    )
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments._creative_prompt_backend_error",
        "",
    )
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.search_company_creative_memory",
        lambda query, *, db_path, limit: company_memory,
    )

    result = await generate_prompt_candidates(
        draft.token,
        PromptCandidatesRequest(
            prompt=source,
            task_data={"aspect_ratio": "9:16", "quality": "high"},
        ),
    )

    assert result["framework"] == "gpt-image-taxonomy-compiler-v2"
    assert len(result["candidates"]) == 3
    assert all(source in item["prompt"] for item in result["candidates"])
    assert all("9:16" in item["prompt"] for item in result["candidates"])
    assert all(
        "春节灯笼街区历史主视觉" in item["prompt"] for item in result["candidates"]
    )
    assert result["company_memory_message"] == "已参考 1 条公司 Obsidian 原文案"


@pytest.mark.asyncio
async def test_prompt_candidate_reports_unavailable_obsidian_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    draft = store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="copy",
    )
    candidates = [
        {
            "id": f"copy-{index}",
            "title": f"方向 {index}",
            "summary": f"摘要 {index}",
            "prompt": f"专业文案提示词 {index}",
        }
        for index in range(1, 4)
    ]
    monkeypatch.delenv("DIANCHI_CREATIVE_PROMPT_SERVICE_URL", raising=False)
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.draft_store", store
    )
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.search_company_creative_memory",
        lambda query, *, db_path, limit: (_ for _ in ()).throw(
            sqlite3.DatabaseError("broken index")
        ),
    )
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments._creative_prompt_backend",
        SimpleNamespace(prompt_candidates=lambda payload: {"candidates": candidates}),
    )

    result = await generate_prompt_candidates(
        draft.token,
        PromptCandidatesRequest(prompt="写一篇新品推文"),
    )

    assert result["candidates"] == candidates
    assert result["company_memory_status"] == "unavailable"
    assert result["company_memory_message"] == "公司 Obsidian 文案索引暂不可用"


@pytest.mark.asyncio
async def test_prompt_candidate_proxy_does_not_silently_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    draft = store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="copy",
    )

    class UnavailableClient:
        """Simulate an unavailable NAS prompt service."""

        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, url, *, json, headers):
            raise httpx.ConnectError("service unavailable")

    monkeypatch.setenv(
        "DIANCHI_CREATIVE_PROMPT_SERVICE_URL", "http://unavailable-prompt:8765"
    )
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.draft_store", store
    )
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.search_company_creative_memory",
        lambda query, *, db_path, limit: [],
    )
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.httpx.AsyncClient",
        UnavailableClient,
    )

    with pytest.raises(HTTPException) as error:
        await generate_prompt_candidates(
            draft.token,
            PromptCandidatesRequest(prompt="写一篇新品推文"),
        )

    assert error.value.status_code == 503
    assert "NAS 专业提词服务暂不可用" in error.value.detail


def test_task_workspace_starts_with_material_before_task_prompt() -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    page = build_task_workspace_page(
        store.create(
            platform_id="巅池-Agent小助手",
            upload_base_url="http://127.0.0.1:6185",
            task_type="file",
        )
    )

    material_tab = '<button class="step active" data-tab="material"'
    prompt_tab = '<button class="step" data-tab="prompt"'
    settings_tab = '<button class="step" data-tab="settings"'
    assert page.index(material_tab) < page.index(prompt_tab) < page.index(settings_tab)
    assert '<section class="panel active" data-panel="material">' in page
    assert '<section class="panel" data-panel="prompt">' in page
    assert "materialStepLabel').textContent=config.tabs[1]" in page
    assert "promptStepLabel').textContent=config.tabs[0]" in page
    assert "settingsStepLabel').textContent=config.tabs[2]" in page
    assert page.index("if(stage==='material'") < page.index("if(stage==='prompt'")


def test_office_workspaces_expose_distinct_task_flows() -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    research = build_task_workspace_page(
        store.create(
            platform_id="巅池-Agent小助手",
            upload_base_url="http://127.0.0.1:6185",
            task_type="research",
        )
    )
    file_processing = build_task_workspace_page(
        store.create(
            platform_id="巅池-Agent小助手",
            upload_base_url="http://127.0.0.1:6185",
            task_type="file",
        )
    )

    assert "研究问题" in research
    assert "准备资料" in research
    assert "分析与交付" in research
    assert "LLM 先读资料和来源" in research
    assert "AI 分析方法" in research
    assert "内容结构" in research
    assert "Word 文档 (.docx)" in research
    assert "飞书云文档" in research
    assert "处理目标" in file_processing
    assert "上传文件" in file_processing
    assert "处理与交付" in file_processing
    assert "LLM 先理解真实文件" in file_processing
    assert "处理范围" in file_processing
    assert "尽量保留原结构" in file_processing
    assert "至少上传一个文件、选择一份云文档，或填写文件链接" in file_processing
    assert research != file_processing


def test_file_workspace_exposes_translation_in_task_step_with_real_controls() -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    page = build_task_workspace_page(
        store.create(
            platform_id="巅池-Agent小助手",
            upload_base_url="http://127.0.0.1:6185",
            task_type="file",
        )
    )

    prompt_panel = page.split('<section class="panel" data-panel="prompt">', 1)[1]
    prompt_panel = prompt_panel.split(
        '<section class="panel" data-panel="settings">', 1
    )[0]
    assert "翻译" in page
    assert "源语言" in page
    assert "目标语言" in page
    assert "双向互译（高级）" in page
    assert "忠实翻译" in page
    assert "专业润色" in page
    assert "专业领域" in page
    assert "仅译文" in page
    assert "双语对照" in page
    assert "术语表" in page
    assert "参考译法" in page
    assert 'id="promptSettingGrid"' in prompt_panel
    assert "field.stage==='prompt'?promptGrid:grid" in page
    assert "translationDeliveryMatrix" in page
    assert "target_language" in page
    assert "请选择目标语言" in page
    assert "background:#ffffff" in page


def test_office_workspace_uses_one_sequential_sidebar_wizard() -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    research = build_task_workspace_page(
        store.create(
            platform_id="巅池-Agent小助手",
            upload_base_url="http://127.0.0.1:6185",
            task_type="research",
        )
    )
    file_processing = build_task_workspace_page(
        store.create(
            platform_id="巅池-Agent小助手",
            upload_base_url="http://127.0.0.1:6185",
            task_type="file",
        )
    )

    for page in (research, file_processing):
        assert 'class="step active" data-tab="material"' in page
        assert 'class="step" data-tab="prompt"' in page
        assert 'class="step" data-tab="settings"' in page
        assert 'class="workflow"' not in page
        assert 'id="back"' in page
        assert 'id="stepStatus"' in page
        assert 'id="runSummary"' in page
        assert "validateStage(currentStage)" in page
        assert "setActiveStage(stageOrder[currentIndex+1])" in page
        assert "field.presentation==='cards'" in page
        assert "choices.className='choice-grid'" in page

    assert "下一步：填写研究问题" in research
    assert "下一步：说明处理目标" in file_processing
    assert "开始分析并直接交付" in research
    assert "开始处理并直接交付" in file_processing


def test_material_quotation_workspace_is_a_real_line_item_calculator() -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    page = build_task_workspace_page(
        store.create(
            platform_id="巅池-Agent小助手",
            upload_base_url="http://127.0.0.1:6185",
            task_type="quotation",
        )
    )

    assert "筹备组内部测算台" in page
    assert "报价工具首页" in page
    assert "项目估价测算" in page
    assert "新增合作公司价格" in page
    assert "手工单项 / 多项录入" in page
    assert "上传报价资料" in page
    assert "保存到历史报价库" in page
    assert "市场部估价结果" in page
    assert 'data-surface="calculator"' in page
    assert 'data-surface="result"' in page
    assert "项目信息" in page
    assert "物料明细" in page
    assert "费用汇总" in page
    assert "新增物料" in page
    assert "物料备注（进入 Excel）" in page
    assert "上传图片" in page
    assert "单张或批量，最多 6 张" in page
    assert "multiple hidden" in page
    assert "清空图片" in page
    assert 'id="imageLightbox"' in page
    assert 'aria-label="关闭图片预览"' in page
    assert "item-image-preview-button" in page
    assert "openImageLightbox" in page
    assert "event.key==='Escape'" in page
    assert "/quotation-item-image" in page
    assert "下载成本清单 Excel" in page
    assert "Excel 会预留图示列" in page
    assert "匹配历史价" in page
    assert "Obsidian 历史价" in page
    assert "运输费" in page
    assert "安装费" in page
    assert "加急费" in page
    assert "损耗率" in page
    assert "利润率" in page
    assert "税率" in page
    assert "待询价" in page
    assert "grandTotal" in page
    assert "/quotation-search" in page
    assert "生成估价结果" in page
    assert "发送估价结果" in page
    assert "全程不调用模型" in page
    assert "补齐价格后才能生成市场部估价结果" in page
    assert "历史资料未记录单位" in page
    assert '<select class="unit">' in page
    assert '<select class="supplier-unit">' in page
    assert 'optgroup label="印刷纸品"' in page
    assert "'张','份','本','册','页','P','联','令','色','款'" in page
    assert 'optgroup label="广告制作 / 搭建"' in page
    result_surface = page.split('<section class="market-result" id="resultSurface"')[
        1
    ].split("</section>", 1)[0]
    assert "估价金额" in result_surface
    assert "物料明细" not in result_surface
    assert "供应商" not in result_surface
    assert "利润" not in result_surface
    assert "历史来源" not in result_surface


def test_task_workspace_state_survives_store_restart(tmp_path) -> None:
    storage_path = tmp_path / "task-drafts.json"
    store = AttachmentDraftStore(ttl_seconds=60, storage_path=storage_path)
    draft = store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="image",
    )
    store.save_task_data(
        draft.token,
        {
            "visual_prompt": "夏季新品横版主视觉",
            "aspect_ratio": "16:9",
            "image_format": "png",
            "model_choice": "auto",
        },
    )

    restored = AttachmentDraftStore(ttl_seconds=60, storage_path=storage_path)
    restored_draft = restored.get(draft.token)

    assert restored_draft.task_type == "image"
    assert restored_draft.task_data["visual_prompt"] == "夏季新品横版主视觉"
    assert restored_draft.task_data["aspect_ratio"] == "16:9"


@pytest.mark.asyncio
async def test_workspace_save_enqueues_one_trusted_internal_start_event() -> None:
    store = AttachmentDraftStore(ttl_seconds=60)
    draft = store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="research",
        session_id="ou_researcher",
        message_type="FriendMessage",
        sender_id="ou_researcher",
        sender_name="研究员",
    )
    store.save_task_data(
        draft.token,
        {
            "research_question": "比较三家供应商的交付能力",
            "output_format": "comparison",
            "model_choice": "auto",
        },
    )
    internal_event = MagicMock()
    platform = MagicMock()
    platform.create_event.return_value = internal_event
    context = SimpleNamespace(
        get_platform_inst=MagicMock(return_value=platform),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                core_lifecycle=SimpleNamespace(star_context=context),
            )
        )
    )

    await _enqueue_workspace_task(request, store.get(draft.token))

    message = platform.create_event.call_args.args[0]
    assert message.is_card_action is True
    assert message.card_action_payload["value"]["action"] == "start_task"
    assert message.card_action_payload["value"]["task_type"] == "research"
    assert message.session_id == "ou_researcher"
    assert "比较三家供应商" in message.message_str
    internal_event.set_extra.assert_any_call("assistant_workbench_auto_submit", True)
    internal_event.set_extra.assert_any_call(
        "assistant_workbench_workspace_url",
        draft.upload_url,
    )
    platform.commit_event.assert_called_once_with(internal_event)


def test_jsapi_signature_uses_feishu_parameter_order() -> None:
    verify = (
        "jsapi_ticket=ticket-1&noncestr=nonce-1&timestamp=1234567890&"
        "url=https://example.feishu.cn/upload"
    )

    assert (
        build_jsapi_signature(
            ticket="ticket-1",
            nonce="nonce-1",
            timestamp=1234567890,
            url="https://example.feishu.cn/upload",
        )
        == hashlib.sha1(verify.encode()).hexdigest()
    )


@pytest.mark.asyncio
async def test_upload_stages_attachment_then_confirmation_patches_card() -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
    )
    draft_store.bind_message(draft.token, "om_test")
    chat_service = SimpleNamespace(
        save_uploaded_file=AsyncMock(
            return_value={
                "attachment_id": "att_test",
                "filename": "brief.pdf",
                "type": "file",
            }
        )
    )
    patch_response = MagicMock()
    patch_response.success.return_value = True
    patch_client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(
                message=SimpleNamespace(apatch=AsyncMock(return_value=patch_response))
            )
        )
    )
    context = SimpleNamespace(
        get_platform_inst=MagicMock(
            return_value=SimpleNamespace(
                lark_api=patch_client,
                config={"app_id": "cli_test"},
            )
        )
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                services=SimpleNamespace(chat=chat_service),
                core_lifecycle=SimpleNamespace(star_context=context),
            )
        )
    )
    uploads = [
        UploadFile(
            file=BytesIO(b"test"),
            filename="brief.pdf",
            size=4,
            headers=Headers({"content-type": "application/pdf"}),
        ),
        UploadFile(
            file=BytesIO(b"a,b"),
            filename="metrics.csv",
            size=3,
            headers=Headers({"content-type": "text/csv"}),
        ),
    ]
    chat_service.save_uploaded_file.side_effect = [
        {
            "attachment_id": "att_test",
            "filename": "brief.pdf",
            "type": "file",
        },
        {
            "attachment_id": "att_csv",
            "filename": "metrics.csv",
            "type": "file",
        },
    ]

    result = await upload_attachment(draft.token, request, uploads)

    assert result["card_updated"] is False
    assert result["total_attachments"] == 2
    assert [item["attachment_id"] for item in result["attachments"]] == [
        "att_test",
        "att_csv",
    ]
    assert [item["filename"] for item in draft_store.get(draft.token).attachments] == [
        "brief.pdf",
        "metrics.csv",
    ]
    assert chat_service.save_uploaded_file.await_count == 2
    patch_client.im.v1.message.apatch.assert_not_awaited()

    confirmed = await confirm_attachments(draft.token, request)

    assert confirmed == {"total_attachments": 2, "card_updated": True}
    patch_client.im.v1.message.apatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_workspace_save_queues_execution_without_second_confirmation() -> (
    None
):
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="video",
    )
    draft_store.bind_message(draft.token, "om_task_workspace")
    request = SimpleNamespace()
    patch_card = AsyncMock()
    enqueue_task = AsyncMock()

    with (
        patch(
            "astrbot.dashboard.api.assistant_attachments._patch_attachment_card",
            patch_card,
        ),
        patch(
            "astrbot.dashboard.api.assistant_attachments._enqueue_workspace_task",
            enqueue_task,
        ),
    ):
        result = await save_task_workspace(
            draft.token,
            WorkspaceRequest(
                task_data={
                    "video_prompt": "人物走入镜头，镜头缓慢后退",
                    "generation_mode": "text_to_video",
                    "duration": "8",
                    "aspect_ratio": "16:9",
                    "video_quality": "1080p",
                    "camera_motion": "free",
                    "audio_mode": "on",
                    "model_choice": "auto",
                }
            ),
            request,
        )

    assert result == {
        "task_type": "video",
        "total_attachments": 0,
        "task_queued": True,
        "card_updated": True,
    }
    stored = draft_store.get(draft.token)
    assert stored.task_data["video_prompt"] == "人物走入镜头，镜头缓慢后退"
    assert stored.task_data["model_choice"] == "auto"
    patch_card.assert_awaited_once_with(request, stored)
    enqueue_task.assert_awaited_once_with(request, stored)


@pytest.mark.asyncio
async def test_task_workspace_save_preserves_long_compiled_video_prompt() -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="video",
    )
    draft_store.bind_message(draft.token, "om_long_video_prompt")
    compiled_prompt = "五菱品牌短片分镜；" + "镜头语言与事实边界。" * 400
    request = SimpleNamespace()

    with (
        patch(
            "astrbot.dashboard.api.assistant_attachments._patch_attachment_card",
            AsyncMock(),
        ),
        patch(
            "astrbot.dashboard.api.assistant_attachments._enqueue_workspace_task",
            AsyncMock(),
        ),
    ):
        await save_task_workspace(
            draft.token,
            WorkspaceRequest(
                task_data={
                    "video_prompt": compiled_prompt,
                    "generation_mode": "text_to_video",
                    "duration": "8",
                }
            ),
            request,
        )

    assert len(compiled_prompt) > 800
    assert draft_store.get(draft.token).task_data["video_prompt"] == compiled_prompt


def test_image_workspace_exposes_real_provider_choices() -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="image",
    )

    page = build_task_workspace_page(draft)

    assert '"modelOptions":[["自动（Image2 优先，即梦兜底）","auto"]' in page
    assert '["Image2（仅使用）","image2"]' in page
    assert '["即梦（仅使用）","dreamina"]' in page
    assert "可指定生图模型" in page


@pytest.mark.asyncio
async def test_image_workspace_accepts_explicit_image2_provider() -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="image",
    )
    draft_store.bind_message(draft.token, "om_image_workspace")
    request = SimpleNamespace()

    with (
        patch(
            "astrbot.dashboard.api.assistant_attachments._patch_attachment_card",
            AsyncMock(),
        ),
        patch(
            "astrbot.dashboard.api.assistant_attachments._enqueue_workspace_task",
            AsyncMock(),
        ),
    ):
        await save_task_workspace(
            draft.token,
            WorkspaceRequest(
                task_data={
                    "visual_prompt": "新能源车发布主视觉",
                    "model_choice": "image2",
                }
            ),
            request,
        )

    assert draft_store.get(draft.token).task_data["model_choice"] == "image2"


@pytest.mark.asyncio
async def test_material_quotation_search_reads_indexed_obsidian_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="quotation",
    )
    search = MagicMock(
        return_value=[
            {
                "item_name": "背胶",
                "specification": "过哑膜",
                "unit": "㎡",
                "unit_price": "8.00",
                "supplier": "柳州东成广告",
                "source_path": "供应商库/柳州-南宁搭建物料.md",
                "source_status": "待复核",
            }
        ]
    )
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.search_material_prices",
        search,
    )

    result = await search_quotation_prices(draft.token, "背胶")

    assert result["matches"][0]["unit_price"] == "8.00"
    assert result["source"] == "Obsidian / NAS 历史报价索引"
    search.assert_called_once()


@pytest.mark.asyncio
async def test_supplier_price_endpoint_archives_employee_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="quotation",
        sender_name="筹备组员工",
    )
    draft_store.bind_message(draft.token, "om_supplier_price")
    save_submission = MagicMock(
        return_value={
            "relative_path": "20_Operations/供应商库/员工录入/搭建类/测试.md",
            "item_count": 1,
            "review_status": "待复核",
            "attachment_filename": "",
            "document_key": "doc-1",
        }
    )
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.save_supplier_price_submission",
        save_submission,
    )
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.get_astrbot_data_path",
        lambda: str(tmp_path),
    )
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.load_nas_config",
        lambda: {"nas": {"mount_point": str(tmp_path / "nas")}},
    )

    result = await save_supplier_prices(
        draft.token,
        payload=json.dumps(
            {
                "supplier_name": "柳州新伙伴广告",
                "items": [
                    {
                        "item_name": "背胶",
                        "unit": "㎡",
                        "unit_price": "8.5",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        file=None,
    )

    assert result["item_count"] == 1
    assert result["review_status"] == "待复核"
    assert result["source"] == "NAS / Obsidian 历史报价库"
    assert save_submission.call_args.kwargs["submitted_by"] == "筹备组员工"
    assert (
        save_submission.call_args.kwargs["vault_path"]
        == tmp_path.parent / "ObsidianVault"
    )
    assert save_submission.call_args.kwargs["nas_path"] == str(tmp_path / "nas")
    assert save_submission.call_args.kwargs["db_path"] == tmp_path / "nas_memory.db"


@pytest.mark.asyncio
async def test_material_quotation_save_recalculates_without_model_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="quotation",
    )
    draft_store.bind_message(draft.token, "om_quote_workspace")
    patch_card = AsyncMock()
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments._patch_attachment_card",
        patch_card,
    )
    enqueue_task = AsyncMock()
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments._enqueue_workspace_task",
        enqueue_task,
    )
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.get_astrbot_data_path",
        lambda: str(tmp_path),
    )
    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    image_dir = nas_root / "projects" / "筹备组物料报价" / draft.token[:16] / "images"
    image_dir.mkdir(parents=True)
    item_image_paths = []
    for index, color in enumerate(("#46513a", "#d96b32"), start=1):
        image_path = image_dir / f"背胶-{index}.png"
        Image.new("RGB", (120, 80), color).save(image_path)
        item_image_paths.append(str(image_path.relative_to(nas_root)))
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.load_nas_config",
        lambda: {"nas": {"mount_point": str(nas_root)}},
    )

    result = await save_task_workspace(
        draft.token,
        WorkspaceRequest(
            task_data={
                "project_name": "柳州用户共创会",
                "quotation_items": json.dumps(
                    [
                        {
                            "item_name": "背胶",
                            "quantity": "10",
                            "unit": "㎡",
                            "unit_price": "8",
                            "image_paths": item_image_paths,
                        }
                    ],
                    ensure_ascii=False,
                ),
                "transport_fee": "20",
                "tax_rate": "6",
            }
        ),
        SimpleNamespace(),
    )

    stored = draft_store.get(draft.token).task_data
    assert result["task_type"] == "quotation"
    assert stored["priced_items_subtotal"] == "80.00"
    assert stored["pre_tax_total"] == "100.00"
    assert stored["tax_fee"] == "6.00"
    assert stored["grand_total"] == "106.00"
    assert stored["quotation_status"] == "估价方案"
    patch_card.assert_awaited_once()
    enqueue_task.assert_not_awaited()
    assert result["task_queued"] is False
    assert result["result_surface"] == "market_estimate"
    assert result["version"] == 1
    assert set(result["deliverables"]) == {
        "internal_xlsx",
        "market_docx",
        "market_pdf",
    }
    assert result["deliverables"]["market_pdf"].endswith(
        "/quotation-deliverables/market_pdf?version=1"
    )
    archive_dir = nas_root / "projects" / "筹备组物料报价" / draft.token[:16] / "v1"
    assert result["nas_archive_path"] == str(archive_dir.relative_to(nas_root))
    assert (archive_dir / "manifest.json").is_file()
    assert (
        json.loads((archive_dir / "manifest.json").read_text(encoding="utf-8"))[
            "images"
        ]
        == item_image_paths
    )
    assert {path.suffix for path in archive_dir.iterdir()} >= {".xlsx", ".docx", ".pdf"}


@pytest.mark.asyncio
async def test_quotation_item_image_upload_normalizes_to_nas_png(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="quotation",
    )
    draft_store.bind_message(draft.token, "om_quote_image")
    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.load_nas_config",
        lambda: {"nas": {"mount_point": str(nas_root)}},
    )
    image_bytes = BytesIO()
    Image.new("RGB", (200, 120), "#46513a").save(image_bytes, format="WEBP")
    payload = image_bytes.getvalue()
    upload = UploadFile(
        filename="礼盒.webp",
        file=BytesIO(payload),
        size=len(payload),
        headers=Headers({"content-type": "image/webp"}),
    )

    result = await upload_quotation_item_image(draft.token, upload)

    saved_path = nas_root / result["image_path"]
    assert saved_path.is_file()
    assert saved_path.suffix == ".png"
    assert result["image_filename"].endswith(".png")
    assert result["images"] == [
        {
            "image_path": result["image_path"],
            "image_filename": result["image_filename"],
        }
    ]
    with Image.open(saved_path) as saved_image:
        assert saved_image.format == "PNG"


@pytest.mark.asyncio
async def test_quotation_item_image_upload_accepts_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="quotation",
    )
    draft_store.bind_message(draft.token, "om_quote_image_batch")
    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.load_nas_config",
        lambda: {"nas": {"mount_point": str(nas_root)}},
    )
    uploads = []
    for index, color in enumerate(("#46513a", "#d96b32"), start=1):
        image_bytes = BytesIO()
        Image.new("RGB", (200, 120), color).save(image_bytes, format="JPEG")
        payload = image_bytes.getvalue()
        uploads.append(
            UploadFile(
                filename=f"礼盒-{index}.jpg",
                file=BytesIO(payload),
                size=len(payload),
                headers=Headers({"content-type": "image/jpeg"}),
            )
        )

    result = await upload_quotation_item_image(
        draft.token,
        file=None,
        files=uploads,
    )

    assert len(result["images"]) == 2
    for image in result["images"]:
        saved_path = nas_root / image["image_path"]
        assert saved_path.is_file()
        with Image.open(saved_path) as saved_image:
            assert saved_image.format == "PNG"


@pytest.mark.asyncio
async def test_quotation_item_image_upload_rejects_more_than_six(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="quotation",
    )
    draft_store.bind_message(draft.token, "om_quote_image_limit")
    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.load_nas_config",
        lambda: {"nas": {"mount_point": str(nas_root)}},
    )
    uploads = [
        UploadFile(filename=f"礼盒-{index}.png", file=BytesIO(b"unused"))
        for index in range(7)
    ]

    with pytest.raises(HTTPException) as exc_info:
        await upload_quotation_item_image(draft.token, file=None, files=uploads)

    assert exc_info.value.status_code == 413
    assert "最多上传 6 张" in exc_info.value.detail


@pytest.mark.asyncio
async def test_quotation_deliverable_route_serves_manifest_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    token = "a" * 32
    version_dir = tmp_path / "output" / "material_quotations" / token / "v2"
    version_dir.mkdir(parents=True)
    generated = version_dir / "市场估价.pdf"
    generated.write_bytes(b"%PDF-1.4\n")
    (version_dir / "manifest.json").write_text(
        json.dumps({"files": {"market_pdf": generated.name}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments.get_astrbot_data_path",
        lambda: str(tmp_path),
    )

    response = await download_quotation_deliverable(token, "market_pdf", 2)

    assert response.path == generated
    assert response.media_type == "application/pdf"
    assert "filename*=utf-8''" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_feishu_doc_sync_is_optional_idempotent_and_sanitized() -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="quotation",
    )
    draft_store.save_task_data(
        draft.token,
        {
            "project_name": "柳州用户共创会",
            "client_name": "五菱",
            "grand_total": "106000.00",
            "validity_days": "15",
            "quotation_status": "估价方案",
            "quotation_items": '[{"item_name":"背胶"}]',
            "profit_fee": "4321.98",
        },
    )
    draft_store.save_quotation_delivery(
        draft.token,
        deliverables={"market_pdf": "https://example.com/market.pdf"},
        version=1,
    )
    create_response = SimpleNamespace(
        success=lambda: True,
        data=SimpleNamespace(document=SimpleNamespace(document_id="doccn_quote")),
        msg="",
    )
    write_response = SimpleNamespace(success=lambda: True, msg="")
    create = AsyncMock(return_value=create_response)
    write = AsyncMock(return_value=write_response)
    client = SimpleNamespace(
        docx=SimpleNamespace(
            v1=SimpleNamespace(
                document=SimpleNamespace(acreate=create),
                document_block_children=SimpleNamespace(acreate=write),
            )
        )
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                core_lifecycle=SimpleNamespace(
                    star_context=SimpleNamespace(
                        get_platform_inst=lambda _platform_id: SimpleNamespace(
                            lark_api=client
                        )
                    )
                )
            )
        )
    )

    result = await sync_quotation_to_feishu_doc(draft.token, request)
    repeated = await sync_quotation_to_feishu_doc(draft.token, request)

    assert result == {
        "feishu_doc_url": "https://feishu.cn/docx/doccn_quote",
        "version": 1,
        "created": True,
    }
    assert repeated["created"] is False
    create.assert_awaited_once()
    write.assert_awaited_once()
    write_request = write.await_args.args[0]
    text = "\n".join(
        block.text.elements[0].text_run.content
        for block in write_request.request_body.children
    )
    assert "柳州用户共创会" in text
    assert "¥106000.00" in text
    assert "背胶" not in text
    assert "4321.98" not in text


@pytest.mark.asyncio
async def test_material_quotation_cannot_send_partial_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="quotation",
    )
    draft_store.bind_message(draft.token, "om_quote_workspace_pending")
    patch_card = AsyncMock()
    monkeypatch.setattr(
        "astrbot.dashboard.api.assistant_attachments._patch_attachment_card",
        patch_card,
    )

    with pytest.raises(Exception) as error:
        await save_task_workspace(
            draft.token,
            WorkspaceRequest(
                task_data={
                    "project_name": "柳州用户共创会",
                    "quotation_items": json.dumps(
                        [
                            {
                                "item_name": "现场摄影",
                                "quantity": "1",
                                "unit": "项",
                                "unit_price": "",
                            }
                        ],
                        ensure_ascii=False,
                    ),
                }
            ),
            SimpleNamespace(),
        )

    assert getattr(error.value, "status_code", None) == 400
    assert "待询价" in str(getattr(error.value, "detail", ""))
    patch_card.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_workspace_requires_a_real_file_or_link() -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="file",
    )
    draft_store.bind_message(draft.token, "om_file_workspace")

    with pytest.raises(Exception) as error:
        await save_task_workspace(
            draft.token,
            WorkspaceRequest(task_data={"file_goal": "总结合同风险"}),
            SimpleNamespace(),
        )

    assert getattr(error.value, "status_code", None) == 400
    assert "文件" in str(getattr(error.value, "detail", ""))

    with pytest.raises(Exception) as invalid_link_error:
        await save_task_workspace(
            draft.token,
            WorkspaceRequest(
                task_data={"file_goal": "总结合同风险", "file_source": "合同第二页"}
            ),
            SimpleNamespace(),
        )

    assert getattr(invalid_link_error.value, "status_code", None) == 400
    assert "有效文件链接" in str(getattr(invalid_link_error.value, "detail", ""))


@pytest.mark.asyncio
async def test_file_workspace_accepts_an_explicit_file_link() -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="file",
    )
    draft_store.bind_message(draft.token, "om_file_link_workspace")
    patch_card = AsyncMock()
    enqueue_task = AsyncMock()

    with (
        patch(
            "astrbot.dashboard.api.assistant_attachments._patch_attachment_card",
            patch_card,
        ),
        patch(
            "astrbot.dashboard.api.assistant_attachments._enqueue_workspace_task",
            enqueue_task,
        ),
    ):
        result = await save_task_workspace(
            draft.token,
            WorkspaceRequest(
                task_data={
                    "file_goal": "总结合同风险",
                    "file_source": "https://example.feishu.cn/docx/contract",
                }
            ),
            SimpleNamespace(),
        )

    assert result["task_type"] == "file"
    assert result["task_queued"] is True
    patch_card.assert_awaited_once()
    enqueue_task.assert_awaited_once()


@pytest.mark.asyncio
async def test_research_workspace_defaults_to_llm_analysis_and_result_card() -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="research",
    )
    draft_store.bind_message(draft.token, "om_research_workspace")

    with (
        patch(
            "astrbot.dashboard.api.assistant_attachments._patch_attachment_card",
            AsyncMock(),
        ),
        patch(
            "astrbot.dashboard.api.assistant_attachments._enqueue_workspace_task",
            AsyncMock(),
        ),
    ):
        result = await save_task_workspace(
            draft.token,
            WorkspaceRequest(
                task_data={"research_question": "判断三家供应商的交付能力"}
            ),
            SimpleNamespace(),
        )

    saved = draft_store.get(draft.token)
    assert result["task_queued"] is True
    assert saved is not None
    assert saved.task_data["analysis_mode"] == "evidence_review"
    assert saved.task_data["research_depth"] == "standard"
    assert saved.task_data["source_policy"] == "mixed"
    assert saved.task_data["output_format"] == "short_answer"
    assert saved.task_data["delivery_format"] == "result_card"


@pytest.mark.asyncio
async def test_file_compare_requires_two_real_sources() -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="file",
    )
    draft_store.bind_message(draft.token, "om_file_compare_workspace")

    with pytest.raises(Exception) as error:
        await save_task_workspace(
            draft.token,
            WorkspaceRequest(
                task_data={
                    "file_goal": "对比合同差异",
                    "file_source": "https://example.feishu.cn/docx/contract-a",
                    "operation": "compare",
                    "output_format": "pdf",
                }
            ),
            SimpleNamespace(),
        )

    assert getattr(error.value, "status_code", None) == 400
    assert "至少需要两份" in str(getattr(error.value, "detail", ""))


@pytest.mark.asyncio
async def test_file_translation_requires_target_and_source_compatible_delivery() -> (
    None
):
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="file",
    )
    draft_store.bind_message(draft.token, "om_file_translation")
    draft_store.attach_many(
        draft.token,
        [
            {
                "attachment_id": "att_quote",
                "filename": "quote.xlsx",
                "size": 1200,
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "type": "local_file",
            }
        ],
    )

    with pytest.raises(Exception) as target_error:
        await save_task_workspace(
            draft.token,
            WorkspaceRequest(
                task_data={
                    "file_goal": "翻译整个报价表",
                    "operation": "translate",
                    "output_format": "xlsx",
                }
            ),
            SimpleNamespace(),
        )
    assert getattr(target_error.value, "status_code", None) == 400
    assert "目标语言" in str(getattr(target_error.value, "detail", ""))

    with pytest.raises(Exception) as delivery_error:
        await save_task_workspace(
            draft.token,
            WorkspaceRequest(
                task_data={
                    "file_goal": "翻译整个报价表",
                    "operation": "translate",
                    "target_language": "English",
                    "output_format": "docx",
                }
            ),
            SimpleNamespace(),
        )
    assert getattr(delivery_error.value, "status_code", None) == 400
    assert "Excel" in str(getattr(delivery_error.value, "detail", ""))


@pytest.mark.asyncio
async def test_cloud_documents_are_staged_then_confirmed() -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
    )
    draft_store.bind_message(draft.token, "om_docs")
    patch_response = MagicMock()
    patch_response.success.return_value = True
    patch_client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(
                message=SimpleNamespace(apatch=AsyncMock(return_value=patch_response))
            )
        )
    )
    context = SimpleNamespace(
        get_platform_inst=MagicMock(
            return_value=SimpleNamespace(
                lark_api=patch_client,
                config={"app_id": "cli_test"},
            )
        )
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                core_lifecycle=SimpleNamespace(star_context=context),
            )
        )
    )

    result = await link_cloud_documents(
        draft.token,
        CloudDocsRequest(
            files=[
                {
                    "fileName": "新品文档",
                    "filePath": "https://example.feishu.cn/docx/doc_123",
                },
                {
                    "fileName": "数据表",
                    "filePath": "https://example.feishu.cn/sheets/sht_456",
                },
            ]
        ),
        request,
    )

    assert result["card_updated"] is False
    assert result["total_attachments"] == 2
    assert len(result["attachments"]) == 2
    assert all(item["type"] == "cloud_doc" for item in result["attachments"])
    assert patch_client.im.v1.message.apatch.await_count == 0

    confirmed = await confirm_attachments(draft.token, request)

    assert confirmed["card_updated"] is True
    assert patch_client.im.v1.message.apatch.await_count == 1


@pytest.mark.asyncio
async def test_cloud_docs_oauth_round_trip_and_drive_listing() -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://192.168.2.162:6185",
    )
    request = SimpleNamespace()

    with (
        patch.dict(
            "os.environ",
            {"FEISHU_UNIFIED_CONNECTOR_BASE_URL": "https://connector.example.test"},
            clear=False,
        ),
        patch(
            "astrbot.dashboard.api.assistant_attachments.secrets.token_urlsafe",
            return_value="draft-state",
        ),
    ):
        redirect = await start_cloud_docs_oauth(draft.token, request)
    authorization = urlsplit(redirect.headers["location"])
    query = parse_qs(authorization.query)
    return_to = urlsplit(query["return_to"][0])
    state = parse_qs(return_to.query)["state"][0]

    assert authorization.scheme == "https"
    assert authorization.netloc == "connector.example.test"
    assert authorization.path == "/api/auth/feishu/start"
    assert query["purpose"] == ["cloud_docs"]
    assert query["app"] == ["agent"]
    assert return_to.path.endswith(f"/{draft.token}/oauth/callback")
    assert state == "draft-state"

    exchange_result = {
        "ok": True,
        "purpose": "cloud_docs",
        "access_token": "u-test",
        "access_token_expires_at": time.time() + 7200,
        "grant_token": "grant-test",
        "grant_expires_at": time.time() + 86400,
    }
    with patch(
        "astrbot.dashboard.api.assistant_attachments._connector_post",
        new=AsyncMock(return_value=exchange_result),
    ):
        callback = await finish_cloud_docs_oauth(
            draft.token,
            request,
            state=state,
            handoff_code="handoff-code",
        )

    assert callback.headers["location"].endswith("?cloud=1")
    assert draft.user_access_token == "u-test"
    assert draft.oauth_grant_token == "grant-test"

    drive_response = MagicMock(
        status_code=200,
        json=MagicMock(
            return_value={
                "code": 0,
                "data": {
                    "files": [
                        {
                            "name": "项目方案",
                            "type": "docx",
                            "token": "docx_1",
                            "url": "https://example.feishu.cn/docx/docx_1",
                        },
                        {
                            "name": "资料目录",
                            "type": "folder",
                            "token": "fld_1",
                            "url": "https://example.feishu.cn/drive/folder/fld_1",
                        },
                    ]
                },
            }
        ),
    )
    drive_client = MagicMock()
    drive_client.__aenter__ = AsyncMock(
        return_value=SimpleNamespace(get=AsyncMock(return_value=drive_response))
    )
    drive_client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch(
            "astrbot.dashboard.api.assistant_attachments._connector_post",
            new=AsyncMock(return_value={"ok": True, "allowed": True}),
        ),
        patch(
            "astrbot.dashboard.api.assistant_attachments.httpx.AsyncClient",
            return_value=drive_client,
        ),
    ):
        listing = await list_cloud_documents(draft.token)

    assert [item["fileName"] for item in listing["files"]] == [
        "项目方案",
        "资料目录",
    ]
    assert listing["files"][1]["isFolder"] is True


@pytest.mark.asyncio
async def test_connector_requests_use_the_dedicated_nas_tunnel() -> None:
    response = MagicMock(status_code=200)
    response.json.return_value = {"ok": True, "allowed": True}
    post = AsyncMock(return_value=response)
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=SimpleNamespace(post=post))
    client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.dict(
            "os.environ",
            {
                "FEISHU_UNIFIED_CONNECTOR_BASE_URL": ("https://dianchi2026.vercel.app"),
                "FEISHU_UNIFIED_CONNECTOR_PROXY_URL": (
                    "http://aihubmix-aws-tunnel:7898"
                ),
            },
            clear=False,
        ),
        patch(
            "astrbot.dashboard.api.assistant_attachments.httpx.AsyncClient",
            return_value=client,
        ) as client_factory,
    ):
        result = await _connector_post(
            "/api/oauth/grant/check",
            {"grant_token": "grant-test"},
        )

    assert result == {"ok": True, "allowed": True}
    client_factory.assert_called_once_with(
        timeout=10,
        proxy="http://aihubmix-aws-tunnel:7898",
    )
    post.assert_awaited_once_with(
        "https://dianchi2026.vercel.app/api/oauth/grant/check",
        json={"grant_token": "grant-test"},
    )


@pytest.mark.asyncio
async def test_cloud_docs_access_expiry_refreshes_grant_and_retries_drive() -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://192.168.2.162:6185",
    )
    draft.user_access_token = "access-old"
    draft.user_access_token_expires_at = time.time() + 7200
    draft.oauth_grant_token = "grant-old"

    unauthorized = MagicMock(
        status_code=401,
        json=MagicMock(return_value={"code": 1061005, "msg": "expired"}),
    )
    success = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"code": 0, "data": {"files": []}}),
    )
    drive_client = MagicMock()
    drive_client.__aenter__ = AsyncMock(
        return_value=SimpleNamespace(get=AsyncMock(side_effect=[unauthorized, success]))
    )
    drive_client.__aexit__ = AsyncMock(return_value=False)
    connector = AsyncMock(
        side_effect=[
            {"ok": True, "allowed": True},
            {
                "ok": True,
                "access_token": "access-new",
                "access_token_expires_at": time.time() + 7200,
                "grant_token": "grant-new",
            },
        ]
    )

    with (
        patch(
            "astrbot.dashboard.api.assistant_attachments._connector_post",
            new=connector,
        ),
        patch(
            "astrbot.dashboard.api.assistant_attachments.httpx.AsyncClient",
            return_value=drive_client,
        ),
    ):
        result = await list_cloud_documents(draft.token)

    assert result == {"files": []}
    assert draft.user_access_token == "access-new"
    assert draft.oauth_grant_token == "grant-new"
    assert connector.await_args_list[0].args[0] == "/api/oauth/grant/check"
    assert connector.await_args_list[1].args[0] == "/api/oauth/grant/refresh"


@pytest.mark.asyncio
async def test_connector_outage_preserves_existing_cloud_grant() -> None:
    draft = draft_store.create(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://192.168.2.162:6185",
    )
    draft.user_access_token = "access-existing"
    draft.user_access_token_expires_at = time.time() + 7200
    draft.oauth_grant_token = "grant-existing"

    with (
        patch(
            "astrbot.dashboard.api.assistant_attachments._connector_post",
            new=AsyncMock(
                side_effect=HTTPException(
                    status_code=503,
                    detail="connector unavailable",
                )
            ),
        ),
        pytest.raises(HTTPException) as error,
    ):
        await list_cloud_documents(draft.token)

    assert error.value.status_code == 503
    assert draft.user_access_token == "access-existing"
    assert draft.oauth_grant_token == "grant-existing"
