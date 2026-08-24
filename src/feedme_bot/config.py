from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    groq_api_key: str = ""

    swiggy_mcp_base_url: str = "https://mcp.swiggy.com"
    swiggy_credentials_path: Path = Path("~/.config/feedme-bot/swiggy_credentials.json").expanduser()

    meta_whatsapp_token: str = ""
    meta_phone_number_id: str = ""
    meta_webhook_verify_token: str = ""

    # Phase 1: only this number may talk to the bot — everyone else is ignored.
    allowed_whatsapp_number: str = ""


settings = Settings()
