# Voice Inbox Bot — agent rules

This is the canonical rule file. Read the task/Issue, this file, and only relevant files.

- Preserve the small-VPS MVP: Telegram long polling, OpenAI transcription/structuring, Airtable records, optional confident project write-through, and Docker Compose.
- Do not add webhooks, web UI, n8n, local speech models, queues, databases beyond the local data folder, or unrelated architecture unless explicitly required.
- Do not expose or commit tokens, `.env`, private Airtable IDs, Telegram user/media/transcript data, or logs.
- Keep deployment assumptions unchanged unless the task explicitly authorizes a change; do not merge or perform destructive actions without authorization.

Make the smallest focused change and run the relevant test or check. Report changed files, exact verification, likely cause for bug fixes, and remaining limitations.
