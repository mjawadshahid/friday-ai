"""Central config: loads provider credentials and model name from .env.

Why a single config module?
    The "brain" should never know which provider we're talking to. By
    loading everything here and re-exporting it, the rest of the codebase
    just imports `from config import settings` and stays portable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Find the project root (where this file lives) and load .env from there.
# load_dotenv is a no-op if .env doesn't exist, so the app still starts.
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    """Frozen = immutable, so nothing in the app can accidentally swap models mid-run."""
    api_key: str
    base_url: str
    model: str
    destructive_threshold: int  # files; tools must confirm above this

    @property
    def has_key(self) -> bool:
        return bool(self.api_key) and not self.api_key.endswith("REPLACE_ME")


def load_settings() -> Settings:
    api_key = os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    model = os.getenv("FRIDAY_MODEL", "meta-llama/llama-3.1-70b-instruct")
    threshold = int(os.getenv("FRIDAY_DESTRUCTIVE_THRESHOLD", "5"))
    return Settings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        destructive_threshold=threshold,
    )


settings = load_settings()
