"""
Domain models. These are the typed contracts between pipeline stages —
each stage takes one of these in, and produces one of these (or a list
of them) out. Keeping them here means the Graph Builder is the *only*
place that knows about Cypher; every other stage just deals with these
objects.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class CoverageStatus(str, Enum):
    COVERED = "covered"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"


# ---------------------------------------------------------------------------
# Stage 1 output: Spec Parser
# ---------------------------------------------------------------------------

class Requirement(BaseModel):
    id: str  # deterministic slug, e.g. "req-booking-timezone-select"
    text: str
    feature_area: str
    testable: bool
    source_doc: str
    # Filled in later by the Graph Builder after the crawl is ingested —
    # not set by the Spec Parser itself.
    coverage_status: Optional[CoverageStatus] = None


class SpecParseResult(BaseModel):
    requirements: list[Requirement]
    source_doc: str


# ---------------------------------------------------------------------------
# Stage 2 output: Crawler
# ---------------------------------------------------------------------------

class UIElement(BaseModel):
    id: str
    screen_id: str
    selector: str
    element_type: str  # button | input | link | select | text | ...
    label: str  # LLM-assigned semantic purpose, e.g. "submit booking"
    raw_text: Optional[str] = None


class Screen(BaseModel):
    id: str
    url: str
    name: str
    elements: list[UIElement] = Field(default_factory=list)


class Flow(BaseModel):
    id: str
    name: str
    screen_ids: list[str]  # ordered


class CrawlResult(BaseModel):
    screens: list[Screen]
    flows: list[Flow]


# ---------------------------------------------------------------------------
# Confidence-carrying edges (Requirement<->UIElement, UIElement<->CodeElement)
# ---------------------------------------------------------------------------

class ScoredEdge(BaseModel):
    source_id: str
    target_id: str
    confidence: float
    needs_review: bool = False
    rationale: Optional[str] = None

    def finalize(self, threshold: float) -> "ScoredEdge":
        self.needs_review = self.confidence < threshold
        return self


class ImplementsEdge(ScoredEdge):
    """UIElement -[:IMPLEMENTS]-> Requirement"""
    kind: Literal["implements"] = "implements"


class BuiltByEdge(ScoredEdge):
    """UIElement -[:BUILT_BY]-> CodeElement"""
    kind: Literal["built_by"] = "built_by"


# ---------------------------------------------------------------------------
# Stage 3 output: Code Mapper
# ---------------------------------------------------------------------------

class CodeElement(BaseModel):
    id: str
    file_path: str
    symbol_name: Optional[str] = None
    repo_ref: str  # e.g. "calcom/cal.com@main"


# ---------------------------------------------------------------------------
# Stage 4 input: PR / diff
# ---------------------------------------------------------------------------

class PullRequest(BaseModel):
    id: str
    number: int
    title: str
    url: str
    changed_files: list[str]


# ---------------------------------------------------------------------------
# Stage 4 output: blast radius report
# ---------------------------------------------------------------------------

class BlastRadiusItem(BaseModel):
    ui_element_label: Optional[str] = None
    screen_name: Optional[str] = None
    flow_name: Optional[str] = None
    requirement_text: Optional[str] = None
    confidence: float
    needs_review: bool


class BlastRadiusReport(BaseModel):
    pr: PullRequest
    affected: list[BlastRadiusItem]
    needs_review: list[BlastRadiusItem]
    summary: str  # LLM-generated, non-engineer-readable prose
