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
- Фото в MVP обрабатываются по подписи. Анализ изображения добавим позже.
- Если проект не найден в Airtable, запись остаётся только в `Voice Inbox`.
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

- `manifest.json` с `item_id`, source, type, text и Drive file IDs;
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
- Анализ фото через vision-модель.
- GigaAM как альтернатива OpenAI Speech-to-Text.
- Команды `/inbox`, `/projects`, `/today`, `/last`.
- Сохранение ссылок на медиафайлы в Airtable.
