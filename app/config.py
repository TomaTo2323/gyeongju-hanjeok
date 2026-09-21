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

    # .env에서
    # CORS_ORIGINS=*
    # 또는
    # CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
    # 형태를 사용할 수 있습니다.
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    database_url: str = "sqlite:///./data/gyeongju_hanjeok.db"

    # -------------------------------------------------------------------------
    # 한국관광공사 / 공공데이터포털
    # -------------------------------------------------------------------------
    # 공공데이터포털에서 발급되는 일반 인증키.
    # 각 OpenAPI 서비스별 활용신청/승인은 별도로 필요합니다.
    public_data_service_key: str = ""

    # 국문 관광정보 서비스는 최신 KorService2 사용
    tour_api_base_url: str = "https://apis.data.go.kr/B551011/KorService2"

    # 아래 서비스들은 서로 별도의 OpenAPI이므로
    # KorService2 변경과 관계없이 각각의 서비스 URL을 유지합니다.
    congestion_api_base_url: str = (
        "https://apis.data.go.kr/B551011/TatsCnctrRateService"
    )
    related_api_base_url: str = (
        "https://apis.data.go.kr/B551011/TarRlteTarService1"
    )
    hub_api_base_url: str = (
        "https://apis.data.go.kr/B551011/LocgoHubTarService1"
    )
    photo_api_base_url: str = (
        "https://apis.data.go.kr/B551011/PhotoGalleryService1"
    )

    mobile_os: str = "ETC"
    mobile_app: str = "GyeongjuHanjeok"

    # 관광공사 TourAPI 지역코드
    # 경상북도 35 / 경주시 2
    tour_area_code: str = "35"
    tour_sigungu_code: str = "2"

    # 행정구역 코드 기반 API용
    admin_area_code: str = "47"
    admin_sigungu_code: str = "47130"

    # -------------------------------------------------------------------------
    # Kakao
    # -------------------------------------------------------------------------
    kakao_rest_api_key: str = ""

    kakao_navi_base_url: str = (
        "https://apis-navi.kakaomobility.com"
    )

    kakao_walking_base_url: str = (
        "https://apis-navi.kakaomobility.com/affiliate/walking"
    )

    kakao_service_name: str = "gyeongju-hanjeok"

    # 카카오 도보 길찾기 API 사용 승인 전에는 False
    enable_kakao_walking_api: bool = False

    # -------------------------------------------------------------------------
    # 기상청
    # -------------------------------------------------------------------------
    kma_service_key: str = ""

    kma_base_url: str = (
        "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
    )

    # -------------------------------------------------------------------------
    # OpenAI
    # -------------------------------------------------------------------------
    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_base_url: str = "https://api.openai.com/v1"
    rag_min_similarity: float = 0.15

    # -------------------------------------------------------------------------
    # Naver / YouTube
    # -------------------------------------------------------------------------
    naver_client_id: str = ""
    naver_client_secret: str = ""
    youtube_api_key: str = ""


    # -------------------------------------------------------------------------
    # 회원 인증 / 약관 버전
    # -------------------------------------------------------------------------
    # 배포 시 반드시 .env의 AUTH_SECRET_KEY를 충분히 긴 임의 문자열로 변경하세요.
    auth_secret_key: str = "gyeongju-hanjeok-dev-secret-change-me"
    auth_access_token_minutes: int = 10080  # 7일
    terms_version: str = "2026-09-14"
    privacy_version: str = "2026-09-14"
    location_consent_version: str = "2026-09-14"
    notification_consent_version: str = "2026-09-21"

    # Firebase Cloud Messaging
    firebase_project_id: str = ""
    firebase_service_account_json: str = ""

    # 비밀번호 찾기 이메일 인증
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

    # 친구 / 동행 초대
    # 실제 카카오 초대는 외부에서 접근 가능한 HTTPS 주소를 권장합니다.
    # 예: https://api.example.com
    public_invite_base_url: str = ""
    app_download_url: str = ""
    friend_invite_hours: int = 168  # 7일

    # -------------------------------------------------------------------------
    # 서비스 설정
    # -------------------------------------------------------------------------
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

    # -------------------------------------------------------------------------
    # Validators
    # -------------------------------------------------------------------------

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: object) -> object:
        """Railway PostgreSQL URL을 psycopg3 SQLAlchemy URL로 정규화합니다."""
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
        """
        환경변수에서 다음 형식을 모두 지원합니다.

        CORS_ORIGINS=*
        CORS_ORIGINS=http://localhost:3000
        CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000

        JSON 배열이 이미 전달된 경우에는 그대로 사용합니다.
        """
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
        """
        SQLite 데이터베이스를 사용할 경우 실제 파일 경로를 반환합니다.
        다른 DB URL인 경우 None을 반환합니다.
        """
        prefix = "sqlite:///"

        if self.database_url.startswith(prefix):
            return Path(
                self.database_url.removeprefix(prefix)
            ).resolve()

        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()