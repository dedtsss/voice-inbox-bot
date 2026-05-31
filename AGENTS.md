# AGENTS.md

Rules for AI coding agents working on this repository.

## Project goal

This repository contains a minimal Voice Inbox Telegram Bot.

The bot is an input channel for a personal Projects OS system in Airtable. It accepts Telegram text, voice messages, photos with captions, files, and links, then writes structured records to Airtable.

Keep the project focused on the MVP:

- Telegram bot via long polling.
- OpenAI Speech-to-Text for voice transcription.
- OpenAI text structuring.
- Airtable `Voice Inbox` records.
- Optional write-through to `Projects OS / Items` when project detection is confident.
- Docker Compose deployment on a small Ubuntu VPS.

Do not add webhook, web UI, n8n, local Whisper, local GigaAM, queues, admin panels, databases beyond the current local data folder, or unrelated architecture unless explicitly requested.

## Core rules

### 1. Think Before Coding

Before editing files:

- Read the relevant files first.
- Identify the smallest set of changes needed.
- Check existing naming, structure, environment variables, and Docker setup.
- Avoid assumptions about Airtable field names, table IDs, model names, or secrets.

### 2. Simplicity First

Prefer the simplest reliable implementation.

- Keep the bot easy to deploy on a 1 CPU / 1 GB RAM VPS.
- Prefer long polling over webhook for the MVP.
- Prefer clear Python code over abstractions.
- Do not introduce frameworks or services unless they solve a current problem.
- Do not optimize prematurely.

### 3. Surgical Changes

Make targeted changes only.

- Modify only the files required for the task.
- Do not reformat unrelated code.
- Do not rename public environment variables without updating `.env.example` and README.
- Do not change deployment assumptions without explaining why.
- Never commit secrets, tokens, local `.env`, logs, downloaded media, or private user data.

### 4. Goal-Driven Execution

Every change must move the MVP toward a testable result.

A completed task should include:

- What changed.
- Which files changed.
- How to run or test it.
- Known risks or limitations.

For bug fixes, include the likely cause and the verification command.

## Security rules

Never expose or commit:

- `TELEGRAM_BOT_TOKEN`
- `OPENAI_API_KEY`
- `AIRTABLE_TOKEN`
- `.env`
- Telegram user IDs unless they are already intentionally documented
- Airtable private base/table/field IDs if the user did not ask to commit them
- downloaded voice, audio, photo, file, or transcript data

Use `.env.example` for placeholders only.

## Deployment assumptions

Default deployment target:

- Ubuntu 22.04 or 24.04 VPS.
- Docker + Docker Compose plugin.
- Long polling.
- No domain.
- No SSL.
- No opened HTTP port required.
- 2 GB swap recommended for 1 GB RAM VPS.

## Out of scope until MVP works

Do not add unless explicitly requested after the MVP is running:

- webhook mode;
- web admin panel;
- multi-user roles;
- n8n integration;
- local Whisper;
- local GigaAM;
- image vision analysis;
- complex queues;
- PostgreSQL;
- external frontend;
- automatic project dashboards outside Airtable.
