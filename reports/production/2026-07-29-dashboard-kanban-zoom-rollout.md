# Dashboard Kanban zoom rollout — 2026-07-29

## Change identity

- Feature PR: #27
- Feature commit: `f8562c8403f59ede78cae72b6c93a51c7c139e19`
- Merge commit: `e5b181568c38e3566f8d291a6ad4a99cd8464a52`
- Production commit after fast-forward: `e5b181568c38e3566f8d291a6ad4a99cd8464a52`
- Rollout completed: 2026-07-29 02:43 Europe/Amsterdam

## Implementation

The existing FastAPI, Jinja2, CSS, and plain-JavaScript dashboard was extended without replacing the dashboard or adding a runtime frontend library. Kanban now uses an overflow-constrained outer viewport, a logical inner board, `transform: scale()` with a top-left origin, and a compensating stage whose dimensions track the scaled board. Horizontal and vertical movement therefore remain inside Kanban.

The supported scale range is 40–120%, with 5% button steps. Card content switches from normal to compact below 85% and to overview below 60%. The scale toolbar remains unscaled and exposes minus, plus, current percentage, 100%, and fit controls.

“Вместить” calculates the smaller width/height fit ratio, never enlarges beyond 100%, clamps to the 40% minimum, and recalculates after viewport or orientation changes. When the full board cannot fit at the minimum, the board remains at 40% with internal panning.

The device-local key `voice-inbox.dashboard.kanban.zoom.v1` stores validated scale and manual/fit mode state in `localStorage`. Invalid or corrupted values reset to 100%. No server or Airtable persistence is used for zoom.

## Mobile gestures and drag-and-drop

- A one-finger gesture inside Kanban pans its own viewport; a short tap still opens the normal detail page.
- A two-pointer pinch changes only Kanban scale and preserves the gesture focal point.
- Normal page scrolling and browser wheel behavior remain unchanged outside Kanban; ordinary wheel events inside Kanban are not intercepted.
- Ctrl+wheel inside Kanban changes board scale on desktop.
- Card movement starts only from a dedicated drag handle. Touch uses a short hold, so a swipe on card content pans instead of writing a status.
- Drag preview is a fixed, unscaled overlay. Edge auto-pan affects only the Kanban viewport.
- Drop hit-testing converts visual pointer coordinates back into logical board coordinates using the active scale before selecting a destination column.
- The new move endpoint is protected by the existing origin, rate-limit, and CSRF controls, allowlists statuses, and updates only the existing processing-status field. The existing record save API is unchanged.

Before this change, Kanban drag-and-drop was explicitly disabled and there was no drag library or drag API to preserve. The handle-based implementation is therefore the first production drag mechanism.

## Validation

Local gates:

- `python -m pytest -q`: 197 passed; one third-party deprecation warning.
- `python -m compileall src tests`: passed.
- `git diff --check`: passed.
- JavaScript zoom unit tests: 4 passed.
- Playwright Chromium browser suite: passed.
- `docker build .`: passed.
- `docker compose config`: passed.

Feature PR CI:

- `pytest`: passed.
- `browser`: passed.

Fake-fixture Playwright coverage included 360×800, 390×844, 430×932, mobile landscape, tablet, and desktop; buttons, 100%, fit, persistence after reload, two-touch pinch, one-touch pan, detail open, and handle drop at 65% all passed. Five screenshots were captured locally from fake fixtures and were not committed.

Production read-only browser smoke results:

| Viewport | Controls | Fit | Page horizontal overflow |
| --- | --- | --- | --- |
| 360×800 | Passed | 40% fallback with internal pan | None |
| 390×844 | Passed | 40% fallback with internal pan | None |
| 430×932 | Passed | 40% fallback with internal pan | None |
| 1440×900 | Passed | 40% fallback for the current tall backlog | None |

Production pinch, pan, detail open, scale restoration, and scale-aware drag preview/drop-target checks passed with no browser console errors. The production drag smoke ended with `pointercancel`; no move request was sent.

An anonymized before/after hash of the rendered record identifiers, statuses, and columns was stable across the final smoke. All production smoke requests were read-only, and no real record was changed.

## Rollout and health

A private pre-rollout backup of the old production checkout and dashboard runtime configuration was created with owner-only file permissions. The production checkout was fast-forwarded from the previous commit to the merge commit. Only the dashboard image was rebuilt and only the dashboard container was recreated. The Telegram/backend container retained its original container identity and start time, so no second backend worker was started.

Final dashboard health returned `ok: true` with the configured exact Airtable-field sorting mode. Both dashboard and backend containers were running after smoke verification.

## Risks and blockers

- Moving a card changes a real processing status and may affect downstream processing; the dedicated handle, touch hold, status allowlist, CSRF validation, and cancelled production smoke reduce accidental-write risk.
- Platform accessibility or system gestures may override browser-level pinch handling in some environments.
- A tall production backlog may force fit mode to the 40% minimum before every column and card can fit simultaneously; internal pan is the intentional fallback.
- Remaining blockers: none.
