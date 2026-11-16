"""Small helpers shared by the services: logging, the runtime header, /health.

On Cloud Run every log line is one JSON object on stdout. Cloud Logging reads
the fields: "severity" (so you can filter for errors), "message", and the trace
of the request that wrote it (so all lines of one request group together in
Logs Explorer). Locally the logs stay plain text.

Business events (a signup, a promotion, a check-in) are logged with
event(...): their fields become jsonPayload fields, which log-based metrics in
infra/monitoring.tf count. The trace id also travels inside Pub/Sub messages,
so one signup can be followed from web to ticket-worker.
"""

import contextvars
import json
import logging
import os

from fastapi import FastAPI, Request

from .config import get_settings

_trace: contextvars.ContextVar = contextvars.ContextVar("trace", default="")


class CloudLoggingFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "service": self.service,
        }
        if record.exc_info:
            entry["message"] += "\n" + self.formatException(record.exc_info)
        entry.update(getattr(record, "fields", {}))
        trace = _trace.get()
        if trace:
            entry["logging.googleapis.com/trace"] = trace
        return json.dumps(entry, ensure_ascii=False, default=str)


def event(logger: logging.Logger, name: str, message: str, **fields) -> None:
    """Log a business event: {"event": name, ...fields} becomes countable in Cloud Monitoring."""
    logger.info(message, extra={"fields": {"event": name, **fields}})


def current_trace() -> str:
    return _trace.get()


def use_trace(trace: str):
    """Continue a trace that arrived in a message (returns a token for reset_trace)."""
    return _trace.set(trace or _trace.get())


def reset_trace(token) -> None:
    _trace.reset(token)


def configure_logging(service: str) -> None:
    if get_settings().on_cloud_run:
        handler = logging.StreamHandler()
        handler.setFormatter(CloudLoggingFormatter(service))
        logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
        logging.getLogger("uvicorn.access").disabled = True  # Cloud Run already logs every request
    else:
        logging.basicConfig(
            level=logging.INFO, format=f"%(asctime)s [{service}] %(name)s: %(message)s", datefmt="%H:%M:%S"
        )


def setup(app: FastAPI, service: str, secrets: tuple = ()) -> None:
    # Fail at startup, not on the first ticket: Cloud Run then keeps serving the previous revision
    missing = [name for name in secrets if not os.environ.get(name, "").strip()]
    if os.environ.get("BACKEND") == "gcp" and missing:
        raise RuntimeError(f"{service} cannot start without {', '.join(missing)}")
    configure_logging(service)

    @app.middleware("http")
    async def served_by(request: Request, call_next):
        # Cloud Run sends "TRACE_ID/SPAN_ID;o=1"; Cloud Logging wants the full trace name
        header = request.headers.get("x-cloud-trace-context", "")
        project = os.environ.get("GCP_PROJECT", "")
        token = _trace.set(f"projects/{project}/traces/{header.split('/')[0]}" if header and project else "")
        try:
            response = await call_next(request)
        finally:
            _trace.reset(token)
        # Which copy answered? Under load you will see many instance ids here.
        settings = get_settings()
        response.headers["X-Served-By"] = f"{settings.service_name}/{settings.revision}/{settings.instance_id}"
        # Basic browser protections: no framing (clickjacking), no type guessing, no full URL to other sites
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    # Not /healthz: Cloud Run's front end reserves paths ending in "z" and answers 404 itself
    @app.get("/health")
    def health():
        return {"ok": True, "service": service}
