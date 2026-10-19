"""Env settings. Read at call time so tests and smoke can override with env vars."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env_file() -> None:
    """Load ROOT/.env into os.environ without overriding real variables."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_env_file()


def _path(name: str, default: str) -> Path:
    p = Path(os.environ.get(name) or default).expanduser()
    return p if p.is_absolute() else ROOT / p


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


_probe = threading.local()


def _engine_has_llm() -> bool:
    """True when the classifier says its LLM path is live. Imported lazily because nlp imports config.

    A call that comes back in while the first is still asking (an nlp.llm_enabled() that reads
    Settings.mode) answers False instead of recursing.
    """
    if getattr(_probe, "busy", False):
        return False
    _probe.busy = True
    try:
        from app.analyze import nlp
        return bool(nlp.llm_enabled())
    except Exception:
        return False
    finally:
        _probe.busy = False


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    llm_model: str
    llm_mode: str
    db_path: Path
    ledger_path: Path
    log_level: str
    log_file: Path
    port: int
    open_log_window: bool
    api_url: str

    @property
    def llm_enabled(self) -> bool:
        return self.llm_mode != "offline" and bool(self.anthropic_api_key)

    @property
    def mode(self) -> str:
        """The engine that runs, not the flag. llm only when the config asks for it and nlp has a live LLM path."""
        return "llm" if self.llm_enabled and _engine_has_llm() else "offline"

    def redacted(self) -> dict:
        d = {k: str(v) for k, v in self.__dict__.items()}
        d["anthropic_api_key"] = "set" if self.anthropic_api_key else "unset"
        return d


def get_settings() -> Settings:
    port = _int("PORT", 8000)
    return Settings(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        llm_model=os.environ.get("LLM_MODEL") or "claude-haiku-5-5",
        llm_mode=(os.environ.get("LLM_MODE") or "auto").strip().lower(),
        db_path=_path("DB_PATH", "data/feedback.db"),
        ledger_path=_path("LEDGER_PATH", "data/ledger.jsonl"),
        log_level=(os.environ.get("LOG_LEVEL") or "INFO").upper(),
        log_file=_path("LOG_FILE", "logs/app.log"),
        port=port,
        open_log_window=(os.environ.get("OPEN_LOG_WINDOW") or "1") not in ("0", "false", "no"),
        api_url=(os.environ.get("API_URL") or f"http://127.0.0.1:{port}").rstrip("/"),
    )
