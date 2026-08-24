"""
Graph Builder helper — Requirement <-> UIElement linking (IMPLEMENTS edges).

Not a numbered pipeline stage of its own: it runs inside the graph-build step,
after the spec parse and crawl, because it needs both sides. One LLM call per
screen matches that screen's labeled elements against the requirement list;
every produced edge carries confidence and gets needs_review stamped. A
requirement matched by nothing simply ends up with no incoming :IMPLEMENTS
edge — which is exactly the absence signal the graph is designed around.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel

from app.agents.llm import complete_validated
from app.config import settings
from app.models import ImplementsEdge, Requirement, Screen

log = logging.getLogger(__name__)

SYSTEM = """You link UI elements of the Cal.com web app to product
requirements for a QA traceability graph. Given one screen's elements and the
full requirement list, return matches ONLY where the element plausibly
implements (fully or partially) the requirement.

Rules:
- Use the given element ids and requirement ids verbatim.
- confidence: 0.9+ only for direct, obvious implementations; 0.5-0.8 for
  partial or probable; below 0.5 for speculative. Do NOT force matches —
  unmatched requirements are expected and meaningful.
- rationale: one short sentence.
"""


class _Match(BaseModel):
    element_id: str
    requirement_id: str
    confidence: float
    rationale: str


class _MatchBatch(BaseModel):
    matches: list[_Match]


def link_requirements(screens: list[Screen],
                      requirements: list[Requirement]) -> list[ImplementsEdge]:
    threshold = settings.confidence_threshold
    req_payload = [
        {"id": r.id, "text": r.text, "feature_area": r.feature_area}
        for r in requirements if r.testable
    ]
    req_ids = {r["id"] for r in req_payload}
    edges: list[ImplementsEdge] = []

    for screen in screens:
        if not screen.elements:
            continue
        el_payload = [
            {"id": el.id, "label": el.label, "type": el.element_type,
             "text": el.raw_text}
            for el in screen.elements
        ]
        el_ids = {el.id for el in screen.elements}
        try:
            batch = complete_validated(
                SYSTEM,
                f"Screen: {screen.name} ({screen.url})\n\n"
                f"Elements:\n{json.dumps(el_payload, indent=2)}\n\n"
                f"Requirements:\n{json.dumps(req_payload, indent=2)}",
                _MatchBatch,
            )
        except Exception:
            log.exception("linking failed for %s — skipping screen", screen.id)
            continue
        for m in batch.matches:
            if m.element_id not in el_ids or m.requirement_id not in req_ids:
                log.warning("dropping hallucinated match %s -> %s",
                            m.element_id, m.requirement_id)
                continue
            edges.append(ImplementsEdge(
                source_id=m.element_id, target_id=m.requirement_id,
                confidence=m.confidence, rationale=m.rationale,
            ).finalize(threshold))
    return edges
