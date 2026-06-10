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


# ──────────────────────────────────────────────────────────────────────
# OpenAI-compat chat shim
#
# The chat_loop worker (operator follow-ups) was written against the old
# OpenRouter client's OpenAI shape: chat(cfg, messages, tools=...) returning
# response.choices[0].message with .content and .tool_calls. Rather than
# rewrite that loop, this shim translates OpenAI-shape messages/tools to
# google-genai and wraps the reply back in the same shape. The converters
# are pure functions so they're unit-testable without a network call.
# ──────────────────────────────────────────────────────────────────────

import json as _json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ShimFunction:
    name: str
    arguments: str  # JSON string, OpenAI-style


@dataclass
class ShimToolCall:
    id: str
    function: ShimFunction
    type: str = "function"


@dataclass
class ShimMessage:
    content: str | None = None
    tool_calls: list[ShimToolCall] | None = None


@dataclass
class ShimChoice:
    message: ShimMessage
    finish_reason: str = "stop"


@dataclass
class ShimUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class ShimResponse:
    choices: list[ShimChoice] = field(default_factory=list)
    usage: ShimUsage | None = None


def _strip_unsupported_schema(schema: Any) -> Any:
    """Remove OpenAI-strict-mode keys Gemini's declaration parser rejects."""
    if isinstance(schema, dict):
        return {
            k: _strip_unsupported_schema(v)
            for k, v in schema.items()
            if k not in ("additionalProperties", "strict", "$schema")
        }
    if isinstance(schema, list):
        return [_strip_unsupported_schema(v) for v in schema]
    return schema


def openai_tools_to_genai(tools: list[dict[str, Any]] | None) -> list[types.Tool] | None:
    """OpenAI function-tool definitions -> genai Tool with declarations."""
    if not tools:
        return None
    decls: list[types.FunctionDeclaration] = []
    for t in tools:
        fn = t.get("function", t)  # tolerate both wrapped and bare shapes
        name = fn.get("name")
        if not name:
            continue
        decls.append(
            types.FunctionDeclaration(
                name=name,
                description=fn.get("description", ""),
                parameters=_strip_unsupported_schema(fn.get("parameters")) or None,
            )
        )
    return [types.Tool(function_declarations=decls)] if decls else None


def openai_messages_to_genai(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[types.Content]]:
    """OpenAI chat messages -> (system_instruction, genai Content list).

    Handles the four roles chat_loop emits: system, user, assistant (text
    and/or tool_calls), and tool (function results, matched back to the
    originating call's function name via the id map).
    """
    system_parts: list[str] = []
    contents: list[types.Content] = []
    call_id_to_name: dict[str, str] = {}

    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_parts.append(str(m["content"]))
        elif role == "user":
            contents.append(
                types.Content(role="user", parts=[types.Part(text=str(m.get("content") or ""))])
            )
        elif role == "assistant":
            parts: list[types.Part] = []
            if m.get("content"):
                parts.append(types.Part(text=str(m["content"])))
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                name = fn.get("name") or "_unknown"
                call_id_to_name[tc.get("id", "")] = name
                try:
                    args = _json.loads(fn.get("arguments") or "{}")
                except (_json.JSONDecodeError, TypeError):
                    args = {}
                parts.append(
                    types.Part(function_call=types.FunctionCall(name=name, args=args))
                )
            if parts:
                contents.append(types.Content(role="model", parts=parts))
        elif role == "tool":
            name = call_id_to_name.get(m.get("tool_call_id", ""), "_unknown")
            raw = m.get("content") or "{}"
            try:
                payload = _json.loads(raw) if isinstance(raw, str) else raw
            except (_json.JSONDecodeError, TypeError):
                payload = {"text": str(raw)}
            if not isinstance(payload, dict):
                payload = {"result": payload}
            contents.append(
                types.Content(
                    role="user",
                    parts=[types.Part(
                        function_response=types.FunctionResponse(name=name, response=payload)
                    )],
                )
            )
    return ("\n\n".join(system_parts) or None), contents


def genai_response_to_shim(resp: Any) -> ShimResponse:
    """Wrap a genai GenerateContentResponse in the OpenAI shim shape."""
    content_text: list[str] = []
    tool_calls: list[ShimToolCall] = []
    candidate = (getattr(resp, "candidates", None) or [None])[0]
    parts = getattr(getattr(candidate, "content", None), "parts", None) or []
    for i, part in enumerate(parts):
        fc = getattr(part, "function_call", None)
        if fc is not None:
            tool_calls.append(ShimToolCall(
                id=getattr(fc, "id", None) or f"call_{i}",
                function=ShimFunction(
                    name=fc.name or "_unknown",
                    arguments=_json.dumps(dict(fc.args or {})),
                ),
            ))
        elif getattr(part, "text", None):
            content_text.append(part.text)

    usage_md = getattr(resp, "usage_metadata", None)
    usage = ShimUsage(
        prompt_tokens=getattr(usage_md, "prompt_token_count", 0) or 0,
        completion_tokens=getattr(usage_md, "candidates_token_count", 0) or 0,
    ) if usage_md is not None else None

    return ShimResponse(
        choices=[ShimChoice(message=ShimMessage(
            content="".join(content_text) or None,
            tool_calls=tool_calls or None,
        ))],
        usage=usage,
    )


def chat(
    cfg: Config,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    model: str | None = None,
) -> ShimResponse:
    """OpenAI-shape chat completion over Gemini (AI Studio).

    Drop-in replacement for the old OpenRouter chat(): same signature, same
    response access pattern (response.choices[0].message.{content,tool_calls},
    tool_calls[i].function.{name,arguments-json}).
    """
    system, contents = openai_messages_to_genai(messages)
    cfg_obj = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_tokens,
        system_instruction=system,
        tools=openai_tools_to_genai(tools),
    )
    c = client(cfg)  # bind to a local — see generate_text note on GC/retry.
    resp = c.models.generate_content(
        model=model or cfg.model_subagent,
        contents=contents,
        config=cfg_obj,
    )
    return genai_response_to_shim(resp)
