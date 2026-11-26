"""What the two people can do: the HR officer and the Interviewer.

The web pages call these functions. Every action checks the candidate's current
status first, so an action can only happen at the right moment.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from sqlalchemy import select

from . import checks, config, db, letters, outlook, tools, vacancies


class ActionError(Exception):
    """A message for the person, shown on the page."""


def _candidate(app_no: str) -> db.Candidate:
    c = db.get_candidate(app_no)
    if c is None:
        raise ActionError(f"There is no candidate {app_no}.")
    return c


def _need(c: db.Candidate, statuses: set[str], what: str) -> None:
    if c.status not in statuses:
        raise ActionError(f"{what} is not possible now. {c.app_no} is \"{config.STATUSES[c.status][0]}\".")


def _resolve_flags(c: db.Candidate, starts: tuple[str, ...]) -> list:
    flags = []
    for f in c.flags or []:
        f = dict(f)
        if f.get("open") and f.get("text", "").startswith(starts):
            f["open"] = False
            f["resolved_at"] = db.now()
        flags.append(f)
    return flags


# ---- Adding CVs (HR officer) ----------------------------------------------------------
def add_cv(filename: str, data: bytes, typed_email: str = "", source: str = "") -> dict:
    name = Path(filename or "cv").name[:200]
    ext = Path(name).suffix.lower()
    if ext not in config.ALLOWED_CV_TYPES:
        return {"ok": False, "file": name, "reason": "Only PDF, Word (.docx) and text (.txt) files can be added."}
    if not data:
        return {"ok": False, "file": name, "reason": "The file is empty."}
    if len(data) > config.MAX_UPLOAD_BYTES:
        return {"ok": False, "file": name, "reason": "The file is larger than 5 MB."}
    typed_email = (typed_email or "").strip()
    if typed_email and not checks.EMAIL_RE.fullmatch(typed_email):
        return {"ok": False, "file": name, "reason": f'"{typed_email}" is not a valid email address.'}
    h = tools.file_hash(data)
    with db.Session() as s:
        same = s.scalar(select(db.Candidate).where(db.Candidate.cv_hash == h))
    if same is not None:
        return {"ok": False, "file": name, "reason": f"This CV was already added as {same.app_no}."}
    app_no = db.next_app_no()
    stored = f"{app_no}{ext}"
    path = db.paths.inbox / stored
    path.write_bytes(data)
    try:
        text = tools.read_cv(path)
    except Exception:
        text = ""
    with db.Session() as s:
        c = db.Candidate(app_no=app_no, cv_filename=name, cv_stored=stored, cv_hash=h, cv_text=text,
                         email=typed_email, email_source="Typed by HR officer" if typed_email else "",
                         source=(source or "").strip()[:200],
                         status="new", created_at=db.now(), updated_at=db.now(),
                         profile={}, area_reasons=[], area_evidence=[], score_parts={}, gaps=[], questions=[],
                         checklist=[], flags=[])
        s.add(c)
        s.commit()
        cid = c.id
    db.log_event(cid, "HR officer", f"Added the CV file {name}.")
    return {"ok": True, "file": name, "app_no": app_no}


def add_samples() -> list[dict]:
    out = []
    for p in sorted(config.SAMPLES_DIR.glob("*.*")):
        if p.suffix.lower() in config.ALLOWED_CV_TYPES and p.is_file():
            out.append(add_cv(p.name, p.read_bytes()))
    return out


def cv_file(app_no: str) -> tuple[Path, str]:
    c = _candidate(app_no)
    path = (db.paths.inbox / c.cv_stored).resolve()
    if db.paths.inbox.resolve() not in path.parents or not path.exists():
        raise ActionError("The CV file is missing.")
    return path, c.cv_filename


# ---- HR officer decisions ----------------------------------------------------------------
def set_email(app_no: str, email: str) -> None:
    c = _candidate(app_no)
    email = (email or "").strip()
    if not checks.EMAIL_RE.fullmatch(email):
        raise ActionError(f'"{email}" is not a valid email address.')
    flags = _resolve_flags(c, ("No email address", "The email typed by the HR officer"))
    db.update_candidate(c.id, email=email, email_source="Typed by HR officer", flags=flags)
    db.log_event(c.id, "HR officer", f"Set the email to {email}.")


def choose_area(app_no: str, area_name: str) -> dict:
    c = _candidate(app_no)
    _need(c, {"needs_area", "borderline", "not_shortlisted_check"}, "Choosing the area")
    area = db.area_by_name(area_name)
    if area is None or not area.active:
        raise ActionError(f'"{area_name}" is not an active area.')
    if not area.required_skills:
        raise ActionError(f"{area.name} has no required skills yet. Add them on the Areas page first.")
    areas = db.active_areas()
    profile = c.profile or tools.rule_profile(c.cv_text, tools.skill_vocabulary(areas))
    score, parts, gaps = tools.score_cv(profile, c.cv_text, area, tools.shared_terms(areas))
    status = tools.shortlist_status(score)
    flags = _resolve_flags(c, ("AI suggested a new area", "The Recruiter could not choose", "The scoring rules suggest",
                               "None of the usual", "The area "))
    ok = db.update_candidate(c.id, expect_status=c.status, status=status, area=area.name,
                             area_source="Chosen by HR officer", area_reasons=["Chosen by the HR officer."],
                             area_evidence=[], suggested_new_area="", profile=profile, score=score,
                             score_parts=parts, gaps=gaps, flags=flags)
    if not ok:
        raise ActionError("The candidate changed while you were working. Reload the page.")
    db.log_event(c.id, "HR officer", f"Chose the area {area.name}. Scored {score} of 100 "
                                     f"({config.STATUSES[status][0]}).")
    return {"status": status, "score": score}


def decide(app_no: str, decision: str) -> str:
    c = _candidate(app_no)
    if c.status == "borderline":
        new = {"shortlist": "shortlisted", "not_shortlist": "not_shortlisted"}.get(decision)
        text = {"shortlist": "Shortlisted the borderline candidate.",
                "not_shortlist": "Decided Not shortlisted for the borderline candidate."}.get(decision)
    elif c.status == "not_shortlisted_check":
        new = {"confirm": "not_shortlisted", "not_shortlist": "not_shortlisted", "shortlist": "shortlisted"}.get(decision)
        text = {"confirm": "Confirmed Not shortlisted.", "not_shortlist": "Confirmed Not shortlisted.",
                "shortlist": "Shortlisted the candidate instead (overruled the score)."}.get(decision)
    else:
        raise ActionError(f"No decision is needed now. {c.app_no} is \"{config.STATUSES[c.status][0]}\".")
    if new is None:
        raise ActionError("Unknown decision.")
    if not db.update_candidate(c.id, expect_status=c.status, status=new):
        raise ActionError("The candidate changed while you were working. Reload the page.")
    db.log_event(c.id, "HR officer", text)
    return new


def retry(app_no: str) -> None:
    c = _candidate(app_no)
    _need(c, {"needs_person"}, "Trying again")
    try:
        text = tools.read_cv(db.paths.inbox / c.cv_stored)
    except Exception:
        text = c.cv_text
    flags = _resolve_flags(c, ("Could not read",))
    db.update_candidate(c.id, expect_status="needs_person", status="new", cv_text=text, flags=flags)
    db.log_event(c.id, "HR officer", "Sent the CV back to the Recruiter to try again.")


def close(app_no: str) -> None:
    c = _candidate(app_no)
    _need(c, {"needs_person", "needs_area"}, "Closing")
    db.update_candidate(c.id, expect_status=c.status, status="closed", outcome="Closed by HR officer")
    db.log_event(c.id, "HR officer", "Closed the application without a reply.")
    vacancies.on_result(c.id)


def resolve_flag(app_no: str, index: int) -> None:
    c = _candidate(app_no)
    flags = [dict(f) for f in (c.flags or [])]
    if not 0 <= index < len(flags):
        raise ActionError("Unknown note.")
    flags[index]["open"] = False
    flags[index]["resolved_at"] = db.now()
    db.update_candidate(c.id, flags=flags)
    db.log_event(c.id, "HR officer", f"Marked as checked: {flags[index]['text'][:120]}")


# ---- The CV pool (HR officer) --------------------------------------------------------------
def _profile(c: db.Candidate) -> dict:
    return c.profile or tools.rule_profile(c.cv_text, tools.skill_vocabulary(db.active_areas()))


def invite_from_pool(app_no: str, area_name: str) -> dict:
    """The HR officer picks a CV from the pool for one area. It moves into the hiring flow as
    Shortlisted, and the Coordinator books the interview and drafts the invitation."""
    c = _candidate(app_no)
    _need(c, config.POOL_STATUSES, "Inviting to interview")
    area = db.area_by_name(area_name)
    if area is None or not area.active:
        raise ActionError(f'"{area_name}" is not an active area.')
    profile = _profile(c)
    score, parts, gaps = tools.score_cv(profile, c.cv_text, area, tools.shared_terms(db.active_areas()))
    if score is None:
        raise ActionError(f"{area.name} has no required skills yet. Add them on the Areas page first.")
    same = area.name == c.area
    fields = dict(status="shortlisted", area=area.name, profile=profile, score=score, score_parts=parts, gaps=gaps)
    if not same:
        fields.update(area_source="Chosen by HR officer", area_reasons=["Chosen by the HR officer from the pool."],
                      area_evidence=[])
    if not db.update_candidate(c.id, expect_status=c.status, **fields):
        raise ActionError("The candidate changed while you were working. Reload the page.")
    db.log_event(c.id, "HR officer", f"Invited to interview from the pool for {area.name} (score {score} of 100).")
    return {"status": "shortlisted", "area": area.name, "score": score}


def archive(app_no: str) -> None:
    c = _candidate(app_no)
    _need(c, config.POOL_STATUSES | {"needs_person"}, "Archiving")
    if not db.update_candidate(c.id, expect_status=c.status, status="archived", outcome="Archived by HR officer"):
        raise ActionError("The candidate changed while you were working. Reload the page.")
    db.log_event(c.id, "HR officer", "Archived the CV (removed from the pool).")


def restore(app_no: str) -> str:
    """Puts an archived CV back in the pool. It is read again with the current areas."""
    c = _candidate(app_no)
    _need(c, {"archived"}, "Restoring")
    if not db.update_candidate(c.id, expect_status="archived", status="new", outcome=""):
        raise ActionError("The candidate changed while you were working. Reload the page.")
    db.log_event(c.id, "HR officer", "Restored the CV to the pool. The Recruiter reads it again.")
    return "new"


def rescore_pool() -> dict:
    """After an area is added, changed, turned off or on: score every CV in the pool again.
    CVs with "No matching area" go back to the Recruiter, which reads them with the new list."""
    areas = db.active_areas()
    rescored = requeued = 0
    for c in db.candidates_in({"in_pool"}):
        profile = _profile(c)
        scores = tools.score_all_areas(profile, c.cv_text, areas)
        area, source = c.area, c.area_source
        if area not in scores:                      # its best area was turned off or lost its skills
            best = tools.top_areas(scores, n=1)
            area, source = (best[0][0], "Chosen by rules") if best else ("", "")
        fields = dict(area_scores=scores, area=area, area_source=source)
        if area:
            fields.update(score=scores[area]["score"], score_parts=scores[area]["parts"], gaps=scores[area]["gaps"])
        else:
            fields.update(status="no_area", score=None, score_parts={}, gaps=[])
        if db.update_candidate(c.id, expect_status="in_pool", **fields):
            rescored += 1
    for c in db.candidates_in({"no_area"}):
        if db.update_candidate(c.id, expect_status="no_area", status="new"):
            db.log_event(c.id, "HR officer", "The areas changed, so the CV was sent back to the Recruiter.")
            requeued += 1
    return {"rescored": rescored, "requeued": requeued}


SCORING_VERSION = "2"


def upgrade_scoring() -> dict:
    """Runs once on a register made before areas had their own related studies.
    The starter areas get their studies and tidied keywords from shared/areas.json, and every
    CV waiting in the pool is scored again, so the vacancy suggestions clean up by themselves."""
    if db.get_setting("scoring_version") == SCORING_VERSION:
        return {"rescored": 0}
    db.update_starter_areas()
    areas = db.active_areas()
    rescored = 0
    for c in db.candidates_in(config.POOL_STATUSES):
        scores = tools.score_all_areas(_profile(c), c.cv_text, areas)
        fields = dict(area_scores=scores)
        if c.area in scores:
            fields.update(score=scores[c.area]["score"], score_parts=scores[c.area]["parts"], gaps=scores[c.area]["gaps"])
        if db.update_candidate(c.id, expect_status=c.status, **fields):
            rescored += 1
    db.set_setting("scoring_version", SCORING_VERSION)
    if rescored:
        db.log_event(None, "Recruiter", f"Scored {rescored} CV(s) in the pool again with the related studies of each area.")
    vacancies.suggest_for_open()
    return {"rescored": rescored}


# ---- Vacancies (HR officer) ----------------------------------------------------------
def approve_suggestion(vacancy_id: int, app_no: str) -> dict:
    """The HR officer approves a suggested CV for a vacancy. The candidate leaves the pool
    and joins the normal hiring flow, and the Coordinator books the interview."""
    v = db.get_vacancy(vacancy_id)
    if v is None:
        raise ActionError("There is no such vacancy.")
    if v.status != "open":
        raise ActionError(f"This vacancy is {v.status}.")
    if not any(s["app_no"] == app_no and s["state"] == "suggested" for s in (v.suggestions or [])):
        raise ActionError(f"{app_no} is not waiting for your approval on this vacancy.")
    out = invite_from_pool(app_no, v.area)
    c = _candidate(app_no)
    db.update_candidate(c.id, vacancy_id=v.id)
    rows = [dict(s) for s in v.suggestions]
    for s in rows:
        if s["app_no"] == app_no:
            s["state"], s["at"] = "approved", db.now()
    db.update_vacancy(v.id, suggestions=rows)
    vacancies.mark_taken_elsewhere(app_no, v.id)
    db.log_event(c.id, "HR officer", f"Approved for the {v.area} vacancy.")
    return out


def decline_suggestion(vacancy_id: int, app_no: str) -> None:
    """The HR officer passes over a suggestion. The next best CV is suggested instead."""
    v = db.get_vacancy(vacancy_id)
    if v is None or v.status != "open":
        raise ActionError("There is no open vacancy with that number.")
    rows = [dict(s) for s in (v.suggestions or [])]
    if not any(s["app_no"] == app_no and s["state"] == "suggested" for s in rows):
        raise ActionError(f"{app_no} is not waiting for your approval on this vacancy.")
    for s in rows:
        if s["app_no"] == app_no:
            s["state"], s["at"] = "failed", db.now()
    db.update_vacancy(v.id, suggestions=rows)
    vacancies.refresh_suggestions(db.get_vacancy(v.id))


def add_vacancy(area_name: str, places: int, reason: str, opens_on: str = "") -> int:
    """The HR officer opens a vacancy by hand, for example when an intern leaves early."""
    area = db.area_by_name(area_name)
    if area is None or not area.active:
        raise ActionError(f'"{area_name}" is not an active area.')
    try:
        places = int(places)
    except (TypeError, ValueError):
        raise ActionError("How many places? Type a number.") from None
    if not 1 <= places <= 50:
        raise ActionError("The number of places must be between 1 and 50.")
    opens_on = (opens_on or "").strip()[:10] or date.today().isoformat()
    with db.Session() as s:
        v = db.Vacancy(area=area.name, places=places, opens_on=opens_on,
                       reason=(reason or "").strip()[:500] or "Opened by the HR officer.",
                       trainee_ids=[], suggestions=[], status="open", created_by="HR officer",
                       created_at=db.now(), updated_at=db.now())
        s.add(v)
        s.commit()
        vacancy_id = v.id
    vacancies.refresh_suggestions(db.get_vacancy(vacancy_id))
    return vacancy_id


def close_vacancy(vacancy_id: int) -> None:
    v = db.get_vacancy(vacancy_id)
    if v is None:
        raise ActionError("There is no such vacancy.")
    if v.status != "open":
        raise ActionError(f"This vacancy is already {v.status}.")
    db.update_vacancy(v.id, status="closed", reason=v.reason + " - closed by the HR officer.")


# ---- Interviewer -------------------------------------------------------------------------
def enter_result(app_no: str, result: str, comment: str = "") -> str:
    c = _candidate(app_no)
    result = (result or "").capitalize()
    if c.status == "awaiting_result":
        allowed = {"Pass", "Fail", "Hold"}
    elif c.status == "on_hold":
        allowed = {"Pass", "Fail"}
    else:
        raise ActionError(f"A result cannot be entered now. {c.app_no} is \"{config.STATUSES[c.status][0]}\".")
    if result not in allowed:
        raise ActionError("Choose " + ", ".join(sorted(allowed)) + ".")
    comment = (comment or "").strip()[:2000]
    new = {"Pass": "passed", "Fail": "failed", "Hold": "hold"}[result]
    if not db.update_candidate(c.id, expect_status=c.status, status=new, result=result, result_comment=comment,
                               result_at=db.now()):
        raise ActionError("The candidate changed while you were working. Reload the page.")
    db.log_event(c.id, "Interviewer", f"Entered the result: {result}." + (f" Comment: {comment}" if comment else ""))
    vacancies.on_result(c.id)
    return new


# ---- Replies (HR officer releases every email) -----------------------------------------
KIND_LABELS = {"invitation": "interview invitation", "not_shortlisted": "Not shortlisted reply",
               "rejection": "rejection reply", "hold": "hold note", "acceptance": "acceptance email"}
RELEASE_MOVES = {
    "invitation": ("invite_ready", "awaiting_result", ""),
    "not_shortlisted": ("reply_ready", "closed", "Not shortlisted"),
    "rejection": ("reply_ready", "closed", "Not selected after interview"),
    "hold": ("hold_note_ready", "on_hold", ""),
    "acceptance": ("offer_ready", "closed", "Selected"),
}


def _draft_facts(c: db.Candidate) -> tuple[dict, list[str], list[str]]:
    facts = {"name": c.name or "Candidate", "area": c.area or "the role", "app_no": c.app_no,
             "interview": c.interview_label or None, "work_id": str(c.work_id) if c.work_id else None}
    facts = {k: v for k, v in facts.items() if v}
    return facts, [], []


def edit_draft(draft_id: int, subject: str, body: str) -> list[str]:
    with db.Session() as s:
        d = s.get(db.Draft, draft_id)
        if d is None or d.status != "waiting":
            raise ActionError("This email can no longer be edited.")
        c = s.get(db.Candidate, d.candidate_id)
        facts, _, _ = _draft_facts(c)
        must = {"invitation": [c.area, c.interview_label], "acceptance": [c.area, str(c.work_id or "")]}.get(d.kind, [])
        _, problems = checks.check_draft({"subject": subject, "body": body}, facts, [m for m in must if m])
        d.subject, d.body = subject.strip()[:200], body.strip()
        d.written_by = "Edited by HR officer"
        d.check_notes = problems
        s.commit()
        cid = c.id
    db.log_event(cid, "HR officer", "Edited the draft email.")
    return problems


def release_draft(draft_id: int) -> str:
    """Saves the email to the outbox (.eml with Placement Letter PDF when acceptance), dispatches
    via the configured Microsoft Outlook mode (if enabled), and moves the candidate on."""
    with db.Session() as s:
        d = s.get(db.Draft, draft_id)
        kind, cid = (d.kind, d.candidate_id) if d is not None else ("", 0)
    if kind == "acceptance":
        c = db.get_candidate(cid)
        if c is not None and c.status == RELEASE_MOVES["acceptance"][0]:
            letters.make(cid)                      # the latest details, made just before release
            c = db.get_candidate(cid)
        if c is not None and not letters.dates_ready(c):
            raise ActionError("Add the start and end dates for the placement letter on the candidate page before releasing.")
    with db.Session() as s:
        d = s.get(db.Draft, draft_id)
        if d is None or d.status != "waiting":
            raise ActionError("This email is not waiting for release.")
        c = s.get(db.Candidate, d.candidate_id)
        if not c.email or not checks.EMAIL_RE.fullmatch(c.email):
            raise ActionError("Add the candidate's email address on the candidate page before releasing.")
        before, after, outcome = RELEASE_MOVES[d.kind]
        if c.status != before:
            raise ActionError(f"{c.app_no} is \"{config.STATUSES[c.status][0]}\", so this email cannot be released now.")
        fname = re.sub(r"[^A-Za-z0-9_-]", "", f"{c.app_no}_{d.kind}_{d.id}") + ".eml"
        attach = (letters.file_name(c), letters.read(c)) if d.kind == "acceptance" else None
        eml_path = db.paths.outbox / fname
        eml_path.write_bytes(letters.email_file(c, d.subject, d.body, attach=(d.kind == "acceptance")))
        att_paths = [db.paths.letters / letters.file_name(c)] if attach is not None else None
        res = outlook.deliver_draft(
            c.email, c.name or "", d.subject, d.body,
            attachment=attach, eml_path=eml_path, attachment_paths=att_paths,
        )
        d.status, d.released_at, d.file = "released", db.now(), fname
        d.delivery_status = res["delivery_status"]
        d.delivery_error = res.get("error", "")
        d.sent_to = res.get("sent_to", "")
        d.sent_at = db.now() if res["ok"] and res["delivery_status"] in ("sent", "outlook_draft", "opened_app") else ""
        if res.get("message_id"):
            d.outlook_message_id = res["message_id"]
        if res.get("web_link"):
            d.outlook_web_link = res["web_link"]
        c.status = after
        if outcome:
            c.outcome = outcome + (f" (work ID {c.work_id})" if d.kind == "acceptance" and c.work_id else "")
        c.updated_at = db.now()
        s.commit()
        cid, label = c.id, KIND_LABELS[d.kind]
    extra = " with the placement letter attached" if label == KIND_LABELS["acceptance"] else ""
    delivery_status = res["delivery_status"]
    if delivery_status == "sent":
        note = f"Released the {label}{extra}. Sent via Outlook to {res['sent_to']} and saved to the outbox."
    elif delivery_status == "outlook_draft":
        note = f"Released the {label}{extra}. Saved as an Outlook Draft for {res['sent_to']} and saved to the outbox."
    elif delivery_status == "opened_app":
        note = f"Released the {label}{extra}. Opened in {res.get('opened_in') or 'Outlook / Mail'} ready to send and saved to the outbox."
    elif delivery_status == "failed":
        note = f"Released the {label}{extra} to the outbox (Outlook delivery failed: {res.get('error', '')[:160]})."
    else:
        note = f"Released the {label}{extra}. It was saved to the outbox (not sent)."
    db.log_event(cid, "HR officer", note)
    if after == "closed":
        vacancies.on_result(cid)
    return after


def export_draft_eml(draft_id: int) -> tuple[str, bytes]:
    """Build a ready-to-open RFC 5322 .eml file (X-Unsent: 1 + Placement Letter PDF if acceptance)."""
    with db.Session() as s:
        d = s.get(db.Draft, draft_id)
        if d is None:
            raise ActionError("Draft not found.")
        c = s.get(db.Candidate, d.candidate_id)
        if c is None:
            raise ActionError("Candidate not found.")
    if not c.email or not checks.EMAIL_RE.fullmatch(c.email):
        raise ActionError("Add the candidate's email address on the candidate page before downloading .eml.")
    fname = d.file or (re.sub(r"[^A-Za-z0-9_-]", "", f"{c.app_no}_{d.kind}_{d.id}") + ".eml")
    attach = d.kind == "acceptance" and bool(c.work_id)
    if attach and d.file == "":
        letters.read(c)
    raw = letters.email_file(c, d.subject, d.body, attach=attach)
    return fname, raw


def open_draft_in_app(draft_id: int) -> dict:
    """Save an .eml copy of a draft and open it in Outlook Desktop / Mail app."""
    with db.Session() as s:
        d = s.get(db.Draft, draft_id)
        if d is None:
            raise ActionError("Draft not found.")
        c = s.get(db.Candidate, d.candidate_id)
        if c is None:
            raise ActionError("Candidate not found.")
        if not c.email or not checks.EMAIL_RE.fullmatch(c.email):
            raise ActionError("Add the candidate's email address on the candidate page before opening in Outlook.")
        fname = d.file or (re.sub(r"[^A-Za-z0-9_-]", "", f"{c.app_no}_{d.kind}_{d.id}") + ".eml")
        attach = d.kind == "acceptance" and bool(c.work_id)
        att_paths = [db.paths.letters / letters.file_name(c)] if attach else []
        if attach:
            letters.read(c)
        eml_path = db.paths.outbox / fname
        if not eml_path.is_file():
            eml_path.write_bytes(letters.email_file(c, d.subject, d.body, attach=attach))
        subject, body, to_email, to_name = d.subject, d.body, c.email, c.name or ""
    try:
        opened_in = outlook.open_eml_in_desktop_app(
            eml_path,
            sender_email=outlook.raw_settings().get("sender_email", ""),
            to_email=to_email,
            to_name=to_name,
            subject=subject,
            text=body,
            attachments=att_paths,
        )
    except outlook.OutlookError as exc:
        raise ActionError(str(exc)) from exc
    return {"ok": True, "opened_in": opened_in, "file": fname}


def push_draft_to_outlook(draft_id: int) -> dict:
    """Create an Outlook Draft in the user's mailbox via Microsoft Graph (including PDF if acceptance)."""
    with db.Session() as s:
        d = s.get(db.Draft, draft_id)
        if d is None:
            raise ActionError("Draft not found.")
        c = s.get(db.Candidate, d.candidate_id)
        if c is None:
            raise ActionError("Candidate not found.")
        subject, body, kind, d_status = d.subject, d.body, d.kind, d.status
        cid = c.id
    if not c.email or not checks.EMAIL_RE.fullmatch(c.email):
        raise ActionError("Add the candidate's valid email address before saving a draft to Outlook.")
    attach = (letters.file_name(c), letters.read(c)) if (kind == "acceptance" and c.work_id) else None
    res = outlook.create_outlook_draft(c.email, c.name or "", subject, body, attachment=attach)
    with db.Session() as s:
        d = s.get(db.Draft, draft_id)
        if d is not None:
            if res["ok"]:
                d.outlook_message_id = res.get("message_id", "")
                d.outlook_web_link = res.get("web_link", "")
                d.sent_to = res.get("sent_to", "")
                d.delivery_error = ""
                if d.status == "released" and d.delivery_status != "sent":
                    d.delivery_status = "outlook_draft"
                    d.sent_at = db.now()
            elif d.status == "released":
                d.delivery_error = res.get("error", "")
            s.commit()
    if res["ok"]:
        db.log_event(cid, "HR officer", f"Saved {KIND_LABELS.get(kind, kind)} draft in Outlook for {res['sent_to']}.")
    elif d_status == "released":
        db.log_event(cid, "HR officer", f"Could not save draft in Outlook: {res.get('error', '')[:160]}")
    return res


def resend_draft(draft_id: int) -> dict:
    """Send or re-send a released email via active Microsoft Outlook Graph, Desktop app, or SMTP."""
    with db.Session() as s:
        d = s.get(db.Draft, draft_id)
        if d is None or d.status != "released":
            raise ActionError("Only released emails can be sent or retried.")
        c = s.get(db.Candidate, d.candidate_id)
        if c is None or not c.email or not checks.EMAIL_RE.fullmatch(c.email):
            raise ActionError("Add the candidate's valid email address before sending.")
        subject, body, kind, cid = d.subject, d.body, d.kind, c.id
        fname = d.file or (re.sub(r"[^A-Za-z0-9_-]", "", f"{c.app_no}_{d.kind}_{d.id}") + ".eml")
        attach_bool = kind == "acceptance" and bool(c.work_id)
        attach = (letters.file_name(c), letters.read(c)) if attach_bool else None
        att_paths = [db.paths.letters / letters.file_name(c)] if attach_bool else None
        eml_path = db.paths.outbox / fname
        if not eml_path.is_file():
            eml_path.write_bytes(letters.email_file(c, subject, body, attach=attach_bool))
    cfg = outlook.raw_settings()
    if outlook.effective_mode(cfg) == "eml":
        raise ActionError("Outlook mode is set to '.eml file only'. Choose Automatic, Open in Outlook App, Graph Send, Graph Drafts, or Office 365 SMTP in Outlook settings first.")
    res = outlook.deliver_draft(
        c.email, c.name or "", subject, body,
        attachment=attach, eml_path=eml_path, attachment_paths=att_paths, cfg=cfg,
    )
    with db.Session() as s:
        d = s.get(db.Draft, draft_id)
        if d is not None:
            d.delivery_status = res["delivery_status"]
            d.delivery_error = res.get("error", "")
            d.sent_to = res.get("sent_to", "")
            if res["ok"]:
                d.sent_at = db.now()
                if res.get("message_id"):
                    d.outlook_message_id = res["message_id"]
                if res.get("web_link"):
                    d.outlook_web_link = res["web_link"]
            s.commit()
    if res["ok"]:
        db.log_event(cid, "HR officer", f"Delivered {KIND_LABELS.get(kind, kind)} via Outlook ({res['delivery_status']}) to {res['sent_to']}.")
    else:
        db.log_event(cid, "HR officer", f"Outlook delivery failed: {res.get('error', '')[:160]}")
    return res


# ---- Areas (HR officer) --------------------------------------------------------------------
def _list(value, sep: str = r"[,\n]") -> list[str]:
    if isinstance(value, str):
        value = re.split(sep, value)
    out = []
    for v in value or []:
        v = str(v).strip()[:120]
        if v and v not in out:
            out.append(v)
    return out[:30]


LETTER_EDIT_STATUSES = ("passed", "offer_ready")


def save_letter_dates(app_no: str, start_date: str, end_date: str) -> dict:
    """The HR officer changes the start and end dates; the placement letter is made again.
    An empty date goes back to the automatic one (the Monday after the interview, six months)."""
    c = db.get_candidate(app_no)
    if c is None:
        raise ActionError("Candidate not found.")
    if not c.work_id or c.status not in LETTER_EDIT_STATUSES:
        raise ActionError("The placement letter can only be changed before the acceptance email is released.")
    try:
        start, end = letters.check_dates(start_date, end_date)
    except letters.LetterError as e:
        raise ActionError(str(e)) from e
    letter = dict(c.letter or {})
    letter.update(start_date=start, end_date=end)
    if not db.update_candidate(c.id, expect_status=set(LETTER_EDIT_STATUSES), letter=letter):
        raise ActionError("This candidate changed in the meantime. Reload the page.")
    letters.make(c.id)
    db.log_event(c.id, "HR officer", "Saved the placement letter dates: "
                 f"{letters.long_date(start)} to {letters.long_date(end)}. The letter was made again.")
    return db.get_candidate(c.id).letter


def save_area(area_id: int | None, name: str, description: str, required, preferred, keywords, checklist,
              active: bool = True, studies=None) -> int:
    name = checks.clean_area_name(name)
    if not name:
        raise ActionError("Give the area a name.")
    if name.lower() == config.NEEDS_REVIEW_AREA.lower():
        raise ActionError("That name is reserved.")
    required = _list(required)
    if not required:
        raise ActionError("Add at least one required skill, so CVs can be scored for this area.")
    with db.Session() as s:
        clash = s.scalar(select(db.Area).where(db.Area.name == name))
        if clash is not None and clash.id != area_id:
            raise ActionError(f"An area called {name} already exists.")
        if area_id is None:
            a = db.Area(name=name, added_by="HR officer")
            s.add(a)
        else:
            a = s.get(db.Area, area_id)
            if a is None:
                raise ActionError("Unknown area.")
        a.name, a.description = name, (description or "").strip()[:300]
        a.required_skills, a.preferred_skills = required, _list(preferred)
        a.keywords, a.checklist = _list(keywords), _list(checklist, r"\n")
        if studies is not None:
            a.studies = _list(studies)
        a.active = bool(active)
        s.commit()
        new_id = a.id
    db.log_event(None, "HR officer", f"{'Added' if area_id is None else 'Updated'} the area {name}.")
    return new_id
