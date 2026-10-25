"""Tables. JSONB columns hold Pydantic models (the screening state, the summary, PydanticAI messages), validated
when the service loads them."""

import enum
import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from screening.config import Language
from screening.contract import Author
from screening.domain import Outcome, Status


class Source(enum.StrEnum):
    APPLY_FORM = "apply_form"


class MessageKind(enum.StrEnum):
    TEXT = "text"
    TEMPLATE = "template"


class Base(DeclarativeBase):
    type_annotation_map = {
        # Enums as their values in VARCHAR columns: no Postgres enum type to migrate when a value is added.
        enum.Enum: sa.Enum(
            enum.Enum, native_enum=False, length=32, values_callable=lambda e: [member.value for member in e]
        ),
        datetime: sa.DateTime(timezone=True),
        # None is SQL NULL, not JSON null, so `summary IS NULL` finds the screenings without one.
        dict[str, Any]: JSONB(none_as_null=True),
        list[Any]: JSONB,
        list[str]: JSONB,
    }


class Candidate(Base):
    """One application, and its screening."""

    __tablename__ = "candidates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # The application's id at its source: a resubmitted application returns the same candidate.
    external_id: Mapped[str] = mapped_column(unique=True)
    source: Mapped[Source]
    full_name: Mapped[str]
    phone: Mapped[str]
    vacancy_location_id: Mapped[str]
    language: Mapped[Language]
    whatsapp_opt_in: Mapped[bool]
    # The secret in the candidate's chat link; candidates have no accounts.
    chat_token: Mapped[str] = mapped_column(unique=True)
    # Copies of state.status and state.outcome, for the recruiter's lists.
    status: Mapped[Status] = mapped_column(index=True)
    outcome: Mapped[Outcome | None] = mapped_column(index=True)
    state: Mapped[dict[str, Any]]
    summary: Mapped[dict[str, Any] | None]
    # The recruiter's decision, final once made. Only the decision endpoint writes these and the worker never does, so
    # a decision during a running turn is safe; `outcome` stays the rule's.
    decided_outcome: Mapped[Outcome | None]
    decided_by: Mapped[str | None]
    decision_note: Mapped[str | None]
    decided_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    first_reply_at: Mapped[datetime | None]
    last_inbound_at: Mapped[datetime | None]
    # Our last message that waits for an answer: the first message, a reply or the decision. Reminders don't move it:
    # the silence they break is timed from it, and so is *abandoned*.
    last_outbound_at: Mapped[datetime | None]
    # Reminders sent in the current silence; the next reply starts a new one.
    reminders_sent: Mapped[int] = mapped_column(default=0, server_default="0")
    completed_at: Mapped[datetime | None]


class Turn(Base):
    """One agent run, or a template we sent: the model history, and the audit trail for debugging."""

    __tablename__ = "turns"

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    # PydanticAI messages exactly as sent (ModelMessagesTypeAdapter): the next turn's history, append-only.
    messages: Mapped[list[Any]]
    prompt_version: Mapped[str | None]
    models: Mapped[list[str]] = mapped_column(default=list)
    usage: Mapped[dict[str, Any] | None]
    duration_ms: Mapped[int | None]
    error: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Message(Base):
    """One chat bubble. A candidate message without a turn is pending: the worker hasn't answered it yet."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("candidate_id", "client_id"),
        Index("ix_messages_pending", "candidate_id", postgresql_where=text("turn_id IS NULL AND author = 'candidate'")),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    author: Mapped[Author]
    kind: Mapped[MessageKind]
    template_key: Mapped[str | None]
    text: Mapped[str]
    quick_replies: Mapped[list[str]] = mapped_column(default=list)
    client_id: Mapped[str | None]
    turn_id: Mapped[int | None] = mapped_column(ForeignKey("turns.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def ping(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("select 1"))
