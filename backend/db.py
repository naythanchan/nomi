"""SQLAlchemy engine, session factory, and ORM models.

Deliberately small: because we book straight to Google Calendar and send real
invites, we don't need a proposal/acceptance state machine. We only persist
users (for their OAuth tokens) + sessions, plus a lightweight log of meetings
we've booked so a user can see their scheduling history.
"""
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, ForeignKey,
)
from sqlalchemy.orm import sessionmaker, relationship, declarative_base

from config import DATABASE_URL

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def _utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(320), unique=True, nullable=False, index=True)
    name = Column(String(255))
    timezone = Column(String(64))  # IANA tz, best-effort from Google
    google_access_token = Column(Text)
    google_refresh_token = Column(Text)
    token_expiry = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    sessions = relationship("Session", back_populates="user", cascade="all, delete-orphan")
    meetings = relationship("BookedMeeting", back_populates="organizer", cascade="all, delete-orphan")


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)
    token = Column(String(255), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    expires_at = Column(DateTime(timezone=True))

    user = relationship("User", back_populates="sessions")


class BookedMeeting(Base):
    __tablename__ = "booked_meetings"

    id = Column(Integer, primary_key=True)
    organizer_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    google_event_id = Column(String(255))
    title = Column(String(255), nullable=False)
    description = Column(Text)
    location = Column(String(255))
    location_type = Column(String(20), default="virtual")
    attendees = Column(Text)  # comma-separated emails
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    organizer = relationship("User", back_populates="meetings")

    def to_dict(self):
        return {
            "id": self.id,
            "google_event_id": self.google_event_id,
            "title": self.title,
            "description": self.description,
            "location": self.location,
            "location_type": self.location_type,
            "attendees": (self.attendees or "").split(",") if self.attendees else [],
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def init_db():
    Base.metadata.create_all(bind=engine)


@contextmanager
def db_session():
    """Context-managed session that commits on success and always closes."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
