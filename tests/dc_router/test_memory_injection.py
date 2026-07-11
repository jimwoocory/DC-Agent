from data.plugins.dc_router import memory_injection


def test_new_creative_work_does_not_trigger_historical_memory() -> None:
    assert memory_injection._should_retrieve("帮我搭建一个新的活动方案框架") is False
    assert memory_injection._should_retrieve("帮我生成一张五菱夏至海报") is False
    assert memory_injection._should_retrieve("查一下之前五菱活动方案") is True


def test_governed_memory_recall_uses_bounded_index_queries(
    monkeypatch, tmp_path
) -> None:
    db_path = tmp_path / "governed_memory.db"
    db_path.touch()
    calls: list[tuple[str, int]] = []

    monkeypatch.setattr(memory_injection, "GOVERNED_MEMORY_DB", db_path)
    monkeypatch.setattr(
        memory_injection, "MemoryGovernanceStore", lambda _path: object()
    )

    def fake_recall(*, store, query, limit, **_kwargs):
        _ = store
        calls.append((query, limit))
        return []

    monkeypatch.setattr(memory_injection, "list_recall_memories", fake_recall)

    result = memory_injection.retrieve_governed_memory_context(
        "查一下之前五菱活动方案",
        limit=5,
    )

    assert result["governed_memories"] == []
    assert 0 < len(calls) <= 8
    assert all(query for query, _limit in calls)
    assert all(limit <= 20 for _query, limit in calls)
