# Design doc — knowledge-graph-agent

## 1. Problem

Given a code change (a merged PR) to a real web application, answer: *what UI
elements, user flows, and product requirements are put at risk?* — readable by
a non-engineer. This requires connecting three worlds that normally never meet
in one data structure: the spec (intent), the rendered UI (what was built),
and the source code (how it was built).

## 2. Shape of the solution

A four-stage single-pass pipeline feeding one Neo4j graph:

1. **Crawl** — Playwright captures screens, interactive elements, and
   screenshots from app.cal.com; an LLM labels each element's semantic purpose.
2. **Ingest** — an LLM extracts atomic, testable `Requirement` objects from a
   public Cal.com doc, validated against a Pydantic schema.
3. **Graph** — build the three layers and the cross-layer edges
   (`IMPLEMENTS`, `BUILT_BY`), then stamp `coverage_status`.
4. **Reason** — pull a merged PR's changed files from GitHub, connect them to
   `CodeElement`s, traverse outward, and render a prose report.

Target app: **Cal.com** — chosen because its booking / event-type flows are
multi-screen, so the Flow layer has real depth, and because the repo's
pervasive `data-testid` usage makes the deliberately-cut code-mapping layer
honest rather than hopeless.

## 3. Graph schema

Nodes: `Requirement`, `Screen`, `UIElement`, `Flow`, `CodeElement`, `PR`.
Edges: `EXPECTED_ON`, `PART_OF`, `STEP_IN` (with an `order` property),
`IMPLEMENTS {confidence, needs_review, rationale}`,
`BUILT_BY {confidence, needs_review, rationale}`, `CHANGES`.

All node ids are deterministic slugs and unique-constrained; every write is a
`MERGE` upsert, so any stage can be re-run and the graph converges instead of
duplicating.

### 3.1 Modeling absence

The novel requirement of the schema is representing what *isn't* there. Two
options were considered:

- **A `Gap`/`MissingImplementation` node type** — rejected: it turns a derived
  fact into stored state that goes stale the moment a new `IMPLEMENTS` edge
  lands, and it forces every writer to maintain it.
- **Absence as a query result** (chosen) — a testable `Requirement` with no
  incoming `:IMPLEMENTS` edge *is* the gap. The query also distinguishes
  `ambiguous` (only `needs_review` edges) from `not_found` (no edges).

We do denormalize the answer into a `coverage_status` property on
`Requirement`, recomputed in one pass after every graph build
(`GraphClient.stamp_coverage_status`). This is a read-path cache of the
canonical query, never hand-written — the graph stays the source of truth.

### 3.2 Ambiguity handling

Every LLM-produced edge carries `confidence: float`. Below
`settings.confidence_threshold` (default 0.5) the edge is stamped
`needs_review: true` — included in the graph, excluded from confident
conclusions. The blast-radius traversal multiplies confidences along the path
(`built_by * implements`), so a weak link anywhere demotes the whole finding
to the report's "needs human review" section rather than overstating
certainty. Hallucinated ids in LLM linking output are dropped with a warning
(`req_linker.py`), never written.

## 4. Deliberate scope cuts

These are decisions, not oversights:

- **Fixed crawl list (6 screens)**, not autonomous exploration. Open-ended
  crawling of an authenticated app is its own project (loop detection, state
  pollution, destructive-action safety). The fixed list keeps the crawl
  deterministic and puts the effort where the assignment's value is: the
  graph and the reasoning.
- **Code mapping is a heuristic** — `data-testid` grep (unique hit ⇒ 0.9
  confidence) then filename/label token overlap, with an LLM choosing among
  shortlisted candidates when the heuristic is weak. No AST, no call graph,
  no import resolution. The confidence machinery exists precisely so this cut
  layer can be honest about its own quality.
- **Single-pass, no self-healing** — a screen that fails to load is logged and
  skipped; an LLM response that fails schema validation gets exactly one
  corrective retry (with the validation error fed back), then raises. Retry
  loops hide failure modes; for a prototype, surfaced failures are worth more.
- **No full eval harness** — instead, a small hand-verified golden set: for
  one chosen PR, the expected affected screens/requirements are written down
  by hand and the report is checked against them. §6 sketches the real
  harness.

## 5. Blast-radius traversal

```
(PR)-[:CHANGES]->(CodeElement)<-[:BUILT_BY]-(UIElement)
                                    ├─[:PART_OF]->(Screen)-[:STEP_IN]->(Flow)
                                    └─[:IMPLEMENTS]->(Requirement)
```

`PR->CodeElement` is exact (file paths from the GitHub diff), so path
confidence is `built_by.confidence * implements.confidence`. Results are
deduped per (element, screen, flow, requirement) keeping the strongest path,
split into `affected` vs `needs_review`, and handed to one LLM call that is
explicitly instructed to write for a QA lead: no selectors, no file paths,
under 300 words, with an honest "uncertain" section.

A PR whose changed files match nothing in the graph is reported as
**unassessed, not safe** — the summary says so explicitly.

## 6. What a real eval harness would look like

- **Edge-level**: a labeled set of (UIElement, Requirement) and (UIElement,
  file) pairs across ~50 elements; measure precision/recall of `IMPLEMENTS`
  and `BUILT_BY` at each confidence band, calibrating the threshold instead
  of hand-picking 0.5.
- **Report-level**: for ~20 historical PRs with known regressions (mined from
  cal.com issues referencing PRs), check whether the blast radius contains
  the screen/flow where the regression actually surfaced — recall@report.
- **Absence-level**: seed the spec with requirements known to be
  unimplemented; assert they surface as `not_found` and never `covered`.
- Regression-run the whole thing on a pinned repo commit + pinned crawl
  artifacts so LLM drift is the only moving part per run.

## 7. Known limitations

- Code mapping ignores non-component code (API routes, hooks, server logic) —
  a PR touching only `packages/lib` will usually come back "unassessed".
- The crawler's element capture is visible-elements-only; modals and menus
  that need interaction to open are missed.
- Requirement extraction quality is bounded by the public doc's specificity.
- One LLM mislabel can propagate through both cross-layer edges — mitigated,
  not eliminated, by confidence multiplication.
