"""OpenTelemetry tracing + per-request latency timings.

Greenfield instrumentation: this project had no tracing infra before (no
OTel/Prometheus dependency). ``setup_tracing()`` wires up a TracerProvider and
FastAPI auto-instrumentation; spans are exported via OTLP only when
``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, so this is a safe no-export default in
environments without a collector (Tempo/Jaeger/etc.) — spans are still
created (near-zero overhead), just dropped instead of shipped.

``stage()`` is the single helper query.py/retriever_service.py/
hybrid_search_service.py use to instrument a pipeline step: it opens an OTel
child span AND preserves the existing ``logger.info("STAGE %s: %.2fs ...")``
log line AND records the elapsed seconds into a per-request timings dict
(surfaced in QueryResponse.timings) — one call site, three outputs.
"""
import contextvars
import logging
import time
from contextlib import contextmanager
from typing import Optional

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import settings

logger = logging.getLogger(__name__)

tracer = trace.get_tracer("multi_store.rag")

# Per-request timings, keyed by stage name -> elapsed seconds. Populated by
# stage() and read via get_timings() to build QueryResponse.timings. A
# ContextVar (not a global dict) so concurrent requests never bleed into each
# other's timings, and so it survives crossing asyncio.to_thread() boundaries
# (contextvars propagate automatically into to_thread's copied context).
_current_timings: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "multi_store_current_timings", default=None
)


def setup_tracing(app) -> None:
    """Initialize the TracerProvider and instrument the FastAPI app. No-op
    entirely when OTEL_ENABLED=False. Exporting is additionally gated on
    OTEL_EXPORTER_OTLP_ENDPOINT being set — spans are always created (so
    stage() timings always work) but are only shipped to a collector when an
    endpoint is configured."""
    if not settings.OTEL_ENABLED:
        logger.info("OpenTelemetry tracing disabled (OTEL_ENABLED=False)")
        return

    resource = Resource.create({"service.name": settings.OTEL_SERVICE_NAME})
    provider = TracerProvider(resource=resource)

    if settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info("OpenTelemetry OTLP export enabled -> %s", settings.OTEL_EXPORTER_OTLP_ENDPOINT)
        except Exception as exc:
            logger.warning("OTLP exporter setup failed (spans will be created but not exported): %s", exc)
    else:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT unset — spans created but not exported")

    trace.set_tracer_provider(provider)

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:
        logger.warning("FastAPI auto-instrumentation failed (non-fatal): %s", exc)


def reset_timings() -> None:
    """Call once at the start of each request to start a fresh timings dict."""
    _current_timings.set({})


def get_timings() -> dict:
    """Return the current request's accumulated {stage_name: seconds} dict."""
    return dict(_current_timings.get() or {})


@contextmanager
def stage(name: str, **attrs):
    """Instrument one pipeline stage: OTel span + STAGE log line + timings entry.

    Usage: ``with tracing.stage("retrieve", chunks=len(retrieved)):`` — same
    logging behavior as the manual `_t = time.time(); ...; logger.info(...)`
    pattern this replaces, plus a nested span and a `timings[name]` entry.
    """
    t0 = time.time()
    with tracer.start_as_current_span(name) as span:
        for k, v in attrs.items():
            try:
                span.set_attribute(k, v)
            except Exception:
                pass
        try:
            yield span
        finally:
            elapsed = time.time() - t0
            span.set_attribute("duration_seconds", elapsed)
            logger.info("STAGE %s: %.2fs", name, elapsed)
            timings = _current_timings.get()
            if timings is not None:
                timings[name] = round(elapsed, 4)
