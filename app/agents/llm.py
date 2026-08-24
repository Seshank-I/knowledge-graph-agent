"""
Thin shared wrapper around the Anthropic client used by every LLM-touching
stage (spec parser, crawler element labeling, code-mapper fallback, reasoner
report). One place for: model choice, JSON-extraction, and Pydantic
validation with a single retry that feeds the validation error back to the
model — that's the whole "structured output" story, deliberately no more.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Type, TypeVar

from anthropic import Anthropic
from pydantic import BaseModel, ValidationError

from app.config import settings

T = TypeVar("T", bound=BaseModel)

_client: Anthropic | None = None


def client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


def _complete_api(system: str, user: str, max_tokens: int) -> str:
    resp = client().messages.create(
        model=settings.llm_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text


def _complete_claude_cli(system: str, user: str) -> str:
    """Route the completion through a locally-authenticated Claude Code CLI
    (`claude -p`). Single turn, prompt over stdin (avoids argv size limits),
    JSON result envelope parsed for the text."""
    proc = subprocess.run(
        ["claude", "-p", "--output-format", "json", "--max-turns", "1",
         "--model", settings.llm_model],
        input=f"{system}\n\n{user}",
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (exit {proc.returncode}): "
            f"stderr={proc.stderr[:400]!r} stdout={proc.stdout[:400]!r}")
    data = json.loads(proc.stdout)
    if data.get("is_error"):
        raise RuntimeError(f"claude CLI returned an error result: {data.get('result', '')[:500]}")
    return data.get("result", "")


_openai_client = None


def _complete_openai_compat(system: str, user: str, max_tokens: int) -> str:
    """Any provider speaking the OpenAI chat-completions protocol, selected
    by base_url: OpenAI itself, Gemini, Mistral, Groq, a local Ollama, or
    Anthropic's compat endpoint. One backend instead of one per vendor."""
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=settings.llm_api_key,
                                base_url=settings.llm_base_url)
    resp = _openai_client.chat.completions.create(
        model=settings.llm_model,
        max_tokens=max_tokens,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
    )
    return resp.choices[0].message.content or ""


def complete(system: str, user: str, max_tokens: int = 4096) -> str:
    if settings.llm_backend == "claude_cli":
        return _complete_claude_cli(system, user)
    if settings.llm_backend == "openai_compat":
        return _complete_openai_compat(system, user, max_tokens)
    return _complete_api(system, user, max_tokens)


def _extract_json(text: str) -> str:
    """Pull the first JSON object/array out of a response that may wrap it
    in prose or a ```json fence."""
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=0)
    return text[start:].strip()


def complete_validated(system: str, user: str, model_cls: Type[T],
                       max_tokens: int = 8192) -> T:
    """
    Ask for JSON matching `model_cls`, validate, and on failure retry ONCE
    with the validation error appended. Two strikes raises — per the
    no-self-healing-loops constraint, failures surface instead of spinning.
    """
    schema = json.dumps(model_cls.model_json_schema(), indent=2)
    system_full = (
        f"{system}\n\nRespond with ONLY a JSON object matching this JSON schema "
        f"(no prose, no markdown fence):\n{schema}"
    )
    raw = complete(system_full, user, max_tokens=max_tokens)
    try:
        return model_cls.model_validate_json(_extract_json(raw))
    except (ValidationError, json.JSONDecodeError) as first_err:
        retry_user = (
            f"{user}\n\nYour previous response failed validation:\n{first_err}\n"
            f"Previous response:\n{raw}\n\nReturn corrected JSON only."
        )
        raw = complete(system_full, retry_user, max_tokens=max_tokens)
        return model_cls.model_validate_json(_extract_json(raw))
