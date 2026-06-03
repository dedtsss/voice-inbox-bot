from __future__ import annotations

import json
from pathlib import Path

from openai import AsyncOpenAI

from app.config import Settings


class OpenAIProcessor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def transcribe_audio(self, audio_path: Path) -> str:
        with audio_path.open("rb") as audio_file:
            result = await self.client.audio.transcriptions.create(
                model=self.settings.openai_transcribe_model,
                file=audio_file,
            )
        return str(getattr(result, "text", "")).strip()

    async def structure_text(self, raw_text: str, message_type: str) -> dict:
        text = raw_text.strip()
        if not text:
            return _fallback_structure(raw_text, message_type)

        completion = await self.client.chat.completions.create(
            model=self.settings.openai_structuring_model,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You structure personal inbox notes for Airtable. "
                        "Return only valid JSON with keys: title, type, project, "
                        "project_confidence, priority, next_action, summary, clean_text, tags. "
                        "Write title, summary, next_action, clean_text, and tags in Russian by default. "
                        "If the source note is Russian, keep clean_text in Russian and do not translate "
                        "Russian voice notes to English. Use short practical Russian wording. "
                        "Never use English default phrases such as 'Review note content'. "
                        "If the next action is unclear, use 'Разобрать позже'. "
                        "If no action is actually needed, use 'Не требуется'. "
                        "project_confidence is 0..1. tags is an array of short strings. "
                        "If unsure about project, use empty project and confidence 0."
                    ),
                },
                {"role": "user", "content": text[:12000]},
            ],
        )
        content = completion.choices[0].message.content or "{}"
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return _fallback_structure(raw_text, message_type)
        return _normalize_structure(data, raw_text, message_type)


def _normalize_structure(data: dict, raw_text: str, message_type: str) -> dict:
    fallback = _fallback_structure(raw_text, message_type)
    normalized = {
        "title": _clean_str(data.get("title")) or fallback["title"],
        "type": _clean_str(data.get("type")) or message_type,
        "project": _clean_str(data.get("project")),
        "priority": _clean_str(data.get("priority")) or "Normal",
        "next_action": _normalize_next_action(data.get("next_action"), raw_text),
        "summary": _clean_str(data.get("summary")) or fallback["summary"],
        "clean_text": _clean_str(data.get("clean_text")) or raw_text.strip(),
        "tags": _clean_tags(data.get("tags")),
    }
    try:
        normalized["project_confidence"] = max(0.0, min(1.0, float(data.get("project_confidence", 0))))
    except (TypeError, ValueError):
        normalized["project_confidence"] = 0.0
    return normalized


def _fallback_structure(raw_text: str, message_type: str) -> dict:
    text = " ".join(raw_text.strip().split())
    title = text[:89].rstrip() + "..." if len(text) > 90 else text
    return {
        "title": title or "Заметка из Telegram",
        "type": message_type,
        "project": "",
        "project_confidence": 0.0,
        "priority": "Normal",
        "next_action": _default_next_action(raw_text),
        "summary": text[:500],
        "clean_text": raw_text.strip(),
        "tags": [],
    }


def _clean_str(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def _normalize_next_action(value: object, raw_text: str) -> str:
    action = _clean_str(value)
    if not action:
        return _default_next_action(raw_text)

    lowered = action.casefold()
    no_action_phrases = {
        "нет",
        "не требуется",
        "не нужно",
        "ничего не требуется",
        "none",
        "n/a",
        "no action needed",
        "not required",
    }
    generic_english_phrases = {
        "review note content",
        "review content",
        "review the note",
        "follow up",
        "follow up later",
    }
    if lowered in no_action_phrases:
        return "Не требуется"
    if lowered in generic_english_phrases:
        return "Разобрать позже"
    return action


def _default_next_action(raw_text: str) -> str:
    lowered = raw_text.casefold()
    no_action_markers = (
        "ничего делать не нужно",
        "действий не требуется",
        "не требует действий",
        "просто к сведению",
        "для информации",
        "без действия",
    )
    if any(marker in lowered for marker in no_action_markers):
        return "Не требуется"
    return "Разобрать позже"


def _clean_tags(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    for item in value:
        tag = _clean_str(item)
        if tag and tag not in tags:
            tags.append(tag[:50])
    return tags[:10]
