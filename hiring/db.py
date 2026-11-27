"""The shared register: one SQLite database that every AI employee and person uses.

The status of a candidate is the hand-off between employees. A change of
status is only saved when the candidate is still in the status the caller
expected, so a person and an employee can never overwrite each other.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (JSON, Boolean, Float, ForeignKey, Integer, String, Text,
                        create_engine, event, func, select, update as sa_update)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


class Candidate(Base):
    __tablename__ = "candidates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_no: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(200), index=True, default="")
    email_source: Mapped[str] = mapped_column(String(60), default="")
    cv_filename: Mapped[str] = mapped_column(String(255), default="")
    cv_stored: Mapped[str] = mapped_column(String(255), default="")
    cv_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    cv_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), index=True, default="new")
    outcome: Mapped[str] = mapped_column(String(80), default="")
    area: Mapped[str] = mapped_column(String(80), default="")
    area_source: Mapped[str] = mapped_column(String(60), default="")
    area_reasons: Mapped[list] = mapped_column(JSON, default=list)
    area_evidence: Mapped[list] = mapped_column(JSON, default=list)
    suggested_new_area: Mapped[str] = mapped_column(String(80), default="")
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    profile_source: Mapped[str] = mapped_column(String(40), default="")
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_parts: Mapped[dict] = mapped_column(JSON, default=dict)
    gaps: Mapped[list] = mapped_column(JSON, default=list)
    vacancy_id: Mapped[int | None] = mapped_column(Integer, default=None)   # invited to fill this vacancy
    area_scores: Mapped[dict] = mapped_column(JSON, default=dict)   # pool: {area: {score, parts, gaps}}
    questions: Mapped[list] = mapped_column(JSON, default=list)
    questions_source: Mapped[str] = mapped_column(String(40), default="")
    interview_at: Mapped[str] = mapped_column(String(40), default="")
    interview_label: Mapped[str] = mapped_column(String(80), default="")
    result: Mapped[str] = mapped_column(String(10), default="")
    result_comment: Mapped[str] = mapped_column(Text, default="")
    result_at: Mapped[str] = mapped_column(String(40), default="")
    work_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checklist: Mapped[list] = mapped_column(JSON, default=list)
    letter: Mapped[dict] = mapped_column(JSON, default=dict)        # placement letter: start/end date, file
    source: Mapped[str] = mapped_column(String(200), default="")     # who sent us the CV (mail-only mode)
    university: Mapped[str] = mapped_column(String(200), default="") # read from the CV's education
    invite: Mapped[dict] = mapped_column(JSON, default=dict)        # the invitation mail (mail-only mode)
    register: Mapped[dict] = mapped_column(JSON, default=dict)      # the intern register row (mail-only mode)
    flags: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[str] = mapped_column(String(40), default="")
    updated_at: Mapped[str] = mapped_column(String(40), default="")


class Draft(Base):
    __tablename__ = "drafts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    subject: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), index=True, default="waiting")  # waiting / released / discarded
    written_by: Mapped[str] = mapped_column(String(60), default="")
    check_notes: Mapped[list] = mapped_column(JSON, default=list)
    file: Mapped[str] = mapped_column(String(255), default="")
    delivery_status: Mapped[str] = mapped_column(String(24), default="not_sent")  # not_sent / eml_only / outlook_draft / sent / failed
    delivery_error: Mapped[str] = mapped_column(Text, default="")
    sent_to: Mapped[str] = mapped_column(String(200), default="")
    sent_at: Mapped[str] = mapped_column(String(40), default="")
    outlook_message_id: Mapped[str] = mapped_column(String(255), default="")
    outlook_web_link: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(40), default="")
    released_at: Mapped[str] = mapped_column(String(40), default="")


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    at: Mapped[str] = mapped_column(String(40), index=True)
    actor: Mapped[str] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(Text)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee: Mapped[str] = mapped_column(String(30), index=True)
    routine: Mapped[str] = mapped_column(String(60))
    candidate_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[str] = mapped_column(String(40), index=True)
    seconds: Mapped[float] = mapped_column(Float, default=0.0)
    outcome: Mapped[str] = mapped_column(String(20))   # ok / fixed / fallback / stopped / error
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")


class Area(Base):
    __tablename__ = "areas"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    required_skills: Mapped[list] = mapped_column(JSON, default=list)
    preferred_skills: Mapped[list] = mapped_column(JSON, default=list)
    studies: Mapped[list] = mapped_column(JSON, default=list)         # related subjects, for education points
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    checklist: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    added_by: Mapped[str] = mapped_column(String(40), default="")


class Intern(Base):
    """A copy of one Talent Hub record. We only ever read Talent Hub, never write to it."""
    __tablename__ = "interns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trainee_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    start_date: Mapped[str] = mapped_column(String(10), default="")
    end_date: Mapped[str] = mapped_column(String(10), default="", index=True)
    specialization: Mapped[str] = mapped_column(String(80), default="")   # as written in Talent Hub
    area: Mapped[str] = mapped_column(String(80), default="")             # matched to one of our areas
    synced_at: Mapped[str] = mapped_column(String(40), default="")


class Vacancy(Base):
    """A place (or places) to fill in one area, because interns are finishing."""
    __tablename__ = "vacancies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    area: Mapped[str] = mapped_column(String(80), index=True)
    places: Mapped[int] = mapped_column(Integer, default=1)
    opens_on: Mapped[str] = mapped_column(String(10), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    trainee_ids: Mapped[list] = mapped_column(JSON, default=list)
    suggestions: Mapped[list] = mapped_column(JSON, default=list)   # [{app_no, name, score, state}]
    filled: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)   # open, filled, closed
    created_by: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[str] = mapped_column(String(40), default="")
    updated_at: Mapped[str] = mapped_column(String(40), default="")


class Brief(Base):
    __tablename__ = "briefs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    created_at: Mapped[str] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(Text)
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    written_by: Mapped[str] = mapped_column(String(40), default="")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------------------------
class Paths:
    data: Path = config.DATA_DIR

    @property
    def inbox(self) -> Path: return self.data / "inbox"
    @property
    def outbox(self) -> Path: return self.data / "outbox"
    @property
    def letters(self) -> Path: return self.data / "letters"
    @property
    def mail(self) -> Path: return self.data / "mail"
    @property
    def mail_attachments(self) -> Path: return self.data / "mail" / "attachments"
    @property
    def run_logs(self) -> Path: return self.data / "run-logs"
    @property
    def notes(self) -> Path: return self.data / "notes"
    @property
    def db(self) -> Path: return self.data / "register.db"


paths = Paths()
_engine = None
Session = sessionmaker(expire_on_commit=False)
_id_lock = threading.Lock()


def now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _configure_sqlite(dbapi_conn, _connection_record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA temp_store=MEMORY")
    cur.execute("PRAGMA cache_size=-16000")
    cur.close()


def setup(data_dir: Path | None = None) -> None:
    """Create the data folders and database, and copy the starter areas in once."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    if data_dir is not None:
        paths.data = Path(data_dir)
    for d in (paths.data, paths.inbox, paths.outbox, paths.letters, paths.mail, paths.mail_attachments,
              paths.run_logs, paths.notes):
        d.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(f"sqlite:///{paths.db}", connect_args={"check_same_thread": False, "timeout": 30})
    event.listen(_engine, "connect", _configure_sqlite)
    Session.configure(bind=_engine)
    Base.metadata.create_all(_engine)
    _add_missing_columns()
    with Session() as s:
        if s.scalar(select(func.count(Area.id))) == 0:
            for a in json.loads(config.AREAS_FILE.read_text(encoding="utf-8")):
                s.add(Area(name=a["name"], description=a.get("description", ""),
                           required_skills=a.get("required_skills", []),
                           preferred_skills=a.get("preferred_skills", []), studies=a.get("studies", []),
                           keywords=a.get("keywords", []), checklist=a.get("checklist", []),
                           active=True, added_by="Starter list"))
        if s.get(Setting, "next_work_id") is None:
            s.add(Setting(key="next_work_id", value=str(config.WORK_ID_START)))
        if s.get(Setting, "next_app_no") is None:
            s.add(Setting(key="next_app_no", value="1"))
        if s.get(Setting, "paused") is None:
            s.add(Setting(key="paused", value="0"))
        s.commit()


def _add_missing_columns() -> None:
    """Older registers were made before some columns existed. Add them, keeping all data."""
    with _engine.begin() as conn:
        for table in (Candidate.__table__, Draft.__table__, Area.__table__):
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table.name})")}
            for col in table.columns:
                if col.name in existing or col.primary_key:
                    continue
                col_type = col.type.compile(dialect=_engine.dialect)
                default_sql = ""
                if col.default is not None and col.default.is_scalar:
                    arg = col.default.arg
                    if isinstance(arg, str):
                        default_sql = " DEFAULT '" + arg.replace("'", "''") + "'"
                    elif isinstance(arg, (int, float)) and not isinstance(arg, bool):
                        default_sql = f" DEFAULT {arg}"
                elif col.name in ("area_scores", "letter", "invite", "register"):
                    default_sql = " DEFAULT '{}'"
                elif col.name == "studies":
                    default_sql = " DEFAULT '[]'"
                conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type}{default_sql}")
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS ix_candidates_email ON candidates (email)",
            "CREATE INDEX IF NOT EXISTS ix_drafts_status ON drafts (status)",
            "CREATE INDEX IF NOT EXISTS ix_events_at ON events (at)",
            "CREATE INDEX IF NOT EXISTS ix_runs_started_at ON runs (started_at)",
        ):
            conn.exec_driver_sql(idx_sql)


def update_starter_areas() -> int:
    """Copies the related studies and the keywords from shared/areas.json into the starter
    areas of an older register. Areas the HR officer added are not touched. Returns how many changed."""
    starter = {a["name"]: a for a in json.loads(config.AREAS_FILE.read_text(encoding="utf-8"))}
    changed = 0
    with Session() as s:
        for a in s.scalars(select(Area).where(Area.added_by == "Starter list")):
            src = starter.get(a.name)
            if src is None:
                continue
            a.studies = src.get("studies", [])
            a.keywords = src.get("keywords", [])
            changed += 1
        s.commit()
    return changed


# ---- small helpers ----------------------------------------------------------------
def get_setting(key: str, default: str = "") -> str:
    with Session() as s:
        row = s.get(Setting, key)
        return row.value if row else default


def set_setting(key: str, value: str) -> None:
    with Session() as s:
        row = s.get(Setting, key)
        if row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))
        s.commit()


def log_event(candidate_id: int | None, actor: str, text: str) -> None:
    with Session() as s:
        s.add(Event(candidate_id=candidate_id, at=now(), actor=actor, text=text))
        s.commit()


def log_run(employee: str, routine: str, candidate_id: int | None, started: float, seconds: float,
            outcome: str, model_calls: int, note: str = "") -> None:
    started_at = datetime.fromtimestamp(started).replace(microsecond=0).isoformat()
    with Session() as s:
        s.add(Run(employee=employee, routine=routine, candidate_id=candidate_id, started_at=started_at,
                  seconds=round(seconds, 2), outcome=outcome, model_calls=model_calls, note=note))
        s.commit()
    # The same line also goes to a plain log file, one file per day.
    line = {"at": started_at, "employee": employee, "routine": routine, "candidate_id": candidate_id,
            "seconds": round(seconds, 2), "outcome": outcome, "model_calls": model_calls, "note": note}
    try:
        with open(paths.run_logs / f"{started_at[:10]}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    except OSError:
        pass


def next_app_no() -> str:
    with _id_lock, Session() as s:
        row = s.get(Setting, "next_app_no")
        n = int(row.value)
        row.value = str(n + 1)
        s.commit()
    return f"{config.APP_NO_PREFIX}{n:04d}"


def get_candidate(app_no_or_id: str | int) -> Candidate | None:
    with Session() as s:
        if isinstance(app_no_or_id, int):
            return s.get(Candidate, app_no_or_id)
        return s.scalar(select(Candidate).where(Candidate.app_no == app_no_or_id))


def get_candidates_by_app_nos(app_nos: list[str] | set[str]) -> dict[str, Candidate]:
    """Batch-fetch candidates keyed by app_no to avoid N+1 SQLite round-trips."""
    clean = [str(a).strip() for a in app_nos if a]
    if not clean:
        return {}
    with Session() as s:
        rows = s.scalars(select(Candidate).where(Candidate.app_no.in_(clean)))
        return {c.app_no: c for c in rows}


def update_candidate(candidate_id: int, expect_status: str | set | None = None, **fields: Any) -> bool:
    """Save fields on a candidate atomically. If expect_status is given, only save when the
    candidate is still in that status inside SQLite. Returns True when saved."""
    stmt = sa_update(Candidate).where(Candidate.id == candidate_id)
    if expect_status is not None:
        allowed = {expect_status} if isinstance(expect_status, str) else set(expect_status)
        if not allowed:
            return False
        stmt = stmt.where(Candidate.status.in_(allowed))
    values = {**fields, "updated_at": now()}
    with Session() as s:
        res = s.execute(stmt.values(**values))
        s.commit()
        return (res.rowcount or 0) == 1


def add_flag(candidate_id: int, text: str, by: str) -> None:
    with Session() as s:
        c = s.get(Candidate, candidate_id)
        if c is None:
            return
        if any(f.get("text") == text and f.get("open") for f in (c.flags or [])):
            return
        c.flags = list(c.flags or []) + [{"text": text, "by": by, "at": now(), "open": True}]
        c.updated_at = now()
        s.commit()


def candidates_in(statuses: set[str] | str, limit: int | None = None) -> list[Candidate]:
    statuses = {statuses} if isinstance(statuses, str) else statuses
    with Session() as s:
        q = select(Candidate).where(Candidate.status.in_(statuses)).order_by(Candidate.updated_at, Candidate.id)
        if limit:
            q = q.limit(limit)
        return list(s.scalars(q))


def interns_ending_by(day: str) -> list[Intern]:
    """Interns whose last day is on or before the given date (and who have an area)."""
    with Session() as s:
        return list(s.scalars(select(Intern).where(Intern.end_date != "", Intern.end_date <= day,
                                                   Intern.area != "").order_by(Intern.end_date, Intern.id)))


def vacancies_in(statuses: set[str] | str) -> list[Vacancy]:
    statuses = {statuses} if isinstance(statuses, str) else statuses
    with Session() as s:
        return list(s.scalars(select(Vacancy).where(Vacancy.status.in_(statuses))
                              .order_by(Vacancy.opens_on, Vacancy.id)))


def get_vacancy(vacancy_id: int) -> Vacancy | None:
    with Session() as s:
        return s.get(Vacancy, vacancy_id)


def update_vacancy(vacancy_id: int, **fields: Any) -> bool:
    with Session() as s:
        v = s.get(Vacancy, vacancy_id)
        if v is None:
            return False
        for k, val in fields.items():
            setattr(v, k, val)
        v.updated_at = now()
        s.commit()
        return True


def active_areas() -> list[Area]:
    with Session() as s:
        return list(s.scalars(select(Area).where(Area.active.is_(True)).order_by(Area.id)))


def area_by_name(name: str) -> Area | None:
    with Session() as s:
        for a in s.scalars(select(Area)):
            if a.name.lower() == (name or "").strip().lower():
                return a
    return None


def issue_work_id(candidate_id: int) -> int:
    """Give a passed candidate a 4-digit work ID.

    Rules enforced here, in plain code:
    - only a candidate whose status is "passed" can get an ID;
    - the same email always gets the same ID;
    - IDs start at 1001, count up and are never reused.
    """
    with _id_lock, Session() as s:
        c = s.get(Candidate, candidate_id)
        if c is None:
            raise ValueError("Unknown candidate")
        if c.status != "passed":
            raise PermissionError("A work ID can only be issued after the interviewer enters Pass.")
        if c.work_id:
            return c.work_id
        if c.email:
            same = s.scalar(select(Candidate).where(Candidate.email == c.email, Candidate.work_id.is_not(None),
                                                    Candidate.id != c.id))
            if same is not None:
                c.work_id = same.work_id
                s.commit()
                return c.work_id
        row = s.get(Setting, "next_work_id")
        n = int(row.value)
        if n > config.WORK_ID_MAX:
            raise RuntimeError("No 4-digit work IDs are left.")
        c.work_id = n
        row.value = str(n + 1)
        c.updated_at = now()
        s.commit()
        return n
