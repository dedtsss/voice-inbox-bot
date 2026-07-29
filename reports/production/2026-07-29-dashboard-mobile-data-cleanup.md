# Dashboard mobile data cleanup — production report

- UTC date: 2026-07-29
- Feature PR: #30, `Fix mobile dashboard overflow and data cleanup`
- Feature commit: `04821fe800ccecdd6015374023b94686b118e3f9`
- Feature merge and deployed application commit: `df1b5dba1c7bc4cb92ab3c29cb3063f686435aef`

## Mobile dashboard

The record detail page now permits flex and grid children to shrink, wraps long user-controlled text, bounds fields and forms to their panel, and confines horizontal scrolling to technical JSON. The detail hero wraps its action group instead of shrinking a button below its intrinsic width.

Recent-event cards now use a constrained grid. The icon remains left, the title has a two-line clamp, metadata remains below it, and the status pill moves to its own row at narrow widths.

Fake-Airtable Playwright coverage and visual review passed at 360 × 800, 390 × 844, 430 × 932, and 1440 × 900. The production read-only smoke checked the overview and a real long-title detail page at 360 × 800, 390 × 844, and 430 × 932: document/body overflow was zero and all detail panels remained within the viewport. No production screenshots were retained.

## Type strategy

UI and processor options are canonicalized only by trimming and case-folding, with stable ordering. No semantic type was translated or merged with a media/content type. Existing media/content and otherwise legacy values remain visible once as `Устаревшее: …` on the current record and can be replaced by a canonical current option.

The active metadata still contains historical case aliases, but the UI no longer exposes duplicate options. A safe case-only migration was run only after dry-run and a private backup, with a bounded batch size of 1 and post-check:

- `file` → `File`: 1
- `note` → `Note`: 3
- `задача` → `Задача`: 5
- `заметка` → `Заметка`: 2

Total migrated: 11. Legacy semantic/content values such as `Text`, `Voice`, `Video`, and `Mixed` were intentionally not mass-converted because no deterministic semantic mapping is available. Existing values were preserved; no user text, processing status, or AI reprocessing changed.

## Next action semantics

The field is now named **«Следующий конкретный шаг»**. It means one concrete step that follows from the record. It is optional for a note, idea without a decision, or reference information. Empty read-only values render as **«Действие не определено»** and are not a checklist/training error. Existing text was not changed.

## Missing source

The overview label is **«Источник не указан»**, while its link carries the fixed sentinel `source=__empty__`. Server-side code accepts only that literal sentinel and maps it to an Airtable empty-field formula; all other query values remain escaped equality values. It does not accept a query-supplied formula.

Previously the visual label **«Без источника»** was sent as `source=Без источника`; Airtable then searched for that literal text even though the stored field was blank, producing an empty list. The fixed production link opened 25 records on the first page with a next-page control; the overview count was 28. The overview audit was not limited for this dataset, so those counts agree.

## Anonymised missing-source audit

- Records with empty source: 28 of 72 scanned; scan was not limited.
- Created date range: 2026-06-03 through 2026-07-16.
- Statuses: Needs Review 4, New 5, Processed 19.
- Processing route: empty 28.
- External ID prefix/shape: empty 28.
- Google Drive bundle: 0.
- Existing technical-pattern indication: 9.
- Legacy indication from missing processing route: 28.
- Manual origin cannot be determined from stable machine fields.
- Deterministic Android candidates: 0; deterministic Telegram candidates: 0.
- Deterministic source backfill: 0; remaining undetermined: 28.

No source backfill was run because no stable machine marker proved Android or Telegram. The audit and the migration emit aggregate counters only; they do not publish titles, record IDs, external IDs, URLs, filenames, texts, or secrets.

## Validation and rollout

- Python: 208 passed, 1 pre-existing dependency deprecation warning.
- `python -m compileall src tests`, `git diff --check`, `docker build .`, and production-environment `docker compose config` passed.
- GitHub CI for feature PR #30: `pytest` and browser jobs passed.
- A private production backup was made before fast-forward from the prior production commit.
- Dashboard health after rollout: `ok: true`, sorting mode `airtable_field`.
- Exactly one dashboard container and one voice processor container were running after controlled recreate. No subscription worker was started.

No blockers remained at the time of this report.
