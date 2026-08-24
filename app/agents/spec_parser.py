"""
Stage 1 — Spec Parser.

Takes a public product doc (a docs URL or a local markdown/text file) for the
target app and extracts structured, *testable* Requirement objects via the
LLM, with Pydantic validation on the output (see agents/llm.py).

Requirement ids are deterministic slugs derived from the requirement text, so
re-parsing the same doc upserts rather than duplicates in the graph.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx

from app.agents.llm import complete_validated
from app.models import Requirement, SpecParseResult

SYSTEM = """You extract product requirements from documentation for a QA
knowledge graph. Given a docs page for a scheduling app (Cal.com), produce a
list of atomic, testable requirements.

Rules:
- Each requirement is ONE observable behavior a tester could verify in the UI
  ("User can select a timezone on the booking page"), not a marketing claim.
- Set testable=false for statements that are real requirements but cannot be
  verified from the UI alone (performance, compliance, backend-only).
- feature_area is a short lowercase slug like "booking", "event-types",
  "availability", "auth".
- Do NOT invent requirements not grounded in the provided text.
- Leave `id` as an empty string and `coverage_status` as null — they are
  assigned by the pipeline, not by you.
"""


class _LLMSpecOutput(SpecParseResult):
    """Same shape as SpecParseResult; separate class only so schema tweaks for
    the LLM never leak into the pipeline contract."""


def _slugify(text: str, max_words: int = 6) -> str:
    words = re.sub(r"[^a-z0-9\s-]", "", text.lower()).split()
    return "req-" + "-".join(words[:max_words])


def _load_source(source: str) -> str:
    if source.startswith("http://") or source.startswith("https://"):
        resp = httpx.get(source, follow_redirects=True, timeout=30)
        resp.raise_for_status()
        return resp.text
    return Path(source).read_text()


def parse_spec(source: str) -> SpecParseResult:
    """`source` is a URL or local file path to the product doc."""
    doc_text = _load_source(source)
    # Crude HTML -> text so we don't burn tokens on markup; docs pages are
    # mostly prose so this is good enough for extraction.
    doc_text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", doc_text,
                      flags=re.DOTALL)
    doc_text = re.sub(r"<[^>]+>", " ", doc_text)
    doc_text = re.sub(r"\s+", " ", doc_text)[:60_000]

    result = complete_validated(
        SYSTEM,
        f"Source document ({source}):\n\n{doc_text}",
        _LLMSpecOutput,
    )

    requirements: list[Requirement] = []
    seen_ids: set[str] = set()
    for req in result.requirements:
        rid = _slugify(req.text)
        # Disambiguate collisions deterministically.
        base, n = rid, 2
        while rid in seen_ids:
            rid, n = f"{base}-{n}", n + 1
        seen_ids.add(rid)
        requirements.append(Requirement(
            id=rid,
            text=req.text,
            feature_area=req.feature_area,
            testable=req.testable,
            source_doc=source,
        ))
    return SpecParseResult(requirements=requirements, source_doc=source)
