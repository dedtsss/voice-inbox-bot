# Subscription Worker production rollout — 2026-07-28 UTC

## Result

- Runtime rollout: successful.
- Runtime rollout commit: `77e23291cb1b5b13c9c1f1eb327238d3c559aa6d`.
- Production checkout was updated only by fast-forward; tracked files are clean and all five pre-existing untracked runtime files were preserved.
- Private pre-change backup: `/var/backups/voice-inbox-bot/20260728T131713Z-pre-subscription-worker` (root-only, mode `0700`; contained a checkout, runtime configuration, OAuth material, and systemd units without logging their content).

## Official Codex Subscription execution

- Verified CLI: `codex-cli 0.144.0`.
- Official non-interactive entry point: `codex exec` with stdin prompt, `--ephemeral`, `--output-schema`, and `--output-last-message`.
- Authentication: existing official ChatGPT login owned by the `codex` system user; `codex login status` reports `Logged in using ChatGPT` and `auth_mode=chatgpt`.
- The saved auth has no non-empty API key. The production `OPENAI_API_KEY` entry was removed from `.env`, containers were recreated, and the variable is absent inside the application container.
- No OpenAI Platform API processor or other external AI API is enabled or called. The only model network path is the official Codex CLI using ChatGPT/Codex Subscription authentication; no API billing path is configured.
- The execution is unattended while the saved ChatGPT authorization remains refreshable. An expired or revoked login stops preflight before claim and requires an operator to run the official login flow again.

## Architecture and security boundary

`systemd timer -> host worker -> prerequisite checks -> one claim -> Drive bundle -> local extraction -> isolated Codex CLI -> strict validation -> existing queue finalizer -> claim cleanup`

- Entry point: `PYTHONPATH=src python -m app.subscription_worker`.
- The host worker alone owns Airtable and Google Drive access, media extraction, validation, finalization, and safe counters.
- Work is sequential; at most one record is claimed. A process `flock` prevents concurrent local workers.
- Stale recovery is limited to the stable automatic-worker prefix, the same worker instance, `Awaiting Subscription`, the configured age, and absence of the active local lock. Manual, foreign, and unknown claims are not released by age.
- Each item uses a mode-`0700` temporary directory. Only sanitized manifest data, extracted content, allowed schema values, project titles, sanitized active rules, JSON Schema, and bounded local image copies enter the model boundary. Infrastructure identifiers and production secrets do not.
- Codex receives an environment allowlist containing only `PATH`, `HOME`, `CODEX_HOME`, `LANG`, and `LC_ALL`.
- The ChatGPT auth file is staged alone in a temporary mode-`0700` Codex home; a refreshed auth is validated and persisted atomically to the operator-owned file. It is never copied into the repository or `.env`.
- Outer systemd restrictions include the dedicated `codex` user, read-only production tree, inaccessible `.env`, data, SSH, root and runtime-secret paths, `NoNewPrivileges`, private devices/tmp, protected kernel controls, `MemoryMax=2G`, `CPUQuota=150%`, and a 35-minute service timeout.
- Inner bubblewrap exposes read-only `/usr`, the already protected read-only `/proc`, CA/DNS files, an empty `/tmp`, the staged auth home, and the one-item `/work` directory. Production checkout, home, SSH, runtime secrets, and host filesystem are absent. Network is shared only because official subscription execution requires the Codex service.
- Codex itself runs ephemeral, ignores user config/rules, uses read-only sandbox and approval policy `never`, disables shell/apps/browser/computer/image-generation/multi-agent tools, disables web search, and receives the strict output schema.

## Media handling and limits

- Text: manifest text and bounded text originals are extracted locally.
- Audio: local `faster-whisper` `small`, CPU, `int8`, Russian, cached for offline inference; an ambiguous transcript gets one normalized offline pass.
- Images: bounded local metadata and OCR; supported local images may also be attached with the official `codex exec --image` option. Incomplete extraction routes to review.
- PDF: bounded `pdftotext`; scan fallback renders only the configured page limit and uses local OCR.
- Video: `ffmpeg` extracts one audio track and a bounded frame sample; audio uses local STT and frames use the image path.
- File, total-record, prompt, response, Codex, STT, media, PDF-page, image, and video-frame limits are configurable. Generation has at most one validation retry.

## Verification and CI

- Final local suite: `193 passed`, with one pre-existing Starlette/httpx deprecation warning.
- `python -m compileall src tests`: passed.
- `git diff --check`: passed.
- `docker build .`: passed.
- `docker compose config`: passed.
- `systemd-analyze verify deploy/systemd/*.service deploy/systemd/*.timer`: passed.
- Feature PR #19: CI passed; feature commit `54f7bf532ee23ce120ee5fd90639870c9f8bcce3`; merge commit `ef7b8478cc9eb8cf3e4e645f0de6cbe9d413e031`.
- Rollout corrections PRs #20–#23 and OAuth ownership PR #25 each passed CI before merge. They cover an optional absent namespace path, host OAuth path precedence, bubblewrap proc visibility, protected proc binding, and preservation of token ownership across users. Final runtime merge is `77e23291cb1b5b13c9c1f1eb327238d3c559aa6d`.

## Production checks

- Initial `--dry-run`: success; all prerequisites passed; `queue_seen=0`.
- The empty queue triggered one synthetic control record through normal Drive and Airtable intake. The final successful run reported: `queue_seen=1`, `claimed=1`, `processed=1`, `needs_review=0`, `released=0`, `codex_failed=0`, `validation_failed=0`, `media_failed=0`, `stale_claims_recovered=0`, `duration_seconds=20.228`.
- The strict output passed on the first response, the claim was cleared, and the material numbers and condition were preserved.
- Control cleanup was verified: Airtable residue `0`, Drive residue `0`, private state residue `0`.
- Post-cleanup scheduled/idle run: all counters `0`, `duration_seconds=2.214`; a later explicit idle check was also successful.
- Queue remainder: `0`.
- Active or stale Subscription claims: `0`.
- Duplicate external identifiers: `0`.
- The complete pre-existing `Needs Review` record set was unchanged by the rollout (before/after count `19`, identical aggregate set digest), including the explicitly protected records.

## systemd status

- `voice-inbox-subscription-worker.service`: oneshot, last result `success`, exit status `0`; inactive between runs as designed.
- `voice-inbox-subscription-worker.timer`: enabled and active.
- Schedule: ten minutes after the previous unit becomes inactive, randomized by up to 60 seconds, persistent, one bounded worker invocation.

## Google Drive OAuth persistence

- Exact prior warning cause: the OAuth token was an individual read-only Docker bind mount, so atomic replacement after refresh was impossible even though the containing directory appeared writable.
- The token now lives in a dedicated private runtime-secret directory (directory `0700`, token `0600`) mounted read-write at the container runtime-secret boundary. The repository and `.env` contain no token.
- A safe real refresh was performed. Atomic save, fsync, read-back verification, restrictive mode, and `.bak` creation all succeeded.
- A second real refresh was forced from the root backend container. The token and backup retained the host worker's UID/GID and mode `0600`; a subsequent worker run as `codex` passed OAuth persistence, Drive access, and the empty-queue check.
- Persistence verification also succeeded inside the rebuilt container; new `oauth_token_persistence_failed` warnings: `0`.

## Remaining limitations

- ChatGPT Subscription authorization is user-bound and may require official interactive re-login after revocation or an unrecoverable refresh failure; preflight fails closed before claim.
- Subscription service/network availability remains an external runtime dependency. Transient failures release only the worker's own claim and do not loop indefinitely in one invocation.
- The host has constrained memory headroom. Local Whisper is bounded by the service `MemoryMax`; an out-of-memory or timeout condition fails the item safely rather than using a cloud transcription API.
- No rollout blocker remains.
