# Voice Inbox Bot — agent rules

## Обязательный язык публикаций GitHub

**Весь новый авторский человекочитаемый текст, публикуемый в GitHub от имени владельца, писать по-русски.** Это относится к названиям и описаниям задач, запросам на слияние, комментариям и отзывам, этапам и результатам исполнения, сообщениям коммитов, отчётам, журналам, документации и авторским пояснениям в коде. Перед публикацией проверять язык и исправлять английские шаблонные фразы. Не переводить программные идентификаторы, имена и пути, команды, параметры, API, машинные статусы (`RUN_STARTED`, `SUCCESS`, `BLOCKED` и др.), код и дословные внешние цитаты, если перевод нарушает точность или совместимость. Исключения — только по явному указанию владельца либо при строгой технической необходимости. Исторические записи массово не переписывать. Общее обязательное правило: [политика русского языка](https://github.com/dedtsss/agent-dispatch/blob/main/docs/memory/github-russian-language-policy.md).

This is the canonical rule file. Read the task/Issue, this file, and only relevant files.

- Preserve the small-VPS MVP: Telegram long polling, OpenAI transcription/structuring, Airtable records, optional confident project write-through, and Docker Compose.
- Do not add webhooks, web UI, n8n, local speech models, queues, databases beyond the local data folder, or unrelated architecture unless explicitly required.
- Do not expose or commit tokens, `.env`, private Airtable IDs, Telegram user/media/transcript data, or logs.
- Keep deployment assumptions unchanged unless the task explicitly authorizes a change; do not merge or perform destructive actions without authorization.

Make the smallest focused change and run the relevant test or check. Report changed files, exact verification, likely cause for bug fixes, and remaining limitations.
