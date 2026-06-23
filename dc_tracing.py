from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger("dc.tracing")


@dataclass(slots=True)
class DCTraceSpan:
    name: str
    attributes: dict[str, object] = field(default_factory=dict)
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None

    def finish(self, *, error: BaseException | None = None) -> None:
        if self.finished_at is not None:
            return
        self.finished_at = time.time()
        if error is not None:
            self.error = error.__class__.__name__
        _logger.info(
            "dc_trace_span",
            extra={
                "span_id": self.span_id,
                "span_name": self.name,
                "duration_ms": round((self.finished_at - self.started_at) * 1000, 3),
                "attributes": self.attributes,
                "error": self.error,
            },
        )


@contextmanager
def trace_span(name: str, **attributes: Any) -> Iterator[DCTraceSpan]:
    span = DCTraceSpan(name=name, attributes=dict(attributes))
    try:
        yield span
    except BaseException as exc:
        span.finish(error=exc)
        raise
    else:
        span.finish()
