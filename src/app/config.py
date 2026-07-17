from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
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
    voice_field_due_date: str = Field(default="Срок", alias="VOICE_FIELD_DUE_DATE")
    voice_field_counterparty: str = Field(default="Контрагент", alias="VOICE_FIELD_COUNTERPARTY")
    voice_field_amount: str = Field(default="Сумма", alias="VOICE_FIELD_AMOUNT")
    voice_field_period: str = Field(default="Период", alias="VOICE_FIELD_PERIOD")
    voice_field_next_action: str = Field(alias="VOICE_FIELD_NEXT_ACTION")
    voice_field_summary: str = Field(alias="VOICE_FIELD_SUMMARY")
    voice_field_clean_text: str = Field(alias="VOICE_FIELD_CLEAN_TEXT")
    voice_field_raw_text: str = Field(alias="VOICE_FIELD_RAW_TEXT")
    voice_field_tags: str = Field(alias="VOICE_FIELD_TAGS")
    voice_field_processing_status: str = Field(alias="VOICE_FIELD_PROCESSING_STATUS")
    voice_field_processing_status_query_name: str = Field(
        default="Статус обработки",
        alias="VOICE_FIELD_PROCESSING_STATUS_QUERY_NAME",
    )
    voice_field_attachments: str = Field(default="fld7RljviBo0ybvnP", alias="VOICE_FIELD_ATTACHMENTS")
    voice_field_notes: str = Field(default="Notes", alias="VOICE_FIELD_NOTES")
    voice_field_external_id: str = Field(default="External ID", alias="VOICE_FIELD_EXTERNAL_ID")
    voice_field_external_id_query_name: str = Field(
        default="External ID",
        alias="VOICE_FIELD_EXTERNAL_ID_QUERY_NAME",
    )
    voice_field_google_drive: str = Field(default="Google Drive", alias="VOICE_FIELD_GOOGLE_DRIVE")
    voice_field_source: str = Field(default="Источник", alias="VOICE_FIELD_SOURCE")
    voice_field_processing_error: str = Field(default="Ошибка обработки", alias="VOICE_FIELD_PROCESSING_ERROR")
    voice_field_ai_result_json: str = Field(default="AI результат JSON", alias="VOICE_FIELD_AI_RESULT_JSON")
    voice_field_ai_confidence: str = Field(default="Уверенность AI", alias="VOICE_FIELD_AI_CONFIDENCE")
    voice_field_processor_version: str = Field(default="Версия обработчика", alias="VOICE_FIELD_PROCESSOR_VERSION")
    voice_field_train_on_correction: str = Field(
        default="Обучить на исправлении",
        alias="VOICE_FIELD_TRAIN_ON_CORRECTION",
    )
    voice_field_correction_comment: str = Field(
        default="Комментарий к исправлению",
        alias="VOICE_FIELD_CORRECTION_COMMENT",
    )
    voice_field_training_applied: str = Field(default="Обучение учтено", alias="VOICE_FIELD_TRAINING_APPLIED")

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
            "video/mp4,video/quicktime,video/webm,application/json,application/pdf,text/plain"
        ),
        alias="MOBILE_INBOX_ALLOWED_MIME_TYPES",
    )
    airtable_upload_base_url: str = Field(
        default="https://content.airtable.com/v0",
        alias="AIRTABLE_UPLOAD_BASE_URL",
    )
    airtable_auto_ensure_fields: bool = Field(default=False, alias="AIRTABLE_AUTO_ENSURE_FIELDS")

    voice_processor_enabled: bool = Field(default=False, alias="VOICE_PROCESSOR_ENABLED")
    voice_processor_interval_seconds: int = Field(default=60, alias="VOICE_PROCESSOR_INTERVAL_SECONDS")
    voice_processor_batch_size: int = Field(default=5, alias="VOICE_PROCESSOR_BATCH_SIZE")
    voice_processor_text_model: str = Field(default="gpt-4o-mini", alias="VOICE_PROCESSOR_TEXT_MODEL")
    voice_processor_transcription_model: str = Field(
        default="gpt-4o-transcribe",
        alias="VOICE_PROCESSOR_TRANSCRIPTION_MODEL",
    )
    voice_processor_confidence_threshold: float = Field(default=0.80, alias="VOICE_PROCESSOR_CONFIDENCE_THRESHOLD")
    voice_processor_max_video_frames: int = Field(default=12, alias="VOICE_PROCESSOR_MAX_VIDEO_FRAMES")
    voice_processor_video_frame_interval_seconds: int = Field(
        default=5,
        alias="VOICE_PROCESSOR_VIDEO_FRAME_INTERVAL_SECONDS",
    )
    voice_processor_create_project_items: bool = Field(
        default=False,
        validation_alias=AliasChoices("VOICE_PROCESSOR_CREATE_PROJECT_ITEMS", "PROCESSOR_CREATE_PROJECT_ITEMS"),
    )
    voice_processor_version: str = Field(default="v1", alias="VOICE_PROCESSOR_VERSION")
    voice_processor_stale_processing_seconds: int = Field(
        default=900,
        alias="VOICE_PROCESSOR_STALE_PROCESSING_SECONDS",
    )
    voice_processor_max_retries: int = Field(default=3, alias="VOICE_PROCESSOR_MAX_RETRIES")
    voice_processor_retry_base_seconds: float = Field(default=1.0, alias="VOICE_PROCESSOR_RETRY_BASE_SECONDS")
    voice_processor_max_prompt_chars: int = Field(default=24000, alias="VOICE_PROCESSOR_MAX_PROMPT_CHARS")
    voice_processor_max_rules: int = Field(default=8, alias="VOICE_PROCESSOR_MAX_RULES")
    voice_processor_max_image_bytes: int = Field(default=4_000_000, alias="VOICE_PROCESSOR_MAX_IMAGE_BYTES")
    voice_processor_image_max_edge: int = Field(default=1600, alias="VOICE_PROCESSOR_IMAGE_MAX_EDGE")
    voice_processor_rules_table_id: str = Field(default="", alias="VOICE_PROCESSOR_RULES_TABLE_ID")
    voice_processor_rules_table_name: str = Field(default="Правила обработки", alias="VOICE_PROCESSOR_RULES_TABLE_NAME")

    google_drive_enabled: bool = Field(default=False, alias="GOOGLE_DRIVE_ENABLED")
    google_drive_root_folder_id: str = Field(default="", alias="GOOGLE_DRIVE_ROOT_FOLDER_ID")
    google_drive_credentials_file: str = Field(default="", alias="GOOGLE_DRIVE_CREDENTIALS_FILE")
    google_drive_token_file: str = Field(default="", alias="GOOGLE_DRIVE_TOKEN_FILE")
    google_drive_spool_dir: str = Field(default="/app/data/google_drive_spool", alias="GOOGLE_DRIVE_SPOOL_DIR")

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
