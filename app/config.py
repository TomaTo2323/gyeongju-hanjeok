from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "경주한적 API"
    app_version: str = "1.0.0"
    app_env: str = "development"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    database_url: str = "sqlite:///./data/gyeongju_hanjeok.db"

    public_data_service_key: str = ""
    tour_api_base_url: str = "https://apis.data.go.kr/B551011/KorService2"
    congestion_api_base_url: str = "https://apis.data.go.kr/B551011/TatsCnctrRateService"
    related_api_base_url: str = "https://apis.data.go.kr/B551011/TarRlteTarService1"
    hub_api_base_url: str = "https://apis.data.go.kr/B551011/LocgoHubTarService1"
    photo_api_base_url: str = "https://apis.data.go.kr/B551011/PhotoGalleryService1"
    mobile_os: str = "ETC"
    mobile_app: str = "GyeongjuHanjeok"
    tour_area_code: str = "35"
    tour_sigungu_code: str = "2"
    admin_area_code: str = "47"
    admin_sigungu_code: str = "47130"

    kakao_rest_api_key: str = ""
    kakao_navi_base_url: str = "https://apis-navi.kakaomobility.com"
    kakao_walking_base_url: str = "https://apis-navi.kakaomobility.com/affiliate/walking"
    kakao_service_name: str = "gyeongju-hanjeok"
    enable_kakao_walking_api: bool = False

    kma_service_key: str = ""
    kma_base_url: str = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"

    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_base_url: str = "https://api.openai.com/v1"
    rag_min_similarity: float = 0.15

    naver_client_id: str = ""
    naver_client_secret: str = ""
    youtube_api_key: str = ""

    auth_secret_key: str = "gyeongju-hanjeok-dev-secret-change-me"
    auth_access_token_minutes: int = 10080
    terms_version: str = "2026-09-14"
    privacy_version: str = "2026-09-14"
    location_consent_version: str = "2026-09-14"
    notification_consent_version: str = "2026-09-21"

    firebase_project_id: str = ""
    firebase_service_account_json: str = ""

    # 비밀번호 찾기 이메일 인증 - Gmail API HTTPS
    # Railway Hobby에서는 SMTP가 차단되므로 Gmail API를 HTTPS로 호출합니다.
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    gmail_api_base_url: str = "https://gmail.googleapis.com/gmail/v1"
    google_oauth_token_url: str = "https://oauth2.googleapis.com/token"
    email_from_email: str = "datahater2323@gmail.com"
    email_from_name: str = "경주한적"

    # 기존 SMTP 설정은 다른 환경 호환성을 위해 유지합니다.
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_from_name: str = "경주한적"

    password_reset_code_minutes: int = 10
    password_reset_token_minutes: int = 10
    password_reset_max_attempts: int = 5
    password_reset_resend_seconds: int = 60

    public_invite_base_url: str = ""
    app_download_url: str = ""
    friend_invite_hours: int = 168

    congestion_threshold: float = 80.0
    replacement_congestion_threshold: float = 50.0
    max_candidate_places: int = 35
    max_course_places: int = 6
    default_stay_minutes: int = 45
    nsga_population_size: int = 60
    nsga_generations: int = 35
    http_timeout_seconds: float = 20.0
    cache_ttl_seconds: int = 600
    daily_sync_enabled: bool = True
    daily_sync_hour: int = 4

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: object) -> object:
        if not isinstance(value, str):
            return value

        stripped = value.strip()
        if stripped.startswith("postgres://"):
            return "postgresql+psycopg://" + stripped.removeprefix("postgres://")
        if stripped.startswith("postgresql://"):
            return "postgresql+psycopg://" + stripped.removeprefix("postgresql://")
        return stripped

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return ["*"]

            return [
                item.strip()
                for item in stripped.split(",")
                if item.strip()
            ]

        return value

    @property
    def database_path(self) -> Path | None:
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            return Path(
                self.database_url.removeprefix(prefix)
            ).resolve()

        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
