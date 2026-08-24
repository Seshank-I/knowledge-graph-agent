# knowledge-graph-agent

Blast-radius reasoning for web-app changes: given a merged PR in
[calcom/cal.com](https://github.com/calcom/cal.com), report which UI elements,
user flows, and product requirements of the live app (app.cal.com) are put at
risk — written for a QA lead, not an engineer.

Three-layer Neo4j knowledge graph:

```
Requirement  (intent — what the product should do)
     ↑ IMPLEMENTS {confidence, needs_review}
UIElement ── PART_OF ─→ Screen ── STEP_IN ─→ Flow   (what was built)
     ↓ BUILT_BY {confidence, needs_review}
CodeElement ←── CHANGES ── PR                        (how it was built)
```

**Absence is a first-class query, not a node**: a testable `Requirement` with
no incoming `:IMPLEMENTS` edge is "specified but not found in the UI". A
denormalized `coverage_status` property (`covered` / `ambiguous` / `not_found`)
caches this for cheap reads. Every LLM-produced edge carries a `confidence`
float; below the threshold it is flagged `needs_review` instead of being
silently trusted or dropped.

## Setup

```bash
# 1. Python 3.10+ (developed on 3.12)
uv venv --python 3.12 .venv && uv pip install -r requirements.txt --python .venv/bin/python
.venv/bin/playwright install chromium

# 2. Local Neo4j
docker compose up -d           # bolt://localhost:7687, browser at :7474

# 3. Config
cp .env.example .env           # fill in ANTHROPIC_API_KEY + a Cal.com test account

# 4. The target repo (for code mapping + PR analysis)
git clone --depth 1 https://github.com/calcom/cal.com data/cal.com
```

## Quickstart — no API key needed

The repo ships the graph from the sample run (`data/graph_export.json`), so
you can exercise the interesting endpoints without an Anthropic key, a
Cal.com account, or a repo clone:

```bash
docker compose up -d
.venv/bin/python -m scripts.seed_sample_graph   # loads the sample graph
.venv/bin/uvicorn app.main:app --port 8000
```

Then:

```bash
curl localhost:8000/health                        # graph node counts
curl localhost:8000/graph/absent-requirements     # the absence query
curl -X POST localhost:8000/pipeline/blast-radius/28534   # DatePicker PR
curl -X POST localhost:8000/pipeline/blast-radius/29940   # backend-only PR
```

Blast-radius calls fetch the PR live from GitHub (repo set by
`TARGET_REPO_SLUG`, default calcom/cal.com — the sample graph was built
against `calcom/cal.diy`, so set `TARGET_REPO_SLUG=calcom/cal.diy` in `.env`
for the two PRs above). Anonymous GitHub API limits are low; set
`GITHUB_TOKEN` in `.env` if you hit 403s. The structured findings are
computed without any LLM; without an `ANTHROPIC_API_KEY` the prose summary
falls back to a deterministic one-liner (and 29940's "unassessed, not safe"
summary is always deterministic).

## Run the full pipeline

The LLM stages need credentials — either of:

- `ANTHROPIC_API_KEY` in `.env` (default backend, Anthropic SDK), or
- `LLM_BACKEND=claude_cli` in `.env` — routes LLM calls through a locally
  installed, logged-in [Claude Code](https://claude.com/claude-code) CLI
  (`claude -p`), so a Claude subscription works with no API key. Same
  prompts and schema validation; slower per call.

```bash
.venv/bin/uvicorn app.main:app --reload
```

Then drive the pipeline in order (each stage is idempotent — safe to re-run):

```bash
# Stage 1 — extract requirements from a public doc
curl -X POST localhost:8000/pipeline/spec \
  -H 'content-type: application/json' \
  -d '{"source": "https://cal.com/docs"}'

# Stage 2 — crawl the fixed 6-screen Cal.com list (screenshots/DOM -> data/artifacts/)
curl -X POST localhost:8000/pipeline/crawl

# Stage 3 — build IMPLEMENTS + BUILT_BY edges, stamp coverage_status
curl -X POST localhost:8000/pipeline/link

# Stage 4 — the payoff: blast radius for a merged PR
curl -X POST localhost:8000/pipeline/blast-radius/17423

# The absence query
curl localhost:8000/graph/absent-requirements
```

## Tests

```bash
docker compose up -d
.venv/bin/python -m tests.test_graph_smoke
```

The smoke test seeds a synthetic three-layer graph (no LLM calls), writes
everything twice to prove upsert idempotency, and asserts coverage stamping,
the absence query, and confidence multiplication along the blast-radius path.

## Layout

```
app/config.py            pydantic-settings, single source of env truth
app/models.py            typed contracts between stages
app/graph/schema.py      constraints/indexes + absence-modeling doc
app/graph/client.py      the ONLY Cypher-writing module (MERGE upserts)
app/graph/queries.py     BLAST_RADIUS + ABSENT_REQUIREMENTS
app/agents/llm.py        Anthropic wrapper: schema-validated JSON, one retry
app/agents/spec_parser.py   Stage 1: doc -> Requirements
app/agents/crawler.py       Stage 2: Playwright over a fixed screen list
app/agents/req_linker.py    Graph build: Requirement<->UIElement (IMPLEMENTS)
app/agents/code_mapper.py   Stage 3: UIElement -> CodeElement (BUILT_BY)
app/agents/reasoner.py      Stage 4: PR -> blast-radius report
app/main.py              FastAPI wiring
```

## Sample run

`SAMPLE_OUTPUT.md` documents a real end-to-end run against app.cal.com and
`calcom/cal.diy` (including two blast-radius reports and the absence query
output). That run used `code_mapper.map_elements_remote`, which maps code
**without a local clone** via the GitHub tree + code-search APIs — useful
when cloning the monorepo isn't practical. Set `GITHUB_TOKEN` (and
`TARGET_REPO_SLUG` if not calcom/cal.com) in `.env` for higher API limits.

See `DESIGN.md` for the reasoning behind the scope cuts (fixed crawl list,
heuristic code mapping, single-pass pipeline, golden set instead of an eval
harness).
