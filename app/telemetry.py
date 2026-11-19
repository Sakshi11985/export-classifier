"""OpenTelemetry tracing (D5). Exports spans over OTLP/HTTP to Jaeger. When OTEL_ENABLED is false the
helpers are no-ops, so nothing else in the code base needs to care."""
from __future__ import annotations

import contextlib
from typing import Iterator

from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

from .config import Settings

_configured = False


def setup_tracing(s: Settings, service: str, exporter=None) -> None:
    """exporter is injectable so tests can capture spans in memory."""
    global _configured
    if _configured:
        return
    if not (s.otel_enabled or exporter):
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=s.otel_endpoint.rstrip("/") + "/v1/traces")))
    trace.set_tracer_provider(provider)
    _configured = True


def tracer():
    return trace.get_tracer("export-classifier")


@contextlib.contextmanager
def span(name: str, **attrs) -> Iterator:
    with tracer().start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
        yield sp


def current_traceparent() -> str:
    carrier: dict = {}
    inject(carrier)
    return carrier.get("traceparent", "")


def context_from_traceparent(tp: str | None):
    return extract({"traceparent": tp}) if tp else None
