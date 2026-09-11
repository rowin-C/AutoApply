"""SQLite persistence (SQLModel/SQLAlchemy).

One listings table drives the whole pipeline; status is the state machine.
A tiny spend ledger enforces the daily budgets from caps.yaml.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import Enum

from sqlmodel import Field, Session, SQLModel, create_engine, select

from .settings import DATA_DIR


class Source(str, Enum):
    LINKEDIN = "linkedin"
    INDEED = "indeed"
    NAUKRI = "naukri"


class ApplyPath(str, Enum):
    INLINE = "inline"
    REDIRECT = "redirect"
    UNKNOWN = "unknown"


class ListingStatus(str, Enum):
    NEW = "new"  # captured in phase A, awaiting detail
    DETAILED = "detailed"  # phase B fetched, apply-path known
    QUEUED = "queued"  # passed qualifying -> review queue
    SKIPPED = "skipped"  # failed pre-filter or qualifying rules
    NEEDS_MANUAL = "needs_manual"  # captcha/block/edge -> human attention
    APPLIED = "applied"  # Step 2


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


class Listing(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)
    job_id: str | None = None
    url: str = ""
    dedupe_key: str = Field(index=True)
    title: str = ""
    company: str = ""
    location: str = ""
    salary_raw: str = ""
    posted_ago: str | None = None
    full_jd: str = ""
    match_score: float | None = None
    reason: str = ""
    apply_path: str = ApplyPath.UNKNOWN.value
    external_url: str | None = None
    ats_type: str | None = None
    status: str = ListingStatus.NEW.value
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class SpendRecord(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(index=True)  # e.g. "detail_fetch"
    day: str = Field(index=True)  # ISO date
    amount: int = 1


_engine = None


def configure_engine(url: str | None = None) -> None:
    """Point the global engine at a database (default: data/autoapply.db).

    Tests swap this to a temp file before init_db().
    """
    global _engine
    if url is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{DATA_DIR / 'autoapply.db'}"
    _engine = create_engine(url, connect_args={"check_same_thread": False})


def get_engine():
    if _engine is None:
        configure_engine()
    return _engine


def init_db() -> None:
    SQLModel.metadata.create_all(get_engine())


def get_session() -> Session:
    return Session(get_engine())


def make_dedupe_key(
    source: str,
    job_id: str | None,
    title: str = "",
    company: str = "",
    location: str = "",
) -> str:
    if job_id:
        return f"{source}:{job_id}"
    raw = "|".join(x.strip().lower() for x in (company, title, location))
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{source}:hash:{digest}"


def upsert_listing(db: Session, listing: Listing) -> tuple[bool, Listing]:
    """Insert-or-update by (source, dedupe_key). Returns (is_new, saved)."""
    existing = db.exec(
        select(Listing).where(
            Listing.source == listing.source,
            Listing.dedupe_key == listing.dedupe_key,
        )
    ).first()
    if existing is None:
        db.add(listing)
        db.commit()
        db.refresh(listing)
        return True, listing

    for field, value in listing.model_dump(exclude_unset=True).items():
        if field in ("id", "dedupe_key", "created_at"):
            continue
        if field == "job_id" and value is None:
            continue  # never clobber a real job_id with None
        setattr(existing, field, value)
    existing.updated_at = _utcnow()
    db.add(existing)
    db.commit()
    db.refresh(existing)
    return False, existing


def find_listing(db: Session, source: str, dedupe_key: str) -> Listing | None:
    return db.exec(
        select(Listing).where(
            Listing.source == source,
            Listing.dedupe_key == dedupe_key,
        )
    ).first()


# ---- daily spend ledger --------------------------------------------------


def spent_today(db: Session, kind: str, day: str | None = None) -> int:
    today = day or _today_iso()
    rows = db.exec(
        select(SpendRecord).where(SpendRecord.kind == kind, SpendRecord.day == today)
    ).all()
    return sum(r.amount for r in rows)


def register_spend(db: Session, kind: str, amount: int = 1) -> None:
    db.add(SpendRecord(kind=kind, day=_today_iso(), amount=amount))
    db.commit()


def remaining_budget(
    db: Session, kind: str, cap: int | None, day: str | None = None
) -> int | None:
    if cap is None:
        return None
    return max(0, cap - spent_today(db, kind, day))
