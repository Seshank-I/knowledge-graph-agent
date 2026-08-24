"""
Stage 3 — Code Mapper: UIElement -> CodeElement (BUILT_BY edges).

Explicitly the most "cut" layer (see design doc): a filename/symbol-name
heuristic over a local clone of calcom/cal.com, with an LLM fallback for
elements the heuristic scores below the confidence threshold. No AST or
call-graph analysis.

Heuristic signal, in descending confidence:
  1. data-testid grep — Cal.com uses data-testid pervasively; a testid that
     appears verbatim in exactly one source file is a near-certain match.
  2. token overlap between the element's label/testid and file paths under
     the app's UI directories.
The LLM fallback picks among the heuristic's shortlisted candidate files and
returns its own confidence; either way the edge keeps a `confidence` float
and gets `needs_review` stamped by ScoredEdge.finalize(threshold).
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path, PurePath

from pydantic import BaseModel

from app.agents.llm import complete_validated
from app.config import settings
from app.models import BuiltByEdge, CodeElement, UIElement

log = logging.getLogger(__name__)

# Directories worth searching in the cal.com monorepo; keeps grep fast and
# avoids matching tests/fixtures.
UI_DIRS = ["apps/web", "packages/features", "packages/ui", "packages/platform/atoms"]
SOURCE_EXTS = (".tsx", ".ts")


def _is_test_path(path: str) -> bool:
    p = path.lower()
    return "test" in PurePath(p).name or "/playwright/" in p or "e2e" in p

FALLBACK_SYSTEM = """You map a UI element of the Cal.com web app to the source
file most likely to render it, choosing ONLY from the provided candidate
files (paths from the calcom/cal.com monorepo). Return the chosen file path,
a confidence between 0 and 1 (be conservative — 0.3 if you're guessing from
the path name alone), and a one-line rationale. If no candidate is plausible,
return an empty file_path and confidence 0."""


class _FallbackChoice(BaseModel):
    file_path: str
    confidence: float
    rationale: str


def _repo() -> Path:
    repo = Path(settings.target_repo_path)
    if not repo.exists():
        raise FileNotFoundError(
            f"target repo not found at {repo} — clone calcom/cal.com there first")
    return repo


def _grep_testid(repo: Path, testid: str) -> list[str]:
    """Files containing the data-testid string, restricted to UI dirs."""
    dirs = [str(repo / d) for d in UI_DIRS if (repo / d).exists()]
    if not dirs:
        return []
    proc = subprocess.run(
        ["grep", "-rl", "--include=*.tsx", "--include=*.ts", f'"{testid}"', *dirs],
        capture_output=True, text=True,
    )
    return [str(Path(p).relative_to(repo)) for p in proc.stdout.splitlines()
            if not _is_test_path(p)]


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) > 2}


def _token_candidates(repo: Path, element: UIElement, limit: int = 8) -> list[tuple[str, float]]:
    """Rank UI source files by token overlap with the element's label/testid."""
    el_tokens = _tokens(element.label) | _tokens(element.selector)
    if not el_tokens:
        return []
    scored: list[tuple[str, float]] = []
    for d in UI_DIRS:
        base = repo / d
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if f.suffix not in SOURCE_EXTS or _is_test_path(str(f)):
                continue
            path_tokens = _tokens(str(f.relative_to(repo)))
            overlap = len(el_tokens & path_tokens)
            if overlap:
                scored.append((str(f.relative_to(repo)),
                               min(0.6, 0.2 + 0.15 * overlap)))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:limit]


def _testid_of(element: UIElement) -> str | None:
    m = re.match(r'\[data-testid="([^"]+)"\]', element.selector)
    return m.group(1) if m else None


def _code_element(file_path: str, repo_ref: str) -> CodeElement:
    slug = re.sub(r"[^a-z0-9]+", "-", file_path.lower()).strip("-")
    return CodeElement(id=f"code-{slug}", file_path=file_path, repo_ref=repo_ref)


def _token_candidates_from_paths(paths: list[str], element: UIElement,
                                 limit: int = 8) -> list[tuple[str, float]]:
    """Same token-overlap ranking as _token_candidates, but over a provided
    path list (e.g. from the GitHub tree API) instead of a local clone."""
    el_tokens = _tokens(element.label) | _tokens(element.selector)
    if not el_tokens:
        return []
    scored = []
    for p in paths:
        if _is_test_path(p):
            continue
        overlap = len(el_tokens & _tokens(p))
        if overlap:
            scored.append((p, min(0.6, 0.2 + 0.15 * overlap)))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:limit]


def map_elements_remote(elements: list[UIElement], paths: list[str],
                        search_testid, repo_ref: str,
                        llm_fallback: bool = True,
                        ) -> tuple[list[CodeElement], list[BuiltByEdge]]:
    """map_elements without a local clone: `paths` is the repo's UI file list
    (GitHub tree API) and `search_testid(testid) -> list[path]` is a remote
    code search (GitHub search API). Confidence rules are identical to the
    local heuristic."""
    threshold = settings.confidence_threshold
    code_by_path: dict[str, CodeElement] = {}
    edges: list[BuiltByEdge] = []

    for el in elements:
        match: tuple[str, float, str] | None = None

        testid = _testid_of(el)
        if testid:
            hits = search_testid(testid)
            if len(hits) == 1:
                match = (hits[0], 0.9, f"unique data-testid search hit for '{testid}'")
            elif 1 < len(hits) <= 3:
                match = (hits[0], 0.55,
                         f"data-testid '{testid}' in {len(hits)} files; picked first")

        candidates = _token_candidates_from_paths(paths, el)
        if match is None and candidates and candidates[0][1] >= threshold:
            match = (candidates[0][0], candidates[0][1],
                     "token overlap between element label and file path")

        if llm_fallback and (match is None or match[1] < threshold) and candidates:
            try:
                choice = complete_validated(
                    FALLBACK_SYSTEM,
                    f"Element: label={el.label!r} selector={el.selector!r} "
                    f"type={el.element_type} text={el.raw_text!r}\n"
                    f"Candidates:\n" + "\n".join(p for p, _ in candidates),
                    _FallbackChoice,
                )
                if choice.file_path and (match is None or choice.confidence > match[1]):
                    match = (choice.file_path, choice.confidence,
                             f"LLM fallback: {choice.rationale}")
            except Exception:
                log.exception("LLM fallback failed for %s — keeping heuristic", el.id)

        if match is None:
            log.info("no code match for %s (%s)", el.id, el.label)
            continue

        path, confidence, rationale = match
        code = code_by_path.setdefault(path, _code_element(path, repo_ref))
        edges.append(BuiltByEdge(
            source_id=el.id, target_id=code.id,
            confidence=confidence, rationale=rationale,
        ).finalize(threshold))

    return list(code_by_path.values()), edges


def map_elements(elements: list[UIElement],
                 repo_ref: str = "calcom/cal.com@main"
                 ) -> tuple[list[CodeElement], list[BuiltByEdge]]:
    repo = _repo()
    threshold = settings.confidence_threshold
    code_by_path: dict[str, CodeElement] = {}
    edges: list[BuiltByEdge] = []

    for el in elements:
        match: tuple[str, float, str] | None = None  # (path, confidence, rationale)

        testid = _testid_of(el)
        if testid:
            hits = _grep_testid(repo, testid)
            if len(hits) == 1:
                match = (hits[0], 0.9, f"unique data-testid grep hit for '{testid}'")
            elif 1 < len(hits) <= 3:
                match = (hits[0], 0.55,
                         f"data-testid '{testid}' in {len(hits)} files; picked first")

        candidates = _token_candidates(repo, el)
        if match is None and candidates and candidates[0][1] >= threshold:
            match = (candidates[0][0], candidates[0][1],
                     "token overlap between element label and file path")

        # LLM fallback for low-confidence / no heuristic match, if there are
        # any candidates to choose among.
        if (match is None or match[1] < threshold) and candidates:
            try:
                choice = complete_validated(
                    FALLBACK_SYSTEM,
                    f"Element: label={el.label!r} selector={el.selector!r} "
                    f"type={el.element_type} text={el.raw_text!r}\n"
                    f"Candidates:\n" + "\n".join(p for p, _ in candidates),
                    _FallbackChoice,
                )
                if choice.file_path and (match is None or choice.confidence > match[1]):
                    match = (choice.file_path, choice.confidence,
                             f"LLM fallback: {choice.rationale}")
            except Exception:
                log.exception("LLM fallback failed for %s — keeping heuristic", el.id)

        if match is None:
            log.info("no code match for %s (%s)", el.id, el.label)
            continue

        path, confidence, rationale = match
        code = code_by_path.setdefault(path, _code_element(path, repo_ref))
        edges.append(BuiltByEdge(
            source_id=el.id, target_id=code.id,
            confidence=confidence, rationale=rationale,
        ).finalize(threshold))

    return list(code_by_path.values()), edges
