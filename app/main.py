"""
FastAPI wiring. Each pipeline stage is one endpoint so stages can be run (and
re-run — every write is an idempotent upsert) independently:

  POST /pipeline/spec      — parse a docs source into Requirements, upsert
  POST /pipeline/crawl     — crawl the fixed screen list, upsert Screens/Flows
  POST /pipeline/link      — build IMPLEMENTS + BUILT_BY edges, stamp coverage
  POST /pipeline/blast-radius/{pr_number} — the payoff: full report
  GET  /graph/absent-requirements — the absence query
  GET  /health

Intermediate stage outputs are cached to data/ as JSON so the link stage
doesn't force a re-crawl.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.agents import code_mapper, crawler, req_linker, spec_parser
from app.agents.reasoner import generate_report
from app.config import settings
from app.graph.client import GraphClient
from app.graph.queries import absent_requirements
from app.models import BlastRadiusReport, CrawlResult, SpecParseResult

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

DATA_DIR = Path("data")
SPEC_CACHE = DATA_DIR / "spec_result.json"
CRAWL_CACHE = DATA_DIR / "crawl_result.json"

app = FastAPI(title="knowledge-graph-agent",
              description="Blast-radius reasoning over a Requirements↔UI↔Code graph")


def _client() -> GraphClient:
    client = GraphClient()
    client.init_schema()
    return client


@app.get("/health")
def health() -> dict:
    with _client() as client:
        counts = client.query(
            "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS n ORDER BY label")
    return {"status": "ok", "graph": {r["label"]: r["n"] for r in counts}}


class SpecRequest(BaseModel):
    source: str  # URL or local file path of the product doc


@app.post("/pipeline/spec", response_model=SpecParseResult)
def run_spec(body: SpecRequest) -> SpecParseResult:
    result = spec_parser.parse_spec(body.source)
    with _client() as client:
        client.upsert_requirements(result.requirements)
    DATA_DIR.mkdir(exist_ok=True)
    SPEC_CACHE.write_text(result.model_dump_json(indent=2))
    return result


@app.post("/pipeline/crawl", response_model=CrawlResult)
async def run_crawl(username: str | None = None) -> CrawlResult:
    result = await crawler.crawl(username=username)
    if not result.screens:
        raise HTTPException(502, "crawl produced no screens — see server logs")
    with _client() as client:
        client.upsert_screens(result.screens)
        client.upsert_flows(result.flows)
    DATA_DIR.mkdir(exist_ok=True)
    CRAWL_CACHE.write_text(result.model_dump_json(indent=2))
    return result


@app.post("/pipeline/link")
def run_link() -> dict:
    """Graph Builder: IMPLEMENTS (LLM linking) + BUILT_BY (code mapping) edges,
    then stamp coverage_status. Needs /pipeline/spec and /pipeline/crawl to
    have run (uses their cached outputs)."""
    if not SPEC_CACHE.exists() or not CRAWL_CACHE.exists():
        raise HTTPException(409, "run /pipeline/spec and /pipeline/crawl first")
    spec = SpecParseResult.model_validate_json(SPEC_CACHE.read_text())
    crawl = CrawlResult.model_validate_json(CRAWL_CACHE.read_text())

    implements = req_linker.link_requirements(crawl.screens, spec.requirements)
    elements = [el for s in crawl.screens for el in s.elements]
    try:
        code_elements, built_by = code_mapper.map_elements(elements)
    except FileNotFoundError as e:
        raise HTTPException(409, str(e))

    with _client() as client:
        client.upsert_implements_edges(implements)
        client.upsert_code_elements(code_elements)
        client.upsert_built_by_edges(built_by)
        coverage = client.stamp_coverage_status()

    return {
        "implements_edges": len(implements),
        "code_elements": len(code_elements),
        "built_by_edges": len(built_by),
        "coverage": coverage,
    }


@app.post("/pipeline/blast-radius/{pr_number}", response_model=BlastRadiusReport)
def run_blast_radius(pr_number: int) -> BlastRadiusReport:
    with _client() as client:
        report = generate_report(client, pr_number)
    out = DATA_DIR / f"blast_radius_pr_{pr_number}.json"
    DATA_DIR.mkdir(exist_ok=True)
    out.write_text(report.model_dump_json(indent=2))
    return report


@app.get("/graph/absent-requirements")
def get_absent_requirements() -> list[dict]:
    with _client() as client:
        return absent_requirements(client)
