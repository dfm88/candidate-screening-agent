from datetime import time, timedelta
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_NAME = "candidate-screening-agent"
DEFAULT_CLIENT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "grupo_sazon.yaml"


class Settings(BaseSettings):
    # Values come from the process environment only (`uv run --env-file .env`, Cloud Run, compose). Reading
    # .env here too would hide it from the Logfire SDK and Google auth, which only read the environment.
    model_config = SettingsConfigDict(extra="ignore")

    environment: str = "local"
    database_url: str = "postgresql+asyncpg://screening:screening@localhost:55432/screening"
    # Prompts and completions contain candidate PII, so they are only sent to Logfire when explicitly enabled.
    llm_content_logging: bool = False
    # Built frontend, served by the API in the container; absent in local dev where Vite serves it.
    static_dir: Path | None = None


class Country(StrEnum):
    ES = "ES"
    MX = "MX"


class Language(StrEnum):
    ES = "es"
    EN = "en"


class Locale(StrEnum):
    ES_ES = "es-ES"
    ES_MX = "es-MX"
    EN = "en"

    @classmethod
    def of(cls, language: Language, country: Country) -> "Locale":
        # The regional variant follows the country of work, never how the candidate writes.
        if language is Language.EN:
            return cls.EN
        return cls(f"{language}-{country}")


class Location(BaseModel):
    id: str
    name: str
    country: Country
    city: str
    timezone: str
    postal_prefixes: list[str]
    places: dict[str, str]

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def _places_inside_area(self) -> "Location":
        for place, code in self.places.items():
            if not any(code.startswith(p) or p.startswith(code) for p in self.postal_prefixes):
                raise ValueError(f"{self.id}: place {place!r} maps to {code}, outside its postal prefixes")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class QuietHours(BaseModel):
    start: time
    end: time


class Timings(BaseModel):
    reminders_not_started: list[timedelta]
    reminders_in_progress: list[timedelta]
    abandon_after: timedelta
    quiet_hours: QuietHours


TemplateText = dict[Locale, list[str]]


class ClientConfig(BaseModel):
    client_name: str
    persona_name: str
    synthetic: bool
    max_tries: int
    timings: Timings
    locations: list[Location]
    platforms: dict[str, list[str]]
    labels: dict[Locale, dict[str, str | list[str]]]
    templates: dict[str, TemplateText]

    @model_validator(mode="after")
    def _complete_locales(self) -> "ClientConfig":
        for key, by_locale in self.templates.items():
            missing = set(Locale) - set(by_locale)
            if missing:
                raise ValueError(f"template {key!r} is missing locales {sorted(missing)}")
        return self

    @cached_property
    def locations_by_id(self) -> dict[str, Location]:
        return {loc.id: loc for loc in self.locations}

    def label(self, locale: Locale, key: str) -> str:
        value = self.labels[locale][key]
        assert isinstance(value, str), key
        return value

    def label_list(self, locale: Locale, key: str) -> list[str]:
        value = self.labels[locale][key]
        assert isinstance(value, list), key
        return value

    def render(self, key: str, locale: Locale, **params: str) -> list[str]:
        common = {"client": self.client_name, "persona": self.persona_name}
        return [bubble.format(**common, **params) for bubble in self.templates[key][locale]]


def load_client_config(path: Path) -> ClientConfig:
    return ClientConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
