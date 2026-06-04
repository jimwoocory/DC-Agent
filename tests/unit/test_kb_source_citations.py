import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def stub_provider_manager_module():
    original_module = sys.modules.get("astrbot.core.provider.manager")
    stub_module = types.ModuleType("astrbot.core.provider.manager")

    class ProviderManager: ...

    setattr(stub_module, "ProviderManager", ProviderManager)
    sys.modules["astrbot.core.provider.manager"] = stub_module

    try:
        yield
    finally:
        if original_module is not None:
            sys.modules["astrbot.core.provider.manager"] = original_module
        else:
            sys.modules.pop("astrbot.core.provider.manager", None)


@pytest.mark.asyncio
async def test_retrieval_result_includes_source_path_metadata(
    stub_provider_manager_module,
):
    from astrbot.core.knowledge_base.models import KBDocument, KnowledgeBase
    from astrbot.core.knowledge_base.retrieval.manager import RetrievalManager
    from astrbot.core.knowledge_base.retrieval.rank_fusion import FusedResult

    source_path = "/NAS/FeishuDocs/客户A/交付方案.md"
    kb = KnowledgeBase(kb_id="kb-1", kb_name="项目交付知识库")
    doc = KBDocument(
        doc_id="doc-1",
        kb_id="kb-1",
        doc_name="交付方案.md",
        file_type="md",
        file_size=128,
        file_path=source_path,
        chunk_count=1,
        media_count=0,
    )

    vec_db = SimpleNamespace(retrieve=AsyncMock(return_value=[]), rerank_provider=None)
    kb_helper = SimpleNamespace(kb=kb, vec_db=vec_db)
    sparse_retriever = SimpleNamespace(retrieve=AsyncMock(return_value=[]))
    rank_fusion = SimpleNamespace(
        fuse=AsyncMock(
            return_value=[
                FusedResult(
                    chunk_id="chunk-1",
                    chunk_index=0,
                    doc_id="doc-1",
                    kb_id="kb-1",
                    content="客户A项目交付周期为四周。",
                    score=0.42,
                )
            ]
        )
    )
    kb_db = SimpleNamespace(
        get_documents_with_metadata_batch=AsyncMock(
            return_value={"doc-1": {"document": doc, "knowledge_base": kb}}
        )
    )
    manager = RetrievalManager(
        sparse_retriever=sparse_retriever,
        rank_fusion=rank_fusion,
        kb_db=kb_db,
    )

    results = await manager.retrieve(
        query="客户A交付周期",
        kb_ids=["kb-1"],
        kb_id_helper_map={"kb-1": kb_helper},
    )

    assert results[0].metadata["source_path"] == source_path


def test_formatted_context_includes_source_path_for_agent_citations(
    stub_provider_manager_module,
):
    from astrbot.core.knowledge_base.kb_mgr import KnowledgeBaseManager
    from astrbot.core.knowledge_base.retrieval.manager import RetrievalResult

    source_path = "/NAS/FeishuDocs/客户A/交付方案.md"
    manager = KnowledgeBaseManager.__new__(KnowledgeBaseManager)

    context_text = manager._format_context(
        [
            RetrievalResult(
                chunk_id="chunk-1",
                doc_id="doc-1",
                doc_name="交付方案.md",
                kb_id="kb-1",
                kb_name="项目交付知识库",
                content="客户A项目交付周期为四周。",
                score=0.42,
                metadata={
                    "chunk_index": 0,
                    "char_count": 14,
                    "source_path": source_path,
                },
            )
        ]
    )

    assert f"来源路径: {source_path}" in context_text


def test_formatted_context_includes_obsidian_wikilink_for_source_document(
    stub_provider_manager_module,
):
    from astrbot.core.knowledge_base.kb_mgr import KnowledgeBaseManager
    from astrbot.core.knowledge_base.retrieval.manager import RetrievalResult

    manager = KnowledgeBaseManager.__new__(KnowledgeBaseManager)

    context_text = manager._format_context(
        [
            RetrievalResult(
                chunk_id="chunk-1",
                doc_id="doc-1",
                doc_name="交付方案.md",
                kb_id="kb-1",
                kb_name="项目交付知识库",
                content="客户A项目交付周期为四周。",
                score=0.42,
                metadata={
                    "chunk_index": 0,
                    "char_count": 14,
                    "source_path": "/NAS/FeishuDocs/客户A/交付方案.md",
                },
            )
        ]
    )

    assert "来源引用: [[交付方案]]" in context_text


@pytest.mark.asyncio
async def test_fixture_import_retrieval_context_requires_source_citation(
    stub_provider_manager_module,
):
    from astrbot.core.astr_main_agent import KB_CITATION_INSTRUCTION
    from astrbot.core.knowledge_base.kb_mgr import KnowledgeBaseManager
    from astrbot.core.knowledge_base.models import KBDocument, KnowledgeBase
    from astrbot.core.knowledge_base.retrieval.manager import RetrievalManager
    from astrbot.core.knowledge_base.retrieval.rank_fusion import FusedResult
    from astrbot.dashboard.routes.knowledge_base import KnowledgeBaseRoute

    fixture_path = Path("tests/fixtures/kb/customer_a_delivery.md")
    fixture_text = fixture_path.read_text(encoding="utf-8").strip()
    source_path = "/NAS/FeishuDocs/客户A/交付方案.md"
    kb = KnowledgeBase(kb_id="kb-fixture", kb_name="项目交付知识库")

    class RecordingKBHelper:
        def __init__(self):
            self.kb = kb
            self.vec_db = SimpleNamespace(
                retrieve=AsyncMock(return_value=[]),
                rerank_provider=None,
            )
            self.document = None
            self.chunk_text = ""

        async def upload_document(
            self,
            *,
            file_name,
            file_type,
            pre_chunked_text,
            source_path=None,
            **kwargs,
        ):
            self.chunk_text = "\n".join(pre_chunked_text)
            self.document = KBDocument(
                doc_id="doc-fixture",
                kb_id=kb.kb_id,
                doc_name=file_name,
                file_type=file_type,
                file_size=len(self.chunk_text),
                file_path=source_path or "",
                chunk_count=len(pre_chunked_text),
                media_count=0,
            )
            return self.document

    kb_helper = RecordingKBHelper()
    route = KnowledgeBaseRoute.__new__(KnowledgeBaseRoute)
    route.upload_progress = {}
    route.upload_tasks = {}

    await KnowledgeBaseRoute._background_import_task(
        route,
        task_id="fixture-citation",
        kb_helper=kb_helper,
        documents=[
            {
                "file_name": fixture_path.name,
                "file_type": "md",
                "chunks": [fixture_text],
                "source_path": source_path,
            }
        ],
        batch_size=32,
        tasks_limit=3,
        max_retries=3,
    )

    assert route.upload_tasks["fixture-citation"]["result"]["success_count"] == 1
    assert kb_helper.document.file_path == source_path

    retrieval_manager = RetrievalManager(
        sparse_retriever=SimpleNamespace(retrieve=AsyncMock(return_value=[])),
        rank_fusion=SimpleNamespace(
            fuse=AsyncMock(
                return_value=[
                    FusedResult(
                        chunk_id="chunk-fixture",
                        chunk_index=0,
                        doc_id=kb_helper.document.doc_id,
                        kb_id=kb.kb_id,
                        content=kb_helper.chunk_text,
                        score=0.9,
                    )
                ]
            )
        ),
        kb_db=SimpleNamespace(
            get_documents_with_metadata_batch=AsyncMock(
                return_value={
                    kb_helper.document.doc_id: {
                        "document": kb_helper.document,
                        "knowledge_base": kb,
                    }
                }
            )
        ),
    )

    results = await retrieval_manager.retrieve(
        query="客户A交付周期",
        kb_ids=[kb.kb_id],
        kb_id_helper_map={kb.kb_id: kb_helper},
    )
    context_text = KnowledgeBaseManager.__new__(KnowledgeBaseManager)._format_context(
        results
    )

    assert "客户A项目交付周期为四周" in context_text
    assert f"来源路径: {source_path}" in context_text
    assert "来源引用: [[交付方案]]" in context_text
    assert "来源路径" in KB_CITATION_INSTRUCTION
    assert "不要编造来源" in KB_CITATION_INSTRUCTION
