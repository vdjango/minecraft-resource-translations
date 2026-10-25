import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 18448
    public_url: str = "http://127.0.0.1:18448"
    database: str = "data/translations.sqlite3"
    base_url: str = "https://drc.totokens.cc/v1"
    api_key: str = field(default="", repr=False)
    model: str = "gpt-6.1-sol"
    reasoning_effort: str = "medium"
    workers: int = 2
    max_completion_tokens: int = 16384
    daily_token_limit: int = 1000000
    monthly_token_limit: int = 20000000
    daily_call_limit: int = 2000
    model_prices: dict = field(default_factory=dict)
    admin_token: str = field(default="", repr=False)
    source_interval: int = 300
    modrinth_url: str = "https://api.modrinth.com/v2"
    curseforge_url: str = "https://download.bugg.icu/api/curseforge/v1"
    curseforge_key: str = field(default="", repr=False)
    model_timeout: int = 120
    max_queue: int = 1000
    session_fields_per_hour: int = 200
    trusted_proxy: bool = True
    mail_enabled: bool = False
    smtp_host: str = "smtp.163.com"
    smtp_port: int = 465
    smtp_security: str = "ssl"
    smtp_username: str = ""
    smtp_password: str = field(default="", repr=False)
    mail_from: str = ""
    mail_to: str = ""
    mail_timeout: int = 15
    federation_role: str = "hub"
    federation_hub: str = "https://translate.bugg.icu"
    federation_name: str = "Community translation node"
    federation_region: str = "unspecified"
    federation_contact: str = ""
    federation_cache_mb: int = 512
    # Test-only opt-in for private-network laboratories; production stays HTTPS.
    federation_allow_private: bool = False

    @classmethod
    def from_env(cls):
        values = {}
        for name, item in cls.__dataclass_fields__.items():
            value = os.environ.get("TRANSLATION_" + name.upper())
            if value is not None:
                values[name] = (
                    json.loads(value)
                    if name == "model_prices"
                    else
                    value.lower() in ("1", "true", "yes")
                    if item.type is bool
                    else int(value)
                    if item.type is int
                    else value
                )
        config = cls(**values)
        if config.federation_role not in ("hub", "mirror") or not 16 <= config.federation_cache_mb <= 32768:
            raise ValueError("Invalid federation configuration")
        for url in (config.base_url, config.modrinth_url, config.curseforge_url):
            parsed = urlsplit(url)
            if parsed.scheme != "https" and parsed.hostname not in (
                "127.0.0.1",
                "localhost",
                "::1",
            ):
                raise ValueError("Upstream URLs must use HTTPS")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(
                    "Credentials and query strings are not allowed in upstream URLs"
                )
        if not 1 <= config.workers <= 16 or config.max_completion_tokens < 256:
            raise ValueError("Invalid worker/token configuration")
        return config


def load_env(path=".env"):
    """Small literal env reader; never evaluate shell expressions."""
    file = Path(path)
    if file.is_file():
        for line in file.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key.strip().startswith("TRANSLATION_"):
                    os.environ.setdefault(key.strip(), value.strip())
