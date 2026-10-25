"""Private runtime settings and developer credentials, independent of public data."""

import hashlib
import json
import math
import os
import re
import secrets
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

RUNTIME_FIELDS = {
    "base_url",
    "api_key",
    "model",
    "reasoning_effort",
    "daily_token_limit",
    "monthly_token_limit",
    "daily_call_limit",
    "model_prices",
    "max_completion_tokens",
    "model_timeout",
    "mail_enabled",
    "smtp_host",
    "smtp_port",
    "smtp_security",
    "smtp_username",
    "smtp_password",
    "mail_from",
    "mail_to",
    "mail_timeout",
}


class Settings:
    def __init__(self, config):
        self.config = config
        self.path = Path(config.database).resolve().parent / "runtime-config.json"
        if config.database != ":memory:" and self.path.exists():
            self.apply(json.loads(self.path.read_text(encoding="utf-8")), persist=False)

    def public(self):
        return {
            **{
                name: getattr(self.config, name)
                for name in RUNTIME_FIELDS - {"api_key", "smtp_password"}
            },
            "api_key_configured": bool(self.config.api_key),
            "smtp_password_configured": bool(self.config.smtp_password),
            "model_prices": self.config.model_prices,
        }

    def apply(self, changes, persist=True):
        if (
            not isinstance(changes, dict)
            or not changes
            or set(changes) - RUNTIME_FIELDS
        ):
            raise ValueError("invalid settings")
        proposed = replace(self.config, **changes)
        if type(proposed.mail_enabled) is not bool:
            raise ValueError("invalid mail switch")
        for name in (
            "smtp_host",
            "smtp_username",
            "smtp_password",
            "mail_from",
            "mail_to",
            "smtp_security",
        ):
            value = getattr(proposed, name)
            if (
                not isinstance(value, str)
                or len(value) > 1024
                or any(c in value for c in "\r\n\x00")
            ):
                raise ValueError("invalid mail setting")
        if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", proposed.smtp_host):
            raise ValueError("invalid SMTP host")
        if proposed.smtp_security not in ("ssl", "starttls"):
            raise ValueError("TLS is required")
        for name in ("mail_from", "mail_to"):
            value = getattr(proposed, name)
            if value and (
                len(value) > 254
                or not re.fullmatch(
                    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}",
                    value,
                )
            ):
                raise ValueError("invalid mail address")
        if proposed.mail_enabled and not all(
            (
                proposed.smtp_username,
                proposed.smtp_password,
                proposed.mail_from,
                proposed.mail_to,
            )
        ):
            raise ValueError("incomplete mail settings")
        for name in ("base_url", "model", "reasoning_effort", "api_key"):
            if not isinstance(getattr(proposed, name), str):
                raise ValueError("invalid string")
        url = urlsplit(proposed.base_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or len(proposed.base_url) > 2048
        ):
            raise ValueError("invalid upstream URL")
        if (
            not proposed.model
            or len(proposed.model) > 128
            or any(c.isspace() for c in proposed.model)
        ):
            raise ValueError("invalid model")
        if proposed.reasoning_effort not in (
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        ):
            raise ValueError("invalid reasoning effort")
        if len(proposed.api_key) > 4096 or any(c.isspace() for c in proposed.api_key):
            raise ValueError("invalid key")
        if "model_prices" in changes:
            prices = proposed.model_prices
            if not isinstance(prices, dict) or len(prices) > 1000:
                raise ValueError("invalid model prices")
            for model, price in prices.items():
                if (not isinstance(model, str) or not model or len(model) > 128
                        or not isinstance(price, dict)
                        or set(price) != {"input", "cached_input", "output"}
                        or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 or v > 1_000_000 for v in price.values())):
                    raise ValueError("invalid model prices")
        for name, low, high in (
            ("daily_token_limit", 0, 10**10),
            ("monthly_token_limit", 0, 10**11),
            ("daily_call_limit", 0, 10**7),
            ("max_completion_tokens", 256, 131072),
            ("model_timeout", 10, 600),
            ("smtp_port", 1, 65535),
            ("mail_timeout", 3, 60),
        ):
            if (
                type(getattr(proposed, name)) is not int
                or not low <= getattr(proposed, name) <= high
            ):
                raise ValueError("invalid numeric setting")
        # Atomic replacement and restrictive permissions; secrets never enter public SQL responses.
        if persist and self.config.database != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(".runtime-" + secrets.token_hex(8))
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(
                        {name: getattr(proposed, name) for name in RUNTIME_FIELDS},
                        stream,
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        for name in RUNTIME_FIELDS:
            setattr(self.config, name, getattr(proposed, name))
        return self.public()


def key_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()
