import logging
from unittest.mock import call, patch

from astrbot.core.log import (
    LogBroker,
    LogManager,
    LogQueueHandler,
    SafeAstrBotFormatter,
)


def test_log_queue_handler_enriches_plain_log_records() -> None:
    broker = LogBroker()
    handler = LogQueueHandler(broker)
    handler.setFormatter(
        logging.Formatter(
            "%(plugin_tag)s [%(short_levelname)s] "
            "[%(source_file)s:%(source_line)d]: %(message)s"
        )
    )
    record = logging.LogRecord(
        name="external.lib",
        level=logging.INFO,
        pathname="/tmp/external.py",
        lineno=12,
        msg="hello",
        args=(),
        exc_info=None,
    )

    handler.emit(record)

    assert len(broker.log_cache) == 1
    assert broker.log_cache[0]["data"] == "[Core] [INFO] [tmp.external:12]: hello"


def test_safe_astrbot_formatter_enriches_plain_log_records() -> None:
    formatter = SafeAstrBotFormatter(
        "%(plugin_tag)s [%(short_levelname)s] "
        "[%(source_file)s:%(source_line)d]: %(message)s"
    )
    record = logging.LogRecord(
        name="external.lib",
        level=logging.INFO,
        pathname="/tmp/external.py",
        lineno=12,
        msg="hello",
        args=(),
        exc_info=None,
    )

    assert formatter.format(record) == "[Core] [INFO] [tmp.external:12]: hello"


def test_log_manager_shutdown_closes_owned_file_sinks(monkeypatch) -> None:
    monkeypatch.setattr(LogManager, "_file_sink_id", 101)
    monkeypatch.setattr(LogManager, "_trace_sink_id", 202)

    with patch.object(LogManager, "_remove_sink") as remove_sink:
        LogManager.shutdown()

    assert remove_sink.call_args_list == [call(202), call(101)]
    assert LogManager._file_sink_id is None
    assert LogManager._trace_sink_id is None
