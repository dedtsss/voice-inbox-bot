# Voice Processor Production Smoke

Date: 2026-07-18

Status: successful.

## Security note

This public report intentionally omits production Airtable base, table, field and record identifiers, Google Drive file/folder identifiers, private backup paths, message contents and secret values. Detailed operational identifiers remain only in the private deployment environment.

## Scope and safety constraints

Production smoke was executed on the Bruce VPS for `dedtsss/voice-inbox-bot`.

Safety constraints preserved:

- Airtable PAT was retrieved through the approved VPS secret helper and consumed only in memory;
- no PAT value was printed, logged or committed;
- general processor polling remained disabled during controlled smoke;
- no batch run processed unrelated `New` records;
- only dedicated synthetic Android records were processed through explicit record selection;
- correction learning was applied only to the controlled record;
- Projects OS item creation remained disabled;
- temporary media and smoke data were cleaned up.

## Commits

- PR #3 head: `53ae187de27c944fc97bb57299b61a019712d71f`
- PR #3 squash merge: `7931cc4e981c5ebbba8d0ed4542cc4f5c9f1cae6`
- Original deployment hotfix: `22e28165e1420523a46150ae9f7f014bcff850bb`
- Airtable schema compatibility fixes: `ade941b`, `46c61e2`, `2cb4191`
- Idempotency PR #4: `cdeecb62ef3b49512c99dac8e9245f4f04e6dfb0`
- Lazy dependency PR #5: `15d7b2061194b8702b6bf256dbefb3f0cf6fec4d`
- Final production application commit after smoke: `15d7b2061194b8702b6bf256dbefb3f0cf6fec4d`

## Secret handling

The Airtable PAT was obtained from the Bruce VPS secret panel through `bruce-secret` using key `VOICEBOT_AIRTABLE_INBOX`.

Validation:

- secret presence check succeeded;
- value was read inside a non-logging process;
- `.env` was updated without printing the secret;
- a private backup was created before editing;
- final `.env` contained one Airtable token line;
- processor remained disabled, batch size 1, Projects OS creation disabled.

Exact secret values and private backup paths are omitted.

## Production runtime

Final runtime state:

- deployment path: `/opt/voice-inbox-bot`;
- one running `voice-inbox-bot` container;
- restart count: 0;
- `/health`: `{"ok":true}`;
- unauthenticated Android endpoint request: HTTP 401;
- Telegram long polling active;
- Docker healthcheck not configured;
- known non-fatal Drive OAuth persistence warning remained because the token file is mounted read-only.

## Tests

After schema fixes:

- processor tests: 37 passed;
- full suite: 46 passed with one existing Starlette/httpx deprecation warning;
- `git diff --check`: clean.

After idempotency fixes:

- PR #4 CI: passed;
- PR #5 CI: passed;
- full suite: 49 passed with one existing deprecation warning;
- `git diff --check`: clean.

## Airtable schema ensure

`PYTHONPATH=src .venv/bin/python scripts/ensure_airtable_fields.py` completed twice.

First successful run:

- no duplicate metadata fields created;
- `Processing` status choice added;
- rules table created;
- metadata access succeeded.

Second successful run:

- no fields created;
- no choices added;
- existing rules table reused;
- result confirmed idempotency.

Confirmed fields included AI JSON snapshot, confidence, processor version, correction checkbox/comment and training-applied checkbox. Internal Airtable identifiers are omitted.

## Controlled Android text smoke

One synthetic Android text record was created through the live production endpoint and processed inside the compose service with explicit record selection.

Pre-processing:

- type `Text`;
- status `New`;
- source `Android`;
- Drive reference present;
- processing error empty;
- AI JSON empty.

Post-processing:

- same Airtable record updated;
- structured output written;
- project and priority fields populated from allowed values;
- status `Needs Review` because confidence was below the threshold;
- AI JSON snapshot present;
- processor version `v1`;
- no unrelated `New` record processed.

The processor path successfully read the Drive manifest, called OpenAI Structured Outputs and wrote back to the same record.

## Correction learning smoke

The controlled record was manually corrected and explicitly marked for training.

Validation:

- one reusable rule was created;
- rule remained active and auditable;
- training checkbox was cleared;
- training-applied flag set;
- source reference and user comment retained;
- duplicate learning invocation created no second rule.

Internal rule and record identifiers are omitted.

## Idempotency rerun

After PR #4 and PR #5 deployment, the already handled controlled record was run again through explicit record selection.

Result:

- record was skipped;
- no Airtable claim or writeback occurred;
- Drive/OpenAI/media dependencies were not initialized;
- no duplicate Inbox record was created;
- no Projects OS item was created;
- no second correction rule was created.

## Final multimedia smoke

Two synthetic Android records were created:

- audio-only;
- mixed text, photo and video.

Assets were generated inside the production container with ffmpeg. A short synthetic MP3 was used for audio transcription and as the video audio track.

Audio-only result:

- status `Processed`;
- transcription endpoint called;
- strict structured output present;
- source text preserved;
- confidence `0.8`;
- no Projects OS item created.

Mixed result:

- status `Needs Review` due to low confidence, not technical failure;
- payload text read;
- image vision called;
- MP4 classified as video;
- video audio transcribed;
- five representative video frames analyzed;
- strict structured output present;
- temporary processor directories removed.

## Cleanup

Cleanup removed only exact synthetic smoke objects:

- controlled text record;
- audio-only record;
- mixed record;
- correction rule;
- synthetic Drive folders;
- local final-smoke directory.

Post-cleanup validation:

- no active smoke correction rules;
- no smoke records remained under the tested External ID prefixes;
- smoke Drive folders were absent from active listings;
- Projects OS item count was unchanged;
- no real record was updated or deleted.

## Final production checks

- schema ensure remained idempotent on two consecutive runs;
- production commit: `15d7b2061194b8702b6bf256dbefb3f0cf6fec4d`;
- one container;
- restart count 0;
- local and public health OK;
- unauthenticated Android request returned 401;
- Telegram polling active;
- general processor polling still disabled at this stage;
- no recent unexplained errors;
- exact-match secret scans found no hits in Docker logs or tracked Git.

Final safety settings at the end of this smoke:

```text
VOICE_PROCESSOR_ENABLED=false
VOICE_PROCESSOR_CREATE_PROJECT_ITEMS=false
VOICE_PROCESSOR_BATCH_SIZE=1
```

## Final state

The multimodal processor, correction learning, same-record writeback, media handling, idempotency, cleanup and Telegram compatibility passed production smoke. The system was ready for a separate guarded polling rollout. No private production identifiers are included in this public report.
