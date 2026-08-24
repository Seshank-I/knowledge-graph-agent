"""
The two queries the whole project exists to answer.

1. BLAST_RADIUS — given a PR, walk PR -> CHANGES -> CodeElement <- BUILT_BY <-
   UIElement, then fan out from each hit UIElement to its Screen, the Flows
   that Screen participates in, and the Requirements it IMPLEMENTS. The
   traversal multiplies edge confidences (PR->CodeElement is exact, so the
   path confidence is built_by.confidence, and built_by * implements for the
   requirement leg) so the report can rank affected items and route weak
   links to a needs_review bucket instead of overstating certainty.

2. ABSENT_REQUIREMENTS — the absence query: Requirements with no incoming
   :IMPLEMENTS edge (plus the ambiguous ones whose only edges are
   needs_review). This is the canonical source of truth; the denormalized
   `coverage_status` property stamped by GraphClient.stamp_coverage_status
   is just a cached copy of this result for cheap reads.
"""

from __future__ import annotations

from app.graph.client import GraphClient
from app.models import BlastRadiusItem, PullRequest

BLAST_RADIUS = """
MATCH (p:PR {id: $pr_id})-[:CHANGES]->(c:CodeElement)<-[b:BUILT_BY]-(u:UIElement)
OPTIONAL MATCH (u)-[:PART_OF]->(s:Screen)
OPTIONAL MATCH (s)-[:STEP_IN]->(f:Flow)
OPTIONAL MATCH (u)-[i:IMPLEMENTS]->(r:Requirement)
RETURN
    c.file_path                          AS code_file,
    u.label                              AS ui_element_label,
    s.name                               AS screen_name,
    f.name                               AS flow_name,
    r.text                               AS requirement_text,
    b.confidence                         AS built_by_confidence,
    i.confidence                         AS implements_confidence,
    b.confidence * coalesce(i.confidence, 1.0) AS path_confidence,
    (b.needs_review OR coalesce(i.needs_review, false)) AS needs_review
ORDER BY path_confidence DESC
"""

ABSENT_REQUIREMENTS = """
MATCH (r:Requirement)
WHERE r.testable = true
OPTIONAL MATCH (u:UIElement)-[e:IMPLEMENTS]->(r)
WITH r, count(e) AS total,
     sum(CASE WHEN e IS NOT NULL AND NOT e.needs_review THEN 1 ELSE 0 END) AS confident
WHERE total = 0 OR confident = 0
RETURN
    r.id           AS requirement_id,
    r.text         AS requirement_text,
    r.feature_area AS feature_area,
    CASE WHEN total = 0 THEN 'not_found' ELSE 'ambiguous' END AS status
ORDER BY feature_area, requirement_id
"""


def blast_radius_rows(client: GraphClient, pr_id: str) -> list[dict]:
    return client.query(BLAST_RADIUS, pr_id=pr_id)


def blast_radius_items(client: GraphClient, pr: PullRequest) -> list[BlastRadiusItem]:
    """Rows -> typed items, deduped (the same UIElement can reach the same
    Requirement via several flows; keep the highest-confidence row per tuple)."""
    seen: dict[tuple, BlastRadiusItem] = {}
    for row in blast_radius_rows(client, pr.id):
        key = (row["ui_element_label"], row["screen_name"],
               row["flow_name"], row["requirement_text"])
        item = BlastRadiusItem(
            ui_element_label=row["ui_element_label"],
            screen_name=row["screen_name"],
            flow_name=row["flow_name"],
            requirement_text=row["requirement_text"],
            confidence=row["path_confidence"],
            needs_review=row["needs_review"],
        )
        if key not in seen or item.confidence > seen[key].confidence:
            seen[key] = item
    return sorted(seen.values(), key=lambda i: i.confidence, reverse=True)


def absent_requirements(client: GraphClient) -> list[dict]:
    return client.query(ABSENT_REQUIREMENTS)
