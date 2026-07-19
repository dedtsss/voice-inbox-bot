# Google Drive production smoke: voice-inbox-bot

Date: 2026-07-17

Repository: `dedtsss/voice-inbox-bot`

## Security note

This public report intentionally omits production Airtable identifiers, Google Drive file/folder identifiers, private backup paths, user data and secret values. Detailed operational identifiers remain only in the private deployment environment.

## Scope

Production smoke was executed directly on the VPS over SSH.

## Production state

- Previous production commit: `faa2b4b95616a4b16ec08dad82142ebb76d4faa3`
- Current production commit: `c02aa1c0cfa7c26013d529176e338a9ba4bd1edb`
- Container: `voice-inbox-bot`
- Container state: running
- Restart count: 0
- Local health: `{"ok":true}`
- Public health: `{"ok":true}`
- Main process: `python -m app.main`
- One application process and one Telegram polling startup were observed

Google Drive OAuth client and token files were mounted read-only into the container. Their host paths and private folder identifiers are intentionally omitted.

## OAuth

- OAuth client type: installed application
- Required Drive scope: full Drive access for the configured storage account
- Refresh token: present; value not printed
- Runtime token refresh: successful in memory
- Refreshed token persistence: blocked by the read-only mount

The persistence warning was non-fatal and did not block Drive operations.

## Google Drive API

A temporary text file was uploaded, downloaded and deleted.

Validation:

- filename and MIME type matched;
- source and downloaded SHA-256 matched;
- temporary file cleanup succeeded.

## MP3 round trip

A controlled MP3 record was stored through the production path.

Validation:

- original filename matched;
- MIME type matched;
- file size matched;
- SHA-256 matched after download;
- manifest was present;
- Airtable source was Android;
- Drive URL was populated;
- processing error field was empty.

Internal Airtable record ID, External ID, Drive folder ID and file IDs are omitted.

## Android smoke

The live Android HTTP API was exercised with:

- text-only input;
- MP3 input;
- photo plus text;
- video;
- multiple files;
- repeated submission with the same item ID.

Results:

- all requests used the running production service;
- every record had source `Android`;
- every successful storage record had a Drive folder and `manifest.json`;
- no storage error remained on successful records;
- duplicate POST requests with the same item ID returned the same Airtable record, confirming ingest idempotency.

Internal Airtable record IDs, External IDs, Drive folder IDs and file IDs are omitted.

## Telegram smoke

Telegram Bot API availability and polling startup were confirmed. Production processing-layer smoke covered:

- Telegram text;
- Telegram voice with the original media preserved in Drive.

Both records were stored as `Processed`, with source `Telegram`, populated Drive references and no processing errors.

A live inbound user update was not injected because the VPS had no authorized Telegram user session. The running polling process, Bot API availability and production storage path were verified.

## Spool test

A Drive upload failure was simulated in an isolated production-settings test process without changing the running container configuration.

Validation:

- endpoint returned HTTP 502 with `drive_upload_failed`;
- protected local spool directory and manifest were created;
- synthetic secret values were absent from response and manifest;
- redaction marker was present;
- normal production health and Drive access remained OK afterward.

Internal record identifiers and exact spool paths are omitted.

## Secret checks

Sensitive values were loaded only in memory for exact-match scanning and were never printed.

Results:

- Docker logs exact secret hits: 0
- Authorization header leaks: 0
- Bearer token pattern leaks: 0
- tracked Git exact secret hits: 0
- Docker image metadata/history exact secret hits: 0
- full Docker image archive exact secret hits: 0

Untracked and not committed:

- `.env`;
- Docker override with secret mounts;
- OAuth client JSON;
- OAuth token JSON;
- runtime spool files;
- temporary payloads and logs.

## Limitations

- OAuth consent publishing status was not independently queried from the VPS.
- Refreshed OAuth token persistence remains blocked by the read-only mount, though runtime refresh works.
- Telegram live inbound update was not exercised.
- Spool failure was simulated in an isolated process.

## Rollback

```bash
cd /opt/voice-inbox-bot
git fetch origin
git checkout faa2b4b95616a4b16ec08dad82142ebb76d4faa3
docker compose up -d --build
```

## Final result

Google Drive original storage, Android and Telegram persistence paths, ingest idempotency, spool fallback, health checks and secret handling passed production smoke. No private production identifiers are included in this public report.
