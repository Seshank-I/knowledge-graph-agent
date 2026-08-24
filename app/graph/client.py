"""
Neo4j write layer. This is the *only* module that turns domain models into
Cypher — every pipeline stage hands its output here and never touches the
driver directly.

All writes are idempotent MERGE-based upserts keyed on the node `id`
(unique-constrained in schema.py), so re-running any pipeline stage is safe:
it converges the graph instead of duplicating it.
"""

from __future__ import annotations

from typing import Iterable, Optional

from neo4j import Driver, GraphDatabase

from app.config import settings
from app.graph.schema import init_schema
from app.models import (
    BuiltByEdge,
    CodeElement,
    CoverageStatus,
    Flow,
    ImplementsEdge,
    PullRequest,
    Requirement,
    Screen,
    UIElement,
)


class GraphClient:
    def __init__(self, uri: Optional[str] = None, user: Optional[str] = None,
                 password: Optional[str] = None):
        self._driver: Driver = GraphDatabase.driver(
            uri or settings.neo4j_uri,
            auth=(user or settings.neo4j_user, password or settings.neo4j_password),
        )

    def close(self) -> None:
        self._driver.close()

    def __enter__(self) -> "GraphClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def init_schema(self) -> None:
        init_schema(self._driver)

    def _run(self, query: str, **params):
        with self._driver.session() as session:
            return session.run(query, **params).data()

    # ------------------------------------------------------------------
    # Node upserts
    # ------------------------------------------------------------------

    def upsert_requirements(self, requirements: Iterable[Requirement]) -> None:
        self._run(
            """
            UNWIND $rows AS row
            MERGE (r:Requirement {id: row.id})
            SET r.text = row.text,
                r.feature_area = row.feature_area,
                r.testable = row.testable,
                r.source_doc = row.source_doc,
                r.coverage_status = row.coverage_status
            """,
            rows=[
                {
                    **req.model_dump(exclude={"coverage_status"}),
                    "coverage_status": req.coverage_status.value if req.coverage_status else None,
                }
                for req in requirements
            ],
        )

    def upsert_screens(self, screens: Iterable[Screen]) -> None:
        """Upserts each Screen plus its UIElements and PART_OF edges."""
        screens = list(screens)
        self._run(
            """
            UNWIND $rows AS row
            MERGE (s:Screen {id: row.id})
            SET s.url = row.url, s.name = row.name
            """,
            rows=[s.model_dump(exclude={"elements"}) for s in screens],
        )
        elements = [el for s in screens for el in s.elements]
        if elements:
            self.upsert_ui_elements(elements)

    def upsert_ui_elements(self, elements: Iterable[UIElement]) -> None:
        self._run(
            """
            UNWIND $rows AS row
            MERGE (u:UIElement {id: row.id})
            SET u.selector = row.selector,
                u.element_type = row.element_type,
                u.label = row.label,
                u.raw_text = row.raw_text
            WITH u, row
            MATCH (s:Screen {id: row.screen_id})
            MERGE (u)-[:PART_OF]->(s)
            """,
            rows=[el.model_dump() for el in elements],
        )

    def upsert_flows(self, flows: Iterable[Flow]) -> None:
        """Upserts Flows and ordered STEP_IN edges (order stored on the edge)."""
        self._run(
            """
            UNWIND $rows AS row
            MERGE (f:Flow {id: row.id})
            SET f.name = row.name
            WITH f, row
            UNWIND range(0, size(row.screen_ids) - 1) AS idx
            MATCH (s:Screen {id: row.screen_ids[idx]})
            MERGE (s)-[st:STEP_IN]->(f)
            SET st.order = idx
            """,
            rows=[f.model_dump() for f in flows],
        )

    def upsert_code_elements(self, elements: Iterable[CodeElement]) -> None:
        self._run(
            """
            UNWIND $rows AS row
            MERGE (c:CodeElement {id: row.id})
            SET c.file_path = row.file_path,
                c.symbol_name = row.symbol_name,
                c.repo_ref = row.repo_ref
            """,
            rows=[c.model_dump() for c in elements],
        )

    def upsert_pr(self, pr: PullRequest) -> None:
        self._run(
            """
            MERGE (p:PR {id: $id})
            SET p.number = $number, p.title = $title, p.url = $url,
                p.changed_files = $changed_files
            """,
            **pr.model_dump(),
        )

    # ------------------------------------------------------------------
    # Edge upserts
    # ------------------------------------------------------------------

    def upsert_implements_edges(self, edges: Iterable[ImplementsEdge]) -> None:
        """(:UIElement)-[:IMPLEMENTS {confidence, needs_review, rationale}]->(:Requirement)"""
        self._run(
            """
            UNWIND $rows AS row
            MATCH (u:UIElement {id: row.source_id})
            MATCH (r:Requirement {id: row.target_id})
            MERGE (u)-[e:IMPLEMENTS]->(r)
            SET e.confidence = row.confidence,
                e.needs_review = row.needs_review,
                e.rationale = row.rationale
            """,
            rows=[e.model_dump(exclude={"kind"}) for e in edges],
        )

    def upsert_built_by_edges(self, edges: Iterable[BuiltByEdge]) -> None:
        """(:UIElement)-[:BUILT_BY {confidence, needs_review, rationale}]->(:CodeElement)"""
        self._run(
            """
            UNWIND $rows AS row
            MATCH (u:UIElement {id: row.source_id})
            MATCH (c:CodeElement {id: row.target_id})
            MERGE (u)-[e:BUILT_BY]->(c)
            SET e.confidence = row.confidence,
                e.needs_review = row.needs_review,
                e.rationale = row.rationale
            """,
            rows=[e.model_dump(exclude={"kind"}) for e in edges],
        )

    def upsert_expected_on_edges(self, pairs: Iterable[tuple[str, str]]) -> None:
        """(:Requirement {id: req_id})-[:EXPECTED_ON]->(:Screen {id: screen_id})"""
        self._run(
            """
            UNWIND $rows AS row
            MATCH (r:Requirement {id: row.req_id})
            MATCH (s:Screen {id: row.screen_id})
            MERGE (r)-[:EXPECTED_ON]->(s)
            """,
            rows=[{"req_id": r, "screen_id": s} for r, s in pairs],
        )

    def upsert_changes_edges(self, pr_id: str, code_element_ids: Iterable[str]) -> None:
        """(:PR)-[:CHANGES]->(:CodeElement)"""
        self._run(
            """
            MATCH (p:PR {id: $pr_id})
            UNWIND $code_ids AS cid
            MATCH (c:CodeElement {id: cid})
            MERGE (p)-[:CHANGES]->(c)
            """,
            pr_id=pr_id,
            code_ids=list(code_element_ids),
        )

    # ------------------------------------------------------------------
    # Coverage stamping (the denormalized absence cache — see schema.py)
    # ------------------------------------------------------------------

    def stamp_coverage_status(self, threshold: Optional[float] = None) -> dict:
        """
        Recompute `coverage_status` on every Requirement from the live edge set:
          covered    — at least one confident :IMPLEMENTS edge
          ambiguous  — only needs_review :IMPLEMENTS edges
          not_found  — no :IMPLEMENTS edges at all (the "absence" case)
        Run after every graph build; returns counts per status.
        """
        threshold = threshold if threshold is not None else settings.confidence_threshold
        rows = self._run(
            """
            MATCH (r:Requirement)
            OPTIONAL MATCH (u:UIElement)-[e:IMPLEMENTS]->(r)
            WITH r,
                 count(e) AS total,
                 sum(CASE WHEN e IS NOT NULL AND e.confidence >= $threshold THEN 1 ELSE 0 END) AS confident
            SET r.coverage_status = CASE
                WHEN total = 0 THEN $not_found
                WHEN confident > 0 THEN $covered
                ELSE $ambiguous
            END
            RETURN r.coverage_status AS status, count(r) AS n
            """,
            threshold=threshold,
            covered=CoverageStatus.COVERED.value,
            not_found=CoverageStatus.NOT_FOUND.value,
            ambiguous=CoverageStatus.AMBIGUOUS.value,
        )
        return {row["status"]: row["n"] for row in rows}

    # ------------------------------------------------------------------
    # Read helper used by queries.py / the reasoner
    # ------------------------------------------------------------------

    def query(self, cypher: str, **params) -> list[dict]:
        return self._run(cypher, **params)
