"""Chargement de la configuration.

- `config.json` : tout ce qui est réglable (seuils, sources, mots-clés...). Aucun secret.
- Variables d'environnement / `.env` : uniquement les secrets et réglages réseau sensibles.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent


@dataclass
class Secrets:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    proxy_url: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


def load_dotenv_if_present(path: Path | None = None) -> None:
    """Charge `.env` s'il existe (en local). Sur GitHub Actions, les secrets sont déjà dans l'environnement."""
    env_path = path or ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except ImportError:  # python-dotenv absent : lecture minimale
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_secrets() -> Secrets:
    extra: dict[str, str] = {}
    raw = os.environ.get("EXTRA_HEADERS_JSON", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                extra = {str(k): str(v) for k, v in parsed.items()}
            else:
                log.warning("EXTRA_HEADERS_JSON doit être un objet JSON {...} : ignoré")
        except json.JSONDecodeError as exc:
            log.warning("EXTRA_HEADERS_JSON invalide (%s) : ignoré", exc)
    return Secrets(
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        proxy_url=os.environ.get("PROXY_URL", "").strip(),
        extra_headers=extra,
    )


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else ROOT / "config.json"
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    for section in ("general", "http", "sources", "filters", "triggers", "verification", "telegram"):
        cfg.setdefault(section, {})
    return cfg
