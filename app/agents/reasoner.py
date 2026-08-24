"""
Stage 4 — Reasoner: PR -> blast-radius report.

1. Fetch the merged PR's metadata + changed files (GitHub REST, no auth needed
   for a public repo; a token via GITHUB_TOKEN env would just raise rate
   limits — deliberately not required).
2. Match changed files against CodeElements already in the graph (exact path
   first, then basename), write the (:PR)-[:CHANGES]-> edges.
3. Run the blast-radius traversal (graph/queries.py) — confidence multiplies
   along the path, weak links land in needs_review instead of the main list.
4. One LLM call turns the structured hits into a prose summary a QA lead can
   read without seeing the graph.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

import httpx

from app.agents.llm import complete
from app.config import settings
from app.graph.client import GraphClient
from app.graph.queries import blast_radius_items
from app.models import BlastRadiusReport, PullRequest

log = logging.getLogger(__name__)

GITHUB_API = f"https://api.github.com/repos/{settings.target_repo_slug}"

REPORT_SYSTEM = """You write blast-radius summaries for QA leads who do not
read code. Given a merged PR and the UI elements, screens, user flows, and
product requirements its changed files reach in a traceability graph, write
a short report:

- Open with one sentence: what the PR changes in plain product terms.
- Then "What to re-test": the affected screens/flows, grouped, with the
  specific UI elements called out in plain language.
- Then "Requirements at risk": the linked requirements, one line each.
- If there are low-confidence links, add a final "Uncertain — needs human
  review" paragraph and say WHY they are uncertain (automated mapping).
- No jargon (no selectors, file paths, or graph terms). Under 300 words.
"""


def fetch_pr(pr_number: int) -> PullRequest:
    headers = ({"Authorization": f"Bearer {settings.github_token}"}
               if settings.github_token else {})
    with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as http:
        pr_resp = http.get(f"{GITHUB_API}/pulls/{pr_number}")
        pr_resp.raise_for_status()
        pr = pr_resp.json()

        changed_files: list[str] = []
        page = 1
        while True:
            files_resp = http.get(f"{GITHUB_API}/pulls/{pr_number}/files",
                                  params={"per_page": 100, "page": page})
            files_resp.raise_for_status()
            batch = files_resp.json()
            changed_files.extend(f["filename"] for f in batch)
            if len(batch) < 100:
                break
            page += 1

    return PullRequest(
        id=f"pr-{pr_number}",
        number=pr_number,
        title=pr["title"],
        url=pr["html_url"],
        changed_files=changed_files,
    )


def link_pr_to_code(client: GraphClient, pr: PullRequest) -> list[str]:
    """Match the PR's changed files to CodeElements in the graph: exact
    file_path match first, basename match as fallback. Returns matched
    CodeElement ids (and writes the :CHANGES edges)."""
    client.upsert_pr(pr)
    rows = client.query("MATCH (c:CodeElement) RETURN c.id AS id, c.file_path AS path")
    by_path = {r["path"]: r["id"] for r in rows}
    by_name: dict[str, list[str]] = {}
    for r in rows:
        by_name.setdefault(PurePosixPath(r["path"]).name, []).append(r["id"])

    matched: set[str] = set()
    for f in pr.changed_files:
        if f in by_path:
            matched.add(by_path[f])
        elif PurePosixPath(f).name in by_name:
            matched.update(by_name[PurePosixPath(f).name])

    if matched:
        client.upsert_changes_edges(pr.id, matched)
    log.info("PR #%d: %d/%d changed files matched to graph code elements",
             pr.number, len(matched), len(pr.changed_files))
    return sorted(matched)


def generate_report(client: GraphClient, pr_number: int) -> BlastRadiusReport:
    pr = fetch_pr(pr_number)
    link_pr_to_code(client, pr)

    items = blast_radius_items(client, pr)
    affected = [i for i in items if not i.needs_review]
    needs_review = [i for i in items if i.needs_review]

    if not items:
        summary = (
            f"PR #{pr.number} (\"{pr.title}\") touches no code that is mapped to "
            "the crawled UI, so no user-facing impact was traced. This can mean "
            "the change is backend-only, or that it lands outside the crawled "
            "screens — treat it as unassessed rather than safe."
        )
    else:
        def fmt(i):
            return (f"- element: {i.ui_element_label} | screen: {i.screen_name} | "
                    f"flow: {i.flow_name} | requirement: {i.requirement_text} | "
                    f"confidence: {i.confidence:.2f}")
        summary = complete(
            REPORT_SYSTEM,
            f"PR #{pr.number}: {pr.title}\nURL: {pr.url}\n\n"
            f"Confident impacts:\n" + "\n".join(fmt(i) for i in affected) +
            "\n\nLow-confidence impacts (automated mapping, needs review):\n" +
            ("\n".join(fmt(i) for i in needs_review) or "(none)"),
        )

    return BlastRadiusReport(
        pr=pr, affected=affected, needs_review=needs_review, summary=summary)
