"""
End-to-end smoke test for the graph layer against a live local Neo4j
(docker-compose up -d first). No LLM calls — synthetic data exercises:
  - every upsert (idempotency: it runs the writes twice)
  - coverage stamping (covered / ambiguous / not_found)
  - the absence query
  - the blast-radius traversal with confidence multiplication

Run:  .venv/bin/python -m tests.test_graph_smoke
(or via pytest if installed).
"""

from __future__ import annotations

from app.graph.client import GraphClient
from app.graph.queries import absent_requirements, blast_radius_items
from app.models import (
    BuiltByEdge, CodeElement, Flow, ImplementsEdge, PullRequest,
    Requirement, Screen, UIElement,
)

PREFIX = "smoke-"


def _seed(client: GraphClient) -> PullRequest:
    reqs = [
        Requirement(id=f"{PREFIX}req-covered", text="User can book a slot",
                    feature_area="booking", testable=True, source_doc="smoke"),
        Requirement(id=f"{PREFIX}req-ambiguous", text="User can pick a timezone",
                    feature_area="booking", testable=True, source_doc="smoke"),
        Requirement(id=f"{PREFIX}req-absent", text="User can export bookings to CSV",
                    feature_area="bookings", testable=True, source_doc="smoke"),
    ]
    screen = Screen(id=f"{PREFIX}screen-booking", url="https://x/book",
                    name="Booking page", elements=[
        UIElement(id=f"{PREFIX}el-confirm", screen_id=f"{PREFIX}screen-booking",
                  selector="[data-testid=\"confirm\"]", element_type="button",
                  label="submit booking", raw_text="Confirm"),
        UIElement(id=f"{PREFIX}el-tz", screen_id=f"{PREFIX}screen-booking",
                  selector="[data-testid=\"tz\"]", element_type="select",
                  label="timezone selector"),
    ])
    flow = Flow(id=f"{PREFIX}flow-book", name="Book a slot",
                screen_ids=[f"{PREFIX}screen-booking"])
    code = CodeElement(id=f"{PREFIX}code-bookpage", file_path="apps/web/book.tsx",
                       repo_ref="calcom/cal.com@main")
    pr = PullRequest(id=f"{PREFIX}pr-1", number=999999, title="smoke PR",
                     url="https://x/pr/999999", changed_files=["apps/web/book.tsx"])

    implements = [
        ImplementsEdge(source_id=f"{PREFIX}el-confirm", target_id=f"{PREFIX}req-covered",
                       confidence=0.9).finalize(0.5),
        ImplementsEdge(source_id=f"{PREFIX}el-tz", target_id=f"{PREFIX}req-ambiguous",
                       confidence=0.3).finalize(0.5),  # -> needs_review only
    ]
    built_by = [
        BuiltByEdge(source_id=f"{PREFIX}el-confirm", target_id=f"{PREFIX}code-bookpage",
                    confidence=0.9).finalize(0.5),
        BuiltByEdge(source_id=f"{PREFIX}el-tz", target_id=f"{PREFIX}code-bookpage",
                    confidence=0.4).finalize(0.5),
    ]

    for _ in range(2):  # twice: proves idempotency of every MERGE
        client.upsert_requirements(reqs)
        client.upsert_screens([screen])
        client.upsert_flows([flow])
        client.upsert_code_elements([code])
        client.upsert_pr(pr)
        client.upsert_implements_edges(implements)
        client.upsert_built_by_edges(built_by)
        client.upsert_expected_on_edges([(f"{PREFIX}req-covered", f"{PREFIX}screen-booking")])
        client.upsert_changes_edges(pr.id, [code.id])
    return pr


def _cleanup(client: GraphClient) -> None:
    client.query("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=PREFIX)


def test_graph_smoke() -> None:
    with GraphClient() as client:
        client.init_schema()
        _cleanup(client)
        pr = _seed(client)
        try:
            # No duplicates despite double-write
            n = client.query("MATCH (n) WHERE n.id STARTS WITH $p RETURN count(n) AS n",
                             p=PREFIX)[0]["n"]
            # 3 reqs + 1 screen + 2 elements + 1 flow + 1 code + 1 pr
            assert n == 9, f"expected 9 smoke nodes, got {n}"

            coverage = client.stamp_coverage_status()
            smoke_cov = {r["id"]: r["status"] for r in client.query(
                "MATCH (r:Requirement) WHERE r.id STARTS WITH $p "
                "RETURN r.id AS id, r.coverage_status AS status", p=PREFIX)}
            assert smoke_cov[f"{PREFIX}req-covered"] == "covered", smoke_cov
            assert smoke_cov[f"{PREFIX}req-ambiguous"] == "ambiguous", smoke_cov
            assert smoke_cov[f"{PREFIX}req-absent"] == "not_found", smoke_cov

            absent = {r["requirement_id"]: r["status"] for r in absent_requirements(client)
                      if r["requirement_id"].startswith(PREFIX)}
            assert absent == {f"{PREFIX}req-absent": "not_found",
                              f"{PREFIX}req-ambiguous": "ambiguous"}, absent

            items = blast_radius_items(client, pr)
            assert items, "blast radius returned nothing"
            top = items[0]
            assert top.ui_element_label == "submit booking"
            assert abs(top.confidence - 0.81) < 1e-9  # 0.9 built_by * 0.9 implements
            assert not top.needs_review
            weak = [i for i in items if i.ui_element_label == "timezone selector"]
            assert weak and all(i.needs_review for i in weak)

            print("SMOKE_OK — coverage counts:", coverage,
                  "| blast radius items:", len(items))
        finally:
            _cleanup(client)


if __name__ == "__main__":
    test_graph_smoke()
