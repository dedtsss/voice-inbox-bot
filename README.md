# Voice Inbox Telegram Bot

Личный Telegram-бот для входящего потока заметок: голос, текст, фото с подписью, файлы.

MVP-логика:

1. Принимает сообщение в Telegram.
2. Сохраняет оригиналы и `manifest.json` в Google Drive.
3. Выбирает только явно настроенный `VOICE_PROCESSING_ROUTE`.
4. В основном режиме `chatgpt_subscription` ставит запись в очередь без OpenAI API.
5. Всегда пишет запись в Airtable `Voice Inbox`.
6. Если проект определён уверенно, дополнительно создаёт запись в `Projects OS / Items`.
7. Возвращает краткую карточку в Telegram.
8. Принимает записи из Android Dispatcher по HTTPS/HTTP API и сохраняет их в тот же Airtable `Voice Inbox`.
9. Запускает multimodal OpenAI processor только при `VOICE_PROCESSING_ROUTE=openai_api`.

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

## Voice Inbox Dashboard

Dashboard — отдельный server-side web-сервис для просмотра `Voice Inbox / Inbox`, записей `Needs Review`, вложений, AI-результатов и ручного исправления структурированных полей. Он не использует Airtable Interface и не отдаёт Airtable PAT в браузер: все Airtable API-запросы выполняются только сервером.

Сервис живёт отдельно от Telegram-бота, Android HTTP API и processor:

```bash
python -m app.dashboard
```

В Docker Compose он запускается как отдельный процесс:

```bash
docker compose up -d --build voice-inbox-dashboard
```

Локальный health check:

```http
GET /healthz
```

По умолчанию локальный адрес — `http://127.0.0.1:8081`. В production порт должен быть опубликован только на loopback host interface, например:

```text
127.0.0.1:8081 -> container:8081
```

Dashboard не должен быть открыт firewall rule напрямую. Внешний доступ должен идти через Cloudflare Tunnel и Cloudflare Access. Для `inbox.bruce-group.net` сначала настраивается Access Application и allow policy, затем hostname добавляется в существующий Tunnel на локальный dashboard origin. Не переводите `voice-inbox.bruce-group.net` под интерактивный Access, потому что Android API использует существующую Bearer-авторизацию.

### Dashboard env

Только имена переменных, значения хранятся вне Git:

```env
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8081
DASHBOARD_PUBLIC_ORIGIN=http://127.0.0.1:8081
DASHBOARD_ALLOWED_HOSTS=127.0.0.1,localhost
DASHBOARD_CSRF_SECRET=
DASHBOARD_PAGE_SIZE=25
DASHBOARD_OVERVIEW_MAX_RECORDS=1000
DASHBOARD_MAX_FORM_BYTES=32768
DASHBOARD_WRITE_RATE_LIMIT_PER_MINUTE=30
DASHBOARD_AIRTABLE_VIEW=
DASHBOARD_CREATED_TIME_FIELD=
DASHBOARD_ATTACHMENT_TIMEOUT_SECONDS=30
VOICE_FIELD_TRAINING_STATUS=Training Status
VOICE_FIELD_SCOPE=Scope
VOICE_FIELD_LIFE_AREA=Life Area
VOICE_FIELD_CATEGORY=Category
VOICE_FIELD_SUBCATEGORY=Subcategory
VOICE_FIELD_TRAINING_CONFIRMED_AT=Training Confirmed At
VOICE_FIELD_TRAINING_ANSWERS_JSON=Training Answers JSON
VOICE_TRAINING_CREATED_AFTER=2026-07-24T00:00:00Z
VOICE_TRAINING_QUEUE_LIMIT=50
VOICE_TRAINING_BACKLOG_LIMIT=20
VOICE_TRAINING_SIMILARITY_LIMIT=5
VOICE_TRAINING_BATCH_LIMIT=20
VOICE_TRAINING_RULE_THRESHOLD=3
VOICE_TRAINING_LIFE_AREAS=Дом,Семья,Здоровье,Финансы,Покупки,Документы,Обучение,Идеи,Отдых,Другое
VOICE_TRAINING_TAXONOMY_TABLE_NAME=Таксономия
```

`DASHBOARD_CSRF_SECRET` обязателен для запуска dashboard и должен быть случайным значением не короче 32 байт. В production храните его через Bruce Secrets Contract или другой фактически принятый secret storage. Не используйте `MOBILE_INBOX_TOKEN` для dashboard.

`DASHBOARD_PUBLIC_ORIGIN` должен совпадать с внешним origin dashboard, а `DASHBOARD_ALLOWED_HOSTS` — с разрешёнными Host header. Эти значения используются для Host validation и Origin/Referer validation на изменяющих запросах.

### Airtable и глобальная сортировка

Dashboard использует существующий `AirtableClient`, текущие env mapping полей и Airtable metadata. Select-варианты для `Проект`, `Тип` и `Приоритет` берутся из metadata, поэтому устаревшие значения не хардкодятся в UI.

Список записей использует Airtable pagination `offset`, `pageSize`, ограниченный набор `fields[]`, server-side formula filters и поиск по текстовым полям. Сводка загружает только ограниченную выборку полей до `DASHBOARD_OVERVIEW_MAX_RECORDS`; если записей больше, UI показывает `+`.

Глобальная сортировка должна выполняться в Airtable до применения `offset` и `pageSize`. Нельзя полагаться на локальную сортировку одной уже полученной Airtable-страницы: в таком режиме новая запись может находиться на следующей странице и не попасть в начало dashboard.

Поддерживаются два точных server-side режима:

- `DASHBOARD_AIRTABLE_VIEW` — приоритетный режим. Значение должно указывать на существующий Airtable view таблицы `Inbox`, уже отсортированный по времени создания от новых к старым. Dashboard передаёт `view` во все запросы списка и считает порядок точным только если view найден в metadata. Пользовательский `sort=asc` в этом режиме не меняет порядок view.
- `DASHBOARD_CREATED_TIME_FIELD` — режим server-side `sort`. Значение должно указывать на существующее поле Airtable типа `Created time` или на formula field с точной формулой `CREATED_TIME()`, например `Dashboard Created Time`. Dashboard передаёт `sort[0][field]` и `sort[0][direction]` в каждый list-запрос до пагинации. Направление по умолчанию `desc`; `sort=asc` разрешён только как направление для этого же allowlisted поля. Для стабильности одинаковых timestamp добавляется дополнительная сортировка по безопасному существующему текстовому полю, если оно есть.

Если настроены оба варианта, используется `DASHBOARD_AIRTABLE_VIEW`.

Если `DASHBOARD_AIRTABLE_VIEW` или `DASHBOARD_CREATED_TIME_FIELD` настроены, но не существуют в Airtable metadata или указывают на неподходящий тип поля, dashboard возвращает безопасную Airtable-ошибку вместо молчаливого отката на локальную сортировку страницы.

Если ни view, ни Created time field не заданы, dashboard работает в ограниченном режиме `page_only_unsafe`: он может локально упорядочить только текущую полученную страницу по системному `createdTime`, но не гарантирует глобальный порядок между страницами. При запуске пишется безопасное предупреждение в лог, в UI списка показывается предупреждение, а `GET /healthz` возвращает диагностический `sorting_mode`.

Возможные значения `sorting_mode`:

- `airtable_view` — порядок задаёт Airtable view.
- `airtable_field` — порядок задаёт Airtable `Created time` field через server-side `sort`.
- `page_only_unsafe` — точная глобальная сортировка не настроена.

Production-проверка должна подтверждать только факт наличия выбранной переменной и `sorting_mode`; не выводите Airtable IDs, токены, тексты записей, attachment URL или AI JSON.

### Разделы

- `Обзор` — количество записей, статусы, источники, записи за сегодня/7 дней, зависшие и технические записи.
- `Последние` — поиск, фильтры, период, сортировка и пагинация.
- `Needs Review` — карточки для проверки и переход к форме исправления.
- `New / Processing` — возраст записи: до 5 минут нормальное состояние, 5-15 минут задержка, больше 15 минут требует внимания.
- `Processed` — компактный список обработанных записей.
- `Разбор и обучение` — отдельная очередь классификационного обучения с мастером, похожими записями, правилами и структурой.
- `Правила` — безопасный просмотр `Правила обработки`; если есть поле `Активно`, правило можно включить или выключить.
- `Технические` — фильтр по `smoke`, `canary`, `production test`, `TG-SMOKE`, `dashboard-canary`. Такие записи не удаляются автоматически.

### Редактирование

Из формы нельзя передать произвольное Airtable field name. На сервере есть allowlist form keys:

```text
project
entry_type
priority
due_date
amount
counterparty
period
next_action
correction_comment
```

Каждый ключ мапится на реальное поле Airtable только сервером. Для select-полей проверяются реальные варианты из metadata. Для даты принимается `YYYY-MM-DD` или пустое значение. Для суммы принимается число или пустое значение. Текстовые поля ограничены по длине.

После успешного POST используется Post Redirect Get, поэтому обновление страницы не повторяет сохранение. PATCH в Airtable отправляет только изменённые разрешённые поля и служебные флаги действия.

### Сохранить и обучить

Dashboard не создаёт новый механизм обучения. Кнопка `Сохранить и обучить` обновляет исправленные поля и ставит существующие флаги:

```text
Обучить на исправлении = true
Обучение учтено = false
Комментарий к исправлению = <комментарий пользователя>
```

Дальше один существующий voice processor в своём обычном цикле подбирает pending corrections, сравнивает текущие поля с `AI результат JSON`, создаёт правило в `Правила обработки`, ставит `Обучение учтено = true` и очищает `Обучить на исправлении`.

### Разбор и обучение

`Разбор и обучение` находится на `/learning` и не заменяет `Needs Review`.

- `Needs Review` проверяет качество AI: распознавание, сумму, срок, проект, тип, next action, ошибку и низкую уверенность.
- `Разбор и обучение` подтверждает пользовательскую структуру даже для корректно распознанных записей: scope, рабочий проект или личную сферу, тип, действие, category, subcategory, priority, due date и tags.

Очередь обучения берёт реальные записи `Processed`, `Needs Review` и безопасные `New`, если они не technical/smoke/canary, имеют текст для показа и не имеют завершённого `Training Status`. Legacy blank `Training Status` трактуется как `Pending`, но только внутри безопасной выборки.

Чтобы старый архив не попал в очередь массово, действует `VOICE_TRAINING_CREATED_AFTER`. По умолчанию это `2026-07-24T00:00:00Z`. Ручной backlog включается фильтром на странице и ограничивается `VOICE_TRAINING_BACKLOG_LIMIT`.

Training statuses:

- `Pending` — запись ожидает разбора или legacy blank внутри cutoff.
- `In Progress` — пользователь начал сессию через POST-кнопку.
- `Completed` — ответы сохранены.
- `Skipped` — запись пропущена.
- `Auto Confirmed` — зарезервировано для будущей безопасной автоклассификации.

Мастер показывает одну запись за раз, media отдаёт только через существующий `/records/{record_id}/attachments/{index}` proxy. Scope управляет адаптивной веткой: `Рабочее` показывает project, `Личное` показывает life area, `Смешанное` показывает оба блока. Список проектов берётся из Airtable metadata/Projects OS, life areas берутся из поля `Life Area` и `VOICE_TRAINING_LIFE_AREAS`, поэтому набор расширяется без изменения кода.

Сохранение пишет только allowlisted поля:

```text
Training Status
Scope
Life Area
Category
Subcategory
Training Confirmed At
Training Answers JSON
Проект
Тип
Приоритет
Срок
Следующее действие
Теги
```

`Training Answers JSON` содержит только schema version, record id, optional applied-from record id, timestamp и структурированные ответы. Тексты заметок, attachment URL и полный `AI результат JSON` туда не дублируются.

После сохранения похожие записи предлагаются через локальный token overlap/Jaccard с небольшими бонусами за совпадение project/type/source/tags. Batch apply требует явного checkbox выбора, максимум `VOICE_TRAINING_BATCH_LIMIT`, и меняет только выбранные record IDs.

Правило processor не создаётся после одного примера. После `VOICE_TRAINING_RULE_THRESHOLD` одинаковых подтверждений вкладка `Правила` предлагает rule candidate. Создание требует явного POST и использует существующую таблицу `Правила обработки`; processor продолжает применять общий correction-learning механизм.

Вкладка `Структура` показывает рабочие проекты, личные сферы, категории и подкатегории по безопасной ограниченной выборке. Schema ensure идемпотентно создаёт таблицу `Таксономия` с полями `Название`, `Тип`, `Родитель`, `Активно`, `Количество применений`, `Дата последнего применения`; сложный древовидный редактор в MVP не реализован.

### Безопасность

Dashboard включает:

- CSRF token для всех изменяющих запросов;
- Host validation;
- Origin/Referer validation для POST;
- ограничение размера form body;
- in-memory rate limiting для изменяющих запросов;
- защитные HTTP-заголовки `Content-Security-Policy`, `Cache-Control: no-store`, `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, `Permissions-Policy`, `X-Robots-Tag`;
- `robots.txt` с запретом индексации;
- выключенный uvicorn access log в dashboard entrypoint;
- server-side proxy route для Airtable attachments, чтобы attachment URL и PAT не попадали в HTML.

Логи dashboard должны содержать только route, HTTP status, duration, operation type и обезличенный тип ошибки. Не логируйте полный текст заметок, расшифровки, AI JSON, attachment URL или секреты.

### Тесты

Dashboard tests используют fake Airtable client и не обращаются к production Airtable:

```bash
python -m pytest tests/test_dashboard.py
python -m pytest
```

Покрытие включает health endpoint, списки, detail card, пустую таблицу, Airtable error, pagination, filters, search, зависшие записи, валидацию, запрет неизвестных полей, CSRF, Origin/Referer, Host validation, XSS escaping, partial update, `Сохранить`, `Сохранить и обучить`, правила, security headers, training queue, cutoff/backlog, adaptive questions, complete/skip, batch apply, similarity, explicit rule creation и рендер queue/session/rules/structure.

### Диагностика зависших записей

Зависшей считается запись со статусом `New` или `Processing` старше 15 минут. Проверка:

1. Откройте раздел `New / Processing`.
2. Посмотрите возраст и `Ошибка обработки` в detail card.
3. Убедитесь, что в production запущен ровно один processor.
4. Проверьте, что `VOICE_PROCESSING_ROUTE=openai_api`, `VOICE_PROCESSOR_SOURCE_FILTER` соответствует источнику, а `VOICE_PROCESSOR_CREATED_AFTER` не отсекает запись.
5. При временной ошибке processor вернёт запись в `New` до лимита retries; после лимита запись уйдёт в `Needs Review`.

## Google Drive originals

### Drive fail-safe

Google Drive является обязательным хранилищем оригиналов для записей, которые должны попасть в AI-очередь. Если Drive отключён, не инициализировался или вернул любую ошибку API:

- backend продолжает принимать Android и Telegram ingest;
- оригинальные файлы и `manifest.json` сохраняются в закрытый локальный `GOOGLE_DRIVE_SPOOL_DIR`;
- Airtable-запись сохраняет выбранный `Processing Route`, но получает `Статус обработки = Needs Review` без Drive URL;
- запись не попадает в ChatGPT Subscription или OpenAI polling queue;
- Android получает `502 drive_upload_failed`, поэтому источник не принимает ошибку хранения за успешную доставку;
- повтор того же `item_id` остаётся ошибкой хранения, пока запись не восстановлена, и не создаёт дубликат.

Если одновременно недоступны Drive и локальный spool, Android получает `503 drive_spool_failed`, а вводящая в заблуждение Airtable-запись не создаётся. Telegram просит повторить отправку и не удаляет уже скачанный временный оригинал.

Ошибка инициализации Drive не останавливает Telegram polling и Android API. Техническое сообщение очищается от token/key values перед записью в Airtable, HTTP-ответом или логированием. Локальный spool — аварийная копия, а не замена Drive; его нужно перенести в Drive и обновить запись отдельной recovery-задачей.

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
- `Training Status` — статус разбора.
- `Scope` — личное, рабочее, смешанное или не уверен.
- `Life Area` — расширяемая личная сфера.
- `Category` — категория разбора.
- `Subcategory` — подкатегория.
- `Training Confirmed At` — UTC timestamp подтверждения.
- `Training Answers JSON` — компактные структурированные ответы мастера.

Если Airtable token имеет schema permissions, поля можно создать так:

```bash
PYTHONPATH=src python scripts/ensure_airtable_fields.py
```

Этот script idempotent: он добавляет metadata поля Drive ingest, `Processing Route`, служебные queue claim-поля, новые статусы, feedback поля OpenAI processor, таблицу `Правила обработки`, training-поля, training select choices, минимальные варианты `Тип` и таблицу `Таксономия`, если они ещё отсутствуют.

## Voice processing routes

`VOICE_PROCESSING_ROUTE` — единственный переключатель AI-маршрута:

- `chatgpt_subscription` — сохраняет Airtable + Drive, ставит `Processing Route = ChatGPT Subscription` и `Статус обработки = Awaiting Subscription`; OpenAI clients и polling не создаются;
- `openai_api` — сохраняет `Processing Route = OpenAI API` и разрешает существующие Speech-to-Text, vision, Structured Outputs, correction learning и polling;
- `disabled` — сохраняет Airtable + Drive, ставит `Processing Route = Disabled` и `Статус обработки = Processing Disabled`.

Пропущенное или неизвестное значение безопасно трактуется как `disabled` с предупреждением. `VOICE_PROCESSOR_ENABLED` deprecated: оно только вызывает предупреждение и никогда не включает OpenAI API. `OPENAI_API_KEY` обязателен и проверяется при запуске только для `openai_api`.

## Multimodal OpenAI API processor

Processor живёт в этом же backend и запускается только при `VOICE_PROCESSING_ROUTE=openai_api`.

Что делает worker:

1. Берёт не больше `VOICE_PROCESSOR_BATCH_SIZE` записей с `Processing Route = OpenAI API`, `Статус обработки = New`, `Источник = VOICE_PROCESSOR_SOURCE_FILTER` и, если задано, Airtable `createdTime > VOICE_PROCESSOR_CREATED_AFTER`.
2. Claims запись через заранее существующий choice `Processing`, lock trace и bounded attempt count в `Ошибка обработки`.
3. Читает Drive folder URL, `manifest.json` и потоково скачивает оригиналы во временную директорию с лимитами размера и проверкой size/SHA-256.
4. Обрабатывает text/audio/photo/video/mixed: audio transcription, vision analysis для images, video audio + representative frames.
5. Отправляет итоговый контекст в OpenAI Structured Outputs со strict JSON Schema.
6. Валидирует project по choices самого поля `Voice Inbox / Проект` и остальные select values по текущим Airtable options.
7. Обновляет ту же Airtable запись, пишет в `Проект` имя singleSelect choice, сохраняет `AI результат JSON`, confidence и processor version.
8. При низкой уверенности, неизвестном проекте/type/select conflict ставит `Needs Review`.
9. Создаёт persistent learning rule только если пользователь явно отметил `Обучить на исправлении`.

Processor не создаёт Projects OS tasks в первой версии. `VOICE_PROCESSOR_CREATE_PROJECT_ITEMS=false` оставлен как future guard; legacy alias `PROCESSOR_CREATE_PROJECT_ITEMS=false` тоже принимается.

`VOICE_PROCESSOR_SOURCE_FILTER` по умолчанию равен `Android`. `VOICE_PROCESSOR_CREATED_AFTER` необязателен, но при включении production polling его нужно выставлять в UTC ISO 8601, например `2026-07-19T02:00:00Z`, чтобы не подбирать старый backlog. Некорректный timestamp останавливает запуск.

V1 processor рассчитан строго на один running worker. Текущий lock trace нужен для диагностики, но не является атомарной межпроцессной блокировкой Airtable. Не запускайте второй контейнер, `docker compose --scale`, cron-копию или ручной batch параллельно с `VOICE_PROCESSING_ROUTE=openai_api`.

### Processor env

```env
VOICE_PROCESSING_ROUTE=disabled
VOICE_PROCESSOR_ENABLED=false
VOICE_PROCESSOR_INTERVAL_SECONDS=60
VOICE_PROCESSOR_BATCH_SIZE=5
VOICE_PROCESSOR_TEXT_MODEL=gpt-4o-mini
VOICE_PROCESSOR_TRANSCRIPTION_MODEL=gpt-4o-transcribe
VOICE_PROCESSOR_CONFIDENCE_THRESHOLD=0.80
VOICE_PROCESSOR_MAX_VIDEO_FRAMES=12
VOICE_PROCESSOR_VIDEO_FRAME_INTERVAL_SECONDS=5
VOICE_PROCESSOR_CREATE_PROJECT_ITEMS=false
VOICE_PROCESSOR_SOURCE_FILTER=Android
VOICE_PROCESSOR_CREATED_AFTER=
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
VOICE_FIELD_PROCESSING_ROUTE=Processing Route
VOICE_FIELD_PROCESSING_ROUTE_QUERY_NAME=Processing Route
```

### Commands

При route, отличном от `openai_api`, команда безопасно завершится без OpenAI-вызовов:

```bash
PYTHONPATH=src python -m app.voice_processor --once
```

Для контролируемого запуска сначала явно установите `VOICE_PROCESSING_ROUTE=openai_api`, затем:

```bash
PYTHONPATH=src python -m app.voice_processor \
  --record-id recXXXXXXXXXXXXXX
```

Run one batch manually:

```bash
PYTHONPATH=src python -m app.voice_processor \
  --once \
  --batch-size 1
```

In Docker:

```bash
docker compose run --rm voice-inbox-bot \
  python -m app.voice_processor --record-id recXXXXXXXXXXXXXX
```

### ChatGPT Subscription queue contract

Точный Airtable filter: `Processing Route = ChatGPT Subscription` AND `Статус обработки = Awaiting Subscription` AND `Google Drive != blank` AND `Subscription Queue Claim = blank` AND запись не содержит маркеры `smoke`, `canary`, `production test`, `TG-SMOKE`, `dashboard-canary`. После Airtable filter код дополнительно проверяет наличие `manifest.json` в Drive.

Dry-run ограниченной пачки (не печатает пользовательские тексты, Drive URL или credentials):

```bash
PYTHONPATH=src python -m app.subscription_queue --batch-size 5 --created-after 2026-07-01T00:00:00Z --dry-run
```

Получить и claim следующую пачку:

```bash
PYTHONPATH=src python -m app.subscription_queue --batch-size 5 --created-after 2026-07-01T00:00:00Z --claim
```

Внутренний `SubscriptionQueue.load_bundle()` возвращает Airtable record ID, External ID, источник, тип, дату, исходный текст (если есть), Drive folder URL, разобранный `manifest.json` и проверенные оригинальные файлы. Публичный API для очереди не создаётся.

Quota migration:

```bash
PYTHONPATH=src python scripts/migrate_insufficient_quota.py --dry-run
PYTHONPATH=src python scripts/migrate_insufficient_quota.py --apply
```

### Correction learning UX

1. Processor writes `AI результат JSON` before user edits.
2. User manually fixes Airtable structured fields.
3. User checks `Обучить на исправлении` only when the correction should become a reusable rule.
4. Processor compares current fields with the AI snapshot, creates one concise active rule in `Правила обработки`, sets `Обучение учтено = true`, and clears `Обучить на исправлении`.
5. Manual edits without the checkbox are ignored by learning.

### Safe deploy

1. Merge and deploy with `VOICE_PROCESSING_ROUTE=disabled` или `chatgpt_subscription`.
2. Run `PYTHONPATH=src python scripts/ensure_airtable_fields.py` once with an Airtable token that has schema permissions.
3. Restart the container and verify Telegram plus `/health`.
4. Create or choose one controlled smoke record with Drive originals.
5. Только для проверки OpenAI API явно переключите route и run one-record command with `--record-id ...`.
6. Confirm same Airtable record was updated, `AI результат JSON` is present, temp media is gone, and no duplicate processing happened.
7. Confirm there is only one processor instance for the deployment.
8. Only after OpenAI smoke passes, keep `VOICE_PROCESSING_ROUTE=openai_api` with `VOICE_PROCESSOR_BATCH_SIZE=1`, then increase cautiously.

### Rollback

```bash
docker compose down
git checkout <previous_good_commit>
docker compose up -d --build
```

Fast disable without code rollback:

```bash
VOICE_PROCESSING_ROUTE=disabled
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
