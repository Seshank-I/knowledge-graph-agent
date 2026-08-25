"""
Debug harness for the crawler: runs the crawl directly (no FastAPI, no graph
writes, no cache writes) with element labeling stubbed out, so you can put
breakpoints in app/agents/crawler.py and step through capture/interaction
logic without burning LLM calls.

Run:  .venv/bin/python -m scripts.debug_crawl [username] [--headed] [--only screen-id,...]

  --headed        launch a visible Chromium window (watch it click)
  --only ids      crawl only the named screen ids, e.g.
                  --only screen-booker-landing,screen-booking-page
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents import crawler  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> None:
    args = sys.argv[1:]
    username = next((a for a in args if not a.startswith("--")), "peer")

    if "--headed" in args:
        crawler.LAUNCH_OPTIONS = {"headless": False, "slow_mo": 300}

    only = next((a.split("=", 1)[-1] for a in args if a.startswith("--only")), None)
    if only:
        ids = {s.strip() for s in only.replace("--only", "").strip(" =").split(",")}
        crawler.SCREENS = [s for s in crawler.SCREENS if s["id"] in ids]

    crawler._label_elements = lambda name, els: els  # no LLM in debug runs

    result = asyncio.run(crawler.crawl(username=username))
    for s in result.screens:
        print(f"\n== {s.id}  ({len(s.elements)} elements)  {s.url}")
        for el in s.elements:
            print(f"   {el.element_type:10s} {el.selector[:60]:60s} {el.raw_text or ''}"[:120])
    print("\nflows:", [(f.id, f.screen_ids) for f in result.flows])


if __name__ == "__main__":
    main()
