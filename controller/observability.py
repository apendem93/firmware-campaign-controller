import json
import logging
import time

import httpx
from prometheus_client import Counter, Gauge, Histogram

INCOMING = Counter(
    "controller_http_requests_total", "Incoming HTTP requests", ["method", "endpoint", "status"]
)
LATENCY = Histogram(
    "controller_http_request_seconds", "Incoming request duration", ["method", "endpoint"]
)
OUTGOING = Counter(
    "controller_outgoing_requests_total",
    "Simulator HTTP requests",
    ["endpoint", "status", "outcome"],
)
OUT_LATENCY = Histogram(
    "controller_outgoing_request_seconds", "Simulator request duration", ["endpoint"]
)
CALLBACKS = Counter("controller_callback_observations_total", "Callback classification", ["kind"])
HEARTBEAT = Gauge(
    "controller_scheduler_last_success_timestamp_seconds", "Last successful scheduler iteration"
)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        data.update(getattr(record, "fields", {}))
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def outbound(client: httpx.AsyncClient, method: str, url: str, endpoint: str, **kwargs):
    started = time.monotonic()
    try:
        response = await client.request(method, url, **kwargs)
        OUTGOING.labels(
            endpoint, str(response.status_code), "success" if response.is_success else "error"
        ).inc()
        return response
    except httpx.HTTPError:
        OUTGOING.labels(endpoint, "none", "transport_error").inc()
        raise
    finally:
        OUT_LATENCY.labels(endpoint).observe(time.monotonic() - started)
