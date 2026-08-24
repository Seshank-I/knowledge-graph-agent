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
from typing import Optional

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


def _scope(requirements: list[Requirement],
           max_requirements: Optional[int],
           feature_areas: Optional[list[str]]) -> list[Requirement]:
    """Deterministic post-extraction scoping for tight live runs: keep only
    the named feature areas (if any), then cap the count preserving the
    doc's order. Testable requirements survive a cap first — they are the
    ones the graph's coverage story is about."""
    areas = {a.strip().lower() for a in (feature_areas or []) if a.strip()}
    if areas:
        requirements = [r for r in requirements if r.feature_area.lower() in areas]
    if max_requirements and len(requirements) > max_requirements:
        ranked = sorted(range(len(requirements)),
                        key=lambda i: (not requirements[i].testable, i))
        keep = set(ranked[:max_requirements])
        requirements = [r for i, r in enumerate(requirements) if i in keep]
    return requirements


def parse_spec(source: str,
               max_requirements: Optional[int] = None,
               feature_areas: Optional[list[str]] = None) -> SpecParseResult:
    """`source` is a URL or local file path to the product doc.
    `max_requirements` / `feature_areas` default to settings
    (spec_max_requirements / spec_feature_areas)."""
    from app.config import settings
    if max_requirements is None:
        max_requirements = settings.spec_max_requirements or None
    if feature_areas is None and settings.spec_feature_areas:
        feature_areas = settings.spec_feature_areas.split(",")
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

    scoped = _scope(list(result.requirements), max_requirements, feature_areas)

    requirements: list[Requirement] = []
    seen_ids: set[str] = set()
    for req in scoped:
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
