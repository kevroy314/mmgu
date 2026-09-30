"""Process-level configuration, read from environment variables (prefix ``MMGU_``) or ``.env``.

Anything a guild might want to change while the app is running (channels, feature toggles,
permission tweaks) lives in the database instead; see ``mmgu.core.store``.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


def _split(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [v.strip() for v in value if v.strip()]
    return [v.strip() for v in value.split(",") if v.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MMGU_", env_file=".env", extra="ignore")

    # Identity of this hall
    app_name: str = "Hallkeeper"
    guild_name: str = "Your Guild"
    base_url: str = "http://localhost:8420"

    # Storage
    data_dir: Path = REPO_ROOT / "data"
    database_url: str = ""  # default: sqlite file inside data_dir
    game_pack: str = "monsters-and-memories"

    # Web
    host: str = "0.0.0.0"
    port: int = 8420
    secret_key: str = ""  # generated and persisted to data_dir on first run when empty
    root_path: str = ""

    # Auth. Modes: discord (Discord OAuth2), header (trusted reverse proxy), dev (local testing only)
    auth_modes: str = "discord"
    proxy_secret: str = ""  # header mode: the proxy must send this in X-MMGU-Proxy-Secret
    proxy_email_header: str = "X-Email"
    dev_login: bool = False
    # Comma-separated identities that are made Leader on first login, e.g. "email:a@b.c,discord:1234"
    bootstrap_leaders: str = ""
    default_role: str = "recruit"

    # Discord
    discord_token: str = ""
    discord_guild_id: str = ""
    discord_client_id: str = ""
    discord_client_secret: str = ""
    # Comma-separated "<discord role id>=<hall role>" pairs, e.g. "1234=officer,5678=member"
    discord_role_map: str = ""

    run_bot: bool = True
    run_scheduler: bool = True

    @field_validator("data_dir", mode="after")
    @classmethod
    def _abs_data_dir(cls, v: Path) -> Path:
        return v if v.is_absolute() else (REPO_ROOT / v)

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{self.data_dir / 'mmgu.db'}"

    @property
    def is_sqlite(self) -> bool:
        return self.db_url.startswith("sqlite")

    @property
    def auth_mode_list(self) -> list[str]:
        return _split(self.auth_modes)

    @property
    def bootstrap_leader_list(self) -> list[str]:
        return _split(self.bootstrap_leaders)

    @property
    def discord_role_pairs(self) -> dict[str, str]:
        pairs = {}
        for item in _split(self.discord_role_map):
            if "=" in item:
                rid, role = item.split("=", 1)
                pairs[rid.strip()] = role.strip()
        return pairs

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)

    def resolved_secret(self) -> str:
        if self.secret_key:
            return self.secret_key
        self.ensure_dirs()
        path = self.data_dir / ".secret_key"
        if not path.exists():
            path.write_text(secrets.token_urlsafe(48))
            path.chmod(0o600)
        return path.read_text().strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
