# Voice Inbox Telegram Bot

Личный Telegram-бот для входящего потока заметок: голос, текст, фото с подписью, файлы.

MVP-логика:

1. Принимает сообщение в Telegram.
2. Для голосовых скачивает файл и конвертирует через `ffmpeg` в MP3 16 kHz mono.
3. Распознаёт речь через OpenAI Speech-to-Text.
4. Чистит и структурирует текст через OpenAI.
5. Всегда пишет запись в Airtable `Voice Inbox`.
6. Если проект определён уверенно, дополнительно создаёт запись в `Projects OS / Items`.
7. Возвращает краткую карточку в Telegram.
8. Принимает записи из Android Dispatcher по HTTPS/HTTP API и сохраняет их в тот же Airtable `Voice Inbox`.
9. Опционально запускает multimodal processor для Android raw-записей из Google Drive, если `VOICE_PROCESSOR_ENABLED=true`.

## Быстрый старт

### 1. Создать Telegram-бота

В Telegram открыть `@BotFather`:

```text
/newbot
```

Сохранить токен вида:

```text
123456789:AA...
```

### 2. Узнать свой Telegram ID

После запуска бота отправить ему:

```text
/id
```

Или заранее использовать любого бота для определения user ID.

### 3. Создать `.env`

```bash
cp .env.example .env
```

Заполнить:

```env
TELEGRAM_BOT_TOKEN=
ALLOWED_TELEGRAM_USER_IDS=
OPENAI_API_KEY=
AIRTABLE_TOKEN=
MOBILE_INBOX_TOKEN=
```

Airtable-токен должен иметь доступ на запись в базы:

- `Voice Inbox`
- `Projects OS`

Минимальные права: `data.records:read`, `data.records:write`.

`MOBILE_INBOX_TOKEN` должен быть случайным секретом не короче 32 байт. Сгенерировать можно так:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 4. Запустить через Docker

```bash
docker compose up -d --build
```

Проверить логи:

```bash
docker compose logs -f
```

### 5. Проверить

В Telegram отправить:

```text
Проверка. Добавь в проект DIY-камера задачу: проверить UVC-модуль IMX678.
```

Потом отправить голосовую заметку.

## Что важно

- Бот работает через long polling, вебхук и домен на MVP не нужны.
- HTTP API запускается в том же контейнере и слушает `HTTP_PORT`, по умолчанию `8080`.
- Для production Android должен использовать HTTPS URL reverse proxy или Cloudflare Tunnel, не прямой HTTP.
- Доступ ограничивается `ALLOWED_TELEGRAM_USER_IDS`.
- Telegram-фото в текущем pipeline по-прежнему обрабатываются по подписи, чтобы не менять работающий Telegram ingest.
- Android raw-записи могут обрабатываться отдельным Drive-based processor, но он выключен по умолчанию.
- Если проект не найден в Airtable, запись остаётся только в `Voice Inbox`.
- `Voice Inbox / Проект` — `singleSelect`: код пишет туда только имя существующего choice, не record ID из `Projects OS`.
- Для `Projects OS / Items` типы приводятся к уже существующим значениям Airtable.
- Android-записи при `ANDROID_RAW_MODE=true` сохраняются сырыми: OpenAI-транскрипция и структурирование для них не запускаются.

## Android HTTP API

Проверка:

```http
GET /health
```

Ответ:

```json
{"ok": true}
```

Создание записи:

```http
POST /api/mobile-inbox/items
Authorization: Bearer <MOBILE_INBOX_TOKEN>
Content-Type: multipart/form-data
```

Parts:

- `payload` — JSON string.
- `files[]` — 0..N файлов.

Успешный ответ:

```json
{
  "ok": true,
  "remote_id": "rec...",
  "status": "stored"
}
```

Android-вход пишет запись в Airtable `Voice Inbox / Inbox`:

- `Название`: первые слова текста или `Android: <тип> <дата-время>`.
- `Исходная фраза`: текст из `payload`, если он есть.
- `Тип`: `Text`, `Voice`, `Photo`, `Video`, `File` или `Mixed`.
- `Статус обработки`: `New`.
- `Notes`: источник `Android Dispatcher` и краткая техническая информация.
- `Attachments`: файлы, загруженные через Airtable Upload Attachment API.

Ограничения задаются env:

- `MOBILE_INBOX_MAX_FILE_BYTES` — максимальный размер одного файла. По умолчанию `5000000`, чтобы соответствовать лимиту direct upload Airtable.
- `MOBILE_INBOX_MAX_FILES` — максимум файлов в одном запросе.
- `MOBILE_INBOX_ALLOWED_MIME_TYPES` — allow-list MIME-типов.
- `MOBILE_INBOX_MAX_REQUEST_BYTES` — общий лимит multipart-запроса.
- `MOBILE_INBOX_MAX_PAYLOAD_BYTES` — лимит JSON payload.
- `HTTP_PUBLISHED_PORT` — localhost-порт Docker host для reverse proxy, по умолчанию `8080`.
- `AIRTABLE_UPLOAD_BASE_URL` — host Airtable Upload Attachment API, по умолчанию `https://content.airtable.com/v0`.
- `GOOGLE_DRIVE_ENABLED` — включает сохранение оригиналов в Google Drive.
- `GOOGLE_DRIVE_ROOT_FOLDER_ID` — родительская папка Google Drive для входящих подпапок.
- `GOOGLE_DRIVE_CREDENTIALS_FILE` и `GOOGLE_DRIVE_TOKEN_FILE` — OAuth/service-account файлы внутри контейнера.
- `GOOGLE_DRIVE_SPOOL_DIR` — локальный защищённый spool на случай временной ошибки Drive.

## Google Drive originals

Когда `GOOGLE_DRIVE_ENABLED=true`, для каждого входящего Android или Telegram элемента создаётся папка:

```text
<GOOGLE_DRIVE_ROOT_FOLDER_ID>/<YYYY-MM-DD>_<item_id>/
```

Внутри сохраняются:

- `manifest.json` с `item_id`, source, type, text, Drive file IDs, size и SHA-256 для оригиналов;
- оригинальные файлы без перекодирования;
- Telegram audio дополнительно конвертируется во временный MP3 только для текущей OpenAI-транскрипции, но в Drive кладётся оригинал.

Новые поля Airtable `Voice Inbox / Inbox`:

- `External ID` — ключ идемпотентности.
- `Google Drive` — URL папки входящей записи.
- `Источник` — `Android` или `Telegram`.
- `Ошибка обработки` — последняя техническая ошибка.

Если Airtable token имеет schema permissions, поля можно создать так:

```bash
PYTHONPATH=src python scripts/ensure_airtable_fields.py
```

Этот script idempotent: он добавляет metadata поля Drive ingest, feedback поля processor, choice `Processing` в `Статус обработки` и таблицу `Правила обработки`, если они ещё отсутствуют.

## Multimodal Voice Processor

Processor живёт в этом же backend и использует существующие Airtable, Google Drive и OpenAI credentials. При `VOICE_PROCESSOR_ENABLED=false` он не создаётся и не запускает Drive/OpenAI код.

Что делает worker:

1. Берёт не больше `VOICE_PROCESSOR_BATCH_SIZE` записей `Voice Inbox / Inbox` со `Статус обработки = New` или stale `Processing`.
2. Claims запись через заранее существующий choice `Processing`, lock trace и bounded attempt count в `Ошибка обработки`.
3. Читает Drive folder URL, `manifest.json` и потоково скачивает оригиналы во временную директорию с лимитами размера и проверкой size/SHA-256.
4. Обрабатывает text/audio/photo/video/mixed: audio transcription, vision analysis для images, video audio + representative frames.
5. Отправляет итоговый контекст в OpenAI Structured Outputs со strict JSON Schema.
6. Валидирует project по choices самого поля `Voice Inbox / Проект` и остальные select values по текущим Airtable options.
7. Обновляет ту же Airtable запись, пишет в `Проект` имя singleSelect choice, сохраняет `AI результат JSON`, confidence и processor version.
8. При низкой уверенности, неизвестном проекте/type/select conflict ставит `Needs Review`.
9. Создаёт persistent learning rule только если пользователь явно отметил `Обучить на исправлении`.

Processor не создаёт Projects OS tasks в первой версии. `VOICE_PROCESSOR_CREATE_PROJECT_ITEMS=false` оставлен как future guard; legacy alias `PROCESSOR_CREATE_PROJECT_ITEMS=false` тоже принимается.

V1 processor рассчитан строго на один running worker. Текущий lock trace нужен для recovery и диагностики, но не является атомарной межпроцессной блокировкой Airtable. Не запускайте второй контейнер, `docker compose --scale`, cron-копию или ручной batch параллельно с включённым `VOICE_PROCESSOR_ENABLED=true`.

### Processor env

```env
VOICE_PROCESSOR_ENABLED=false
VOICE_PROCESSOR_INTERVAL_SECONDS=60
VOICE_PROCESSOR_BATCH_SIZE=5
VOICE_PROCESSOR_TEXT_MODEL=gpt-4o-mini
VOICE_PROCESSOR_TRANSCRIPTION_MODEL=gpt-4o-transcribe
VOICE_PROCESSOR_CONFIDENCE_THRESHOLD=0.80
VOICE_PROCESSOR_MAX_VIDEO_FRAMES=12
VOICE_PROCESSOR_VIDEO_FRAME_INTERVAL_SECONDS=5
VOICE_PROCESSOR_CREATE_PROJECT_ITEMS=false
VOICE_PROCESSOR_VERSION=v1
VOICE_PROCESSOR_STALE_PROCESSING_SECONDS=900
VOICE_PROCESSOR_MAX_RETRIES=3
VOICE_PROCESSOR_RETRY_BASE_SECONDS=1
VOICE_PROCESSOR_MAX_PROMPT_CHARS=24000
VOICE_PROCESSOR_MAX_RULES=8
VOICE_PROCESSOR_MAX_FILE_BYTES=25000000
VOICE_PROCESSOR_MAX_RECORD_BYTES=50000000
VOICE_PROCESSOR_MAX_IMAGE_BYTES=4000000
VOICE_PROCESSOR_IMAGE_MAX_EDGE=1600
VOICE_PROCESSOR_RULES_TABLE_ID=
VOICE_PROCESSOR_RULES_TABLE_NAME=Правила обработки
VOICE_FIELD_DUE_DATE=Срок
VOICE_FIELD_COUNTERPARTY=Контрагент
VOICE_FIELD_AMOUNT=Сумма
VOICE_FIELD_PERIOD=Период
VOICE_FIELD_AI_RESULT_JSON=AI результат JSON
VOICE_FIELD_AI_CONFIDENCE=Уверенность AI
VOICE_FIELD_PROCESSOR_VERSION=Версия обработчика
VOICE_FIELD_TRAIN_ON_CORRECTION=Обучить на исправлении
VOICE_FIELD_CORRECTION_COMMENT=Комментарий к исправлению
VOICE_FIELD_TRAINING_APPLIED=Обучение учтено
VOICE_FIELD_PROCESSING_STATUS_QUERY_NAME=Статус обработки
```

### Commands

Run one disabled no-op check:

```bash
PYTHONPATH=src python -m app.voice_processor --once
```

Run exactly one controlled Airtable smoke record while global polling remains disabled:

```bash
PYTHONPATH=src python -m app.voice_processor \
  --record-id recXXXXXXXXXXXXXX \
  --ignore-enabled-flag
```

Run one batch manually:

```bash
PYTHONPATH=src python -m app.voice_processor \
  --once \
  --batch-size 1 \
  --ignore-enabled-flag
```

In Docker:

```bash
docker compose run --rm voice-inbox-bot \
  python -m app.voice_processor --record-id recXXXXXXXXXXXXXX --ignore-enabled-flag
```

### Correction learning UX

1. Processor writes `AI результат JSON` before user edits.
2. User manually fixes Airtable structured fields.
3. User checks `Обучить на исправлении` only when the correction should become a reusable rule.
4. Processor compares current fields with the AI snapshot, creates one concise active rule in `Правила обработки`, sets `Обучение учтено = true`, and clears `Обучить на исправлении`.
5. Manual edits without the checkbox are ignored by learning.

### Safe deploy

1. Merge and deploy with `VOICE_PROCESSOR_ENABLED=false`.
2. Run `PYTHONPATH=src python scripts/ensure_airtable_fields.py` once with an Airtable token that has schema permissions, so `Processing` is an explicit existing select choice.
3. Restart the container and verify Telegram plus `/health`.
4. Create or choose one controlled smoke record with Drive originals.
5. Run the one-record command with `--record-id ... --ignore-enabled-flag`.
6. Confirm same Airtable record was updated, `AI результат JSON` is present, temp media is gone, and no duplicate processing happened.
7. Confirm there is only one processor instance for the deployment.
8. Only after smoke passes, set `VOICE_PROCESSOR_ENABLED=true` with `VOICE_PROCESSOR_BATCH_SIZE=1`, then increase cautiously.

### Rollback

```bash
docker compose down
git checkout <previous_good_commit>
docker compose up -d --build
```

Fast disable without code rollback:

```bash
VOICE_PROCESSOR_ENABLED=false
docker compose up -d
```

Records left in `Processing` recover automatically after `VOICE_PROCESSOR_STALE_PROCESSING_SECONDS`, or can be manually set back to `New`.

Для OAuth 2.0 offline access:

```bash
PYTHONPATH=src python scripts/google_drive_oauth.py \
  --client /path/to/google-drive-client.json \
  --token /path/to/google-drive-token.json \
  --port 8090
```

На headless VPS удобнее выполнить команду на локальной машине с браузером, затем безопасно перенести `google-drive-token.json` на VPS. Не вставляйте client secret, refresh token или access token в чат, Git, логи или issue.

Smoke-test text-only:

```bash
curl -sS https://<domain>/api/mobile-inbox/items \
  -H "Authorization: Bearer $MOBILE_INBOX_TOKEN" \
  -F 'payload={"type":"text","text":"Проверка Android inbox"}'
```

Smoke-test с MP3:

```bash
curl -sS https://<domain>/api/mobile-inbox/items \
  -H "Authorization: Bearer $MOBILE_INBOX_TOKEN" \
  -F 'payload={"type":"voice","text":"Тестовая голосовая запись из Android"}' \
  -F 'files[]=@22-33_mono_16khz_64kbps.mp3;type=audio/mpeg'
```

## Следующие доработки

- Кнопки: `Записать в проект`, `Оставить в Inbox`, `Удалить`.
- Очередь задач для долгих аудио.
- Vision-анализ для Telegram-фото в основном Telegram pipeline.
- GigaAM как альтернатива OpenAI Speech-to-Text.
- Команды `/inbox`, `/projects`, `/today`, `/last`.
- Сохранение ссылок на медиафайлы в Airtable.
