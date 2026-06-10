"""Tests for the optional tracing helpers — pure logic, no exporters, no GCP.

setup_tracing must be a silent no-op when nothing is configured, and
current_trace_ids must be safe to call from anywhere (it stamps every Event).
"""

from __future__ import annotations

import importlib.util

import pytest

from manthan_agent.tracing import current_trace_ids, setup_tracing


def test_current_trace_ids_none_without_provider() -> None:
    # No tracer provider / no active span -> both ids are None.
    assert current_trace_ids() == (None, None)


def test_setup_tracing_false_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert setup_tracing("manthan-test") is False


@pytest.mark.skipif(
    importlib.util.find_spec("opentelemetry.exporter") is not None,
    reason="a real OTel exporter is installed; setup would mutate global state",
)
def test_setup_tracing_missing_exporter_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Env asks for Cloud Trace but the gcp extra isn't installed -> no-op False.
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-project")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert setup_tracing("manthan-test") is False


def test_current_trace_ids_hex_inside_a_real_span() -> None:
    # The OTel SDK ships with google-adk; a local (non-global) provider gives
    # us a real span context without installing any exporter.
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("manthan-test")
    with tracer.start_as_current_span("probe"):
        trace_id, span_id = current_trace_ids()

    assert trace_id is not None and span_id is not None
    assert len(trace_id) == 32 and len(span_id) == 16
    int(trace_id, 16)   # valid hex
    int(span_id, 16)
    # context restored once the span exits
    assert current_trace_ids() == (None, None)
