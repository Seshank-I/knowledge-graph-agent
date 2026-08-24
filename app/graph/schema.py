"""
Schema setup for the three-layer graph.

Layers:
  Requirements  -> (:Requirement)
  DOM / UI      -> (:Screen), (:UIElement), (:Flow)
  Code          -> (:CodeElement), (:PR)

Edges:
  (:Requirement)-[:EXPECTED_ON]->(:Screen)
  (:UIElement)-[:PART_OF]->(:Screen)
  (:Screen)-[:STEP_IN]->(:Flow)
  (:UIElement)-[:IMPLEMENTS {confidence, needs_review, rationale}]->(:Requirement)
  (:UIElement)-[:BUILT_BY {confidence, needs_review, rationale}]->(:CodeElement)
  (:PR)-[:CHANGES]->(:CodeElement)

Absence is NOT a node type. A Requirement is "absent from the UI" when it has
no incoming :IMPLEMENTS edge — see graph/queries.py::ABSENT_REQUIREMENTS.
We do additionally stamp `coverage_status` on the Requirement node as a
denormalized read-path optimization (see design doc §6) so the common case
of "list uncovered requirements" doesn't require a traversal.
"""

CONSTRAINTS = [
    "CREATE CONSTRAINT req_id IF NOT EXISTS FOR (r:Requirement) REQUIRE r.id IS UNIQUE",
    "CREATE CONSTRAINT screen_id IF NOT EXISTS FOR (s:Screen) REQUIRE s.id IS UNIQUE",
    "CREATE CONSTRAINT ui_id IF NOT EXISTS FOR (u:UIElement) REQUIRE u.id IS UNIQUE",
    "CREATE CONSTRAINT flow_id IF NOT EXISTS FOR (f:Flow) REQUIRE f.id IS UNIQUE",
    "CREATE CONSTRAINT code_id IF NOT EXISTS FOR (c:CodeElement) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT pr_id IF NOT EXISTS FOR (p:PR) REQUIRE p.id IS UNIQUE",
]

INDEXES = [
    "CREATE INDEX req_coverage IF NOT EXISTS FOR (r:Requirement) ON (r.coverage_status)",
    "CREATE INDEX code_file_path IF NOT EXISTS FOR (c:CodeElement) ON (c.file_path)",
]


def init_schema(driver) -> None:
    with driver.session() as session:
        for stmt in CONSTRAINTS + INDEXES:
            session.run(stmt)
