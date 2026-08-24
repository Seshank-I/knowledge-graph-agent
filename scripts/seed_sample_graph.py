"""
Seed Neo4j with the committed sample graph (data/graph_export.json) — the
graph produced by the real end-to-end run documented in SAMPLE_OUTPUT.md.

This gives a reviewer the full populated graph WITHOUT needing an Anthropic
API key, a Cal.com account, or a repo clone, so the blast-radius and absence
endpoints can be exercised immediately:

    .venv/bin/python -m scripts.seed_sample_graph

Idempotent: MERGE-based like every other write in the project.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.graph.client import GraphClient  # noqa: E402

EXPORT = Path(__file__).resolve().parents[1] / "data" / "graph_export.json"

# Only labels/types the schema defines are allowed — the export is data,
# not arbitrary Cypher.
NODE_LABELS = {"Requirement", "Screen", "UIElement", "Flow", "CodeElement", "PR"}
EDGE_TYPES = {"EXPECTED_ON", "PART_OF", "STEP_IN", "IMPLEMENTS", "BUILT_BY", "CHANGES"}


def main() -> None:
    data = json.loads(EXPORT.read_text())
    with GraphClient() as client:
        client.init_schema()
        for row in data["nodes"]:
            label = row["label"]
            if label not in NODE_LABELS:
                raise ValueError(f"unexpected node label in export: {label}")
            client.query(
                f"MERGE (n:{label} {{id: $id}}) SET n += $props",
                id=row["props"]["id"], props=row["props"],
            )
        for row in data["edges"]:
            etype = row["type"]
            if etype not in EDGE_TYPES:
                raise ValueError(f"unexpected edge type in export: {etype}")
            client.query(
                f"""MATCH (a {{id: $source}}), (b {{id: $target}})
                    MERGE (a)-[e:{etype}]->(b) SET e += $props""",
                source=row["source"], target=row["target"], props=row["props"],
            )
        counts = client.query(
            "MATCH (n) RETURN labels(n)[0] AS l, count(n) AS n ORDER BY l")
    print("seeded:", {r["l"]: r["n"] for r in counts})


if __name__ == "__main__":
    main()
