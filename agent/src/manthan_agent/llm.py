"""Gemini (AI Studio) client helpers.

The ADK Investigator agent talks to Gemini through its `model=` string —
ADK owns those calls (function-calling, streaming, retries) internally.
This module is the thin google-genai helper for the NON-agent LLM calls
that still need raw one-shot generation: the event prettifier and the
cross-case chat summariser.

AI Studio, not Vertex: we authenticate with GOOGLE_API_KEY against
generativelanguage.googleapis.com. The SDK selects AI Studio when
GOOGLE_GENAI_USE_VERTEXAI is unset or FALSE.
"""

from __future__ import annotations

from google import genai
from google.genai import types

from .config import Config


class LLMNotConfigured(RuntimeError):
    """Raised when GOOGLE_API_KEY is missing."""


def client(cfg: Config) -> genai.Client:
    """Return a google-genai client bound to AI Studio."""
    if not cfg.google_api_key:
        raise LLMNotConfigured(
            "GOOGLE_API_KEY is not set. Add your AI Studio key to agent/.env "
            "(get one at https://aistudio.google.com/apikey) and re-run."
        )
    return genai.Client(
        api_key=cfg.google_api_key,
        vertexai=cfg.gemini_use_vertexai,  # False -> AI Studio
    )


def _config(
    *, system: str | None, temperature: float, max_output_tokens: int | None
) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        system_instruction=system,
    )


def generate_text(
    cfg: Config,
    *,
    user: str,
    system: str | None = None,
    model: str | None = None,
    temperature: float = 0.0,
    max_output_tokens: int | None = None,
) -> str:
    """One-shot text generation (sync). Returns the model's text reply."""
    # Bind the client to a local: a temporary genai.Client() can be GC'd
    # mid-request (its __del__ closes the httpx pool), which surfaces as
    # "Cannot send a request, as the client has been closed" on retry.
    c = client(cfg)
    resp = c.models.generate_content(
        model=model or cfg.model,
        contents=user,
        config=_config(
            system=system, temperature=temperature, max_output_tokens=max_output_tokens
        ),
    )
    return (resp.text or "").strip()


async def agenerate_text(
    cfg: Config,
    *,
    user: str,
    system: str | None = None,
    model: str | None = None,
    temperature: float = 0.0,
    max_output_tokens: int | None = None,
) -> str:
    """One-shot text generation (async)."""
    c = client(cfg)  # bind to a local — see generate_text note on GC/retry.
    resp = await c.aio.models.generate_content(
        model=model or cfg.model,
        contents=user,
        config=_config(
            system=system, temperature=temperature, max_output_tokens=max_output_tokens
        ),
    )
    return (resp.text or "").strip()
