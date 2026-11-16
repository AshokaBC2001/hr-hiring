"""The web server: the API for the pages, and the pages themselves (web/ folder).

It only listens on 127.0.0.1, refuses other host names and refuses changes
that come from other websites. Two roles use it, chosen at the top of the page:
"hr" (HR officer) and "interviewer".
"""
from __future__ import annotations

import base64
import csv
import hmac
import io
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from . import actions, config, db, flows, llm, mailer, scheduler, supervisor, talent_hub, tools, vacancies
from .employees import EMPLOYEES, lessons, role_summary, routines, schedule

VERSION = "1.1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.setup()
    if config.SCHEDULER_ENABLED:
        scheduler.start()
    yield
    scheduler.stop()
    talent_hub.close()
    llm.close()


app = FastAPI(title="HR Hiring", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)


def _strip_host_port(raw: str) -> str:
    val = (raw or "").split(",", 1)[0].strip().lower()
    if val.startswith("["):
        return val.split("]", 1)[0] + "]"
    return val.split(":", 1)[0]


def _host_allowed(host_header: str) -> bool:
    allowed = config.ALLOWED_HOSTS
    if "*" in allowed:
        return True
    host = _strip_host_port(host_header)
    if not host:
        return False
    for pattern in allowed:
        p = pattern.lower()
        if p == host or (p.startswith(".") and (host.endswith(p) or host == p.lstrip("."))):
            return True
    return False


def _origin_allowed(origin: str, request: Request) -> bool:
    orig = origin.strip().lower()
    if orig in config.ALLOWED_ORIGINS:
        return True
    parsed = urlparse(orig)
    orig_host = (parsed.hostname or "").lower()
    if not orig_host:
        return False
    trusted_hosts = {"localhost", "127.0.0.1", "::1"}
    for h in (request.headers.get("host"), request.headers.get("x-forwarded-host")):
        stripped = _strip_host_port(h or "").strip("[]")
        if stripped:
            trusted_hosts.add(stripped)
    for item in config.ALLOWED_ORIGINS:
        p_host = (urlparse(item).hostname or item).lower()
        if p_host:
            trusted_hosts.add(p_host)
    return orig_host in trusted_hosts


def _check_auth(request: Request) -> Response | None:
    if request.url.path == "/api/health":
        return None
    has_basic = bool(config.AUTH_USER and config.AUTH_PASS)
    has_token = bool(config.ACCESS_TOKEN)
    if not has_basic and not has_token:
        return None
    auth = request.headers.get("authorization", "")
    token_hdr = request.headers.get("x-access-token", "").strip()
    if has_token:
        if auth.lower().startswith("bearer ") and hmac.compare_digest(auth[7:].strip(), config.ACCESS_TOKEN):
            return None
        if token_hdr and hmac.compare_digest(token_hdr, config.ACCESS_TOKEN):
            return None
    if has_basic and auth.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth[6:].strip()).decode("utf-8", errors="strict")
            user, password = decoded.split(":", 1)
            if hmac.compare_digest(user, config.AUTH_USER) and hmac.compare_digest(password, config.AUTH_PASS):
                return None
        except Exception:
            pass
    headers = {"WWW-Authenticate": 'Basic realm="HR Hiring"'} if has_basic else {}
    return JSONResponse({"detail": "Authentication required."}, status_code=401, headers=headers)


@app.middleware("http")
async def hosting_and_security_guard(request: Request, call_next):
    if not _host_allowed(request.headers.get("host", "")):
        return PlainTextResponse("Invalid host header", status_code=400)
    auth_resp = _check_auth(request)
    if auth_resp is not None:
        return auth_resp
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and not _origin_allowed(origin, request):
            return JSONResponse({"detail": "Requests from other websites are not allowed."}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(actions.ActionError)
async def action_error(request: Request, exc: actions.ActionError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(talent_hub.TalentHubUnavailable)
async def talent_hub_unavailable(request: Request, exc: talent_hub.TalentHubUnavailable):
    return JSONResponse({"detail": str(exc)}, status_code=503)


@app.exception_handler(talent_hub.TalentHubAuthError)
async def talent_hub_auth_error(request: Request, exc: talent_hub.TalentHubAuthError):
    return JSONResponse({"detail": str(exc)}, status_code=502)


def _role(request: Request) -> str:
    return request.headers.get("x-role", "hr")


def _need_role(request: Request, role: str) -> None:
    if _role(request) != role:
        who = "HR officer" if role == "hr" else "Interviewer"
        raise actions.ActionError(f"Only the {who} can do this. Switch the role at the top right.")


def _kick() -> None:
    """After a person acts, coalesce employee + vacancy updates on a background thread."""
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


def _draft_json(d: db.Draft, c: db.Candidate | None = None) -> dict:
    out = {"id": d.id, "kind": d.kind, "kind_label": actions.KIND_LABELS.get(d.kind, d.kind), "subject": d.subject,
           "body": d.body, "status": d.status, "written_by": d.written_by, "check_notes": d.check_notes or [],
           "created_at": d.created_at, "released_at": d.released_at, "file": d.file,
           "delivery_status": d.delivery_status or "not_sent", "delivery_error": d.delivery_error or "",
           "sent_at": d.sent_at or "", "sent_to": d.sent_to or "", "message_id": d.message_id or ""}
    if c is not None:
        before = actions.RELEASE_MOVES[d.kind][0]
        out.update(app_no=c.app_no, name=c.name, email=c.email, candidate_status=c.status,
                   can_release=(d.status == "waiting" and c.status == before and bool(c.email)),
                   release_blocker="" if c.email else "Add the candidate's email first.")
    return out


def detail(c: db.Candidate) -> dict:
    with db.Session() as s:
        drafts = list(s.scalars(select(db.Draft).where(db.Draft.candidate_id == c.id).order_by(db.Draft.id)))
        events = list(s.scalars(select(db.Event).where(db.Event.candidate_id == c.id).order_by(db.Event.id)))
        runs = list(s.scalars(select(db.Run).where(db.Run.candidate_id == c.id).order_by(db.Run.id)))
    out = summary(c)
    out.update({
        "email_source": c.email_source, "profile": c.profile or {}, "profile_source": c.profile_source,
        "area_source": c.area_source, "area_reasons": c.area_reasons or [], "area_evidence": c.area_evidence or [],
        "score_parts": c.score_parts or {}, "gaps": c.gaps or [], "questions": c.questions or [],
        "questions_source": c.questions_source, "result_comment": c.result_comment, "result_at": c.result_at,
        "checklist": c.checklist or [],
        "hr_notes": c.hr_notes or [],
        "area_scores": [{"area": a, "score": sc} for a, sc in tools.top_areas(c.area_scores or {})],
        "active_areas": [a.name for a in db.active_areas()],
        "returning_interns": talent_hub.find_matching_interns(email=c.email, name=c.name),
        "sample_mongo_available": talent_hub.is_sample_mongo_target(),
        "flags": [dict(f, index=i) for i, f in enumerate(c.flags or [])],
        "drafts": [_draft_json(d, c) for d in drafts],
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
        draft_delivery = dict(s.execute(
            select(db.Draft.delivery_status, func.count(db.Draft.id))
            .where(db.Draft.status == "released")
            .group_by(db.Draft.delivery_status)
        ).all())
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
        "model": llm.health(), "talent_hub": talent_hub.health(), "notice": flows.NOTICE,
        "email": mailer.public_settings(),
        "paused": db.get_setting("paused") == "1",
        "counts": counts, "total": sum(counts.values()),
        "tiles": {"new": counts.get("new", 0), "review": hr_count + flagged, "release": waiting,
                  "interview": counts.get("awaiting_result", 0) + counts.get("on_hold", 0), "selected": selected,
                  "vacancies": vacancy_waiting, "pool": counts.get("in_pool", 0) + counts.get("no_area", 0)},
        "team": {k: dict(flows.TEAM[k], name=EMPLOYEES[k]) for k in EMPLOYEES},
        "recent": [{"at": e.at, "actor": e.actor, "text": e.text, "app_no": app_no or "", "name": name or ""}
                   for e, app_no, name in recent],
        "areas_chart": [{"area": a, "count": by_area.get(a, 0)} for a in areas] +
                       [{"area": a, "count": n} for a, n in by_area.items() if a not in areas],
        "brief": {"text": brief.text, "created_at": brief.created_at, "written_by": brief.written_by} if brief else None,
        "vacancies": {"open": len(open_vacancies),
                      "places": sum(max(0, v.places - v.filled) for v in open_vacancies),
                      "waiting": vacancy_waiting},
        "database": {
            "candidates": sum(counts.values()),
            "pool": counts.get("in_pool", 0) + counts.get("no_area", 0),
            "selected": selected,
            "emails_sent": draft_delivery.get("sent", 0),
            "emails_outbox": draft_delivery.get("outbox_only", 0),
            "emails_failed": draft_delivery.get("failed", 0),
        },
    }


@app.get("/api/candidates")
def list_candidates(status: str = "", q: str = ""):
    with db.Session() as s:
        query = select(db.Candidate).order_by(db.Candidate.id.desc())
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
        elif status == "all":
            pass
        elif status:
            query = query.where(db.Candidate.status == status)
        else:
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


@app.get("/api/review")
def review():
    with db.Session() as s:
        decisions = list(s.scalars(select(db.Candidate).where(db.Candidate.status.in_(config.HR_STATUSES))
                                   .order_by(db.Candidate.updated_at)))
        open_rows = list(s.scalars(select(db.Candidate).where(db.Candidate.status.not_in({"closed", "archived"}))
                                   .order_by(db.Candidate.id)))
        no_area = list(s.scalars(select(db.Candidate).where(db.Candidate.status == "no_area",
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


def _vacancy_json(v: db.Vacancy, cand_map: dict[str, db.Candidate] | None = None) -> dict:
    if cand_map is None:
        cand_map = db.get_candidates_by_app_nos([s_["app_no"] for s_ in (v.suggestions or [])])
    rows = []
    for s_ in (v.suggestions or []):
        c = cand_map.get(s_["app_no"])
        rows.append({**s_, "state_label": vacancies.STATE_LABELS.get(s_["state"], s_["state"]),
                     "status_label": _status(c)["status_label"] if c else "",
                     "result": c.result if c else "", "work_id": c.work_id if c else None})
    return {"id": v.id, "area": v.area, "places": v.places, "filled": v.filled, "opens_on": v.opens_on,
            "reason": v.reason, "trainee_ids": v.trainee_ids or [], "status": v.status,
            "created_by": v.created_by, "created_at": v.created_at, "suggestions": rows,
            "waiting": sum(1 for s_ in rows if s_["state"] == "suggested")}


@app.get("/api/vacancies")
def list_vacancies(show: str = "open"):
    wanted = {"open"} if show == "open" else ({"filled", "closed"} if show == "done" else
                                              {"open", "filled", "closed"})
    vacs = db.vacancies_in(wanted)
    cand_map = db.get_candidates_by_app_nos(
        [s_["app_no"] for v in vacs for s_ in (v.suggestions or [])]
    )
    rows = [_vacancy_json(v, cand_map=cand_map) for v in vacs]
    with db.Session() as s:
        raw_counts = dict(s.execute(select(db.Vacancy.status, func.count(db.Vacancy.id))
                                    .group_by(db.Vacancy.status)).all())
    counts = {k: raw_counts.get(k, 0) for k in ("open", "filled", "closed")}
    return {"rows": rows, "counts": counts, "show": show,
            "areas": [a.name for a in db.active_areas()],
            "reserves": config.VACANCY_RESERVES, "notice_days": config.VACANCY_NOTICE_DAYS}


@app.post("/api/vacancies/plan")
@app.post("/api/interns/sync")
def plan_vacancies(request: Request):
    _need_role(request, "hr")
    out = flows.run_planner()
    if out.get("error"):
        raise actions.ActionError(out["error"])
    if out.get("skipped"):
        raise actions.ActionError("The Planner is busy. Try again in a moment.")
    return {**out.get("sync", {}), **out.get("found", {}), **out.get("suggested", {})}


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
    """The CV pool: CVs waiting to be invited, ranked per area or by best score."""
    pool_page = {"new", "needs_person"} | config.POOL_STATUSES
    today = date.today().isoformat()
    since = (datetime.now() - timedelta(hours=24)).replace(microsecond=0).isoformat()
    with db.Session() as s:
        everything = list(s.scalars(select(db.Candidate).where(db.Candidate.status.in_(pool_page | {"archived"}))))
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
        rows = [c for c in rows if c.status == "in_pool" and (area_score(c) or 0) >= config.POOL_MIN_SCORE]
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
                                     tools.top_areas(scores, skip=tuple(x for x in (c.area, area) if x),
                                                     n=config.POOL_OTHER_AREAS)]))
    return {"stats": stats, "areas": areas, "min_score": config.POOL_MIN_SCORE, "area": area if area in areas else "",
            "show": show, "rows": out}


@app.get("/api/drafts")
def list_drafts(state: str = "waiting"):
    with db.Session() as s:
        rows = list(s.execute(select(db.Draft, db.Candidate).join(db.Candidate, db.Candidate.id == db.Draft.candidate_id)
                              .where(db.Draft.status == state).order_by(db.Draft.id.desc())).all())
    return [_draft_json(d, c) for d, c in rows]


@app.get("/api/interviews")
def interviews():
    with db.Session() as s:
        waiting = list(s.scalars(select(db.Candidate).where(db.Candidate.status == "awaiting_result")
                                 .order_by(db.Candidate.interview_at)))
        hold = list(s.scalars(select(db.Candidate).where(db.Candidate.status == "on_hold")
                              .order_by(db.Candidate.interview_at)))
        coming = list(s.scalars(select(db.Candidate).where(db.Candidate.status == "invite_ready")
                                .order_by(db.Candidate.interview_at)))
        done = list(s.scalars(select(db.Candidate).where(db.Candidate.result != "")
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
                       "preferred_skills": a.preferred_skills, "keywords": a.keywords, "checklist": a.checklist,
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


@app.get("/api/interns")
def list_interns(specialization: str = "", show: str = "all", q: str = "", limit: int = 1000):
    bounded_limit = max(1, min(limit, 1000))
    area_names = [a.name for a in db.active_areas()]
    enriched = talent_hub.interns_with_area(area_names, limit=0)
    stats = talent_hub.analytics(enriched)
    rows = enriched
    if show == "soon":
        rows = [r for r in rows if r["ending_soon"]]
    elif show == "active":
        rows = [r for r in rows if r["active"]]
    elif show == "finished":
        rows = [r for r in rows if r["finished"]]
    elif show == "upcoming":
        rows = [r for r in rows if r["upcoming"]]
    elif show == "unknown":
        rows = [r for r in rows if not r["area"]]
    rows = talent_hub._filter_rows(rows, specialization=specialization, q=q, limit=bounded_limit)
    return {
        "interns": rows,
        "rows": rows,
        "total": len(rows),
        "stats": stats,
        "show": show,
        "source": "test" if talent_hub._backend is not None else talent_hub.resolve_mode(),
        "notice_days": config.VACANCY_NOTICE_DAYS,
    }


@app.get("/api/interns/{identifier}")
def get_intern(identifier: str):
    doc = talent_hub.get_intern(identifier)
    if doc is None:
        return JSONResponse({"detail": f"No intern matching {identifier} was found."}, status_code=404)
    return doc


# ---- changing (HR officer) -------------------------------------------------------------------
@app.post("/api/cvs")
async def upload_cvs(request: Request, files: list[UploadFile] = File(...), email: str = Form("")):
    _need_role(request, "hr")
    results = []
    for f in files[:30]:
        data = await f.read(config.MAX_UPLOAD_BYTES + 1)
        results.append(actions.add_cv(f.filename or "cv", data, email if len(files) == 1 else ""))
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
        flows.start_background(flows.run_planner)
    elif who == "chief-of-staff":
        flows.start_background(flows.run_chief)
    elif who == "planner":
        flows.start_background(flows.run_planner)
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


@app.post("/api/candidates/{app_no}/checklist/{index}")
async def toggle_checklist(app_no: str, index: int, request: Request):
    _need_role(request, "hr")
    done = None
    if request.headers.get("content-length", "0") != "0":
        try:
            body = await request.json()
            if "done" in body:
                done = bool(body["done"])
        except Exception:
            pass
    return {"checklist": actions.toggle_checklist(app_no, index, done)}


@app.post("/api/candidates/{app_no}/notes")
async def add_note(app_no: str, request: Request):
    role = _role(request)
    actor = "HR officer" if role == "hr" else "Interviewer"
    body = await request.json()
    return {"hr_notes": actions.add_candidate_note(app_no, body.get("text", ""), actor=actor)}


@app.post("/api/candidates/{app_no}/enroll-intern")
async def enroll_intern(app_no: str, request: Request):
    _need_role(request, "hr")
    months = 6
    if request.headers.get("content-length", "0") != "0":
        try:
            body = await request.json()
            months = int(body.get("months", 6))
        except Exception:
            months = 6
    return actions.enroll_candidate_as_intern(app_no, months=months)


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


@app.post("/api/drafts/{draft_id}/release")
def release(draft_id: int, request: Request):
    _need_role(request, "hr")
    new_status = actions.release_draft(draft_id)
    with db.Session() as s:
        d = s.get(db.Draft, draft_id)
        delivery_status = d.delivery_status if d else "outbox_only"
        sent_to = d.sent_to if d else ""
        delivery_error = d.delivery_error if d else ""
    return {
        "status": new_status,
        "delivery_status": delivery_status,
        "sent_to": sent_to,
        "delivery_error": delivery_error,
    }


@app.post("/api/drafts/{draft_id}/resend")
def resend_draft(draft_id: int, request: Request):
    _need_role(request, "hr")
    return actions.resend_draft(draft_id)


@app.get("/api/email/settings")
def get_email_settings():
    return mailer.public_settings()


@app.post("/api/email/settings")
async def update_email_settings(request: Request):
    _need_role(request, "hr")
    try:
        saved = mailer.save_settings(await request.json())
    except ValueError as exc:
        raise actions.ActionError(str(exc)) from exc
    db.log_event(None, "HR officer", f"Updated SMTP email settings (mode: {saved['mode']}).")
    return saved


@app.post("/api/email/test")
async def test_email(request: Request):
    _need_role(request, "hr")
    body = await request.json()
    try:
        res = mailer.send_test_email(body.get("to", ""))
    except ValueError as exc:
        raise actions.ActionError(str(exc)) from exc
    if not res["ok"]:
        raise actions.ActionError(f"SMTP test failed: {res['error']}")
    db.log_event(None, "HR officer", f"Sent test SMTP email to {res['sent_to']}.")
    return res


def _csv_cell(val: object) -> str:
    txt = "" if val is None else str(val)
    if txt and txt[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + txt
    return txt


@app.get("/api/health")
def health_probe():
    with db.Session() as s:
        integrity = str(s.connection().exec_driver_sql("PRAGMA quick_check").scalar() or "")
    ok = integrity == "ok"
    payload = {
        "ok": ok,
        "version": VERSION,
        "sqlite": integrity,
        "talenthub_mode": talent_hub.resolve_mode(),
        "scheduler": config.SCHEDULER_ENABLED,
    }
    return JSONResponse(payload, status_code=200 if ok else 503)


@app.get("/api/diagnostics")
def system_diagnostics():
    return supervisor.diagnostics(force=True)


@app.get("/api/analytics/pipeline")
def pipeline_analytics():
    return supervisor.pipeline_analytics()


@app.get("/api/feedback")
def get_feedback(category: str = "", status: str = ""):
    return supervisor.list_feedback(category=category, status=status)


@app.post("/api/feedback")
async def post_feedback(request: Request):
    b = await request.json()
    default_role = "hr" if _role(request) == "hr" else "interviewer"
    return supervisor.submit_feedback(
        reviewer=b.get("reviewer", "Supervisor"),
        role=b.get("role", default_role),
        category=b.get("category", "general"),
        comment=b.get("comment", ""),
        rating=b.get("rating"),
        target=b.get("target", ""),
    )


@app.post("/api/feedback/{feedback_id}/resolve")
async def resolve_feedback(feedback_id: int, request: Request):
    _need_role(request, "hr")
    b = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    return supervisor.resolve_feedback(
        feedback_id,
        resolution_note=b.get("resolution_note", ""),
        status=b.get("status", "resolved"),
    )


@app.get("/api/export/candidates.csv")
def export_candidates_csv(status: str = "all"):
    rows = list_candidates(status=status)
    cols = ["app_no", "name", "email", "area", "score", "status_label", "interview_label", "result", "work_id", "outcome", "created_at"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(cols)
    for r in rows:
        writer.writerow([_csv_cell(r.get(c)) for c in cols])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="hr_candidates_export.csv"'},
    )


@app.get("/api/export/feedback.csv")
def export_feedback_csv():
    data = supervisor.list_feedback()
    cols = ["id", "reviewer", "role", "category", "rating", "target", "status", "comment", "resolution_note", "created_at", "resolved_at"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(cols)
    for r in data["rows"]:
        writer.writerow([_csv_cell(r.get(c)) for c in cols])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="hr_supervisor_feedback.csv"'},
    )


@app.get("/api/export/supervisor-report.md")
@app.get("/api/export/feedback.md")
def export_supervisor_report():
    return Response(
        content=supervisor.supervisor_report_markdown(),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="hr_supervisor_report.md"'},
    )


@app.get("/api/export/database.json")
def export_database_json():
    with db.Session() as s:
        cands = [summary(c) for c in s.scalars(select(db.Candidate).order_by(db.Candidate.id))]
        vacs = db.vacancies_in({"open", "filled", "closed"})
        cand_map = db.get_candidates_by_app_nos([s_["app_no"] for v in vacs for s_ in (v.suggestions or [])])
        vac_rows = [_vacancy_json(v, cand_map=cand_map) for v in vacs]
        areas = [{"name": a.name, "active": a.active, "required_skills": a.required_skills}
                 for a in s.scalars(select(db.Area).order_by(db.Area.id))]
        drafts = [{"id": d.id, "kind": d.kind, "status": d.status, "delivery_status": d.delivery_status,
                   "sent_to": d.sent_to, "released_at": d.released_at}
                  for d in s.scalars(select(db.Draft).order_by(db.Draft.id))]
    fb = supervisor.list_feedback()
    return {"exported_at": db.now(), "candidates": cands, "vacancies": vac_rows, "areas": areas,
            "drafts": drafts, "feedback": fb["rows"]}


@app.post("/api/areas")
async def add_area(request: Request):
    _need_role(request, "hr")
    b = await request.json()
    new_id = actions.save_area(None, b.get("name", ""), b.get("description", ""), b.get("required_skills", ""),
                               b.get("preferred_skills", ""), b.get("keywords", ""), b.get("checklist", ""), True)
    actions.rescore_pool()
    _kick()
    return {"id": new_id}


@app.put("/api/areas/{area_id}")
async def update_area(area_id: int, request: Request):
    _need_role(request, "hr")
    b = await request.json()
    actions.save_area(area_id, b.get("name", ""), b.get("description", ""), b.get("required_skills", ""),
                      b.get("preferred_skills", ""), b.get("keywords", ""), b.get("checklist", ""),
                      b.get("active", True))
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
