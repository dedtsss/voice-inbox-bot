from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    allowed_telegram_user_ids: str = Field(default="", alias="ALLOWED_TELEGRAM_USER_IDS")

    openai_api_key: str = Field(alias="OPENAI_API_KEY")
    openai_transcribe_model: str = Field(default="gpt-4o-mini-transcribe", alias="OPENAI_TRANSCRIBE_MODEL")
    openai_structuring_model: str = Field(default="gpt-4o-mini", alias="OPENAI_STRUCTURING_MODEL")

    airtable_token: str = Field(alias="AIRTABLE_TOKEN")

    voice_inbox_base_id: str = Field(alias="VOICE_INBOX_BASE_ID")
    voice_inbox_table_id: str = Field(alias="VOICE_INBOX_TABLE_ID")
    voice_field_title: str = Field(alias="VOICE_FIELD_TITLE")
    voice_field_type: str = Field(alias="VOICE_FIELD_TYPE")
    voice_field_project: str = Field(alias="VOICE_FIELD_PROJECT")
    voice_field_priority: str = Field(alias="VOICE_FIELD_PRIORITY")
    voice_field_next_action: str = Field(alias="VOICE_FIELD_NEXT_ACTION")
    voice_field_summary: str = Field(alias="VOICE_FIELD_SUMMARY")
    voice_field_clean_text: str = Field(alias="VOICE_FIELD_CLEAN_TEXT")
    voice_field_raw_text: str = Field(alias="VOICE_FIELD_RAW_TEXT")
    voice_field_tags: str = Field(alias="VOICE_FIELD_TAGS")
    voice_field_processing_status: str = Field(alias="VOICE_FIELD_PROCESSING_STATUS")
    voice_field_attachments: str = Field(default="fld7RljviBo0ybvnP", alias="VOICE_FIELD_ATTACHMENTS")
    voice_field_notes: str = Field(default="Notes", alias="VOICE_FIELD_NOTES")

    projects_base_id: str = Field(alias="PROJECTS_BASE_ID")
    projects_table_id: str = Field(alias="PROJECTS_TABLE_ID")
    projects_field_title: str = Field(alias="PROJECTS_FIELD_TITLE")

    items_table_id: str = Field(alias="ITEMS_TABLE_ID")
    items_field_title: str = Field(alias="ITEMS_FIELD_TITLE")
    items_field_project: str = Field(alias="ITEMS_FIELD_PROJECT")
    items_field_type: str = Field(alias="ITEMS_FIELD_TYPE")
    items_field_status: str = Field(alias="ITEMS_FIELD_STATUS")
    items_field_priority: str = Field(alias="ITEMS_FIELD_PRIORITY")
    items_field_text: str = Field(alias="ITEMS_FIELD_TEXT")
    items_field_next_action: str = Field(alias="ITEMS_FIELD_NEXT_ACTION")
    items_field_source: str = Field(alias="ITEMS_FIELD_SOURCE")
    items_field_date: str = Field(alias="ITEMS_FIELD_DATE")
    items_field_block: str = Field(default="", alias="ITEMS_FIELD_BLOCK")
    items_field_stage: str = Field(default="", alias="ITEMS_FIELD_STAGE")

    write_to_projects_os: bool = Field(default=True, alias="WRITE_TO_PROJECTS_OS")
    save_media_files: bool = Field(default=True, alias="SAVE_MEDIA_FILES")
    data_dir: str = Field(default="/app/data", alias="DATA_DIR")
    timezone: str = Field(default="Europe/Moscow", alias="TIMEZONE")

    http_host: str = Field(default="0.0.0.0", alias="HTTP_HOST")
    http_port: int = Field(default=8080, alias="HTTP_PORT")

    mobile_inbox_token: str = Field(default="", alias="MOBILE_INBOX_TOKEN")
    android_raw_mode: bool = Field(default=True, alias="ANDROID_RAW_MODE")
    mobile_inbox_max_file_bytes: int = Field(default=5_000_000, alias="MOBILE_INBOX_MAX_FILE_BYTES")
    mobile_inbox_max_files: int = Field(default=5, alias="MOBILE_INBOX_MAX_FILES")
    mobile_inbox_max_request_bytes: int = Field(default=25_000_000, alias="MOBILE_INBOX_MAX_REQUEST_BYTES")
    mobile_inbox_max_payload_bytes: int = Field(default=65_536, alias="MOBILE_INBOX_MAX_PAYLOAD_BYTES")
    mobile_inbox_allowed_mime_types: str = Field(
        default=(
            "audio/aac,audio/mp3,audio/mp4,audio/mpeg,audio/ogg,audio/opus,audio/wav,audio/webm,"
            "audio/x-m4a,image/heic,image/heif,image/jpeg,image/png,image/webp,"
            "application/json,application/pdf,text/plain"
        ),
        alias="MOBILE_INBOX_ALLOWED_MIME_TYPES",
    )
    airtable_upload_base_url: str = Field(
        default="https://content.airtable.com/v0",
        alias="AIRTABLE_UPLOAD_BASE_URL",
    )

    @property
    def allowed_user_ids(self) -> set[int]:
        ids: set[int] = set()
        for raw_part in self.allowed_telegram_user_ids.replace(";", ",").split(","):
            part = raw_part.strip()
            if not part:
                continue
            ids.add(int(part))
        return ids

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def allowed_mobile_mime_types(self) -> set[str]:
        return {
            part.strip().casefold()
            for part in self.mobile_inbox_allowed_mime_types.replace(";", ",").split(",")
            if part.strip()
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
