from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


VOICE_PROCESSING_ROUTE_CHATGPT_SUBSCRIPTION = "chatgpt_subscription"
VOICE_PROCESSING_ROUTE_OPENAI_API = "openai_api"
VOICE_PROCESSING_ROUTE_DISABLED = "disabled"
VOICE_PROCESSING_ROUTES = {
    VOICE_PROCESSING_ROUTE_CHATGPT_SUBSCRIPTION,
    VOICE_PROCESSING_ROUTE_OPENAI_API,
    VOICE_PROCESSING_ROUTE_DISABLED,
}


@dataclass(frozen=True)
class VoiceProcessingMode:
    route: str
    airtable_value: str
    intake_status: str
    russian_name: str
    description: str


VOICE_PROCESSING_MODES = {
    VOICE_PROCESSING_ROUTE_CHATGPT_SUBSCRIPTION: VoiceProcessingMode(
        route=VOICE_PROCESSING_ROUTE_CHATGPT_SUBSCRIPTION,
        airtable_value="ChatGPT Subscription",
        intake_status="Awaiting Subscription",
        russian_name="Подписка ChatGPT",
        description="Backend сохраняет запись и оригиналы в очередь Airtable/Google Drive без вызовов OpenAI API.",
    ),
    VOICE_PROCESSING_ROUTE_OPENAI_API: VoiceProcessingMode(
        route=VOICE_PROCESSING_ROUTE_OPENAI_API,
        airtable_value="OpenAI API",
        intake_status="New",
        russian_name="OpenAI API",
        description="Разрешены автоматические транскрибация, vision, структурирование и polling через OpenAI API.",
    ),
    VOICE_PROCESSING_ROUTE_DISABLED: VoiceProcessingMode(
        route=VOICE_PROCESSING_ROUTE_DISABLED,
        airtable_value="Disabled",
        intake_status="Processing Disabled",
        russian_name="Обработка отключена",
        description="Записи и оригиналы сохраняются, AI-обработка не запускается.",
    ),
}


def parse_utc_timestamp(value: str, *, setting_name: str = "VOICE_PROCESSOR_CREATED_AFTER") -> datetime:
    text = value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"{setting_name} must be an ISO 8601 UTC timestamp, "
            "for example 2026-07-19T02:00:00Z"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(
            f"{setting_name} must include UTC timezone, "
            "for example 2026-07-19T02:00:00Z"
        )
    return parsed.astimezone(timezone.utc)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    allowed_telegram_user_ids: str = Field(default="", alias="ALLOWED_TELEGRAM_USER_IDS")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
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
    voice_field_processing_route: str = Field(default="Processing Route", alias="VOICE_FIELD_PROCESSING_ROUTE")
    voice_field_processing_route_query_name: str = Field(
        default="Processing Route",
        alias="VOICE_FIELD_PROCESSING_ROUTE_QUERY_NAME",
    )
    voice_field_attachments: str = Field(default="Attachments", alias="VOICE_FIELD_ATTACHMENTS")
    voice_field_notes: str = Field(default="Notes", alias="VOICE_FIELD_NOTES")
    voice_field_external_id: str = Field(default="External ID", alias="VOICE_FIELD_EXTERNAL_ID")
    voice_field_external_id_query_name: str = Field(
        default="External ID",
        alias="VOICE_FIELD_EXTERNAL_ID_QUERY_NAME",
    )
    voice_field_google_drive: str = Field(default="Google Drive", alias="VOICE_FIELD_GOOGLE_DRIVE")
    voice_field_source: str = Field(default="Источник", alias="VOICE_FIELD_SOURCE")
    voice_field_source_query_name: str = Field(default="Источник", alias="VOICE_FIELD_SOURCE_QUERY_NAME")
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
    voice_field_training_status: str = Field(default="Training Status", alias="VOICE_FIELD_TRAINING_STATUS")
    voice_field_scope: str = Field(default="Scope", alias="VOICE_FIELD_SCOPE")
    voice_field_life_area: str = Field(default="Life Area", alias="VOICE_FIELD_LIFE_AREA")
    voice_field_category: str = Field(default="Category", alias="VOICE_FIELD_CATEGORY")
    voice_field_subcategory: str = Field(default="Subcategory", alias="VOICE_FIELD_SUBCATEGORY")
    voice_field_training_confirmed_at: str = Field(
        default="Training Confirmed At",
        alias="VOICE_FIELD_TRAINING_CONFIRMED_AT",
    )
    voice_field_training_answers_json: str = Field(
        default="Training Answers JSON",
        alias="VOICE_FIELD_TRAINING_ANSWERS_JSON",
    )
    voice_field_subscription_claim: str = Field(
        default="Subscription Queue Claim",
        alias="VOICE_FIELD_SUBSCRIPTION_CLAIM",
    )
    voice_field_subscription_claimed_at: str = Field(
        default="Subscription Queue Claimed At",
        alias="VOICE_FIELD_SUBSCRIPTION_CLAIMED_AT",
    )
    voice_training_created_after: str = Field(
        default="2026-07-24T00:00:00Z",
        alias="VOICE_TRAINING_CREATED_AFTER",
    )
    voice_training_queue_limit: int = Field(default=50, alias="VOICE_TRAINING_QUEUE_LIMIT")
    voice_training_backlog_limit: int = Field(default=20, alias="VOICE_TRAINING_BACKLOG_LIMIT")
    voice_training_similarity_limit: int = Field(default=5, alias="VOICE_TRAINING_SIMILARITY_LIMIT")
    voice_training_batch_limit: int = Field(default=20, alias="VOICE_TRAINING_BATCH_LIMIT")
    voice_training_rule_threshold: int = Field(default=3, alias="VOICE_TRAINING_RULE_THRESHOLD")
    voice_training_life_areas: str = Field(
        default="Дом,Семья,Здоровье,Финансы,Покупки,Документы,Обучение,Идеи,Отдых,Другое",
        alias="VOICE_TRAINING_LIFE_AREAS",
    )
    voice_training_taxonomy_table_name: str = Field(default="Таксономия", alias="VOICE_TRAINING_TAXONOMY_TABLE_NAME")

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

    dashboard_host: str = Field(default="127.0.0.1", alias="DASHBOARD_HOST")
    dashboard_port: int = Field(default=8081, alias="DASHBOARD_PORT")
    dashboard_public_origin: str = Field(default="http://127.0.0.1:8081", alias="DASHBOARD_PUBLIC_ORIGIN")
    dashboard_allowed_hosts: str = Field(default="127.0.0.1,localhost", alias="DASHBOARD_ALLOWED_HOSTS")
    dashboard_csrf_secret: str = Field(default="", alias="DASHBOARD_CSRF_SECRET")
    dashboard_page_size: int = Field(default=25, alias="DASHBOARD_PAGE_SIZE")
    dashboard_overview_max_records: int = Field(default=1000, alias="DASHBOARD_OVERVIEW_MAX_RECORDS")
    dashboard_max_form_bytes: int = Field(default=32_768, alias="DASHBOARD_MAX_FORM_BYTES")
    dashboard_write_rate_limit_per_minute: int = Field(default=30, alias="DASHBOARD_WRITE_RATE_LIMIT_PER_MINUTE")
    dashboard_airtable_view: str = Field(default="", alias="DASHBOARD_AIRTABLE_VIEW")
    dashboard_created_time_field: str = Field(default="", alias="DASHBOARD_CREATED_TIME_FIELD")
    dashboard_attachment_timeout_seconds: int = Field(default=30, alias="DASHBOARD_ATTACHMENT_TIMEOUT_SECONDS")

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

    voice_processing_route: str = Field(default="", alias="VOICE_PROCESSING_ROUTE")
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
    voice_processor_source_filter: str = Field(default="Android", alias="VOICE_PROCESSOR_SOURCE_FILTER")
    voice_processor_created_after: str = Field(default="", alias="VOICE_PROCESSOR_CREATED_AFTER")
    voice_processor_version: str = Field(default="v1", alias="VOICE_PROCESSOR_VERSION")
    voice_processor_stale_processing_seconds: int = Field(
        default=900,
        alias="VOICE_PROCESSOR_STALE_PROCESSING_SECONDS",
    )
    voice_processor_max_retries: int = Field(default=3, alias="VOICE_PROCESSOR_MAX_RETRIES")
    voice_processor_retry_base_seconds: float = Field(default=1.0, alias="VOICE_PROCESSOR_RETRY_BASE_SECONDS")
    voice_processor_max_prompt_chars: int = Field(default=24000, alias="VOICE_PROCESSOR_MAX_PROMPT_CHARS")
    voice_processor_max_rules: int = Field(default=8, alias="VOICE_PROCESSOR_MAX_RULES")
    voice_processor_max_file_bytes: int = Field(default=25_000_000, alias="VOICE_PROCESSOR_MAX_FILE_BYTES")
    voice_processor_max_record_bytes: int = Field(default=50_000_000, alias="VOICE_PROCESSOR_MAX_RECORD_BYTES")
    voice_processor_max_image_bytes: int = Field(default=4_000_000, alias="VOICE_PROCESSOR_MAX_IMAGE_BYTES")
    voice_processor_image_max_edge: int = Field(default=1600, alias="VOICE_PROCESSOR_IMAGE_MAX_EDGE")
    voice_processor_rules_table_id: str = Field(default="", alias="VOICE_PROCESSOR_RULES_TABLE_ID")
    voice_processor_rules_table_name: str = Field(default="Правила обработки", alias="VOICE_PROCESSOR_RULES_TABLE_NAME")

    google_drive_enabled: bool = Field(default=False, alias="GOOGLE_DRIVE_ENABLED")
    google_drive_root_folder_id: str = Field(default="", alias="GOOGLE_DRIVE_ROOT_FOLDER_ID")
    google_drive_credentials_file: str = Field(default="", alias="GOOGLE_DRIVE_CREDENTIALS_FILE")
    google_drive_token_file: str = Field(default="", alias="GOOGLE_DRIVE_TOKEN_FILE")
    google_drive_spool_dir: str = Field(default="/app/data/google_drive_spool", alias="GOOGLE_DRIVE_SPOOL_DIR")

    subscription_codex_binary: str = Field(default="codex", alias="SUBSCRIPTION_CODEX_BINARY")
    subscription_codex_auth_file: str = Field(default="", alias="SUBSCRIPTION_CODEX_AUTH_FILE")
    subscription_worker_lock_file: str = Field(
        default="/run/voice-inbox-subscription-worker/worker.lock",
        alias="SUBSCRIPTION_WORKER_LOCK_FILE",
    )
    subscription_worker_tmp_root: str = Field(
        default="/tmp/voice-inbox-subscription-worker",
        alias="SUBSCRIPTION_WORKER_TMP_ROOT",
    )
    subscription_worker_instance: str = Field(default="default", alias="SUBSCRIPTION_WORKER_INSTANCE")
    subscription_claim_timeout_seconds: int = Field(
        default=3600,
        alias="SUBSCRIPTION_CLAIM_TIMEOUT_SECONDS",
    )
    subscription_codex_timeout_seconds: int = Field(default=600, alias="SUBSCRIPTION_CODEX_TIMEOUT_SECONDS")
    subscription_stt_timeout_seconds: int = Field(default=600, alias="SUBSCRIPTION_STT_TIMEOUT_SECONDS")
    subscription_media_timeout_seconds: int = Field(default=120, alias="SUBSCRIPTION_MEDIA_TIMEOUT_SECONDS")
    subscription_max_pdf_pages: int = Field(default=12, alias="SUBSCRIPTION_MAX_PDF_PAGES")
    subscription_max_video_frames: int = Field(default=6, alias="SUBSCRIPTION_MAX_VIDEO_FRAMES")
    subscription_max_images: int = Field(default=12, alias="SUBSCRIPTION_MAX_IMAGES")
    subscription_max_prompt_chars: int = Field(default=24_000, alias="SUBSCRIPTION_MAX_PROMPT_CHARS")
    subscription_max_response_bytes: int = Field(default=65_536, alias="SUBSCRIPTION_MAX_RESPONSE_BYTES")
    subscription_stt_model: str = Field(default="small", alias="SUBSCRIPTION_STT_MODEL")
    subscription_stt_device: str = Field(default="cpu", alias="SUBSCRIPTION_STT_DEVICE")
    subscription_stt_compute_type: str = Field(default="int8", alias="SUBSCRIPTION_STT_COMPUTE_TYPE")
    subscription_stt_language: str = Field(default="ru", alias="SUBSCRIPTION_STT_LANGUAGE")
    subscription_stt_cache_dir: str = Field(
        default="/var/cache/voice-inbox-subscription-worker/whisper",
        alias="SUBSCRIPTION_STT_CACHE_DIR",
    )

    @field_validator("voice_processor_source_filter")
    @classmethod
    def normalize_voice_processor_source_filter(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("voice_processing_route")
    @classmethod
    def normalize_voice_processing_route(cls, value: str) -> str:
        return str(value or "").strip().casefold()

    @field_validator("voice_processor_created_after")
    @classmethod
    def validate_voice_processor_created_after(cls, value: str) -> str:
        text = str(value or "").strip()
        if text:
            parse_utc_timestamp(text)
        return text

    @field_validator("voice_training_created_after")
    @classmethod
    def validate_voice_training_created_after(cls, value: str) -> str:
        text = str(value or "").strip()
        if text:
            parse_utc_timestamp(text, setting_name="VOICE_TRAINING_CREATED_AFTER")
        return text

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

    @property
    def dashboard_allowed_host_set(self) -> set[str]:
        return {
            part.strip().casefold()
            for part in self.dashboard_allowed_hosts.replace(";", ",").split(",")
            if part.strip()
        }

    @property
    def voice_processor_created_after_datetime(self) -> datetime | None:
        if not self.voice_processor_created_after:
            return None
        return parse_utc_timestamp(self.voice_processor_created_after)

    @property
    def voice_training_created_after_datetime(self) -> datetime | None:
        if not self.voice_training_created_after:
            return None
        return parse_utc_timestamp(self.voice_training_created_after, setting_name="VOICE_TRAINING_CREATED_AFTER")

    @property
    def voice_training_life_area_options(self) -> list[str]:
        return [
            part.strip()
            for part in self.voice_training_life_areas.replace(";", ",").split(",")
            if part.strip()
        ]

    @property
    def effective_voice_processing_route(self) -> str:
        if self.voice_processing_route in VOICE_PROCESSING_ROUTES:
            return self.voice_processing_route
        return VOICE_PROCESSING_ROUTE_DISABLED

    @property
    def voice_processing_mode(self) -> VoiceProcessingMode:
        return VOICE_PROCESSING_MODES[self.effective_voice_processing_route]

    @property
    def openai_api_processor_enabled(self) -> bool:
        return self.effective_voice_processing_route == VOICE_PROCESSING_ROUTE_OPENAI_API

    @property
    def voice_processing_route_warning(self) -> str:
        if not self.voice_processing_route:
            return "VOICE_PROCESSING_ROUTE is missing; using safe route disabled"
        if self.voice_processing_route not in VOICE_PROCESSING_ROUTES:
            return "VOICE_PROCESSING_ROUTE has an unknown value; using safe route disabled"
        return ""


def validate_openai_api_configuration(settings: Settings) -> None:
    if not settings.openai_api_processor_enabled:
        return
    key = settings.openai_api_key.strip()
    if not key:
        raise RuntimeError("VOICE_PROCESSING_ROUTE=openai_api requires OPENAI_API_KEY")
    if not key.startswith("sk-") or len(key) < 20 or "REPLACE_ME" in key.upper():
        raise RuntimeError("VOICE_PROCESSING_ROUTE=openai_api requires a valid OPENAI_API_KEY")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
