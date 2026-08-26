from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    groq_api_key: str = ""

    swiggy_mcp_base_url: str = "https://mcp.swiggy.com"
    swiggy_credentials_path: Path = Path("~/.config/feedme-bot/swiggy_credentials.json")

    meta_whatsapp_token: str = ""
    meta_phone_number_id: str = ""
    meta_webhook_verify_token: str = ""

    # Phase 1: only this number may talk to the bot — everyone else is ignored.
    allowed_whatsapp_number: str = ""

    @field_validator("swiggy_credentials_path")
    @classmethod
    def _expand_path(cls, v: Path) -> Path:
        # Real bug caught live: the Python-literal default had .expanduser()
        # applied, but a value loaded from .env is just cast to Path with no
        # expansion — "~" was being treated as a literal directory name,
        # silently writing credentials into whatever the process's cwd
        # happened to be instead of the real home directory. Expanding here
        # covers both sources regardless of where the value came from.
        return v.expanduser()


settings = Settings()
