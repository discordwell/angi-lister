from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://angi:angi@localhost:5432/angi_lister"
    angi_api_key: str = "test-api-key-change-me"
    resend_api_key: str = ""
    sender_email: str = "Netic <noreply@mail.discordwell.com>"
    console_user: str = "admin"
    console_password: str = "admin"
    worker_poll_interval: float = 1.0

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
