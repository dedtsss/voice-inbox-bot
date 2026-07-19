# Voice Processor Polling Enablement

Date: 2026-07-19

## Commits

- User-provided verified production commit: `15d7b2061194b8702b6bf256dbefb3f0cf6fec4d`
- Actual production commit at start: `48985a74afa7c7ae7567f09b809d9a611a19a462`
- Code PR: <https://github.com/dedtsss/voice-inbox-bot/pull/6>
- PR head commit: `4579abf868546c166cf1cbe896b654172990ad1c`
- Merge commit deployed to production: `08dbcc30ce5209de1daff6f45b4f0ea96862d433`

## Rollout Settings

- Rollout cutoff UTC: `2026-07-19T02:03:06Z`
- `.env` backup: `/opt/voice-inbox-bot/.env.backup-voice-processor-rollout-20260719T020306Z`
- `VOICE_PROCESSOR_ENABLED=true`
- `VOICE_PROCESSOR_INTERVAL_SECONDS=60`
- `VOICE_PROCESSOR_BATCH_SIZE=1`
- `VOICE_PROCESSOR_CREATE_PROJECT_ITEMS=false`
- `VOICE_PROCESSOR_SOURCE_FILTER=Android`
- `VOICE_PROCESSOR_CREATED_AFTER=2026-07-19T02:03:06Z`

Rollback:

```bash
cd /opt/voice-inbox-bot
cp -p /opt/voice-inbox-bot/.env.backup-voice-processor-rollout-20260719T020306Z .env
docker compose up -d --force-recreate voice-inbox-bot
```

Minimal rollback without restoring the whole backup:

```bash
cd /opt/voice-inbox-bot
python3 - <<'PY'
from pathlib import Path
path = Path(".env")
updates = {
    "VOICE_PROCESSOR_ENABLED": "false",
    "VOICE_PROCESSOR_CREATE_PROJECT_ITEMS": "false",
    "VOICE_PROCESSOR_BATCH_SIZE": "1",
}
lines = []
seen = set()
for line in path.read_text(encoding="utf-8").splitlines():
    stripped = line.strip()
    if stripped and not stripped.startswith("#") and "=" in stripped:
        key = stripped.split("=", 1)[0]
        if key in updates:
            lines.append(f"{key}={updates[key]}")
            seen.add(key)
            continue
    lines.append(line)
for key, value in updates.items():
    if key not in seen:
        lines.append(f"{key}={value}")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
docker compose up -d --force-recreate voice-inbox-bot
```

## Code Guardrails

PR #6 added:

- default `VOICE_PROCESSOR_SOURCE_FILTER=Android`;
- optional `VOICE_PROCESSOR_CREATED_AFTER`;
- strict ISO 8601 UTC validation for `VOICE_PROCESSOR_CREATED_AFTER`;
- Airtable polling formula limited to `Статус обработки = New`, `Источник = Android`, and `CREATED_TIME() > cutoff`;
- defensive in-memory auto-candidate filtering before claim;
- unchanged explicit `--record-id` behavior outside source/cutoff gating.

An invalid cutoff fails `Settings` validation and stops startup instead of silently disabling the filter.

## Pre-Rollout Airtable Backlog

Safe summary only; no real message contents were fetched or recorded.

- `New` total: 16
- `New` with `Источник=Android`: 11
- `New` with another or empty source: 5
- Min `createdTime`: `2026-07-16T10:29:34.000Z`
- Max `createdTime`: `2026-07-17T18:04:45.000Z`

Existing `New` records captured before enablement:

| Record ID | External ID | Source |
| --- | --- | --- |
| `recqX8uv6VQvLB8gz` |  |  |
| `recbvoJIaw7j4Niey` |  |  |
| `reciaNJXMKiJVPqeZ` |  |  |
| `recSURI2JbCAGy0Wf` |  |  |
| `recFeT3vubaiQW5XT` |  |  |
| `reczZgRMAyPiIAlOu` | `smoke-20260717T162231Z-android-text` | `Android` |
| `recgukndWrzwbCRGU` | `smoke-20260717T162231Z-android-mp3` | `Android` |
| `recAKyYQgFmMcfgGe` | `smoke-20260717T162231Z-android-photo` | `Android` |
| `recdQJjLWM94cDG3b` | `smoke-20260717T162231Z-android-multi` | `Android` |
| `rechR9EmRMVjM1X8J` | `smoke-20260717T162231Z-android-repeat` | `Android` |
| `recCuUFlOKVjcd2cL` | `prod-smoke-android-20260717T180328Z-text` | `Android` |
| `recEwrf8xehUMHIWs` | `prod-smoke-android-20260717T180328Z-mp3` | `Android` |
| `recmjR1izRRoqN0sv` | `prod-smoke-android-20260717T180328Z-photo` | `Android` |
| `recaZdOPZUexcxodc` | `prod-smoke-android-20260717T180328Z-video` | `Android` |
| `recUxyHVyCPJw93xy` | `prod-smoke-android-20260717T180328Z-multi` | `Android` |
| `recBOwGrFGqb7HREV` | `prod-smoke-android-20260717T180442Z-same` | `Android` |

Baseline deviation: active smoke rules were absent, but 11 older smoke-tagged Android `New` records were still present. They were not processed or deleted. Final comparison confirmed `changed_count=0` for all 16 pre-rollout `New` records.

## Canary

- Created through production Android endpoint, not `--record-id`.
- External ID: `prod-polling-canary-20260719T020342Z`
- Airtable record ID: `recXy4FsjTsguwPVc`
- Airtable `createdTime`: `2026-07-19T02:03:45.000Z`
- First automatic processor result: `2026-07-19T02:04:21Z`
- Time from endpoint creation to automatic processing: about 39 seconds
- Final canary status before cleanup: `Needs Review`
- AI JSON present: yes, 1489 characters
- Confidence: `0.8`
- Processor version: `v1`
- Projects OS items created for canary: 0
- Canary correction rules created: 0

Idempotency evidence:

- One processor cycle had `needs_review=1`.
- All following observed cycles were zero: `processed=0, needs_review=0, skipped=0, retried=0, failed=0, learned=0`.
- No retry loop appeared in logs.

Cleanup:

- Canary Airtable record deleted: `recXy4FsjTsguwPVc`
- Canary Drive folder trashed: `1odCZdvLnT_lyzkOhXD_4pBI8R5eqCHB0`
- Canary Airtable lookup after cleanup: not found
- Temp `voice_processor_*` dirs after observation: 0

## Observation

Observation window:

- Start: `2026-07-19T02:07:00Z`
- End: `2026-07-19T02:17:00Z`

Production state:

- One container: `voice-inbox-bot`
- One main process: `python -m app.main`
- Restart count: 0
- `/health`: `{"ok": true}`
- Telegram polling active: `Run polling for bot @VoiceTaskNote_Inbox_bot`
- Processor loop started count: 1
- Processor run stats count in observation logs: 14
- `VOICE_PROCESSOR_BATCH_SIZE=1`
- `VOICE_PROCESSOR_CREATE_PROJECT_ITEMS=false`

No unexplained rollout errors were found:

- No Airtable 403/422 in logs.
- No OpenAI authentication errors in logs.
- No Drive download errors in logs.
- No infinite retry loop.
- Known warning observed: `Could not persist refreshed Google Drive OAuth token`; it was present before rollout and comes from the read-only secret mount.

## Validation

Local:

- `.venv/bin/python -m pytest tests/test_voice_processor.py -q`: 49 passed
- `.venv/bin/python -m pytest -q`: 58 passed, 1 Starlette deprecation warning
- `git diff --check`: clean
- `detect-secrets` scan over tracked files with test placeholders excluded: no results
- Additional `git grep` token-pattern scan outside tests and `.env.example`: no results

GitHub Actions:

- PR #6 `pytest`: pass, 28s
- Run: <https://github.com/dedtsss/voice-inbox-bot/actions/runs/29669466759>

Production:

- Schema ensure run 1: no fields created, no choices added, rules table `tbleRJturAl0mqPhN`, `created_rules_table=false`
- Schema ensure run 2: no fields created, no choices added, rules table `tbleRJturAl0mqPhN`, `created_rules_table=false`
- Production commit: `08dbcc30ce5209de1daff6f45b4f0ea96862d433`
- Android endpoint: canary stored successfully and was processed by polling
- Telegram polling: active after rollout
- Old backlog: 16 checked, 0 changed

## Final State

Polling remains enabled.

The active safeguards against old backlog processing are:

- `VOICE_PROCESSOR_SOURCE_FILTER=Android`;
- `VOICE_PROCESSOR_CREATED_AFTER=2026-07-19T02:03:06Z`;
- Airtable formula requiring `Статус обработки = New`;
- defensive in-process auto-candidate filter;
- batch size fixed at 1;
- one production container and one processor loop.

No secrets were printed or committed.
