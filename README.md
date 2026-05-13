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
```

Airtable-токен должен иметь доступ на запись в базы:

- `Voice Inbox`
- `Projects OS`

Минимальные права: `data.records:read`, `data.records:write`.

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
- Доступ ограничивается `ALLOWED_TELEGRAM_USER_IDS`.
- Фото в MVP обрабатываются по подписи. Анализ изображения добавим позже.
- Если проект не найден в Airtable, запись остаётся только в `Voice Inbox`.
- Для `Projects OS / Items` типы приводятся к уже существующим значениям Airtable.

## Следующие доработки

- Кнопки: `Записать в проект`, `Оставить в Inbox`, `Удалить`.
- Очередь задач для долгих аудио.
- Анализ фото через vision-модель.
- GigaAM как альтернатива OpenAI Speech-to-Text.
- Команды `/inbox`, `/projects`, `/today`, `/last`.
- Сохранение ссылок на медиафайлы в Airtable.
