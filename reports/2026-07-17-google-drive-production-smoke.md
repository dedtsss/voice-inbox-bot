# Google Drive production smoke: voice-inbox-bot

Date: 2026-07-17

Repository: `dedtsss/voice-inbox-bot`
Production checkout: `/opt/voice-inbox-bot`
Production URL: `https://voice-inbox.bruce-group.net/`

## Scope

Production smoke was executed directly on the VPS over SSH, not through `agent-dispatch#191`.

## Production state

- Previous production commit: `faa2b4b95616a4b16ec08dad82142ebb76d4faa3`
- Current production commit: `c02aa1c0cfa7c26013d529176e338a9ba4bd1edb`
- Container: `voice-inbox-bot`
- Docker image ID: `sha256:4b975d437cf95da9bc3faa8741709d594ec4508a4235f00ec1f53402553f6d43`
- Container state: `running`
- Restart count: `0`
- Local health: `{"ok":true}`
- Public health: `{"ok":true}`
- `/proc/1/cmdline`: `python -m app.main`
- Telegram polling startup log entries: `1`
- Running app processes observed through `/proc`: one main process, `python -m app.main`

`docker-compose.override.yml` is untracked and contains only read-only mounts for:

- `/root/.secrets/google-drive-client.json` -> `/run/secrets/google-drive-client.json:ro`
- `/root/.secrets/google-drive-token.json` -> `/run/secrets/google-drive-token.json:ro`

The files exist on the VPS and are available inside the container. Docker mount metadata reports `RW=false` for both mounts.

Backup:

- `/root/backups/voice-inbox-bot/voice-inbox-bot-pre-google-drive-20260717T162148Z.tar.gz`

## OAuth

- OAuth project ID: `bruce-voice-inbox`
- OAuth client type: `installed`
- Refresh token: present in `/root/.secrets/google-drive-token.json` (value not printed)
- Token scopes: `https://www.googleapis.com/auth/drive`
- Publishing status: not verifiable from the installed-client JSON or authorized-user token. `gcloud` is not installed on the VPS, so the OAuth consent screen publishing status was not available from production.

Operational note: Drive calls succeeded, but each smoke process printed `Could not persist refreshed Google Drive OAuth token` because the token JSON is mounted read-only. Runtime refresh worked in memory.

## Google Drive API

Root folder:

- Folder ID: `13Okr1-K519GIJwDFNEssC2R-D8RplJ-j`
- Folder name: `Bruce Voice Inbox Connector Test`
- MIME: `application/vnd.google-apps.folder`

Temporary text round trip:

- File name: `tmp-production-smoke-20260717.txt`
- Temporary file ID: `1zWumP1rnIWoB5tWxdt27pdR4rxWwhiI9`
- MIME: `text/plain`
- Size: `66`
- Source SHA-256: `91cbe60f188a0f1d6659512367d36a93bef2383b9ee9eb0dc918aef02d3b7157`
- Download SHA-256: `91cbe60f188a0f1d6659512367d36a93bef2383b9ee9eb0dc918aef02d3b7157`
- Match: yes
- Cleanup: temporary Drive file deleted

## MP3 round trip

Airtable record: `recm52xeHyTL5StJT`

Expected/source MP3:

- Name: `22-33_mono_16khz_64kbps.mp3`
- MIME: `audio/mpeg`
- Size: `13004 bytes`
- Source SHA-256: `05a710bfdbc8500341b745b825b25de2b4dfb3b00ee42949e4618992a0329856`

Drive result:

- Item ID / Airtable External ID: `android-airtable-recm52xeHyTL5StJT-20260717T180236Z`
- Folder ID: `1o29AculrcJlFegGdnEfJt7zvNuIE6CLR`
- Folder URL: `https://drive.google.com/drive/folders/1o29AculrcJlFegGdnEfJt7zvNuIE6CLR`
- MP3 file ID: `1S_B5dt1Kx9lEkACPNq_pv1RTcpLgon2t`
- Manifest file ID: `1vtlrSphTdd5_0EwfC01-PQy-MG8M4cqP`
- Downloaded SHA-256: `05a710bfdbc8500341b745b825b25de2b4dfb3b00ee42949e4618992a0329856`
- SHA-256 match: yes
- Name match: yes
- MIME match: yes
- Size match: yes
- MP3 cleanup: not deleted

Airtable final fields:

- `External ID`: `android-airtable-recm52xeHyTL5StJT-20260717T180236Z`
- `Google Drive`: `https://drive.google.com/drive/folders/1o29AculrcJlFegGdnEfJt7zvNuIE6CLR`
- `Источник`: `Android`
- `Ошибка обработки`: cleared
- `Статус обработки`: `Needs Review`
- `Processed`: not set by this smoke

## Android smoke

All Android requests used the live production HTTP API in the running container.

| Case | Airtable record | External ID | Status | Type | Drive folder | Drive files |
| --- | --- | --- | --- | --- | --- | --- |
| Text only | `recCuUFlOKVjcd2cL` | `prod-smoke-android-20260717T180328Z-text` | `New` | `Text` | `16RwYwrdTB7cE5HZU-LkjOV_8xzUSkI3M` | `manifest.json` (`1YjXxGsIplJaF5McjTM06UXPzZzBBQxXr`) |
| MP3 | `recEwrf8xehUMHIWs` | `prod-smoke-android-20260717T180328Z-mp3` | `New` | `Voice` | `1jeH3tML8aXSBPgYgtl_7TNWQhDCYbtzT` | `22-33_mono_16khz_64kbps.mp3` (`1aQ68-tJlhhGwRhaLeFV6HcYja6l-xyBU`), `manifest.json` (`1ovkbYTBEjwyXC6m3xNFt8ZFudE4mJsC8`) |
| Photo + text | `recmjR1izRRoqN0sv` | `prod-smoke-android-20260717T180328Z-photo` | `New` | `Mixed` | `1TVd3PVHZkXHGBE90TvRqikUGtZOvj2te` | `photo.png` (`16Pal2p15s_RmX0mWI9OfeBrTFh6ilg-d`), `manifest.json` (`1Ue-iWTVq0UItzeduwFhQy1eB1-x6NMgW`) |
| Video | `recaZdOPZUexcxodc` | `prod-smoke-android-20260717T180328Z-video` | `New` | `Video` | `1tIGywLFF0vQjvbpKs1eg8FWmmjacZ9b2` | `clip.mp4` (`1S0YiD57D3C9eGz2k8-qFgGwawPAl7tMu`), `manifest.json` (`1YJE18Z02OIGTzWz9etrxAhhgIrDyTTA3`) |
| Multiple files | `recUxyHVyCPJw93xy` | `prod-smoke-android-20260717T180328Z-multi` | `New` | `Mixed` | `1xxgwN6LlR58g9VtRDKTvzecTS62et5IZ` | `a.txt` (`1fs3-ZsBo5ytyroj_1V-rrHzXCBtSxViN`), `b.json` (`1BFYVBAvjWWLnp1E20H2tWLFTh3GKFfEw`), `manifest.json` (`1BdCaQUO5-llcH5NeoeiexGNNOHk2E285`) |
| Idempotent repeat | `recBOwGrFGqb7HREV` | `prod-smoke-android-20260717T180442Z-same` | `New` | `Text` | `1fD5q7T6wF2FhKy5r7D4D08YLQddhIxB7` | `manifest.json` (`1HDuA665dT082RomoETUcMzVdemYV11M9`) |

Idempotency result: two POSTs with the same `item_id` both returned `remote_id=recBOwGrFGqb7HREV`.

All Android smoke records have:

- `Источник`: `Android`
- `Google Drive`: populated
- `Ошибка обработки`: empty
- `manifest.json`: present

## Telegram smoke

Telegram Bot API check:

- `getMe`: HTTP `200`
- Bot username: `VoiceTaskNote_Inbox_bot`

The production container is running one polling app process and logged one polling startup. No Telegram user-session was available on the VPS, and Bot API cannot self-inject an inbound user message. Telegram smoke was therefore executed at the production processing layer after content extraction, using `store_telegram_originals` and `save_to_airtable` with production settings, real Drive, and real Airtable.

| Case | Airtable record | External ID | Status | Type | Drive folder | Drive files |
| --- | --- | --- | --- | --- | --- | --- |
| Telegram text | `recS3oc9XXs1o3X4Y` | `prod-smoke-telegram-20260717T180552Z-text` | `Processed` | `Text` | `1Yq9csZf4E8bmfeP36MW8fyAvnbv7T6Sn` | `manifest.json` (`1cJtOEaH3vnUzoq2fpplTLSLc6pvd_w3x`) |
| Telegram voice | `recFhRQlhr1HYqSmq` | `prod-smoke-telegram-20260717T180552Z-voice` | `Processed` | `Voice` | `1ckLPnOmMOWK849XMQi0SUQvFRfNp5Is-` | `telegram_voice_smoke.ogg` (`1ppyWI931mqOnNWDrIRbg_1e25qpai9tw`), `manifest.json` (`1JrnfB7SVPMEqcPr5S_drLHk16nVJvqp-`) |

Telegram voice fixture:

- Source path inside container: `/app/data/incoming/20260717_002144_841323082_44.ogg`
- Size: `383199`
- SHA-256: `9b14e4b729d95d617d046d99ffcd11e05db1964cc4f8f92f7f422a23bf662fbf`

Both Telegram smoke records have:

- `Источник`: `Telegram`
- `Google Drive`: populated
- `Ошибка обработки`: empty
- `manifest.json`: present

## Spool test

Spool was tested with a simulated `DriveStorageError` in an isolated TestClient process using production settings and real Airtable. The running production container configuration was not changed.

- Item ID: `prod-smoke-spool-20260717T180624Z`
- HTTP status: `502`
- Airtable record: `recXFJAfaQMbcqytz`
- Response status: `drive_upload_failed`
- Spool directory: `/app/data/google_drive_spool/2026-07-17_prod-smoke-spool-20260717T180624Z`
- Spool file: `spool.txt`, `23 bytes`
- Spool manifest: `manifest.json`, `662 bytes`
- Fake refresh token in response: no
- Fake client secret in response: no
- Fake refresh token in manifest: no
- Fake client secret in manifest: no
- Redaction marker present: yes

Post-spool normal production checks:

- Container state: `running`
- Restart count: `0`
- Local health: `{"ok":true}`
- Drive root access: ok

## Secret checks

Sensitive values were loaded in memory from `.env`, `/root/.secrets/google-drive-client.json`, and `/root/.secrets/google-drive-token.json` only for exact-match scanning. Values were not printed.

Scan result:

- Sensitive values scanned: `9`
- Docker logs exact secret hits: `0`
- Docker logs `Authorization:` headers: `0`
- Docker logs `Bearer ...` token patterns: `0`
- Tracked Git exact secret hits: `0`
- Docker image metadata/history exact secret hits: `0`
- Full Docker image tar exact secret hits: `0`

Untracked/not committed:

- `.env`
- `docker-compose.override.yml`
- OAuth client JSON
- OAuth token JSON
- runtime spool files
- temporary payloads/logs

## Limitations

- OAuth consent publishing status was not available from the production OAuth JSON/token and `gcloud` is not installed on the VPS.
- OAuth token refresh succeeds in memory, but refreshed token persistence is blocked by the read-only token mount.
- Telegram live inbound update was not exercised because the VPS has no authorized Telegram user-session. The polling process, Bot API availability, and production Telegram storage/Airtable processing path were verified.
- Spool failure was simulated through `DriveStorageError` in an isolated production-settings TestClient process. The running production container config was not modified.

## Rollback

```bash
cd /opt/voice-inbox-bot
git fetch origin
git checkout faa2b4b95616a4b16ec08dad82142ebb76d4faa3
docker compose up -d --build
docker inspect voice-inbox-bot --format '{{.State.Status}} {{.RestartCount}}'
curl -fsS http://127.0.0.1:18081/health
```

If the checkout must be restored from backup instead:

```bash
cd /opt
sudo systemctl stop docker
sudo tar -xzf /root/backups/voice-inbox-bot/voice-inbox-bot-pre-google-drive-20260717T162148Z.tar.gz
sudo systemctl start docker
cd /opt/voice-inbox-bot
docker compose up -d
curl -fsS http://127.0.0.1:18081/health
```
