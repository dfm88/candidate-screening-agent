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
    # Empty values mean "use the default", so a copied example.env with blanks just works.
    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)

    environment: str = "local"
    database_url: str = "postgresql+asyncpg://screening:screening@localhost:55432/screening"
    # Prompts and completions contain candidate PII, so they are only sent to Logfire when explicitly enabled.
    llm_content_logging: bool = False
    # Built frontend, served by the API in the container; absent in local dev where Vite serves it.
    static_dir: Path | None = None
    # PydanticAI model strings: the model for live turns, and the fallbacks tried in order when it fails (a JSON list
    # in the env). The demo calls the providers' APIs; a fallback from another provider means one outage can't take out
    # both (README, "Why GPT-5.6 terra"). Processing in the EU means google-cloud:… models on Vertex AI.
    screening_model: str = "openai:gpt-5.6-terra"
    screening_fallback_models: list[str] = ["google:gemini-3.8-flash"]
    # Vertex AI location for google-cloud: models. `eu` keeps processing in the EU (multi-region endpoint).
    google_cloud_location: str = "eu"
    google_cloud_project: str | None = None
    max_history_messages: int = 50
    # Operational bounds on a live turn (README, "Timeouts, retries and limits"). They don't change what the model
    # answers, so they stay out of the model settings and the prompt version, and evals run without them.
    # One deadline per request to one model, over every SDK retry; the fallback model gets a fresh one.
    request_timeout: float = 10
    # The whole turn: room for two fallbacks in a two-request turn (two primary timeouts, plus the fallback at its p99
    # in the evals, 7 s, twice: 34 s).
    turn_timeout: float = 35
    # Model requests per turn. The evals' largest legitimate turn needed 5 (a postal code, a request for a person and a
    # question in one message); a runaway loop of invented saves needed 9.
    turn_request_limit: int = 7
    # How long a burst of candidate messages may take to settle before one turn answers them all.
    chat_debounce_seconds: float = 1.5
    # Required by the public apply form when set (the reviewer link carries it); unset means no check.
    apply_access_code: str | None = None


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
            if not any(code.startswith(prefix) or prefix.startswith(code) for prefix in self.postal_prefixes):
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


class FaqEntry(BaseModel):
    question: str
    answer: str


class ClientConfig(BaseModel):
    client_name: str
    persona_name: str
    # The language the recruiter summary is written in.
    recruiter_language: Language
    synthetic: bool
    timings: Timings
    locations: list[Location]
    platforms: dict[str, list[str]]
    faq: list[FaqEntry]
    regional_terms: list[dict[Country, str]]
    labels: dict[Locale, dict[str, str]]
    templates: dict[str, dict[Locale, list[str]]]

    @model_validator(mode="after")
    def _complete_locales(self) -> "ClientConfig":
        for key, by_locale in self.templates.items():
            missing = set(Locale) - set(by_locale)
            if missing:
                raise ValueError(f"template {key!r} is missing locales {sorted(missing)}")
        for row in self.regional_terms:
            if set(row) != set(Country):
                raise ValueError(f"regional term {row} needs exactly one term per country {sorted(Country)}")
        return self

    @cached_property
    def locations_by_id(self) -> dict[str, Location]:
        return {location.id: location for location in self.locations}

    def label(self, locale: Locale, key: str) -> str:
        return self.labels[locale][key]

    def render(self, key: str, locale: Locale, **params: str) -> list[str]:
        common = {"client": self.client_name, "persona": self.persona_name}
        return [bubble.format(**common, **params) for bubble in self.templates[key][locale]]


def load_client_config(path: Path) -> ClientConfig:
    return ClientConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
