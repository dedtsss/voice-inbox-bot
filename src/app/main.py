from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message
from fastapi import FastAPI
import uvicorn

from app.airtable import AirtableClient, AirtableError, ProjectMatch
from app.config import Settings, get_settings
from app.mobile_api import create_mobile_api
from app.openai_ops import OpenAIProcessor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IncomingContent:
    raw_text: str
    message_type: str


def is_allowed(message: Message, settings: Settings) -> bool:
    user_id = message.from_user.id if message.from_user else None
    return user_id is not None and user_id in settings.allowed_user_ids


async def convert_audio_to_mp3(source: Path, target: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-ar",
        "16000",
        "-ac",
        "1",
        str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-1000:]
        raise RuntimeError(f"ffmpeg failed with code {process.returncode}: {detail}")


async def transcribe_telegram_file(
    *,
    bot: Bot,
    settings: Settings,
    openai_processor: OpenAIProcessor,
    file_id: str,
    suffix: str,
    message: Message,
) -> str:
    incoming_dir = settings.data_path / "incoming"
    incoming_dir.mkdir(parents=True, exist_ok=True)

    timestamp = local_timestamp(settings)
    user_id = message.from_user.id if message.from_user else 0
    safe_suffix = suffix if suffix.startswith(".") and len(suffix) <= 12 else ".bin"
    source = incoming_dir / f"{timestamp}_{user_id}_{message.message_id}{safe_suffix}"
    converted = incoming_dir / f"{timestamp}_{user_id}_{message.message_id}.mp3"

    telegram_file = await bot.get_file(file_id)
    await bot.download_file(telegram_file.file_path, destination=source)
    await convert_audio_to_mp3(source, converted)
    transcript = await openai_processor.transcribe_audio(converted)

    if not settings.save_media_files:
        for path in (source, converted):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temporary media file: %s", path)
    return transcript


async def extract_content(
    message: Message,
    bot: Bot,
    settings: Settings,
    openai_processor: OpenAIProcessor,
) -> IncomingContent:
    if message.voice:
        transcript = await transcribe_telegram_file(
            bot=bot,
            settings=settings,
            openai_processor=openai_processor,
            file_id=message.voice.file_id,
            suffix=".ogg",
            message=message,
        )
        return IncomingContent(raw_text=transcript, message_type="Voice")

    if message.audio:
        file_name = message.audio.file_name or "audio.mp3"
        transcript = await transcribe_telegram_file(
            bot=bot,
            settings=settings,
            openai_processor=openai_processor,
            file_id=message.audio.file_id,
            suffix=Path(file_name).suffix or ".mp3",
            message=message,
        )
        return IncomingContent(raw_text=transcript, message_type="Audio")

    if message.document:
        file_name = message.document.file_name or "document"
        mime_type = message.document.mime_type or ""
        if mime_type.startswith("audio/"):
            transcript = await transcribe_telegram_file(
                bot=bot,
                settings=settings,
                openai_processor=openai_processor,
                file_id=message.document.file_id,
                suffix=Path(file_name).suffix or ".bin",
                message=message,
            )
            return IncomingContent(raw_text=transcript, message_type="Audio file")
        text = "\n".join(part for part in [message.caption, f"File: {file_name}"] if part)
        return IncomingContent(raw_text=text, message_type="File")

    if message.photo:
        return IncomingContent(raw_text=message.caption or "", message_type="Photo")

    if message.text:
        return IncomingContent(raw_text=message.text, message_type="Text")

    return IncomingContent(raw_text=message.caption or "", message_type="Message")


def save_to_airtable(
    airtable: AirtableClient,
    settings: Settings,
    structured: dict,
    content: IncomingContent,
) -> tuple[dict, dict | None, ProjectMatch | None]:
    project: ProjectMatch | None = None
    if structured.get("project") and float(structured.get("project_confidence") or 0) >= 0.7:
        project = airtable.find_project(structured["project"])

    voice_record = airtable.create_voice_inbox_record(
        structured=structured,
        raw_text=content.raw_text,
        message_type=content.message_type,
        project=project,
    )

    item_record = None
    if settings.write_to_projects_os and project:
        item_record = airtable.create_project_item(
            structured=structured,
            raw_text=content.raw_text,
            message_type=content.message_type,
            project=project,
        )
    return voice_record, item_record, project


def format_reply(structured: dict, voice_record: dict, item_record: dict | None, project: ProjectMatch | None) -> str:
    lines = [
        "Сохранено во входящие.",
        f"Заголовок: {structured.get('title') or 'Заметка из Telegram'}",
    ]
    if structured.get("summary"):
        lines.append(f"Кратко: {structured['summary']}")
    if structured.get("next_action"):
        lines.append(f"Следующее действие: {structured['next_action']}")
    if project:
        lines.append(f"Проект: {project.title}")
    if item_record:
        lines.append("Задача Projects OS: создана")
    lines.append(f"Запись Voice Inbox: {voice_record.get('id', 'создана')}")
    return "\n".join(lines)


def local_timestamp(settings: Settings) -> str:
    try:
        tzinfo = ZoneInfo(settings.timezone)
    except ZoneInfoNotFoundError:
        tzinfo = ZoneInfo("UTC")
    return datetime.now(tzinfo).strftime("%Y%m%d_%H%M%S")


async def build_dispatcher(settings: Settings, bot: Bot) -> Dispatcher:
    router = Router()
    openai_processor = OpenAIProcessor(settings)
    airtable = AirtableClient(settings)

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        if not is_allowed(message, settings):
            await message.answer("Доступ запрещён.")
            return
        await message.answer("Голосовой inbox готов.")

    @router.message(Command("id"))
    async def user_id(message: Message) -> None:
        user = message.from_user
        await message.answer(str(user.id) if user else "Пользователь не определён.")

    @router.message()
    async def handle_message(message: Message) -> None:
        if not is_allowed(message, settings):
            await message.answer("Доступ запрещён.")
            return

        status = await message.answer("Обрабатываю...")
        try:
            content = await extract_content(message, bot, settings, openai_processor)
            if not content.raw_text.strip():
                await status.edit_text("Не нашёл текст для сохранения.")
                return
            structured = await openai_processor.structure_text(content.raw_text, content.message_type)
            voice_record, item_record, project = await asyncio.to_thread(
                save_to_airtable,
                airtable,
                settings,
                structured,
                content,
            )
            await status.edit_text(format_reply(structured, voice_record, item_record, project))
        except AirtableError:
            logger.exception("Airtable write failed")
            await status.edit_text("Не удалось записать в Airtable. Проверь логи бота.")
        except Exception:
            logger.exception("Message processing failed")
            await status.edit_text("Не удалось обработать сообщение. Проверь логи бота.")

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    return create_mobile_api(resolved_settings, AirtableClient(resolved_settings))


async def run_telegram_polling(settings: Settings, bot: Bot, dispatcher: Dispatcher) -> None:
    await bot.delete_webhook(drop_pending_updates=False)
    logger.info("Starting Telegram long polling")
    await dispatcher.start_polling(bot, handle_signals=False)


def install_shutdown_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)


async def stop_task(task: asyncio.Task, timeout: float) -> None:
    if task.done():
        return
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.allowed_user_ids:
        raise RuntimeError("ALLOWED_TELEGRAM_USER_IDS must contain at least one Telegram user id")

    settings.data_path.mkdir(parents=True, exist_ok=True)
    (settings.data_path / "incoming").mkdir(parents=True, exist_ok=True)

    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = await build_dispatcher(settings, bot)
    app = create_app(settings)
    config = uvicorn.Config(app, host=settings.http_host, port=settings.http_port, log_level="info")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None

    stop_event = asyncio.Event()
    install_shutdown_handlers(stop_event)

    telegram_task = asyncio.create_task(run_telegram_polling(settings, bot, dispatcher), name="telegram-polling")
    http_task = asyncio.create_task(server.serve(), name="http-api")
    shutdown_task = asyncio.create_task(stop_event.wait(), name="shutdown-signal")

    try:
        done, _ = await asyncio.wait(
            {telegram_task, http_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if shutdown_task in done:
            logger.info("Shutdown signal received")
        else:
            for task in done:
                task.result()
    finally:
        server.should_exit = True
        if not telegram_task.done():
            with contextlib.suppress(RuntimeError):
                await dispatcher.stop_polling()
        await stop_task(http_task, timeout=30)
        await stop_task(telegram_task, timeout=30)
        shutdown_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await shutdown_task
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
