# Continuation prompt — paste this into a new chat, with the attached zip uploaded

I'm building a working prototype for a system that reasons about "blast radius" —
given a code change (a PR) to a real web application, what UI elements, user
flows, and product requirements are put at risk. This is a personal project /
assignment with a tight deadline, and I need to keep implementing it efficiently.

## What it needs to do (four stages)
1. **Crawl** — a browser agent (Playwright) explores a live public web app and
   captures structured artifacts: DOM, screenshots, interactive elements,
   screen-to-screen transitions.
2. **Ingest** — parse a public product spec / docs / README for the same app
   into structured, testable requirements.
3. **Graph** — build a Neo4j knowledge graph connecting three layers:
   Requirements (intent) ↔ DOM/UI (what was built) ↔ Code (how it was built,
   mapped from the app's public repo). The schema must explicitly model
   **absence** — requirements that should be testable but have no matching UI.
4. **Reason** — given a real merged PR in the public repo, output a blast-radius
   report: which UI elements, flows, and requirements are affected — written so
   a non-engineer (e.g. a QA lead) can read it.

## Constraints / what's explicitly out of scope (deliberate, not an oversight)
- Deep effort goes into the **Graph** (schema + absence modeling) and **Reason**
  (blast-radius traversal + readable report) stages.
- **Crawl** is scoped to a fixed list of 5–6 high-value screens, not open-ended
  autonomous exploration.
- **Code mapping** (UI element → source file) is a filename/symbol-name
  heuristic with an LLM fallback for low-confidence matches — not real static
  analysis / AST / call-graph parsing. This is the most explicitly "cut" layer.
- No retry/self-healing agent loops — single-pass pipeline, failures are logged,
  not auto-recovered.
- No full eval harness — a small hand-verified golden set instead, with a
  stated plan for what a real harness would look like.

## Stack decisions already made
- Python + FastAPI (not Java — the reference ecosystem for this kind of
  browser-agent + graph work is Python-first)
- Playwright for the crawler
- Neo4j for the graph
- An LLM API (Anthropic) for: labeling UI elements' semantic purpose during
  crawl, extracting requirements from the spec doc, low-confidence code-mapping
  fallback, and generating the final natural-language blast-radius report
- Target app: **Cal.com** (app.cal.com), repo: `calcom/cal.com` on GitHub —
  chosen for its multi-screen booking/event-type flows, which give the flow
  graph enough depth to be interesting
- Every LLM-produced edge in the graph carries a `confidence` float; below a
  threshold it's flagged `needs_review: true` rather than silently included or
  dropped — this is the ambiguity-handling story

## Graph schema (already designed)
Nodes: `Requirement`, `Screen`, `UIElement`, `Flow`, `CodeElement`, `PR`
Edges: `Requirement-[:EXPECTED_ON]->Screen`, `UIElement-[:PART_OF]->Screen`,
`Screen-[:STEP_IN]->Flow`, `UIElement-[:IMPLEMENTS {confidence, needs_review}]->Requirement`,
`UIElement-[:BUILT_BY {confidence, needs_review}]->CodeElement`,
`PR-[:CHANGES]->CodeElement`

**Absence is not a node type** — it's the query result of a `Requirement` with
no incoming `:IMPLEMENTS` edge. A denormalized `coverage_status` property on
`Requirement` (`covered` / `not_found` / `ambiguous`) caches this for cheap
reads, set by the Graph Builder stage after ingestion.

## Progress so far (see attached zip: `knowledge-graph-agent-progress.zip`)
Project skeleton exists with:
- `requirements.txt`, `docker-compose.yml` (local Neo4j), `.env.example`
- `app/config.py` — pydantic-settings config, single source of truth for env vars
- `app/models.py` — full Pydantic domain models for every pipeline stage
  (Requirement, Screen, UIElement, Flow, CodeElement, PR, ScoredEdge/
  ImplementsEdge/BuiltByEdge with confidence + needs_review, BlastRadiusReport)
- `app/graph/schema.py` — Neo4j constraints/indexes and the schema doc comment
  describing the absence-modeling approach

## What's next (in priority order)
1. `app/graph/client.py` — Neo4j driver wrapper + write functions for each
   node/edge type (idempotent MERGE-based upserts)
2. `app/graph/queries.py` — the blast-radius traversal query and the
   absent-requirements query
3. `app/agents/spec_parser.py` — LLM-based extraction of Requirement objects
   from a docs page, with structured-output validation
4. `app/agents/crawler.py` — Playwright script for the fixed Cal.com screen
   list, DOM/element capture, LLM element labeling
5. `app/agents/code_mapper.py` — heuristic + LLM-fallback UIElement→CodeElement
   matching
6. `app/agents/reasoner.py` — PR diff parsing, graph traversal, LLM report
   generation
7. `app/main.py` — FastAPI endpoints wiring the pipeline together
8. README, sample output, design doc

Please pick up from step 1 (`app/graph/client.py`) unless I say otherwise —
read the uploaded zip first so the new code matches the existing models and
schema exactly.
