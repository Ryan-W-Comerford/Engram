import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, Date, DateTime, Enum as SAEnum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship
from pgvector.sqlalchemy import Vector


class Base(DeclarativeBase):
    pass


# Helper so SQLAlchemy stores enum values ("error") not names ("ERROR")
def _values(obj):
    return [e.value for e in obj]


class EventType(str, enum.Enum):
    ERROR      = "error"
    TRACE      = "trace"
    DEPLOYMENT = "deployment"


class Severity(str, enum.Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


class IncidentStatus(str, enum.Enum):
    OPEN          = "open"
    INVESTIGATING = "investigating"
    RESOLVED      = "resolved"


class Project(Base):
    __tablename__ = "projects"

    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name         = Column(String(255), nullable=False)
    api_key_hash = Column(String(64), nullable=False, unique=True)
    created_at   = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    events    = relationship("Event",    back_populates="project")
    incidents = relationship("Incident", back_populates="project")


class Event(Base):
    __tablename__ = "events"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id  = Column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    event_type  = Column(SAEnum(EventType, name="eventtype", values_callable=_values), nullable=False)
    environment = Column(String(50), nullable=False, server_default="production")
    service     = Column(String(255), nullable=True)
    timestamp   = Column(DateTime, nullable=False)
    raw_data    = Column(JSONB, nullable=False)
    created_at  = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    project = relationship("Project", back_populates="events")

    __table_args__ = (
        Index("idx_events_project_timestamp", "project_id", "timestamp"),
        Index("idx_events_event_type", "event_type"),
    )


class Incident(Base):
    __tablename__ = "incidents"

    id                  = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id          = Column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    detected_at         = Column(DateTime, nullable=False)
    current_errors      = Column(Integer, nullable=False)
    baseline_avg        = Column(Float, nullable=False)
    spike_ratio         = Column(Float, nullable=False)
    title               = Column(String(500), nullable=True)
    severity            = Column(SAEnum(Severity, name="severity", values_callable=_values), nullable=True)
    ai_summary          = Column(Text, nullable=True)
    affected_endpoints  = Column(JSONB, nullable=True)
    recommended_actions = Column(JSONB, nullable=True)
    deployment_event_id = Column(UUID(as_uuid=True), ForeignKey("events.id"), nullable=True)
    status              = Column(SAEnum(IncidentStatus, name="incidentstatus", values_callable=_values), nullable=False, default=IncidentStatus.OPEN)
    ai_analyzed         = Column(Boolean, nullable=False, default=False)
    embedding           = Column(Vector(1536), nullable=True)
    created_at          = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    project          = relationship("Project", back_populates="incidents")
    deployment_event = relationship("Event", foreign_keys=[deployment_event_id])

    __table_args__ = (
        Index("idx_incidents_project_detected", "project_id", "detected_at"),
        Index("idx_incidents_status", "status"),
    )


class DailyDigest(Base):
    __tablename__ = "daily_digests"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    date       = Column(Date, nullable=False)
    summary    = Column(Text, nullable=True)
    data       = Column(JSONB, nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    project = relationship("Project")

    __table_args__ = (
        UniqueConstraint("project_id", "date", name="uq_digest_project_date"),
        Index("idx_daily_digests_project_date", "project_id", "date"),
    )