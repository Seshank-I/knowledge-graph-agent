# Sample output — real end-to-end run (2026-08-24)

This is a genuine run of the pipeline against the live app and the public
repo, with one substitution: the LLM calls (requirement extraction, element
labeling, requirement↔UI linking, code-map fallback, report prose) were
performed manually, human-in-the-loop, instead of through the Anthropic API
(no API key was configured). Everything else — the Playwright crawl, GitHub
tree/search/PR API calls, Neo4j writes, confidence math, and the blast-radius
traversal — is the real pipeline code. Screenshots and DOM snapshots from the
crawl are in `data/artifacts/`.

Run parameters:
- Target app: app.cal.com (public screens; the four authenticated screens
  were skipped — no test account — and are logged as crawl failures, which is
  the pipeline's designed single-pass behavior)
- Repo: `calcom/cal.diy` (community fork of cal.com, same monorepo layout),
  mapped **without a local clone** via the GitHub tree + code-search APIs
  (`code_mapper.map_elements_remote`)
- Spec source: https://cal.com/docs/llms.txt (Cal.com docs index)

## Resulting graph

| Nodes | | Edges | |
|---|---|---|---|
| Requirement | 19 | IMPLEMENTS | 16 |
| UIElement | 34 | BUILT_BY | 30 |
| Screen | 3 | PART_OF | 34 |
| Flow | 4 | STEP_IN | 5 |
| CodeElement | 13 | CHANGES | 1 |
| PR | 2 | | |

Coverage stamped on requirements: **7 covered, 1 ambiguous, 11 not_found**.

## The absence query (`GET /graph/absent-requirements`)

Nine testable requirements have no confident UI evidence. Most are honest
artifacts of the crawl scope (their screens need login), which is exactly
what the absence layer is supposed to surface:

| Requirement | Status | Why |
|---|---|---|
| Create/edit availability schedules | not_found | screen requires auth, not crawled |
| Create out-of-office entry | not_found | screen requires auth, not crawled |
| View upcoming bookings list | not_found | screen requires auth, not crawled |
| Confirm or decline a booking | not_found | screen requires auth, not crawled |
| Create a new event type | not_found | screen requires auth, not crawled |
| Update profile in settings | not_found | screen requires auth, not crawled |
| Invitee can cancel a booking | not_found | lives on the booking-management page, outside the fixed screen list |
| Invitee can reschedule a booking | not_found | same as above |
| View public profile listing event types | **ambiguous** | this user's booker skips the event-list page; only a 0.4-confidence link exists |

## Blast radius #1 — UI-touching PR

**PR [#28534](https://github.com/calcom/cal.diy/pull/28534): fix(calendar):
add pointer cursor to enabled date buttons** — changes
`packages/features/calendars/components/DatePicker.tsx`.

Traversal result (`data/blast_radius_pr_28534.json`): 4 affected items, path
confidence = `built_by × implements`:

| UI element | Screen | Flow | Requirement | Conf |
|---|---|---|---|---|
| previous month | Public profile | Book a slot | — | 0.90 |
| next month | Public profile | Book a slot | — | 0.90 |
| pick a date | Public profile | Book a slot | Invitee can pick a date and time slot | 0.85 |
| pick a date | Public profile | Book a slot | Invitee can see available time slots | 0.54 |

Generated report (for a QA lead):

> This change adjusts how the date calendar on the public booking page
> responds to the mouse: dates that can be selected now show a pointer
> cursor. It touches the calendar component itself, so anything the calendar
> does is worth a look.
>
> **What to re-test** — Book a slot (invitee) flow, on the public booking
> page: picking a date in the calendar still selects it and loads that day's
> available times; the "previous month" and "next month" arrows still move
> between months; dates that cannot be booked stay unselectable. Quick visual
> check: selectable dates show the hand cursor, disabled dates do not.
>
> **Requirements at risk** — "Invitee can pick a date and time slot to book a
> meeting" (the date half of this lives in the changed calendar); "Invitee
> can open an event type and see available time slots" (weaker link, see
> below).
>
> **Uncertain — needs human review** — The link to "see available time
> slots" is a lower-confidence automated mapping: the calendar shows which
> days have availability, but the slot list itself is a separate component
> the change does not touch. Treat it as a sanity check rather than a likely
> break.

## Blast radius #2 — backend-only PR (the honest negative)

**PR [#29940](https://github.com/calcom/cal.diy/pull/29940): fix(emails):
customReplyToEmail no longer dropped** — changes only
`packages/lib/getReplyToHeader.ts` (+ its test).

No mapped code element matches, so the report says — deterministically, no
LLM involved:

> PR #29940 touches no code that is mapped to the crawled UI, so no
> user-facing impact was traced. This can mean the change is backend-only, or
> that it lands outside the crawled screens — **treat it as unassessed rather
> than safe.**

## Notes on fidelity

- The `data-testid` heuristic performed as designed: unique search hits
  (e.g. `decrementMonth`, `overlay-calendar-switch`, `avatar-href`) mapped at
  0.9 and were verified correct; ambiguous hits dropped to 0.55.
- Two weak heuristic picks were overridden by the (manual) LLM fallback
  with rationale recorded on the edge — e.g. `google` matched
  `apps/web/lib/csp.ts` (a CSP allowlist, not the button) and was corrected
  to `login-view.tsx`.
- Booker layout-toggle testids are template strings in source
  (`toggle-group-item-${...}`), so content search finds nothing — those went
  through the fallback at 0.6 confidence.
