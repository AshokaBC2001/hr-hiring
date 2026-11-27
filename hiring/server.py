"""The web server: the API for the pages, and the pages themselves (web/ folder).

It only listens on 127.0.0.1, refuses other host names and refuses changes
that come from other websites. Two roles use it, chosen at the top of the page:
"hr" (HR officer) and "interviewer".
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import defer

from . import actions, config, db, flows, invitation, letters, llm, mailer, outlook, scheduler, talenthub, tools, vacancies
from .employees import EMPLOYEES, lessons, role_summary, routines, schedule

VERSION = "1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.setup()
    actions.upgrade_scoring()
    if config.SCHEDULER_ENABLED:
        scheduler.start()
    yield
    scheduler.stop()
    llm.close()
    outlook.close()


app = FastAPI(title="HR Hiring", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])


@app.middleware("http")
async def same_site_only(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1].split(":")[0] not in ("localhost", "127.0.0.1"):
            return JSONResponse({"detail": "Requests from other websites are not allowed."}, status_code=403)
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(actions.ActionError)
async def action_error(request: Request, exc: actions.ActionError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(invitation.MailProblem)
async def mail_problem(request: Request, exc: invitation.MailProblem):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(mailer.MailError)
async def mail_error(request: Request, exc: mailer.MailError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


def _role(request: Request) -> str:
    return request.headers.get("x-role", "hr")


def _need_role(request: Request, role: str) -> None:
    if _role(request) != role:
        who = "HR officer" if role == "hr" else "Interviewer"
        raise actions.ActionError(f"Only the {who} can do this. Switch the role at the top right.")


async def _json_dict(request: Request, *, allow_empty: bool = False) -> dict:
    raw = await request.body()
    if not raw or not raw.strip():
        if allow_empty:
            return {}
        raise actions.ActionError("Request body must be a JSON object.")
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise actions.ActionError("Request body must be valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise actions.ActionError("Request body must be a JSON object.")
    return parsed


def _kick() -> None:
    """After a person acts, coalesce employee + vacancy updates on a background thread."""
    if config.SCHEDULER_ENABLED:
        flows.kick_background()


# ---- serialising -------------------------------------------------------------------------
def _status(c) -> dict:
    label, chip, actor = config.STATUSES.get(c.status, (c.status, "gray", ""))
    return {"status": c.status, "status_label": label, "chip": chip, "next_actor": actor}


def summary(c: db.Candidate) -> dict:
    return {"app_no": c.app_no, "name": c.name or "(not read yet)", "email": c.email, **_status(c),
            "area": c.area, "suggested_new_area": c.suggested_new_area, "score": c.score,
            "interview_label": c.interview_label, "interview_at": c.interview_at, "result": c.result, "work_id": c.work_id,
            "outcome": c.outcome, "cv_filename": c.cv_filename,
            "open_flags": sum(1 for f in (c.flags or []) if f.get("open")),
            "created_at": c.created_at, "updated_at": c.updated_at}


def _draft_json(d: db.Draft, c: db.Candidate | None = None, test_recipient: str | None = None) -> dict:
    out = {
        "id": d.id,
        "kind": d.kind,
        "kind_label": actions.KIND_LABELS.get(d.kind, d.kind),
        "subject": d.subject,
        "body": d.body,
        "status": d.status,
        "written_by": d.written_by,
        "check_notes": d.check_notes or [],
        "created_at": d.created_at,
        "released_at": d.released_at,
        "file": d.file,
        "eml_url": f"/api/drafts/{d.id}/eml",
        "delivery_status": d.delivery_status or "not_sent",
        "delivery_error": d.delivery_error or "",
        "sent_to": d.sent_to or "",
        "sent_at": d.sent_at or "",
        "outlook_message_id": d.outlook_message_id or "",
        "outlook_web_link": d.outlook_web_link or "",
    }
    if c is not None:
        before = actions.RELEASE_MOVES[d.kind][0]
        blocker = "" if c.email else "Add the candidate's email first."
        if d.kind == "acceptance":
            out["attachment"] = letters.file_name(c)
            if not blocker and not letters.dates_ready(c):
                blocker = "Add the start and end dates for the placement letter first."
        rec = test_recipient if test_recipient is not None else outlook.raw_settings().get("test_recipient", "")
        links = outlook.compose_links(c.email or "", d.subject, d.body, test_recipient=rec) if c.email else {}
        out.update(
            app_no=c.app_no,
            name=c.name,
            email=c.email,
            candidate_status=c.status,
            can_release=(d.status == "waiting" and c.status == before and not blocker),
            release_blocker=blocker,
            compose_links=links,
        )
    return out


def _letter_json(c: db.Candidate) -> dict | None:
    """The placement letter box on the candidate page (only after a work ID is issued)."""
    if not c.work_id:
        return None
    letter = c.letter or {}
    start, end = letters.letter_dates(c)
    auto = letters.automatic_dates(c)
    return {"start_date": start, "end_date": end, "automatic": (start, end) == auto,
            "file": letter.get("file") or letters.file_name(c), "made_at": letter.get("made_at", ""),
            "ready": letters.dates_ready(c), "can_edit": c.status in actions.LETTER_EDIT_STATUSES}


def detail(c: db.Candidate) -> dict:
    with db.Session() as s:
        drafts = list(s.scalars(select(db.Draft).where(db.Draft.candidate_id == c.id).order_by(db.Draft.id)))
        events = list(s.scalars(select(db.Event).where(db.Event.candidate_id == c.id).order_by(db.Event.id)))
        runs = list(s.scalars(select(db.Run).where(db.Run.candidate_id == c.id).order_by(db.Run.id)))
    test_recipient = outlook.raw_settings().get("test_recipient", "")
    invite = invitation.draft(c) if config.MODE == "mail" else None
    out = summary(c)
    out.update({
        "email_source": c.email_source, "profile": c.profile or {}, "profile_source": c.profile_source,
        "area_source": c.area_source, "area_reasons": c.area_reasons or [], "area_evidence": c.area_evidence or [],
        "score_parts": c.score_parts or {}, "gaps": c.gaps or [], "questions": c.questions or [],
        "questions_source": c.questions_source, "result_comment": c.result_comment, "result_at": c.result_at,
        "checklist": c.checklist or [],
        "letter": _letter_json(c),
        "source": c.source or "", "university": c.university or "",
        "invite": invite,
        "mail_links": (outlook.compose_links(c.email, invite["subject"], invite["body"], test_recipient=test_recipient)
                       if invite is not None and c.email else {}),
        "email_in_cv": invitation.email_in_cv(c) if config.MODE == "mail" and not c.email else "",
        "register": invitation.row(c) if config.MODE == "mail" else None,
        "area_scores": [{"area": a, "score": sc} for a, sc in tools.top_areas(c.area_scores or {})],
        "active_areas": [a.name for a in db.active_areas()],
        "flags": [dict(f, index=i) for i, f in enumerate(c.flags or [])],
        "drafts": [_draft_json(d, c, test_recipient=test_recipient) for d in drafts],
        "events": [{"at": e.at, "actor": e.actor, "text": e.text} for e in events],
        "runs": [{"at": r.started_at, "employee": r.employee, "routine": r.routine, "outcome": r.outcome,
                  "seconds": r.seconds, "model_calls": r.model_calls, "note": r.note} for r in runs],
        "cv_text": c.cv_text[:config.MAX_CV_CHARS],
    })
    return out


# ---- reading -----------------------------------------------------------------------------
@app.get("/api/meta")
def meta():
    return {"version": VERSION, "statuses": {k: {"label": v[0], "chip": v[1], "next_actor": v[2]}
                                             for k, v in config.STATUSES.items()},
            "weights": config.WEIGHTS, "shortlist_at": config.SHORTLIST_AT, "borderline_at": config.BORDERLINE_AT,
            "model_url": config.MODEL_URL}


@app.get("/api/state")
def state():
    with db.Session() as s:
        counts = dict(s.execute(select(db.Candidate.status, func.count(db.Candidate.id))
                                .group_by(db.Candidate.status)).all())
        waiting = s.scalar(select(func.count(db.Draft.id)).where(db.Draft.status == "waiting")) or 0
        flagged = sum(
            1 for fl in s.scalars(
                select(db.Candidate.flags).where(
                    db.Candidate.status.not_in({"closed", "archived"}),
                    db.Candidate.status.not_in(config.HR_STATUSES),
                )
            )
            if any(f.get("open") for f in (fl or []))
        )
        selected = s.scalar(select(func.count(db.Candidate.id)).where(db.Candidate.work_id.is_not(None))) or 0
        recent = list(s.execute(select(db.Event, db.Candidate.app_no, db.Candidate.name)
                                .join(db.Candidate, db.Candidate.id == db.Event.candidate_id, isouter=True)
                                .order_by(db.Event.id.desc()).limit(12)).all())
        by_area = dict(s.execute(select(db.Candidate.area, func.count(db.Candidate.id))
                                 .where(db.Candidate.area != "").group_by(db.Candidate.area)).all())
        brief = s.scalar(select(db.Brief).order_by(db.Brief.id.desc()).limit(1))
        areas = [a.name for a in s.scalars(select(db.Area).where(db.Area.active.is_(True)).order_by(db.Area.id))]
    open_vacancies = db.vacancies_in("open")
    vacancy_waiting = sum(1 for v in open_vacancies
                          for s_ in (v.suggestions or []) if s_["state"] == "suggested")
    hr_count = sum(counts.get(k, 0) for k in config.HR_STATUSES)
    return {
        "model": llm.health(), "outlook": outlook.public_settings(),
        "notice": flows.NOTICE, "paused": db.get_setting("paused") == "1",
        "mode": config.MODE,
        "counts": counts, "total": sum(counts.values()),
        "tiles": {"new": counts.get("new", 0), "review": hr_count + flagged, "release": waiting,
                  "interview": counts.get("awaiting_result", 0) + counts.get("on_hold", 0), "selected": selected,
                  "vacancies": vacancy_waiting},
        "team": {k: dict(flows.TEAM[k], name=EMPLOYEES[k]) for k in EMPLOYEES},
        "recent": [{"at": e.at, "actor": e.actor, "text": e.text, "app_no": app_no or "", "name": name or ""}
                   for e, app_no, name in recent],
        "areas_chart": [{"area": a, "count": by_area.get(a, 0)} for a in areas] +
                       [{"area": a, "count": n} for a, n in by_area.items() if a not in areas],
        "brief": {"text": brief.text, "created_at": brief.created_at, "written_by": brief.written_by} if brief else None,
        "vacancies": {"open": len(open_vacancies),
                      "places": sum(max(0, v.places - v.filled) for v in open_vacancies),
                      "waiting": vacancy_waiting},
    }


@app.get("/api/candidates")
def list_candidates(status: str = "", q: str = ""):
    with db.Session() as s:
        query = select(db.Candidate).options(defer(db.Candidate.cv_text)).order_by(db.Candidate.id.desc())
        groups = {"interview": {"awaiting_result", "on_hold"}, "working": {"new", "shortlisted", "not_shortlisted",
                  "failed", "hold", "passed"}}
        if status == "review":
            query = query.where(db.Candidate.status.in_(config.HR_STATUSES))
        elif status in groups:
            query = query.where(db.Candidate.status.in_(groups[status]))
        elif status == "release":
            query = query.where(db.Candidate.status.in_(config.RELEASE_STATUSES))
        elif status == "open":
            query = query.where(db.Candidate.status.not_in({"closed"} | config.NOT_IN_HIRING))
        elif status:
            query = query.where(db.Candidate.status == status)
        else:                                    # All: the hiring flow only (pool CVs are on the Add CV page)
            query = query.where(db.Candidate.status.not_in(config.NOT_IN_HIRING))
        rows = list(s.scalars(query))
    if q:
        ql = q.lower()
        rows = [c for c in rows if ql in (c.name or "").lower() or ql in c.app_no.lower()
                or ql in (c.area or "").lower() or ql in (c.email or "").lower()]
    return [summary(c) for c in rows]


@app.get("/api/candidates/{app_no}")
def get_candidate(app_no: str):
    c = db.get_candidate(app_no)
    if c is None:
        raise actions.ActionError(f"There is no candidate {app_no}.")
    return detail(c)


@app.get("/api/candidates/{app_no}/cv")
def download_cv(app_no: str):
    path, name = actions.cv_file(app_no)
    return FileResponse(path, filename=name)


@app.get("/api/candidates/{app_no}/letter")
def placement_letter(app_no: str, download: int = 0):
    """The placement letter PDF: shown in the browser, or downloaded with ?download=1."""
    c = db.get_candidate(app_no)
    if c is None or not c.work_id:
        raise actions.ActionError("There is no placement letter for this candidate yet.")
    how = "attachment" if download else "inline"
    return Response(letters.read(c), media_type="application/pdf",
                    headers={"Content-Disposition": f'{how}; filename="{letters.file_name(c)}"'})


@app.post("/api/candidates/{app_no}/letter")
async def save_letter(app_no: str, request: Request):
    _need_role(request, "hr")
    b = await request.json()
    return {"letter": actions.save_letter_dates(app_no, b.get("start_date", ""), b.get("end_date", ""))}


# ---- Mail-only mode: the invitation mail, its settings and the register ----------------------
@app.get("/api/mail/settings")
def mail_settings():
    s = invitation.settings()
    return {"subject": s["subject"], "sender": invitation.effective_sender(), "template": invitation.template(),
            "attachments": invitation.attachments(), "visit_weekday": s.get("visit_weekday", "Tuesday"),
            "next_visit_day": invitation.next_visit_day().isoformat(),
            "statuses": invitation.REGISTER_STATUSES, "ref_suggestion": invitation.suggest_ref(),
            "outlook": outlook.public_settings(), "outbox": invitation.outbox_stats()}


@app.post("/api/mail/detect")
async def detect_mail_provider(request: Request):
    """Guess the provider (Google, Microsoft or another server) from the domain of an email address."""
    _need_role(request, "hr")
    b = await _json_dict(request)
    try:
        return outlook.detect_provider(str(b.get("email", "")))
    except ValueError as exc:
        raise invitation.MailProblem(str(exc)) from exc


@app.post("/api/mail/outbox/shrink")
def shrink_mail_outbox(request: Request):
    _need_role(request, "hr")
    return invitation.shrink_outbox()


@app.post("/api/mail/students")
async def add_student_by_hand(request: Request):
    _need_role(request, "hr")
    return invitation.add_by_hand(await _json_dict(request))


@app.post("/api/mail/bulk/prepare")
async def bulk_prepare_invites(request: Request):
    _need_role(request, "hr")
    b = await _json_dict(request)
    return invitation.bulk_prepare(list(b.get("app_nos") or []), str(b.get("visit_date", "")),
                                   list(b.get("attachments") or []))


@app.post("/api/mail/bulk/send")
async def bulk_send_invites(request: Request):
    _need_role(request, "hr")
    b = await _json_dict(request)
    return invitation.bulk_start(list(b.get("app_nos") or []))


@app.get("/api/mail/bulk/{job_id}")
def bulk_send_status(job_id: str):
    return invitation.bulk_status(job_id)


@app.post("/api/mail/settings")
async def save_mail_settings(request: Request):
    _need_role(request, "hr")
    b = await _json_dict(request)
    if b.get("reset"):
        invitation.reset_settings()
    else:
        invitation.save_settings(b.get("subject", ""), b.get("template", ""))
    return mail_settings()


@app.post("/api/mail/test")
async def test_mail_invite(request: Request):
    _need_role(request, "hr")
    b = await _json_dict(request, allow_empty=True)
    cfg = outlook.raw_settings()
    acct = outlook.get_signed_in_account(cfg)
    target = str(
        b.get("to")
        or cfg.get("test_recipient")
        or cfg.get("sender_email")
        or (acct.get("username") if acct else "")
        or cfg.get("smtp_user")
        or ""
    ).strip()
    return invitation.send_test_invite(target)


@app.post("/api/mail/attachments")
async def upload_attachments(request: Request, files: list[UploadFile] = File(...)):
    _need_role(request, "hr")
    results = []
    for f in files[:20]:
        data = await f.read(config.MAIL_ATTACH_MAX_BYTES + 1)
        results.append(invitation.add_attachment(f.filename or "file", data))
    return {"results": results, "attachments": invitation.attachments()}


@app.get("/api/mail/attachments/{name}")
def get_attachment(name: str):
    path = invitation.attachment_path(name)
    if not path.exists():
        raise invitation.MailProblem("That file is not in the attachments.")
    return FileResponse(path, filename=path.name)


@app.post("/api/mail/attachments/{name}/remove")
def remove_attachment(name: str, request: Request):
    _need_role(request, "hr")
    invitation.remove_attachment(name)
    return {"attachments": invitation.attachments()}


@app.post("/api/candidates/{app_no}/mail")
async def save_invite(app_no: str, request: Request):
    _need_role(request, "hr")
    b = await _json_dict(request)
    return {"invite": invitation.save_draft(app_no, b.get("subject", ""), b.get("body", ""), b.get("visit_date", ""),
                                        b.get("attachments", []))}


@app.post("/api/candidates/{app_no}/mail/send")
async def send_invite(app_no: str, request: Request):
    _need_role(request, "hr")
    b = await _json_dict(request, allow_empty=True)
    if b.get("subject") is not None:          # save what is on the screen first
        c = db.get_candidate(app_no)
        if c is not None and (c.invite or {}).get("status") not in ("sent", "opened"):
            invitation.save_draft(app_no, b.get("subject", ""), b.get("body", ""), b.get("visit_date", ""),
                              b.get("attachments", []))
    return {"invite": invitation.send(app_no)}


@app.post("/api/candidates/{app_no}/mail/confirm")
def confirm_invite_sent(app_no: str, request: Request):
    _need_role(request, "hr")
    return {"invite": invitation.confirm_sent(app_no)}


@app.post("/api/candidates/{app_no}/mail/back-to-draft")
def invite_back_to_draft(app_no: str, request: Request):
    _need_role(request, "hr")
    return {"invite": invitation.back_to_draft(app_no)}


@app.post("/api/candidates/{app_no}/mail/resend")
def resend_invite(app_no: str, request: Request):
    _need_role(request, "hr")
    return {"invite": invitation.resend(app_no)}


@app.post("/api/candidates/{app_no}/reply")
async def read_student_reply(app_no: str, request: Request):
    _need_role(request, "hr")
    return invitation.read_reply(app_no, str((await _json_dict(request)).get("text", "")))


@app.post("/api/candidates/{app_no}/by-hand")
async def fill_candidate_by_hand(app_no: str, request: Request):
    _need_role(request, "hr")
    return invitation.fill_by_hand(app_no, await _json_dict(request))


@app.get("/api/candidates/{app_no}/mail/eml")
def download_invite_eml(app_no: str):
    c = db.get_candidate(app_no)
    if c is None:
        raise invitation.MailProblem("Candidate not found.")
    fname = (c.invite or {}).get("file") or ""
    if not fname:
        raise invitation.MailProblem("No .eml archive exists for this invitation yet.")
    path = (db.paths.outbox / fname).resolve()
    if path.parent != db.paths.outbox.resolve() or not path.is_file():
        raise invitation.MailProblem("Saved .eml file could not be found.")
    return FileResponse(path, media_type="message/rfc822", filename=path.name)


@app.post("/api/candidates/{app_no}/mail/open")
def open_invite_eml(app_no: str, request: Request):
    _need_role(request, "hr")
    return invitation.open_sent_eml(app_no)


@app.get("/api/candidates/{app_no}/mail/preview", response_class=HTMLResponse)
def preview_invite(app_no: str):
    """The mail as the student will see it."""
    c = db.get_candidate(app_no)
    if c is None:
        raise invitation.MailProblem("Candidate not found.")
    d = invitation.draft(c)
    esc = __import__("html").escape
    files = "".join(f"<li>{esc(n)}</li>" for n in d.get("attachments", [])) or "<li>No attachments</li>"
    head = (f'<div style="font-family:Arial,sans-serif;font-size:13px;color:#333;border-bottom:1px solid #ccc;'
            f'padding-bottom:10px;margin-bottom:16px"><b>To:</b> {esc(c.name)} &lt;{esc(c.email or "no email yet")}&gt;<br>'
            f'<b>Subject:</b> {esc(d["subject"])}<br><b>Attachments:</b><ul style="margin:4px 0">{files}</ul>'
            f'<span style="color:#888">Preview only. Nothing is sent from this page.</span></div>')
    return HTMLResponse(f'<!doctype html><html><head><meta charset="utf-8"><title>Mail preview - {esc(c.name)}</title></head>'
                        f'<body style="max-width:820px;margin:24px auto;padding:0 16px">{head}{invitation.to_html(d["body"])}</body></html>')


@app.post("/api/candidates/{app_no}/register")
async def save_register(app_no: str, request: Request):
    _need_role(request, "hr")
    return {"register": invitation.save_row(app_no, await _json_dict(request)), "ref_suggestion": invitation.suggest_ref()}


def _register_filters(date_from: str, date_to: str, status: str, source: str) -> dict:
    return {"date_from": date_from, "date_to": date_to, "status": status, "source": source}


def _export_name(ext: str, date_from: str, date_to: str) -> str:
    span = f"_{date_from or 'start'}_to_{date_to or 'today'}" if (date_from or date_to) else ""
    return f"Intern_register_{date.today().isoformat()}{span}.{ext}"


@app.get("/api/register")
def register(show: str = "all", q: str = "", date_from: str = "", date_to: str = "", status: str = "", source: str = ""):
    data, counts = invitation.register_data(show, q, **_register_filters(date_from, date_to, status, source))
    return {"rows": data, "columns": invitation.REGISTER_COLUMNS, "counts": counts,
            "statuses": invitation.REGISTER_STATUSES, "sources": invitation.sources()}


@app.get("/api/register.xlsx")
def register_xlsx(show: str = "all", q: str = "", date_from: str = "", date_to: str = "", status: str = "", source: str = ""):
    name = _export_name("xlsx", date_from, date_to)
    data = invitation.rows(show, q, **_register_filters(date_from, date_to, status, source))
    return Response(invitation.export_xlsx(data),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/register.csv")
def register_csv(show: str = "all", q: str = "", date_from: str = "", date_to: str = "", status: str = "", source: str = ""):
    name = _export_name("csv", date_from, date_to)
    data = invitation.rows(show, q, **_register_filters(date_from, date_to, status, source))
    return Response(invitation.export_csv(data), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/review")
def review():
    with db.Session() as s:
        decisions = list(s.scalars(select(db.Candidate).options(defer(db.Candidate.cv_text))
                                   .where(db.Candidate.status.in_(config.HR_STATUSES))
                                   .order_by(db.Candidate.updated_at)))
        open_rows = list(s.scalars(select(db.Candidate).options(defer(db.Candidate.cv_text))
                                   .where(db.Candidate.status.not_in({"closed", "archived"}))
                                   .order_by(db.Candidate.id)))
        no_area = list(s.scalars(select(db.Candidate).options(defer(db.Candidate.cv_text))
                                 .where(db.Candidate.status == "no_area",
                                        db.Candidate.suggested_new_area != "")))
        waiting = s.scalar(select(func.count(db.Draft.id)).where(db.Draft.status == "waiting")) or 0
    notes = []
    for c in open_rows:
        if c.status in config.HR_STATUSES:
            continue
        for i, f in enumerate(c.flags or []):
            if f.get("open"):
                notes.append({"app_no": c.app_no, "name": c.name, "index": i, "text": f["text"], "by": f.get("by", ""),
                              "at": f.get("at", ""), **_status(c)})
    suggested: dict[str, list] = {}
    for c in decisions + no_area:
        if c.suggested_new_area:
            suggested.setdefault(c.suggested_new_area, []).append(c.app_no)
    reason = {"needs_person": "The CV could not be read.", "needs_area": "Choose the area.",
              "borderline": "Score is between 50 and 59. Decide: shortlist or not.",
              "not_shortlisted_check": "Score is below 50. Confirm Not shortlisted, or shortlist instead."}
    return {"decisions": [dict(summary(c), reason=reason[c.status]) for c in decisions], "notes": notes,
            "replies_waiting": waiting,
            "suggested_areas": [{"name": k, "app_nos": v} for k, v in suggested.items()]}


def _vacancy_json(v: db.Vacancy, candidates_by_app: dict[str, db.Candidate] | None = None) -> dict:
    suggestions = v.suggestions or []
    lookup = candidates_by_app if candidates_by_app is not None else db.get_candidates_by_app_nos([s["app_no"] for s in suggestions])
    rows = []
    for s in suggestions:
        c = lookup.get(s["app_no"])
        rows.append({**s, "state_label": vacancies.STATE_LABELS.get(s["state"], s["state"]),
                     "status_label": _status(c)["status_label"] if c else "",
                     "result": c.result if c else "", "work_id": c.work_id if c else None})
    return {"id": v.id, "area": v.area, "places": v.places, "filled": v.filled, "opens_on": v.opens_on,
            "reason": v.reason, "trainee_ids": v.trainee_ids or [], "status": v.status,
            "created_by": v.created_by, "created_at": v.created_at, "suggestions": rows,
            "waiting": sum(1 for s in rows if s["state"] == "suggested")}


@app.get("/api/vacancies")
def list_vacancies(show: str = "open"):
    """The places to fill. "open" is what the HR officer works on; the rest are history."""
    wanted = {"open"} if show == "open" else ({"filled", "closed"} if show == "done" else
                                              {"open", "filled", "closed"})
    vacs = db.vacancies_in(wanted)
    app_nos = [s["app_no"] for v in vacs for s in (v.suggestions or [])]
    candidates_by_app = db.get_candidates_by_app_nos(app_nos)
    rows = [_vacancy_json(v, candidates_by_app) for v in vacs]
    with db.Session() as s:
        status_counts = dict(s.execute(select(db.Vacancy.status, func.count(db.Vacancy.id))
                                       .group_by(db.Vacancy.status)).all())
    counts = {k: status_counts.get(k, 0) for k in ("open", "filled", "closed")}
    return {"rows": rows, "counts": counts, "show": show,
            "areas": [a.name for a in db.active_areas()],
            "reserves": config.VACANCY_RESERVES, "notice_days": config.VACANCY_NOTICE_DAYS}


@app.get("/api/interns")
def list_interns(show: str = "all", q: str = ""):
    """The intern records copied from Talent Hub (read only)."""
    soon = (date.today() + timedelta(days=config.VACANCY_NOTICE_DAYS)).isoformat()
    today = date.today().isoformat()
    with db.Session() as s:
        every = list(s.scalars(select(db.Intern).order_by(db.Intern.end_date, db.Intern.id)))
    def ending_soon(i):
        return bool(i.end_date) and today <= i.end_date <= soon
    def finished(i):
        return bool(i.end_date) and i.end_date < today
    stats = {"total": len(every), "ending_soon": sum(1 for i in every if ending_soon(i)),
             "finished": sum(1 for i in every if finished(i)),
             "unknown_area": sum(1 for i in every if not i.area)}
    rows = every
    if show == "soon":
        rows = [i for i in every if ending_soon(i)]
    elif show == "unknown":
        rows = [i for i in every if not i.area]
    elif show == "finished":
        rows = [i for i in every if finished(i)]
    if q:
        ql = q.lower()
        rows = [i for i in rows if ql in (i.name or "").lower() or ql in i.trainee_id.lower()]
    return {"stats": stats, "show": show, "source": config.TALENTHUB_SOURCE,
            "synced_at": db.get_setting("interns_synced_at"), "notice_days": config.VACANCY_NOTICE_DAYS,
            "rows": [{"trainee_id": i.trainee_id, "name": i.name, "start_date": i.start_date,
                      "end_date": i.end_date, "specialization": i.specialization, "area": i.area,
                      "ending_soon": ending_soon(i), "finished": finished(i)} for i in rows]}


@app.post("/api/interns/sync")
def sync_interns(request: Request):
    _need_role(request, "hr")
    out = flows.run_planner()
    if out.get("error"):
        raise actions.ActionError(out["error"])
    if out.get("skipped"):
        raise actions.ActionError("The Planner is busy. Try again in a moment.")
    return {**out.get("sync", {}), **out.get("found", {})}


@app.post("/api/vacancies")
async def add_vacancy(request: Request):
    _need_role(request, "hr")
    b = await request.json()
    return {"id": actions.add_vacancy(b.get("area", ""), b.get("places", 1), b.get("reason", ""),
                                      b.get("opens_on", ""))}


@app.post("/api/vacancies/{vacancy_id}/approve")
async def approve_suggestion(vacancy_id: int, request: Request):
    _need_role(request, "hr")
    out = actions.approve_suggestion(vacancy_id, (await request.json()).get("app_no", ""))
    _kick()
    return out


@app.post("/api/vacancies/{vacancy_id}/decline")
async def decline_suggestion(vacancy_id: int, request: Request):
    _need_role(request, "hr")
    actions.decline_suggestion(vacancy_id, (await request.json()).get("app_no", ""))
    return {"ok": True}


@app.post("/api/vacancies/{vacancy_id}/close")
def close_vacancy(vacancy_id: int, request: Request):
    _need_role(request, "hr")
    actions.close_vacancy(vacancy_id)
    return {"ok": True}


@app.get("/api/pool")
def pool(area: str = "", show: str = "all", q: str = ""):
    """The CV pool: CVs waiting to be invited, ranked for one area or by their best score."""
    pool_page = {"new", "needs_person"} | config.POOL_STATUSES
    today = date.today().isoformat()
    since = (datetime.now() - timedelta(hours=24)).replace(microsecond=0).isoformat()
    with db.Session() as s:
        everything = list(
            s.scalars(
                select(db.Candidate)
                .options(defer(db.Candidate.cv_text))
                .where(db.Candidate.status.in_(pool_page | {"archived"}))
            )
        )
        added_today = s.scalar(select(func.count(db.Candidate.id)).where(db.Candidate.created_at >= today)) or 0
    areas = [a.name for a in db.active_areas()]
    stats = {"in_pool": sum(1 for c in everything if c.status in config.POOL_STATUSES),
             "being_read": sum(1 for c in everything if c.status == "new"),
             "no_area": sum(1 for c in everything if c.status == "no_area"),
             "added_today": added_today,
             "archived": sum(1 for c in everything if c.status == "archived")}
    if show == "archived":
        rows = [c for c in everything if c.status == "archived"]
    elif show == "no_area":
        rows = [c for c in everything if c.status == "no_area"]
    elif show == "recent":
        rows = [c for c in everything if c.status in pool_page and (c.created_at or "") >= since]
    else:
        rows = [c for c in everything if c.status in pool_page]
    if q:
        ql = q.lower()
        rows = [c for c in rows if ql in (c.name or "").lower() or ql in c.app_no.lower()]
    if area and area in areas:
        def area_score(c):
            v = (c.area_scores or {}).get(area)
            return v["score"] if v else None
        # Only CVs that score enough for this area AND show real evidence for it (a related
        # project, job or studies). A skills list alone does not put a CV on an area's list.
        rows = [c for c in rows if c.status == "in_pool"
                and any(n == area for n, _ in tools.good_areas(c.area_scores or {}))]
        rows.sort(key=lambda c: (-area_score(c), c.id))
    elif show == "recent":
        rows.sort(key=lambda c: (c.created_at or "", c.id), reverse=True)
    else:
        rows.sort(key=lambda c: (c.score is None, -(c.score or 0), c.id))
    out = []
    for i, c in enumerate(rows, 1):
        scores = c.area_scores or {}
        shown = area if area in scores else c.area
        v = scores.get(shown) if shown else None
        out.append(dict(summary(c),
                        rank=i if (area or show != "recent") and v else None,
                        best_area=c.area, best_score=c.score,
                        area_score=v["score"] if v else None,
                        gaps=(v or {}).get("gaps", []) if shown else [],
                        other_areas=[{"area": n, "score": sc} for n, sc in
                                     tools.good_areas(scores, skip=tuple(x for x in (c.area, area) if x),
                                                      n=config.POOL_OTHER_AREAS)]))
    return {"stats": stats, "areas": areas, "min_score": config.POOL_MIN_SCORE, "area": area if area in areas else "",
            "show": show, "rows": out}


@app.get("/api/drafts")
def list_drafts(state: str = "waiting"):
    with db.Session() as s:
        rows = list(s.execute(select(db.Draft, db.Candidate).join(db.Candidate, db.Candidate.id == db.Draft.candidate_id)
                              .where(db.Draft.status == state).order_by(db.Draft.id.desc())).all())
    test_recipient = outlook.raw_settings().get("test_recipient", "")
    return [_draft_json(d, c, test_recipient=test_recipient) for d, c in rows]


@app.get("/api/interviews")
def interviews():
    with db.Session() as s:
        waiting = list(s.scalars(select(db.Candidate).options(defer(db.Candidate.cv_text))
                                 .where(db.Candidate.status == "awaiting_result")
                                 .order_by(db.Candidate.interview_at)))
        hold = list(s.scalars(select(db.Candidate).options(defer(db.Candidate.cv_text))
                              .where(db.Candidate.status == "on_hold")
                              .order_by(db.Candidate.interview_at)))
        coming = list(s.scalars(select(db.Candidate).options(defer(db.Candidate.cv_text))
                                .where(db.Candidate.status == "invite_ready")
                                .order_by(db.Candidate.interview_at)))
        done = list(s.scalars(select(db.Candidate).options(defer(db.Candidate.cv_text))
                              .where(db.Candidate.result != "")
                              .order_by(db.Candidate.result_at.desc()).limit(30)))
    return {"waiting": [summary(c) for c in waiting], "on_hold": [summary(c) for c in hold],
            "invitation_not_released": [summary(c) for c in coming], "done": [summary(c) for c in done]}


@app.get("/api/areas")
def list_areas():
    with db.Session() as s:
        rows = list(s.scalars(select(db.Area).order_by(db.Area.id)))
        counts = dict(s.execute(select(db.Candidate.area, func.count(db.Candidate.id))
                                .group_by(db.Candidate.area)).all())
        suggested = dict(s.execute(select(db.Candidate.suggested_new_area, func.count(db.Candidate.id))
                                   .where(db.Candidate.suggested_new_area != "")
                                   .group_by(db.Candidate.suggested_new_area)).all())
    return {"areas": [{"id": a.id, "name": a.name, "description": a.description, "required_skills": a.required_skills,
                       "preferred_skills": a.preferred_skills, "studies": a.studies or [], "keywords": a.keywords, "checklist": a.checklist,
                       "active": a.active, "added_by": a.added_by, "candidates": counts.get(a.name, 0)} for a in rows],
            "suggested": [{"name": k, "count": v} for k, v in suggested.items()]}


@app.get("/api/team")
def team():
    nxt = scheduler.next_runs()
    since = (datetime.now() - timedelta(hours=24)).replace(microsecond=0).isoformat()
    out = []
    with db.Session() as s:
        for key, name in EMPLOYEES.items():
            runs = list(s.scalars(select(db.Run).where(db.Run.employee == name).order_by(db.Run.id.desc()).limit(15)))
            day = list(s.scalars(select(db.Run).where(db.Run.employee == name, db.Run.started_at >= since)))
            plan = schedule(key)
            out.append({
                "key": key, "name": name, "summary": role_summary(key), "schedule": plan, "next_run": nxt.get(key, ""),
                "routines": routines(key), "lessons": lessons(key), **flows.TEAM[key],
                "day": {o: sum(1 for r in day if r.outcome == o) for o in ("ok", "fixed", "fallback", "stopped", "error")},
                "runs": [{"at": r.started_at, "routine": r.routine, "outcome": r.outcome, "seconds": r.seconds,
                          "model_calls": r.model_calls, "note": r.note} for r in runs],
            })
    return {"employees": out, "paused": db.get_setting("paused") == "1", "model": llm.health(),
            "notice": flows.NOTICE, "scheduler": config.SCHEDULER_ENABLED}


# ---- changing (HR officer) -------------------------------------------------------------------
@app.post("/api/cvs")
async def upload_cvs(request: Request, files: list[UploadFile] = File(...), email: str = Form(""),
                      source: str = Form("")):
    _need_role(request, "hr")
    results = []
    for f in files[:30]:
        data = await f.read(config.MAX_UPLOAD_BYTES + 1)
        results.append(actions.add_cv(f.filename or "cv", data, email if len(files) == 1 else "", source))
    if any(r["ok"] for r in results):
        _kick()
    return {"results": results}


@app.post("/api/samples")
def load_samples(request: Request):
    _need_role(request, "hr")
    results = actions.add_samples()
    if any(r["ok"] for r in results):
        _kick()
    return {"results": results}


@app.post("/api/run")
async def run_now(request: Request):
    body = await request.json()
    who = body.get("employee", "all")
    if who == "all":
        flows.start_background(flows.run_all_once)
    elif who == "chief-of-staff":
        flows.start_background(flows.run_chief)
    elif who in flows.TRIGGERS:
        flows.start_background(flows.run_employee, who, 20, True)
    else:
        raise actions.ActionError("Unknown employee.")
    return {"started": who}


@app.post("/api/pause")
async def pause(request: Request):
    _need_role(request, "hr")
    body = await request.json()
    db.set_setting("paused", "1" if body.get("paused") else "0")
    db.log_event(None, "HR officer", "Paused the AI employees." if body.get("paused") else "Resumed the AI employees.")
    return {"paused": bool(body.get("paused"))}


@app.post("/api/candidates/{app_no}/email")
async def set_email(app_no: str, request: Request):
    _need_role(request, "hr")
    actions.set_email(app_no, (await request.json()).get("email", ""))
    return {"ok": True}


@app.post("/api/candidates/{app_no}/area")
async def choose_area(app_no: str, request: Request):
    _need_role(request, "hr")
    out = actions.choose_area(app_no, (await request.json()).get("area", ""))
    _kick()
    return out


@app.post("/api/candidates/{app_no}/decision")
async def decision(app_no: str, request: Request):
    _need_role(request, "hr")
    new = actions.decide(app_no, (await request.json()).get("decision", ""))
    _kick()
    return {"status": new}


@app.post("/api/candidates/{app_no}/retry")
def retry(app_no: str, request: Request):
    _need_role(request, "hr")
    actions.retry(app_no)
    _kick()
    return {"ok": True}


@app.post("/api/candidates/{app_no}/invite")
async def invite(app_no: str, request: Request):
    _need_role(request, "hr")
    out = actions.invite_from_pool(app_no, (await request.json()).get("area", ""))
    _kick()
    return out


@app.post("/api/candidates/{app_no}/archive")
def archive(app_no: str, request: Request):
    _need_role(request, "hr")
    actions.archive(app_no)
    return {"ok": True}


@app.post("/api/candidates/{app_no}/restore")
def restore(app_no: str, request: Request):
    _need_role(request, "hr")
    actions.restore(app_no)
    _kick()
    return {"ok": True}


@app.post("/api/candidates/{app_no}/close")
def close(app_no: str, request: Request):
    _need_role(request, "hr")
    actions.close(app_no)
    return {"ok": True}


@app.post("/api/candidates/{app_no}/flags/{index}/resolve")
def resolve_flag(app_no: str, index: int, request: Request):
    _need_role(request, "hr")
    actions.resolve_flag(app_no, index)
    return {"ok": True}


@app.post("/api/drafts/{draft_id}")
async def edit_draft(draft_id: int, request: Request):
    _need_role(request, "hr")
    body = await request.json()
    problems = actions.edit_draft(draft_id, body.get("subject", ""), body.get("body", ""))
    return {"check_notes": problems}


@app.get("/api/drafts/{draft_id}/eml")
def download_draft_eml(draft_id: int):
    fname, raw = actions.export_draft_eml(draft_id)
    return Response(
        content=raw,
        media_type="message/rfc822",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/drafts/{draft_id}/open")
def open_draft_app(draft_id: int, request: Request):
    _need_role(request, "hr")
    return actions.open_draft_in_app(draft_id)


@app.post("/api/drafts/{draft_id}/outlook-draft")
def push_outlook_draft(draft_id: int, request: Request):
    _need_role(request, "hr")
    return actions.push_draft_to_outlook(draft_id)


@app.post("/api/drafts/{draft_id}/release")
def release(draft_id: int, request: Request):
    _need_role(request, "hr")
    new_status = actions.release_draft(draft_id)
    with db.Session() as s:
        d = s.get(db.Draft, draft_id)
        return {
            "status": new_status,
            "delivery_status": d.delivery_status if d else "eml_only",
            "delivery_error": d.delivery_error if d else "",
            "sent_to": d.sent_to if d else "",
            "outlook_web_link": d.outlook_web_link if d else "",
        }


@app.post("/api/drafts/{draft_id}/resend")
def resend_draft(draft_id: int, request: Request):
    _need_role(request, "hr")
    return actions.resend_draft(draft_id)


@app.get("/api/outlook/settings")
def get_outlook_settings():
    return outlook.public_settings()


@app.post("/api/outlook/settings")
async def save_outlook_settings(request: Request):
    _need_role(request, "hr")
    payload = await _json_dict(request)
    try:
        saved = outlook.save_settings(payload)
    except ValueError as exc:
        raise actions.ActionError(str(exc)) from exc
    db.log_event(None, "HR officer", f"Updated Outlook settings (mode: {saved['mode']}).")
    return saved


@app.post("/api/outlook/auth/start")
def start_outlook_auth(request: Request):
    _need_role(request, "hr")
    try:
        return outlook.start_device_flow()
    except outlook.OutlookError as exc:
        raise actions.ActionError(str(exc)) from exc


@app.post("/api/outlook/auth/interactive")
def start_outlook_interactive(request: Request):
    _need_role(request, "hr")
    try:
        return outlook.start_interactive_login()
    except outlook.OutlookError as exc:
        raise actions.ActionError(str(exc)) from exc


@app.get("/api/outlook/auth/status")
def outlook_auth_status():
    return outlook.auth_status()


@app.post("/api/outlook/auth/signout")
def signout_outlook(request: Request):
    _need_role(request, "hr")
    outlook.sign_out()
    db.log_event(None, "HR officer", "Signed out of Microsoft Outlook.")
    return outlook.public_settings()


@app.post("/api/outlook/test")
async def test_outlook(request: Request):
    _need_role(request, "hr")
    body = await _json_dict(request, allow_empty=True)
    cfg = outlook.raw_settings()
    acct = outlook.get_signed_in_account(cfg)
    target = str(
        body.get("to")
        or cfg.get("test_recipient")
        or cfg.get("sender_email")
        or (acct.get("username") if acct else "")
        or cfg.get("smtp_user")
        or ""
    )
    try:
        res = outlook.send_test(target)
    except ValueError as exc:
        raise actions.ActionError(str(exc)) from exc
    if not res["ok"]:
        raise actions.ActionError(f"Outlook test failed: {res['error']}")
    db.log_event(None, "HR officer", f"Tested Outlook ({res['delivery_status']}) to {res['sent_to']}.")
    return res


@app.post("/api/areas")
async def add_area(request: Request):
    _need_role(request, "hr")
    b = await request.json()
    new_id = actions.save_area(None, b.get("name", ""), b.get("description", ""), b.get("required_skills", ""),
                               b.get("preferred_skills", ""), b.get("keywords", ""), b.get("checklist", ""), True,
                               b.get("studies"))
    actions.rescore_pool()
    _kick()
    return {"id": new_id}


@app.put("/api/areas/{area_id}")
async def update_area(area_id: int, request: Request):
    _need_role(request, "hr")
    b = await request.json()
    actions.save_area(area_id, b.get("name", ""), b.get("description", ""), b.get("required_skills", ""),
                      b.get("preferred_skills", ""), b.get("keywords", ""), b.get("checklist", ""),
                      b.get("active", True), b.get("studies"))
    actions.rescore_pool()
    _kick()
    return {"ok": True}


@app.post("/api/brief")
def brief_now(request: Request):
    _need_role(request, "hr")
    flows.start_background(flows.run_chief)
    return {"started": True}


# ---- changing (Interviewer) ------------------------------------------------------------------
@app.post("/api/candidates/{app_no}/result")
async def result(app_no: str, request: Request):
    _need_role(request, "interviewer")
    b = await request.json()
    new = actions.enter_result(app_no, b.get("result", ""), b.get("comment", ""))
    _kick()
    return {"status": new}


# ---- the pages -------------------------------------------------------------------------------
app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")
