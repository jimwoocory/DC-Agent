import logging

from astrbot.core.log import LogBroker, LogQueueHandler, SafeAstrBotFormatter


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
