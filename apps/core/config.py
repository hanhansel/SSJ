from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator
from apps.core.paths import load_runtime_environment

# Ensure runtime environment variables (from ssj.env or .env) are loaded into os.environ
_loaded_env_path = load_runtime_environment()

class AppSettings(BaseSettings):
    """
    Fail-fast configuration using Pydantic.
    All required settings must be present and valid, otherwise the app will crash immediately on startup.
    """
    app_env: str = Field(default='development', alias='APP_ENV')
    secret_key: str = Field(default='')
    
    # Database
    database_url: str = Field(default='')
    
    # Server configuration
    ssj_internal_host: str = Field(default='0.0.0.0')
    ssj_internal_port: int = Field(default=32425)
    
    # SMTP Configuration
    smtp_host: str = Field(default='')
    smtp_port: int = Field(default=587)
    smtp_username: str = Field(default='')
    smtp_password: str = Field(default='')
    smtp_from_email: str = Field(default='')
    smtp_use_tls: bool = Field(default=True)
    smtp_use_ssl: bool = Field(default=False)
    
    # Session
    session_cookie_secure: bool = Field(default=False)

    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore'
    )

    @field_validator('secret_key', mode='after')
    @classmethod
    def validate_secret_key(cls, v: str, info) -> str:
        if info.data.get('app_env') == 'production' and not v:
            raise ValueError('SECRET_KEY must be set when APP_ENV=production.')
        return v

    @field_validator('database_url', mode='after')
    @classmethod
    def validate_database_url(cls, v: str, info) -> str:
        if info.data.get('app_env') == 'production' and not v:
            raise ValueError('A PostgreSQL DATABASE_URL must be set when APP_ENV=production.')
        if v.startswith('postgres://'):
            return v.replace('postgres://', 'postgresql://', 1)
        return v

    @field_validator('smtp_from_email', mode='after')
    @classmethod
    def validate_smtp(cls, v: str, info) -> str:
        # SMTP is optional; only validate completeness if smtp_host is provided
        smtp_host = info.data.get('smtp_host')
        if smtp_host:
            if not info.data.get('smtp_username') or not info.data.get('smtp_password') or not v:
                raise ValueError('The SMTP configuration is incomplete (host, username, password and from_email are required when SMTP is used).')
        return v

    @field_validator('smtp_use_ssl', mode='after')
    @classmethod
    def validate_smtp_security(cls, v: bool, info) -> bool:
        smtp_host = info.data.get('smtp_host')
        if smtp_host:
            if v == info.data.get('smtp_use_tls'):
                raise ValueError('Choose exactly one SMTP mode: STARTTLS or direct SSL.')
        return v

settings = AppSettings()
