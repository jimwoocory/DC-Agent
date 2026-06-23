from __future__ import annotations

import importlib
from pathlib import Path

DOCKERFILE = Path("Dockerfile")
DOCKERIGNORE = Path(".dockerignore")
MAIN = Path("main.py")


def test_dockerfile_healthcheck_uses_available_runtime_python() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "HEALTHCHECK" in dockerfile
    assert "/api/chat/health" in dockerfile
    assert "python" in dockerfile
    assert "urllib.request" in dockerfile


def test_docker_context_keeps_dashboard_dist_but_excludes_runtime_data() -> None:
    dockerignore = DOCKERIGNORE.read_text(encoding="utf-8")
    ignored_entries = {
        line.strip()
        for line in dockerignore.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "data/*" in dockerignore
    assert "!data/dist/" in dockerignore
    assert "!data/dist/**" in dockerignore
    assert "data/" not in ignored_entries
    assert {
        ".claude/",
        "docs/",
        "hermes-agent/",
        "node_modules/",
        "output/",
        "vendor/",
    }.issubset(ignored_entries)


def test_observability_helpers_import_without_optional_dependencies() -> None:
    dc_logging = importlib.import_module("dc_logging")
    dc_metrics = importlib.import_module("dc_metrics")
    dc_tracing = importlib.import_module("dc_tracing")

    logger = dc_logging.configure_dc_logging("INFO")
    count = dc_metrics.increment_counter("dc agent startup total")
    with dc_tracing.trace_span("test.span", component="pytest") as span:
        span_id = span.span_id

    assert logger.name == "dc"
    assert count >= 1
    assert dc_metrics.normalize_metric_name("1 bad metric") == "dc_agent_1_bad_metric"
    assert isinstance(span_id, str)
    assert "dc_agent_uptime_seconds" in dc_metrics.registry.render_prometheus_text()


def test_main_initializes_dc_observability_without_monkeypatching() -> None:
    source = MAIN.read_text(encoding="utf-8")

    assert "import dc_logging" in source
    assert "import dc_metrics" in source
    assert "import dc_tracing" in source
    assert "configure_dc_logging()" in source
    assert 'increment_counter("dc_agent_startup_total")' in source
    assert 'with dc_tracing.trace_span("main_async"' in source
