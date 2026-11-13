"""OpenTelemetry setup, shared by the harness and the MCP server.

Spans always go to results/traces.jsonl (one JSON object per span), which the dashboard reads.
If OTEL_EXPORTER_OTLP_ENDPOINT is set and the OTLP exporter is installed, the same spans also go
to that collector (Grafana Tempo, Jaeger, Honeycomb, Langfuse, Datadog...), with no code change.

The trace crosses the process boundary inside each MCP request: the harness injects the W3C
`traceparent` into the request's `_meta`, and the server continues the trace from it.
"""
import json
import os

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from .audit import file_lock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACES = os.path.join(ROOT, "results", "traces.jsonl")


class JsonlExporter(SpanExporter):
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def export(self, spans):
        lines = []
        for s in spans:
            lines.append(json.dumps({
                "trace_id": format(s.context.trace_id, "032x"), "span_id": format(s.context.span_id, "016x"),
                "parent_id": format(s.parent.span_id, "016x") if s.parent else None,
                "name": s.name, "service": s.resource.attributes.get("service.name"),
                "start_ns": s.start_time, "end_ns": s.end_time, "status": s.status.status_code.name,
                "attributes": {k: (list(v) if isinstance(v, tuple) else v) for k, v in (s.attributes or {}).items()},
            }, ensure_ascii=False))
        with file_lock(self.path):
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


_configured = False


def setup(service):
    global _configured
    if not _configured:
        provider = TracerProvider(resource=Resource.create({"service.name": service}))
        provider.add_span_processor(SimpleSpanProcessor(JsonlExporter(TRACES)))
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                from opentelemetry.sdk.trace.export import BatchSpanProcessor
                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            except ImportError:
                pass
        trace.set_tracer_provider(provider)
        _configured = True
    return trace.get_tracer(service)


def inject():
    """W3C trace context for the current span, to put into an MCP request's _meta."""
    carrier = {}
    propagate.inject(carrier)
    return carrier


def extract(carrier):
    return propagate.extract(carrier or {})
