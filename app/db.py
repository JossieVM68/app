from datetime import datetime, date
from typing import Any, Optional

from sqlalchemy import (JSON, Boolean, Date, DateTime, ForeignKey, Integer, LargeBinary, String, Text,
                        UniqueConstraint, create_engine, select)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import DATABASE_URL

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.utcnow()


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, default=dict)


class Upload(Base):
    """El CV original que subió Josefina."""
    __tablename__ = "uploads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_job_source_ext"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    external_id: Mapped[str] = mapped_column(String(255))
    dedupe_key: Mapped[str] = mapped_column(String(300), index=True)
    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str] = mapped_column(String(200), default="")
    location: Mapped[str] = mapped_column(String(300), default="")
    remote: Mapped[bool] = mapped_column(Boolean, default=False)
    url: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    salary: Mapped[str] = mapped_column(String(200), default="")
    posted_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    deadline: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    contact_email: Mapped[str] = mapped_column(String(200), default="")
    found_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    prefilter: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    analysis: Mapped[Any] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="nuevo", index=True)  # nuevo|guardado|descartado|aplicando

    application: Mapped[Optional["Application"]] = relationship(back_populates="job", uselist=False)


APP_STATUSES = [
    ("preparando", "Preparando"),
    ("aplicada", "Aplicada"),
    ("seguimiento", "Seguimiento enviado"),
    ("entrevista", "Entrevista"),
    ("oferta", "Oferta"),
    ("rechazada", "No avanzó"),
    ("retirada", "Retirada"),
]
APP_STATUS_LABELS = dict(APP_STATUSES)


class Application(Base):
    __tablename__ = "applications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="preparando", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    applied_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    follow_up_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    interview_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    contact_name: Mapped[str] = mapped_column(String(200), default="")
    contact_email: Mapped[str] = mapped_column(String(200), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    package: Mapped[Any] = mapped_column(JSON, default=dict)  # CV adaptado, carta, emails...
    ats: Mapped[Any] = mapped_column(JSON, default=dict)
    lang: Mapped[str] = mapped_column(String(5), default="en")

    job: Mapped[Job] = relationship(back_populates="application")
    events: Mapped[list["Event"]] = relationship(back_populates="application", order_by="Event.at.desc()",
                                                 cascade="all, delete-orphan")


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(30))
    note: Mapped[str] = mapped_column(Text, default="")
    application: Mapped[Application] = relationship(back_populates="events")


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    trigger: Mapped[str] = mapped_column(String(20), default="auto")
    found: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    scored: Mapped[int] = mapped_column(Integer, default=0)
    log: Mapped[Any] = mapped_column(JSON, default=list)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


DEFAULTS: dict[str, Any] = {
    "profile": {},  # {"resume": {...}, "about": {...}}
    "prefs": {
        "target_titles": ["Teacher Assistant", "Preschool Teacher Assistant", "Bilingual Teacher Assistant",
                          "Early Childhood Teacher", "Preschool Teacher", "Head Start Teacher",
                          "Early Head Start Teacher", "Parent Educator", "Home Visitor", "Family Support Specialist",
                          "Child Care Teacher", "Paraprofessional"],
        "search_terms": ["preschool teacher assistant", "bilingual teacher assistant", "early childhood teacher",
                         "head start teacher", "parent educator home visitor", "child care teacher",
                         "family support specialist bilingual", "paraprofessional"],
        "keywords": ["Early Childhood Education", "preschool", "Head Start", "Early Head Start",
                     "Teaching Strategies Gold", "TSG", "lesson planning", "child observation", "bilingual",
                     "Spanish", "classroom management", "family engagement", "home visiting",
                     "developmental screening", "special needs", "inclusive classroom", "COR Advantage",
                     "ChildPlus", "CPR", "First Aid", "CDA", "Gateways", "DCFS"],
        "exclude_keywords": ["director", "manager", "vice president", "chief", "nurse", "physician",
                             "software", "engineer", "driver cdl"],
        "locations": ["Chicago, IL"],
        "remote_ok": False,
        "hybrid_ok": True,
        "onsite_ok": True,
        "min_salary": 0,
        "min_hourly": 22,
        "max_miles": 5,
        "home_address": "",
        "home_coords": None,
        "strict_unknown": False,  # True = descartar si no se sabe la distancia o el pago
        "work_authorization": "U.S. citizen — authorized to work in the U.S.",
        "doc_lang": "auto",  # auto | en | es
        "extra_context": "",
    },
    "sources": {
        "enabled": ["claude_web", "greenhouse", "lever", "ashby", "smartrecruiters", "adzuna", "usajobs", "jooble",
                    "serpapi"],
        "greenhouse": [], "lever": [], "ashby": [], "smartrecruiters": [],
    },
    "goal": {"interviews": 5, "deadline": None, "daily_applications": 5},
    "schedule": {"every_hours": 24, "hour_local": 6, "max_ai_per_run": 40, "min_prefilter": 30},
    "auth": {},
}


def get_setting(db, key: str) -> Any:
    row = db.get(Setting, key)
    default = DEFAULTS.get(key, {})
    if row is None:
        return _copy(default)
    val = row.value
    if isinstance(default, dict) and isinstance(val, dict):
        merged = _copy(default)
        merged.update(val)
        return merged
    return val


def set_setting(db, key: str, value: Any) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = _copy(value)  # nuevo objeto para que SQLAlchemy detecte el cambio
    db.commit()


def _copy(v: Any) -> Any:
    import copy
    return copy.deepcopy(v)


__all__ = ["SessionLocal", "Setting", "Upload", "Job", "Application", "Event", "Run", "init_db", "get_setting",
           "set_setting", "APP_STATUSES", "APP_STATUS_LABELS", "select"]
