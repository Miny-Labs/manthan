"""Optional OpenTelemetry tracing — Cloud Trace / OTLP if available, no-op otherwise.

ADK already creates OpenTelemetry spans around every agent run, model call and
tool call. All this module does is (a) optionally install an exporter so those
spans actually go somewhere, and (b) expose the current trace/span ids so the
event log can stamp them (Event.trace_id / Event.span_id), letting an auditor
jump from a case event straight to the matching trace.

Design rules:
  * NEVER raise — tracing is observability, not control flow. Missing
    packages or bad env config degrade to a silent no-op.
  * Exporter packages are optional extras (pyproject `gcp` extra). The
    opentelemetry api/sdk themselves ship with google-adk.
"""

from __future__ import annotations

import os

_installed = False


def current_trace_ids() -> tuple[str | None, str | None]:
    """Return (trace_id, span_id) of the current span as hex strings.

    (None, None) when there is no active/valid span — e.g. no exporter was
    installed, or we're outside any instrumented call. Safe to call anywhere.
    """
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if not ctx.is_valid:
            return (None, None)
        return (format(ctx.trace_id, "032x"), format(ctx.span_id, "016x"))
    except Exception:  # noqa: BLE001 — observability must never break the run
        return (None, None)


def setup_tracing(service_name: str) -> bool:
    """Install a span exporter if the environment asks for one.

    Priority:
      1. GOOGLE_CLOUD_PROJECT set + opentelemetry-exporter-gcp-trace importable
         -> CloudTraceSpanExporter (Cloud Trace).
      2. OTEL_EXPORTER_OTLP_ENDPOINT set + an OTLP exporter importable
         -> OTLPSpanExporter (any OTLP collector).
      3. Otherwise -> no-op, return False.

    Returns True only when an exporter was actually wired up. Never raises.
    """
    global _installed
    if _installed:
        return True
    try:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if project and _install_gcp(service_name, project):
            _installed = True
            return True
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") and _install_otlp(service_name):
            _installed = True
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _provider(service_name: str):
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    return TracerProvider(resource=Resource.create({"service.name": service_name}))


def _install_gcp(service_name: str, project_id: str) -> bool:
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception:  # noqa: BLE001 — extra not installed
        return False
    try:
        provider = _provider(service_name)
        provider.add_span_processor(
            BatchSpanProcessor(CloudTraceSpanExporter(project_id=project_id))
        )
        trace.set_tracer_provider(provider)
        return True
    except Exception:  # noqa: BLE001
        return False


def _install_otlp(service_name: str) -> bool:
    exporter_cls = None
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as exporter_cls,
        )
    except Exception:  # noqa: BLE001
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter as exporter_cls,
            )
        except Exception:  # noqa: BLE001
            return False
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = _provider(service_name)
        provider.add_span_processor(BatchSpanProcessor(exporter_cls()))
        trace.set_tracer_provider(provider)
        return True
    except Exception:  # noqa: BLE001
        return False
