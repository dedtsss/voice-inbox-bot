# Voice Processor Polling Enablement

Date: 2026-07-19

## Security note

This public report intentionally omits production Airtable base, table, field and record identifiers, Google Drive folder identifiers, message contents and secret values. Detailed identifiers remain only in the private deployment environment.

## Commits

- Previously verified production application commit: `15d7b2061194b8702b6bf256dbefb3f0cf6fec4d`
- Actual production commit at rollout start: `48985a74afa7c7ae7567f09b809d9a611a19a462`
- Code PR: #6
- PR head commit: `4579abf868546c166cf1cbe896b654172990ad1c`
- Merge commit deployed to production: `08dbcc30ce5209de1daff6f45b4f0ea96862d433`

## Rollout settings

- Rollout cutoff UTC: `2026-07-19T02:03:06Z`
- `VOICE_PROCESSOR_ENABLED=true`
- `VOICE_PROCESSOR_INTERVAL_SECONDS=60`
- `VOICE_PROCESSOR_BATCH_SIZE=1`
- `VOICE_PROCESSOR_CREATE_PROJECT_ITEMS=false`
- `VOICE_PROCESSOR_SOURCE_FILTER=Android`
- `VOICE_PROCESSOR_CREATED_AFTER=2026-07-19T02:03:06Z`

A private `.env` backup was created on the VPS before rollout. Its exact path is intentionally omitted.

Rollback procedure:

```bash
cd /opt/voice-inbox-bot
# restore the private pre-rollout .env backup
# then recreate the single service container
docker compose up -d --force-recreate voice-inbox-bot
```

Minimal rollback:

```bash
cd /opt/voice-inbox-bot
python3 - <<'PY'
from pathlib import Path

path = Path('.env')
updates = {
    'VOICE_PROCESSOR_ENABLED': 'false',
    'VOICE_PROCESSOR_CREATE_PROJECT_ITEMS': 'false',
    'VOICE_PROCESSOR_BATCH_SIZE': '1',
}

lines = []
seen = set()
for line in path.read_text(encoding='utf-8').splitlines():
    stripped = line.strip()
    if stripped and not stripped.startswith('#') and '=' in stripped:
        key = stripped.split('=', 1)[0]
        if key in updates:
            lines.append(f'{key}={updates[key]}')
            seen.add(key)
            continue
    lines.append(line)

for key, value in updates.items():
    if key not in seen:
        lines.append(f'{key}={value}')

path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
PY

docker compose up -d --force-recreate voice-inbox-bot
```

## Code guardrails

PR #6 added:

- default `VOICE_PROCESSOR_SOURCE_FILTER=Android`;
- optional `VOICE_PROCESSOR_CREATED_AFTER`;
- strict ISO 8601 UTC validation for the cutoff;
- Airtable polling restricted to `New`, source `Android`, and creation time after the cutoff;
- defensive in-process candidate filtering before claim;
- unchanged explicit `--record-id` behavior outside automatic rollout gating.

An invalid cutoff fails settings validation and stops startup instead of silently disabling the filter.

## Pre-rollout Airtable backlog

Safe summary only; no message contents or internal identifiers are included.

- `New` total: 16
- `New` with source `Android`: 11
- `New` with another or empty source: 5
- Earliest creation time: `2026-07-16T10:29:34.000Z`
- Latest creation time: `2026-07-17T18:04:45.000Z`

The complete set of 16 pre-rollout records was captured privately before enablement. Final comparison confirmed `changed_count=0`; no old backlog record was processed, rewritten or deleted.

## Canary

- Created through the production Android endpoint, not through `--record-id`.
- Created after the rollout cutoff.
- First automatic processor result arrived approximately 39 seconds after endpoint creation.
- Final status before cleanup: `Needs Review`.
- AI JSON present: yes.
- Confidence: `0.8`.
- Processor version: `v1`.
- Projects OS items created: 0.
- Correction rules created: 0.

Idempotency evidence:

- One processor cycle produced `needs_review=1`.
- All following observed cycles were zero for processed, needs-review, skipped, retried, failed and learned counts.
- No retry loop appeared in logs.

Cleanup:

- Canary Airtable record deleted.
- Canary Google Drive folder moved to trash.
- Canary lookup after cleanup returned not found.
- Temporary `voice_processor_*` directories after observation: 0.

## Observation

Observation window:

- Start: `2026-07-19T02:07:00Z`
- End: `2026-07-19T02:17:00Z`

Production state:

- One container: `voice-inbox-bot`.
- One main process: `python -m app.main`.
- Restart count: 0.
- `/health`: `{"ok": true}`.
- Telegram polling active.
- Processor loop started count: 1.
- Processor run stats count in observation logs: 14.
- `VOICE_PROCESSOR_BATCH_SIZE=1`.
- `VOICE_PROCESSOR_CREATE_PROJECT_ITEMS=false`.

No unexplained rollout errors were found:

- no Airtable 403/422 errors;
- no OpenAI authentication errors;
- no Drive download errors;
- no infinite retry loop.

Known warning: refreshed Google Drive OAuth token could not be persisted because the token file is mounted read-only. Runtime refresh continued to work in memory; this warning existed before rollout.

## Validation

Local:

- processor tests: 49 passed;
- full test suite: 58 passed with one existing Starlette deprecation warning;
- `git diff --check`: clean;
- tracked-file secret scans: no findings.

GitHub Actions:

- PR #6 `pytest`: passed.

Production:

- Schema ensure run 1: no fields or choices created; rules schema already existed.
- Schema ensure run 2: same idempotent result.
- Production application commit: `08dbcc30ce5209de1daff6f45b4f0ea96862d433`.
- Android endpoint stored the canary successfully.
- Polling processed the canary automatically.
- Telegram polling remained active.
- Old backlog: 16 checked, 0 changed.

## Final state

Polling remains enabled.

Active safeguards:

- source filter `Android`;
- cutoff `2026-07-19T02:03:06Z`;
- Airtable query requires status `New`;
- defensive in-process candidate filter;
- batch size fixed at 1;
- one production container and one processor loop;
- Projects OS creation disabled.

No secrets or private production identifiers are included in this report.
