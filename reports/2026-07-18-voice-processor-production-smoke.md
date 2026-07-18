# Voice Processor Production Smoke

Date: 2026-07-18

Status: blocked before Android processor smoke.

## Commits

- PR #3 head before merge: `53ae187de27c944fc97bb57299b61a019712d71f`
- PR #3 squash merge commit: `7931cc4e981c5ebbba8d0ed4542cc4f5c9f1cae6`
- Previous production commit: `35b6f11ad5a32b651626a5e764a9b2eeac39375d`
- Deployed production commit: `22e28165e1420523a46150ae9f7f014bcff850bb`
- Extra production hotfix after PR merge: `22e2816` ignores runtime `data/*` in Docker build context while preserving `data/incoming/.gitkeep`.

## Pre-Merge Checks

- PR #3 was `MERGEABLE` / `CLEAN` before merge.
- GitHub Actions for PR head `53ae187de27c944fc97bb57299b61a019712d71f`: success.
- Local full pytest: `44 passed, 1 warning`.
- `git diff --check`: clean.
- Tracked/untracked secret sweep: no `.env`, OAuth JSON, access token, or refresh token files were present in the repository. Matches were limited to `.env.example` placeholders and test fixtures.
- `VOICE_PROCESSOR_ENABLED=false` remains the code default.
- `VOICE_PROCESSOR_CREATE_PROJECT_ITEMS=false` remains the code default; processor v1 does not create Projects OS tasks.

## Merge

- PR #3 was marked ready for review and squash-merged into `main`.
- Issue #2 was not closed because the production Android processor smoke did not run.

## Production Deploy

- VPS: `bruce-vps`
- Deployment path: `/opt/voice-inbox-bot`
- Rollback backup: `/home/codex/voice-inbox-bot-deploy-backups/20260718T114235Z`
- Build result: successful after the `.dockerignore` hotfix.
- Restart result: existing `voice-inbox-bot` compose service recreated.
- Container count: 1
- Container command: `python -m app.main`
- Restart count after deploy: 0
- Docker healthcheck: not configured.
- `/health`: `{"ok": true}`
- Android endpoint auth behavior: `POST /api/mobile-inbox/items` without bearer token returns `401`.
- Telegram state: long polling started for the existing production bot after restart.
- Logs after deploy: no `ERROR` or traceback found; Google Drive OAuth token persistence warnings are present.

## Production Settings

Left in production `.env`:

```env
VOICE_PROCESSOR_ENABLED=false
VOICE_PROCESSOR_CREATE_PROJECT_ITEMS=false
VOICE_PROCESSOR_BATCH_SIZE=1
```

No general polling was enabled and no second processor worker was created.

## Airtable Schema Ensure

Required command run on the VPS:

```bash
PYTHONPATH=src .venv/bin/python scripts/ensure_airtable_fields.py
```

Result: failed before any Android processor smoke with Airtable metadata `403`.

The failure occurs on `AirtableClient.ensure_voice_inbox_metadata_fields()` while calling Airtable metadata APIs. The current production `AIRTABLE_TOKEN` can run the existing app but does not have the metadata/schema access required by `scripts/ensure_airtable_fields.py`. Because the processor also uses Airtable metadata to validate select choices, the controlled processor smoke cannot be run safely with the current production credential.

No schema changes were confirmed from the script:

- `Processing` choice: not confirmed by script.
- Processor feedback fields: not confirmed by script.
- `Правила обработки` table: not confirmed by script.
- Existing Airtable records/select choices: no delete/rename operation was run by the script.

## Android Smoke

Not run.

Reason: schema ensure and processor metadata read are blocked by production Airtable metadata `403`. No smoke Airtable record was created, no Android media processing was triggered, and no production records with status `New` were processed.

- Test Airtable record ID: not created.
- Android media paths: not exercised.
- Structured output: not exercised.
- Idempotency: not exercised.
- Projects OS task creation: not exercised; processor stayed disabled.

## Correction Learning

Not run.

Reason: blocked before controlled Android processor smoke. No test rule was created.

## Rollback Commands

Only run these if the deployed app regresses. The service is currently healthy with the processor disabled.

```bash
cd /opt/voice-inbox-bot
cp -a /home/codex/voice-inbox-bot-deploy-backups/20260718T114235Z/.env .env
cp -a /home/codex/voice-inbox-bot-deploy-backups/20260718T114235Z/docker-compose.override.yml docker-compose.override.yml
git switch main
git reset --hard 35b6f11ad5a32b651626a5e764a9b2eeac39375d
docker compose up -d --build --no-deps voice-inbox-bot
curl -fsS http://127.0.0.1:18081/health
```

## Required Next Step

Provide or install a production Airtable token for `AIRTABLE_TOKEN` with the required Airtable metadata/schema access for the Voice Inbox base, plus existing record read/write scopes. Then rerun:

```bash
cd /opt/voice-inbox-bot
PYTHONPATH=src .venv/bin/python scripts/ensure_airtable_fields.py
PYTHONPATH=src .venv/bin/python scripts/ensure_airtable_fields.py
```

After both schema ensure runs pass idempotently, run one controlled Android smoke via a single explicit `--record-id`, then correction learning on that same smoke record.

## Secret Handling

This report contains no secret values, no `.env` contents, no OAuth JSON contents, no access token, and no refresh token.
