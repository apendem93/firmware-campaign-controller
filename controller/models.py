import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

STATES = (
    "PENDING",
    "NOTIFIED",
    "DOWNLOAD_ISSUED",
    "AWAITING_RESULT",
    "OUTCOME_UNKNOWN",
    "SUCCEEDED",
    "FAILED",
    "TIMED_OUT",
)
ACTIVE = ("RUNNING", "PAUSED", "HALTED")
SLOTS = ("NOTIFIED", "DOWNLOAD_ISSUED", "AWAITING_RESULT", "OUTCOME_UNKNOWN")
DISPATCHED = ("DOWNLOAD_ISSUED", "AWAITING_RESULT", "OUTCOME_UNKNOWN")
TERMINAL = ("SUCCEEDED", "FAILED", "TIMED_OUT")


class Base(DeclarativeBase):
    pass


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    target_version: Mapped[str] = mapped_column(String(128))
    image_url: Mapped[str] = mapped_column(Text)
    current_version: Mapped[str] = mapped_column(String(128))
    max_concurrent: Mapped[int] = mapped_column(Integer)
    failure_threshold: Mapped[float]
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    halt_reason: Mapped[str | None] = mapped_column(Text)
    tuning: Mapped[dict] = mapped_column(JSONB)
    total: Mapped[int]
    gate_epoch: Mapped[int] = mapped_column(default=0)
    gate_minimum: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("max_concurrent > 0"),
        CheckConstraint("failure_threshold >= 0 AND failure_threshold <= 100"),
        CheckConstraint("status IN ('RUNNING','PAUSED','HALTED','COMPLETED')"),
        Index(
            "one_active_campaign",
            text("(true)"),
            unique=True,
            postgresql_where=text("status IN ('RUNNING','PAUSED','HALTED')"),
        ),
    )


class Device(Base):
    __tablename__ = "devices"
    device_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    firmware: Mapped[str] = mapped_column(String(128))
    last_event: Mapped[str | None] = mapped_column(String(32))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CampaignDevice(Base):
    __tablename__ = "campaign_devices"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaigns.id"))
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"))
    initial_version: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(32), default="PENDING")
    command_key: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True)
    ack_status: Mapped[str | None] = mapped_column(String(20))
    nudge_attempts: Mapped[int] = mapped_column(default=0)
    next_nudge_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inform_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ack_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    boot_version: Mapped[str | None] = mapped_column(String(128))
    boot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completion_fault: Mapped[int | None]
    outcome_reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(String(64))
    outcome_epoch: Mapped[int | None]
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("campaign_id", "device_id"),
        CheckConstraint(
            "state IN ('PENDING','NOTIFIED','DOWNLOAD_ISSUED','AWAITING_RESULT','OUTCOME_UNKNOWN','SUCCEEDED','FAILED','TIMED_OUT')"
        ),
        Index("campaign_state", "campaign_id", "state"),
        Index("due_nudges", "state", "next_nudge_at"),
        Index("due_results", "state", "result_deadline"),
    )


class DeviceEvent(Base):
    __tablename__ = "device_events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("campaigns.id"))
    device_id: Mapped[str | None] = mapped_column(String(200))
    command_key: Mapped[str | None] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(64))
    previous_state: Mapped[str | None] = mapped_column(String(32))
    new_state: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()")
    )
    __table_args__ = (Index("event_history", "campaign_id", "device_id", "id"),)
