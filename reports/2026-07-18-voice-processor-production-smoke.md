# Voice Processor Production Smoke

Date: 2026-07-18

Status: successful.

## Scope

Production smoke was continued on VPS `bruce-vps` for `dedtsss/voice-inbox-bot`.

Required safety constraints were preserved:

- no Airtable PAT value was printed, logged, committed, or included in this report;
- no general processor polling was enabled;
- no batch processor run was used for `New` records;
- only one controlled Android record was processed through an explicit `--record-id`;
- correction learning was applied only to the same controlled test record;
- production processor was left disabled.

## Commits

- PR #3 head before merge: `53ae187de27c944fc97bb57299b61a019712d71f`
- PR #3 squash merge commit: `7931cc4e981c5ebbba8d0ed4542cc4f5c9f1cae6`
- Previous production commit before the original smoke: `35b6f11ad5a32b651626a5e764a9b2eeac39375d`
- Original deployed production commit: `22e28165e1420523a46150ae9f7f014bcff850bb`
- Blocked-smoke report commit: `425250b58e629cc0db2f7c87b5c48b66ca3eb565`
- Airtable metadata hotfixes:
  - `ade941b` - checkbox field create options
  - `46c61e2` - explicit color for added select choices
  - `2cb4191` - typecast fallback for `Processing` select choice
- Current production commit after successful smoke: `2cb4191`
- Final idempotency fixes:
  - PR #4: `cdeecb6` - skip already handled processor records
  - PR #5: `15d7b20` - lazy init processor media dependencies
- Final production commit after multimedia smoke and cleanup: `15d7b2061194b8702b6bf256dbefb3f0cf6fec4d`

## Secret Handling

The new Airtable PAT was retrieved from the Bruce VPS secrets panel, not from Airtable Builder Hub.

Checked mechanism:

- approved helper: `bruce-secret`
- key: `VOICEBOT_AIRTABLE_INBOX`
- presence check: `bruce-secret exists VOICEBOT_AIRTABLE_INBOX` returned `ok`
- value retrieval: consumed inside a non-logging Python process via `bruce-secret get VOICEBOT_AIRTABLE_INBOX`

The value was used only in memory to rewrite `/opt/voice-inbox-bot/.env`.

Backup created before editing:

- `/home/codex/voice-inbox-bot-deploy-backups/20260718T125155Z-airtable-token/.env`

Final `.env` checks:

```text
AIRTABLE_TOKEN: lines=1 present=True
VOICE_PROCESSOR_ENABLED: lines=1 value=false
VOICE_PROCESSOR_CREATE_PROJECT_ITEMS: lines=1 value=false
VOICE_PROCESSOR_BATCH_SIZE: lines=1 value=1
```

## Production Runtime

Deployment path: `/opt/voice-inbox-bot`

The existing `voice-inbox-bot` compose service was recreated after the secret update and after each code hotfix. Final state:

```text
/health: {"ok":true}
container status: running
restart count: 0
Android unauthenticated POST /api/mobile-inbox/items: 401
running containers matching voice-inbox-bot: one main container
```

Telegram long polling after final recreate:

```text
Starting Telegram long polling
Start polling
Run polling for bot @VoiceTaskNote_Inbox_bot
```

Docker healthcheck remains not configured.

Known non-fatal warning remains present: Google Drive OAuth refresh works in memory, but the refreshed token cannot be persisted because the token file is mounted read-only.

## Local Tests

Run after schema hotfixes:

```text
tests/test_voice_processor.py: 37 passed
full pytest: 46 passed, 1 Starlette/httpx deprecation warning
git diff --check: clean
```

Final local/CI run after idempotency fixes:

```text
PR #4 GitHub Actions pytest: pass
PR #5 GitHub Actions pytest: pass
full pytest after PR #5: 49 passed, 1 Starlette/httpx deprecation warning
git diff --check: clean
```

## Airtable Schema Ensure

Command required by the smoke:

```bash
PYTHONPATH=src .venv/bin/python scripts/ensure_airtable_fields.py
```

The old metadata `403` is gone. After code hotfixes, two consecutive runs completed:

First successful run:

```json
{
  "metadata_created": {},
  "processor_schema": {
    "created_fields": {},
    "added_status_choices": ["Processing"],
    "rules_table_id": "tbleRJturAl0mqPhN",
    "created_rules_table": true
  }
}
```

Second successful run:

```json
{
  "metadata_created": {},
  "processor_schema": {
    "created_fields": {},
    "added_status_choices": [],
    "rules_table_id": "tbleRJturAl0mqPhN",
    "created_rules_table": false
  }
}
```

Confirmed through metadata after the runs:

- metadata API access: ok
- `Processing` status choice: present
- feedback fields present:
  - `AI результат JSON`
  - `Уверенность AI`
  - `Версия обработчика`
  - `Обучить на исправлении`
  - `Комментарий к исправлению`
  - `Обучение учтено`
- table `Правила обработки`: present as `tbleRJturAl0mqPhN`
- core rules-table fields present, including `Правило`, `Активно`, `Область`, `Условие`, `Правильное решение`, `Источник записи`
- typecast fallback canary record remaining in Inbox: `0`
- second run was idempotent

## Controlled Android Smoke

Created one new Android record through the live production endpoint using the existing `MOBILE_INBOX_TOKEN` from `.env`, consumed only in memory.

Android create result:

```text
http_status=200
ok=True
status=stored
remote_id=recfio6WnUcvJjg2a
item_id=prod-smoke-processor-20260718T130308Z-android-text
```

Pre-processor state:

```text
Тип=Text
Статус обработки=New
Источник=Android
External ID=prod-smoke-processor-20260718T130308Z-android-text
Google Drive=present
Ошибка обработки=empty
AI результат JSON=empty
```

The host-side processor command was not used because production `.env` points Google credentials to container paths under `/run/secrets`. The controlled processor was therefore run inside compose:

```bash
docker compose run --rm --no-deps voice-inbox-bot \
  python -m app.voice_processor --record-id recfio6WnUcvJjg2a --ignore-enabled-flag
```

Result:

```text
Voice processor completed record recfio6WnUcvJjg2a with result=needs_review
```

Post-processor state:

```text
Название=Production smoke 2026-07-18
Тип=Text
Проект=Финансы
Приоритет=средний
Статус обработки=Needs Review
Очищенный текст=Проект Финансы. Тип задача. Приоритет средний. Нужно проверить тестовый счет и записать результат проверки.
Краткое содержание=Необходимо проверить тестовый счет и записать результат проверки.
Следующее действие=Проверить тестовый счет и записать результат проверки.
Уверенность AI=0
Версия обработчика=v1
Ошибка обработки=empty
AI результат JSON=present
```

Snapshot details:

```text
snapshot_status=Needs Review
snapshot_project=Финансы
snapshot_type=Text
snapshot_priority=средний
snapshot_confidence=0.0
snapshot_reasons=['Приоритет и тип задачи не были указаны явно в записи.', 'Низкая уверенность AI']
media_trace={'manifest_item_id': 'prod-smoke-processor-20260718T130308Z-android-text', 'source': 'android', 'manifest_type': 'text', 'files': [], 'audio_files': 0, 'image_files': 0, 'video_files': 0, 'video_frames': 0}
```

The smoke is considered successful for the processor path: the exact record was claimed, Drive manifest metadata was read, OpenAI structured output completed, Airtable writeback happened on the same record, and no other `New` record was batch-processed.

## Correction Learning Smoke

Correction was applied to the same record `recfio6WnUcvJjg2a`.

Manual correction before learning:

- `Тип`: `задача`
- `Приоритет`: `Высокий`
- `Обучить на исправлении`: checked
- `Комментарий к исправлению`: `Production smoke correction: classify this controlled smoke as a high-priority task.`

Learning was invoked directly for the same fetched record through `VoiceInboxProcessor.apply_correction_learning(record)`. The batch CLI `--once` was not used.

Learning result:

```text
learning_applied=True
Обучить на исправлении=empty
Обучение учтено=True
Ошибка обработки=voice_processor learning rule created
rules_for_record=1
rule_id=reci0uxGl8qZQSV4q
rule_active=True
rule_area=Тип
rule_type=задача
rule_decision={"priority": "Высокий", "type": "задача"}
```

Final controlled record state:

```text
External ID=prod-smoke-processor-20260718T130308Z-android-text
Источник=Android
Google Drive=present
Тип=задача
Проект=Финансы
Приоритет=Высокий
Статус обработки=Needs Review
Уверенность AI=0
Версия обработчика=v1
AI результат JSON=present
Обучить на исправлении=empty
Обучение учтено=True
Ошибка обработки=voice_processor learning rule created
```

## Idempotency Rerun

Before deleting smoke data, the old controlled record was rerun only with an explicit record ID after PR #4 and PR #5 were merged and deployed.

Command:

```bash
docker compose exec -T voice-inbox-bot \
  python -m app.voice_processor --record-id recfio6WnUcvJjg2a --ignore-enabled-flag
```

Result:

```text
Voice processor skipped already handled record recfio6WnUcvJjg2a
Voice processor completed record recfio6WnUcvJjg2a with result=skipped
```

Post-rerun checks:

```text
old_record_status_after=Needs Review
old_external_count_after=1
rules_for_old_record_after=1
rules_for_old_record_ids_after=reci0uxGl8qZQSV4q
```

No new Inbox record was created, no Projects OS item was created, no second correction rule was created, and the existing record was not reprocessed. The first deployed idempotency fix prevented Airtable claim/writeback for handled statuses; the second fix made the skip avoid Drive/OpenAI/media dependency initialization.

Duplicate correction-learning was invoked directly for the same record without a new user correction:

```text
duplicate_learning_result=False
rules_before_duplicate_learning=1
rules_after_duplicate_learning=1
rule_ids_after_duplicate_learning=reci0uxGl8qZQSV4q
```

## Final Multimedia Smoke

Two fully synthetic Android records were created through `https://voice-inbox.bruce-group.net/api/mobile-inbox/items` using `MOBILE_INBOX_TOKEN` only in memory. Local TTS was unavailable on Bruce, so one OpenAI TTS call generated a short MP3 that was reused for the audio-only smoke and as the MP4 audio track. PNG and MP4 assets were generated locally inside the production container under `/app/data/final-smoke` with `ffmpeg`.

Created records:

| Case | Airtable record | External ID | Result |
| --- | --- | --- | --- |
| Audio-only | `recA18TcC64pXmZZQ` | `prod-final-smoke-20260719T002716Z-audio-only` | `Processed` |
| Mixed text/photo/video | `rec9GDZKbfAMeRlA4` | `prod-final-smoke-20260719T002716Z-mixed` | `Needs Review` |

Asset checks:

```text
smoke_tts.mp3 size=98688
photo.png size=10066
clip.mp4 size=69779
clip_audio_codec=aac
clip_video_stream=h264,640,360
```

Each record was processed separately with explicit `--record-id`:

```bash
docker compose exec -T voice-inbox-bot \
  python -m app.voice_processor --record-id recA18TcC64pXmZZQ --ignore-enabled-flag

docker compose exec -T voice-inbox-bot \
  python -m app.voice_processor --record-id rec9GDZKbfAMeRlA4 --ignore-enabled-flag
```

Processor results:

```text
recA18TcC64pXmZZQ: result=processed
rec9GDZKbfAMeRlA4: result=needs_review
Projects OS Items count before=39
Projects OS Items count after=39
```

Audio-only verification:

```text
status=Processed
type_field=Задача
project_field=Мастерская
confidence=0.8
audio transcription endpoint: called
structured output: present
source_text_has_dispatcher_phrase=True
media_trace={"audio_files": 1, "files": [{"drive_file_id": "15OVHhXg9AtsErY3hKZCpRA2-sWVAMhkx", "mime_type": "audio/mpeg", "name": "smoke_tts.mp3", "size": 98688}], "image_files": 0, "manifest_item_id": "prod-final-smoke-20260719T002716Z-audio-only", "manifest_type": "voice", "source": "android", "video_files": 0, "video_frames": 0}
```

Mixed verification:

```text
status=Needs Review
type_field=задача
project_field=Мастерская
confidence=0
payload text read: true
image vision endpoint: called
video classified as video: true
video audio transcription endpoint: called
video frame vision endpoint: called
structured output: present
source_text_has_mixed_text=True
temp voice_processor dirs after run=0
media_trace={"audio_files": 0, "files": [{"drive_file_id": "1m1njoObTXSymJb8mDAO2OtQel5wfGuzB", "mime_type": "image/png", "name": "photo.png", "size": 10066}, {"drive_file_id": "1MvZQKJN24hRirSHq-HwccTBAhmbP7u2S", "mime_type": "video/mp4", "name": "clip.mp4", "size": 69779}], "image_files": 1, "manifest_item_id": "prod-final-smoke-20260719T002716Z-mixed", "manifest_type": "mixed", "source": "android", "video_files": 1, "video_frames": 5}
```

`Needs Review` for the mixed record was accepted because the technical media path succeeded and review was due to low confidence, not missing media.

## Cleanup

Deleted or removed exact smoke objects only:

```text
deleted_rule_id=reci0uxGl8qZQSV4q deleted=True
deleted_record label=old-text record_id=recfio6WnUcvJjg2a deleted=True
deleted_record label=audio-only record_id=recA18TcC64pXmZZQ deleted=True
deleted_record label=mixed record_id=rec9GDZKbfAMeRlA4 deleted=True
local_final_smoke_dir_exists=no
```

Smoke Google Drive folders were validated by exact External ID in the folder name and then trashed:

```text
1xCWZp4jpb3bnY5gXdR41TKYP9M3Eff4Z: 2026-07-18_prod-smoke-processor-20260718T130308Z-android-text trashed=True visible_count=0
1090K00jIcQzLIT3uqb0V-HH7iRCzRNx9: 2026-07-19_prod-final-smoke-20260719T002716Z-audio-only trashed=True visible_count=0
1ProVjiqqwD6haKnSUSWC1SClr3QZRLTf: 2026-07-19_prod-final-smoke-20260719T002716Z-mixed trashed=True visible_count=0
```

Post-cleanup checks:

```text
Inbox External ID prefix prod-smoke-processor- count=0
Inbox External ID prefix prod-final-smoke- count=0
rules_for_smoke_record_ids_count=0
active_smoke_rules_count=0
Projects OS Items count final=39
```

Airtable exact fetches for deleted record IDs returned `403` after deletion; prefix scans and the delete API responses confirmed the smoke records/rule were gone from active table data. No real records were updated by cleanup; deletion was limited to the exact record IDs above and Drive folders whose names contained the exact smoke External IDs.

## Final Tests

Final production checks after cleanup:

```text
schema ensure #1: created_fields={}, added_status_choices=[], rules_table_id=tbleRJturAl0mqPhN, created_rules_table=false
schema ensure #2: created_fields={}, added_status_choices=[], rules_table_id=tbleRJturAl0mqPhN, created_rules_table=false
commit=15d7b2061194b8702b6bf256dbefb3f0cf6fec4d
container_count=1
restart_count=0
local_health={"ok":true}
public_health={"ok":true}
android_unauth_status=401
telegram_polling_log_count=3
processor_loop_log_count=0
recent_error_log_count=0
```

Final `.env` safety state:

```text
VOICE_PROCESSOR_ENABLED=false
VOICE_PROCESSOR_CREATE_PROJECT_ITEMS=false
VOICE_PROCESSOR_BATCH_SIZE=1
```

Secret scan:

```text
secret_values_scanned=4
docker_logs_exact_secret_hits=0
tracked_git_exact_secret_hits=0
```

## Final State

Production remains safe:

- `VOICE_PROCESSOR_ENABLED=false`
- `VOICE_PROCESSOR_CREATE_PROJECT_ITEMS=false`
- `VOICE_PROCESSOR_BATCH_SIZE=1`
- main container: running
- restart count: 0
- `/health`: ok
- Android endpoint auth boundary: 401 without bearer
- Telegram long polling: active
- no active smoke rules remain
- no Inbox records remain with `External ID` prefix `prod-smoke-processor-` or `prod-final-smoke-`
- smoke Google Drive folders are trashed and absent from `trashed=false` listing
- no general processor polling enabled
- no batch processing of unrelated `New` records was run
- system is ready for a separate controlled processor polling enablement

## Issue

Issue #2 can remain closed with this final smoke and cleanup result.

## Secret Checks

This report contains no Airtable PAT value, no mobile bearer token, no OpenAI key, no Telegram token, no OAuth client secret, no OAuth access token, and no OAuth refresh token.
