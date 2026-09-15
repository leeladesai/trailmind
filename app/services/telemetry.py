"""P1-6: structured logging with request-ID correlation, plus lightweight in-process
request metrics for GET /metrics.

No new dependency (no structlog/prometheus_client): a request-id-tagged log line
format and a couple of in-memory counters don't need one, and pulling in a metrics
library for this is more surface than the actual requirement justifies. If this ever
needs to survive a process restart or be aggregated across instances, that's the
point to reach for a real metrics backend — not before.
"""

from __future__ import annotations

import contextvars
import logging
import threading

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


class RequestIdLogFilter(logging.Filter):
    """Injects the current request's id (from `request_id_var`) into every log
    record emitted while that request is in flight, so `%(request_id)s` in the log
    format correlates every line logged anywhere in the codebase during a request —
    without threading a request/logger object through every function signature."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """Replaces the root logger's handlers rather than appending to them, so calling
    this once per create_app() (including once per app instance the test suite
    creates) doesn't accumulate duplicate handlers/log lines."""
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdLogFilter())
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


class RequestMetrics:
    """In-memory HTTP request counters, exposed via GET /metrics in Prometheus text
    exposition format. Lives on `app.state` (one instance per FastAPI app), not at
    module level — a module-level store would leak/accumulate counts across the many
    app instances the test suite creates within a single process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, str, int], int] = {}
        self._duration_sum: dict[tuple[str, str], float] = {}
        self._duration_count: dict[tuple[str, str], int] = {}

    def record(
        self, method: str, path: str, status_code: int, duration_seconds: float
    ) -> None:
        with self._lock:
            count_key = (method, path, status_code)
            self._counts[count_key] = self._counts.get(count_key, 0) + 1
            duration_key = (method, path)
            self._duration_sum[duration_key] = (
                self._duration_sum.get(duration_key, 0.0) + duration_seconds
            )
            self._duration_count[duration_key] = (
                self._duration_count.get(duration_key, 0) + 1
            )

    def render_prometheus_text(self) -> str:
        lines = [
            "# HELP trailmind_http_requests_total Total HTTP requests processed",
            "# TYPE trailmind_http_requests_total counter",
        ]
        with self._lock:
            for (method, path, status_code), count in sorted(self._counts.items()):
                lines.append(
                    f'trailmind_http_requests_total{{method="{method}",'
                    f'path="{path}",status="{status_code}"}} {count}'
                )
            lines.append(
                "# HELP trailmind_http_request_duration_seconds_sum Total time "
                "spent handling requests, in seconds"
            )
            lines.append("# TYPE trailmind_http_request_duration_seconds_sum counter")
            for (method, path), total in sorted(self._duration_sum.items()):
                lines.append(
                    "trailmind_http_request_duration_seconds_sum"
                    f'{{method="{method}",path="{path}"}} {total}'
                )
            lines.append(
                "# HELP trailmind_http_request_duration_seconds_count Count of "
                "requests observed for duration"
            )
            lines.append("# TYPE trailmind_http_request_duration_seconds_count counter")
            for (method, path), count in sorted(self._duration_count.items()):
                lines.append(
                    "trailmind_http_request_duration_seconds_count"
                    f'{{method="{method}",path="{path}"}} {count}'
                )
        return "\n".join(lines) + "\n"
