"""Configuration loading and validation.

YAML files live in the project `config/` directory (or wherever
$AUTOAPPLY_CONFIG_DIR points). This module turns them into validated
pydantic models give to every pipeline stage.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, get_origin

import yaml
from pydantic import BaseModel, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("AUTOAPPLY_CONFIG_DIR", PROJECT_ROOT / "config"))
DATA_DIR = Path(os.environ.get("AUTOAPPLY_DATA_DIR", PROJECT_ROOT / "data"))
PROFILE_DIR = DATA_DIR / "profiles"
LOG_DIR = DATA_DIR / "logs"


class _ConfigModel(BaseModel):
    """Base for all config models: tolerate `key:` with no value in YAML
    (which parses as None) by coercing None -> [] for list-typed fields."""

    @field_validator("*", mode="before")
    @classmethod
    def _none_to_empty_list(cls, value, info) -> Any:
        annotation = getattr(info, "annotation", None)
        if annotation is None:
            field = cls.model_fields.get(info.field_name)
            annotation = field.annotation if field else None
        if value is None and (annotation is list or get_origin(annotation) is list):
            return []
        return value


class SearchFilter(_ConfigModel):
    posted_days: int | None = None
    remote_only: bool = False
    experience: str | None = None


class SearchProfile(_ConfigModel):
    name: str
    base_keywords: str
    extra_keywords: list[str] = Field(default_factory=list)
    locations: list[str]
    filters: SearchFilter = Field(default_factory=SearchFilter)

    def keyword_combos(self) -> list[str]:
        combos = [self.base_keywords, *self.extra_keywords]
        seen: list[str] = []
        for c in combos:
            if c not in seen:
                seen.append(c)
        return seen


class SearchesConfig(_ConfigModel):
    sources: list[str] = Field(default_factory=lambda: ["linkedin"])
    searches: list[SearchProfile] = Field(default_factory=list)


class ProfileConfig(_ConfigModel):
    target_titles: list[str] = Field(default_factory=list)
    must_skills: list[str] = Field(default_factory=list)
    nice_skills: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    salary_floor: int | None = None
    min_experience_years: float = 0.0
    excluded_employers: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    exclude_intern: bool = True
    resume_files: dict[str, str] = Field(default_factory=dict)


class TelegramConfig(_ConfigModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    ask_timeout_seconds: int = 480


class LlmEquivalence(_ConfigModel):
    key: str = ""
    any_of: list[str] = Field(default_factory=list)


class LlmConfig(_ConfigModel):
    """Local LLM (Ollama) semantic matching for Easy Apply questions."""

    enabled: bool = False
    model: str = "qwen2.5:3b"
    host: str = "http://localhost:11434"
    timeout_seconds: int = 90
    min_confidence: int = 75
    equivalences: list[LlmEquivalence] = Field(default_factory=list)
    never_mix: list[list[str]] = Field(default_factory=list)


class AnswersConfig(_ConfigModel):
    """Easy Apply 'required question label' -> value map.

    The apply engine only answers fields whose question label matches a key
    here (case-insensitive substring). Anything it can't answer confidently
    is abandoned and flagged for manual input instead of being auto-submitted.
    """

    values: dict[str, str] = Field(default_factory=dict)

    def match(self, label: str) -> str | None:
        norm = re.sub(r"[:*()\[\]/\\]+", " ", label or "").lower()
        norm = re.sub(r"\s+", " ", norm).strip()
        for key, value in self.values.items():
            k = re.sub(r"[:*()\[\]/\\]+", " ", key).lower()
            k = re.sub(r"\s+", " ", k).strip()
            if len(k) >= 3 and k in norm:
                return value
        return None

    def save(self) -> None:
        """Persist `values` back to answers.yaml, preserving the leading comments."""
        path = CONFIG_DIR / "answers.yaml"
        header = ""
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
            idx = 0
            while idx < len(lines):
                stripped = lines[idx].strip()
                if stripped and not stripped.startswith("#"):
                    break
                idx += 1
            if idx:
                header = "\n".join(lines[:idx]) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".yaml.tmp")
        payload = yaml.safe_dump(
            {"values": dict(self.values)}, sort_keys=False, allow_unicode=True
        )
        tmp.write_text(header + payload, encoding="utf-8")
        os.replace(tmp, path)


class SourceCaps(_ConfigModel):
    pages_per_search: int = 2
    search_delay_seconds: list[float] = Field(default_factory=lambda: [30.0, 60.0])
    detail_delay_seconds: list[float] = Field(default_factory=lambda: [8.0, 20.0])
    scroll_pause_seconds: list[float] = Field(default_factory=lambda: [1.5, 3.5])
    max_searches_per_run: int | None = None

    @field_validator(
        "search_delay_seconds", "detail_delay_seconds", "scroll_pause_seconds"
    )
    @classmethod
    def _delay_range(cls, v: list[float]) -> list[float]:
        if len(v) != 2:
            raise ValueError("delay ranges must be exactly [min, max]")
        return v


class GlobalCaps(_ConfigModel):
    max_searches_per_run: int = 8
    max_listings_per_run: int = 30
    daily_detail_budget: int = 25
    daily_apply_budget: int = 10


class CapsConfig(_ConfigModel):
    global_: GlobalCaps = Field(default_factory=GlobalCaps)
    sources: dict[str, SourceCaps] = Field(default_factory=dict)

    def for_source(self, source: str) -> SourceCaps:
        return self.sources.get(source, SourceCaps())

    def apply_global_max_searches(self, source_caps: SourceCaps) -> SourceCaps:
        effective_max = self.global_.max_searches_per_run
        if source_caps.max_searches_per_run is not None:
            effective_max = min(effective_max, source_caps.max_searches_per_run)
        return source_caps.model_copy(update={"max_searches_per_run": effective_max})


class Settings(BaseModel):
    profile: ProfileConfig
    searches: SearchesConfig
    caps: CapsConfig
    answers: AnswersConfig = Field(default_factory=AnswersConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_optional_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_settings() -> Settings:
    data = _load_yaml("profile.yaml")
    profile = ProfileConfig(**data)

    searches_data = _load_yaml("searches.yaml")
    searches = SearchesConfig(**searches_data)

    caps_yaml = _load_yaml("caps.yaml")
    raw_global = caps_yaml.get("global", {})
    raw_sources = caps_yaml.get("sources", {})
    caps = CapsConfig(
        global_=GlobalCaps(**raw_global),
        sources={name: SourceCaps(**cfg) for name, cfg in raw_sources.items()},
    )

    answers = AnswersConfig(**_load_optional_yaml("answers.yaml"))
    telegram = TelegramConfig(**_load_optional_yaml("telegram.yaml"))
    llm = LlmConfig(**_load_optional_yaml("llm.yaml"))
    return Settings(
        profile=profile, searches=searches, caps=caps,
        answers=answers, telegram=telegram, llm=llm,
    )


def ensure_dirs() -> None:
    for d in (DATA_DIR, PROFILE_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def settings_to_env() -> None:
    """Expose canonical paths as env vars so subprocesses inherit them."""
    os.environ.setdefault("AUTOAPPLY_CONFIG_DIR", str(CONFIG_DIR))
    os.environ.setdefault("AUTOAPPLY_DATA_DIR", str(DATA_DIR))
