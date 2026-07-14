import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StackStatus(str, enum.Enum):
    pending = "pending"
    deploying = "deploying"
    running = "running"
    stopped = "stopped"
    blocked = "blocked"
    failed = "failed"
    deleting = "deleting"
    updating = "updating"


class Stack(Base):
    __tablename__ = "stacks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(63), unique=True, index=True)
    namespace: Mapped[str] = mapped_column(String(63), unique=True)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[StackStatus] = mapped_column(Enum(StackStatus), default=StackStatus.pending)
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    endpoints: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
