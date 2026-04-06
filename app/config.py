from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://angi:angi@localhost:5432/angi_lister"
    angi_api_key: str = "test-api-key-change-me"
    resend_api_key: str = ""
    sender_email: str = "Netic <noreply@mail.discordwell.com>"
    console_user: str = "admin"
    console_password: str = "admin"
    app_url: str = "https://angi.discordwell.com"
    session_secret: str = "angi-dev-session-secret-change-me"
    session_ttl_days: int = 7
    magic_link_ttl_minutes: int = 15
    worker_poll_interval: float = 1.0
    openai_api_key: str = ""
    openai_model: str = "gpt-5.4"
    openai_timeout: float = 10.0
    here_api_key: str = ""
    geocode_cache_ttl_days: int = 90

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
