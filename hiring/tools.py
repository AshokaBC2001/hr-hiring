"""Plain-code tools the AI employees call. No model is used in this file.

- read_cv: get text out of PDF, Word and text files
- rule_profile: a simple profile made by rules (used when the model fails)
- score_cv / skill gaps / best_area_by_rules: the weighted score
- suggest_areas: which areas a CV is suggested for (its best area, and a close second)
- pick_interview_slot: next free slot on a working day
- induction_checklist and the reply templates
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config
from .checks import EMAIL_RE, CVText, norm

# ---- Reading CV files -----------------------------------------------------------
_PDF_LIGATURES = str.maketrans({
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl",
    "\ufb03": "ffi", "\ufb04": "ffl", "\u200b": "", "\ufeff": "",
})


def read_cv(path: Path) -> str:
    suffix = path.suffix.lower()
    text = ""
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    elif suffix == ".docx":
        import docx
        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        text = "\n".join(parts)
    elif suffix == ".txt":
        text = path.read_bytes().decode("utf-8", errors="replace")
    text = text.translate(_PDF_LIGATURES)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = re.sub(r"[  ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- A profile made by rules ---------------------------------------------------------
HEADINGS = {
    "education": "education", "academic qualifications": "education", "qualifications": "education",
    "skills": "skills", "technical skills": "skills", "key skills": "skills", "core skills": "skills",
    "projects": "projects", "academic projects": "projects", "personal projects": "projects",
    "experience": "experience", "work experience": "experience", "internships": "experience",
    "employment": "experience", "professional experience": "experience",
    "summary": "other", "profile": "other", "about me": "other", "certifications": "other",
    "languages": "other", "references": "other", "achievements": "other", "contact": "other",
    "interests": "other", "hobbies": "other", "activities": "other", "extracurricular activities": "other",
}
GENERAL_SKILLS = ["Python", "Java", "JavaScript", "TypeScript", "C++", "C#", "SQL", "Git", "Linux", "HTML",
                  "CSS", "React", "Node.js", "Excel", "Power BI", "Tableau", "Figma", "Communication",
                  "Recruitment", "Payroll", "Accounting", "Marketing", "Sales", "Networking"]


def split_sections(cv_text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"top": []}
    current = "top"
    for line in cv_text.split("\n"):
        key = norm(line.rstrip(":")).strip()
        if key in HEADINGS and len(line) < 40:
            current = HEADINGS[key]
            sections.setdefault(current, [])
            continue
        if line.strip():
            sections.setdefault(current, []).append(line.strip())
    return sections


def _items(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        item = re.sub(r"^[\-•*●▪>]+\s*", "", line).strip()
        if item and item not in out:
            out.append(item)
    return out


def skill_vocabulary(areas: list) -> list[str]:
    vocab = list(GENERAL_SKILLS)
    for a in areas:
        for s in list(a.required_skills or []) + list(a.preferred_skills or []):
            if s not in vocab:
                vocab.append(s)
    return vocab


def rule_profile(cv_text: str, vocab: list[str]) -> dict:
    sec = split_sections(cv_text)
    cv = CVText(cv_text)
    top = sec.get("top", [])
    name = ""
    for line in top[:5]:
        words = line.split()
        if 2 <= len(words) <= 5 and not EMAIL_RE.search(line) and not re.search(r"\d", line):
            name = line.strip()
            break
    m = EMAIL_RE.search(cv_text)
    phone = ""
    pm = re.search(r"(\+?\d[\d\s\-]{8,}\d)", cv_text)
    if pm:
        phone = pm.group(1).strip()
    skills = [s for s in vocab if cv.has(s)]
    for line in sec.get("skills", []):
        for part in re.split(r"[,;|/]", re.sub(r"^[^:]{0,30}:", "", line)):
            part = part.strip(" -•*.")
            if 1 < len(part) <= 40 and part not in skills:
                skills.append(part)
    return {
        "name": name, "email": m.group(0) if m else "", "phone": phone,
        "education": _items(sec.get("education", []))[:8],
        "skills": skills[:25],
        "projects": _items(sec.get("projects", []))[:10],
        "experience": _items(sec.get("experience", []))[:10],
    }


PRIMARY_UNIVERSITY_WORDS = ("university", "institute", "campus", "college", "academy", "polytechnic", "school")
FALLBACK_UNIVERSITY_WORDS = ("faculty of",)
UNIVERSITY_WORDS = PRIMARY_UNIVERSITY_WORDS + FALLBACK_UNIVERSITY_WORDS
UNIVERSITY_SHORT = ("SLIIT", "IIT", "KDU", "NSBM", "NIBM", "SLTC", "APIIT", "ICBT", "ESOFT", "UCSC", "CINEC",
                    "BCAS", "SLIATE", "IIT Madras", "IIT Bombay", "UOM", "UOC", "UOK", "UOJ", "UOP", "UOR")


def _extract_institution(lines: list[str]) -> str:
    parts: list[str] = []
    for line in lines:
        for raw_part in re.split(r"[,|;\n]| - | – | — |\(|\)|\s+at\s+|\s+from\s+", line):
            part = re.sub(r"\b(19|20)\d{2}\b.*$", "", raw_part).strip(" .:-–—")
            if part:
                parts.append(part)
    # Prefer actual institution nouns ("University of Moratuwa") over faculty fragments ("Faculty of IT").
    for part in parts:
        low = part.lower()
        if any(w in low for w in PRIMARY_UNIVERSITY_WORDS) and len(part) <= 120:
            return re.sub(r"^(at|from)\s+", "", part, flags=re.I)
    for part in parts:
        if part.upper() in UNIVERSITY_SHORT:
            return part
    for part in parts:
        low = part.lower()
        if any(w in low for w in FALLBACK_UNIVERSITY_WORDS) and len(part) <= 120:
            return re.sub(r"^(at|from)\s+", "", part, flags=re.I)
    for line in lines:                      # a short name such as SLIIT inside a longer line
        for short in UNIVERSITY_SHORT:
            if re.search(rf"\b{re.escape(short)}\b", line):
                return short
    return ""


def find_university(education: list[str], cv_text: str = "") -> str:
    """The university or institute named in the education lines (or CV fallback text).
    For example "B.Tech in Data Science, Indian Institute of Technology Madras (2021 - 2025)"
    gives "Indian Institute of Technology Madras". Returns "" when none is named."""
    found = _extract_institution(list(education or []))
    if found or not cv_text:
        return found
    lines = [ln.strip() for ln in cv_text[:3000].splitlines() if ln.strip()]
    return _extract_institution(lines[:40])


# ---- Scoring ---------------------------------------------------------------------------
def _area_terms(area) -> list[str]:
    return list(area.required_skills or []) + list(area.preferred_skills or []) + list(area.keywords or [])


def shared_terms(areas: list) -> set[str]:
    """Skills and keywords listed by many areas, for example Excel, SQL and Communication.
    Many jobs ask for them, so on their own they do not show that a project fits one area."""
    count: Counter = Counter()
    for a in areas:
        for term in {norm(x) for x in _area_terms(a)}:
            count[term] += 1
    return {term for term, n in count.items() if n > config.SHARED_SKILL_AREAS}


def _specific_terms(area, shared: set[str] | None) -> list[str]:
    terms = _area_terms(area)
    if not shared:
        return terms
    return [x for x in terms if norm(x) not in shared] or terms


def _related_studies(area, education: str) -> bool:
    """True when the CV's studies fit this area. Each area has its own list of related
    studies; an area without a list falls back to its own name and required skills."""
    edu = " " + norm(education) + " "
    studies = list(getattr(area, "studies", None) or []) or [area.name] + list(area.required_skills or [])
    return any(norm(k) and f" {norm(k)} " in edu for k in studies)


def area_score(
    profile: dict,
    cv_text: str,
    area,
    shared: set[str] | None = None,
    *,
    cv: CVText | None = None,
    proj_cvs: list[CVText] | None = None,
    exp_cvs: list[CVText] | None = None,
) -> dict | None:
    """Weighted score out of 100 against one area, with the parts, the skill gaps and the
    evidence. Returns None when the area has no required skills yet.

    Evidence means the CV shows real work or study in this area: a related project, a related
    job or related studies. A skills list alone is not evidence, because it is easy to write."""
    w = config.WEIGHTS
    required = list(area.required_skills or [])
    if not required:
        return None
    cv = cv or CVText(cv_text)
    found = [s for s in required if cv.has(s)]
    gaps = [s for s in required if s not in found]
    preferred_found = [s for s in (area.preferred_skills or []) if cv.has(s)]
    skills_pts = w["skills"] * len(found) / len(required)

    terms = _specific_terms(area, shared)
    projects = profile.get("projects") or []
    p_cvs = proj_cvs if proj_cvs is not None else [CVText(p) for p in projects]
    relevant_projects = [p_cv.raw for p_cv in p_cvs if any(p_cv.has(x) for x in terms)]
    projects_pts = w["projects"] * min(len(relevant_projects), 2) / 2

    education = " ".join(profile.get("education") or [])
    related_studies = bool(education.strip()) and _related_studies(area, education)
    if related_studies:
        edu_pts, edu_why = w["education"], "Studies a related subject"
    elif education.strip():
        edu_pts, edu_why = w["education"] / 2, "Studies, but not in a related subject"
    else:
        edu_pts, edu_why = 0, "No education found in the CV"

    experience = profile.get("experience") or []
    e_cvs = exp_cvs if exp_cvs is not None else [CVText(e) for e in experience]
    relevant_exp = [e_cv.raw for e_cv in e_cvs if any(e_cv.has(x) for x in terms)]
    if relevant_exp:
        exp_pts, exp_why = w["experience"], "Has related work experience"
    elif experience:
        exp_pts, exp_why = w["experience"] / 2, "Has some experience, not related to this area"
    else:
        exp_pts, exp_why = 0, "No work experience listed"

    parts = {
        "skills": {"points": round(skills_pts), "max": w["skills"],
                   "why": f"Found {len(found)} of {len(required)} required skills"
                          + (f" and {len(preferred_found)} preferred" if preferred_found else "")},
        "projects": {"points": round(projects_pts), "max": w["projects"],
                     "why": f"{len(relevant_projects)} project(s) related to this area"},
        "education": {"points": round(edu_pts), "max": w["education"], "why": edu_why},
        "experience": {"points": round(exp_pts), "max": w["experience"], "why": exp_why},
    }
    evidence = []
    if relevant_projects:
        evidence.append(f"{len(relevant_projects)} related project(s)")
    if relevant_exp:
        evidence.append("related work experience")
    if related_studies:
        evidence.append("related studies")
    total = round(skills_pts + projects_pts + edu_pts + exp_pts)
    return {"score": total, "parts": parts, "gaps": gaps, "evidence": evidence}


def score_cv(
    profile: dict,
    cv_text: str,
    area,
    shared: set[str] | None = None,
    *,
    cv: CVText | None = None,
    proj_cvs: list[CVText] | None = None,
    exp_cvs: list[CVText] | None = None,
) -> tuple[int | None, dict, list[str]]:
    """Weighted score out of 100 against one area. Returns (score, parts, gaps).
    The score is None when the area has no required skills yet."""
    r = area_score(profile, cv_text, area, shared, cv=cv, proj_cvs=proj_cvs, exp_cvs=exp_cvs)
    return (None, {}, []) if r is None else (r["score"], r["parts"], r["gaps"])


def _prepared_cv(profile: dict, cv_text: str) -> tuple[CVText, list[CVText], list[CVText]]:
    return (
        CVText(cv_text),
        [CVText(p) for p in (profile.get("projects") or [])],
        [CVText(e) for e in (profile.get("experience") or [])],
    )


def score_all_areas(profile: dict, cv_text: str, areas: list) -> dict:
    """Scores the CV against every area (plain code). Returns {area name: {score, parts, gaps, evidence}}.
    Areas without required skills are left out, because they cannot be scored."""
    shared = shared_terms(areas)
    cv, proj_cvs, exp_cvs = _prepared_cv(profile, cv_text)
    out = {}
    for a in areas:
        r = area_score(profile, cv_text, a, shared, cv=cv, proj_cvs=proj_cvs, exp_cvs=exp_cvs)
        if r is not None:
            out[a.name] = r
    return out


def top_areas(area_scores: dict, skip: tuple = (), n: int | None = None) -> list[tuple[str, int]]:
    """The areas with the highest scores, best first, leaving out the names in skip."""
    rows = sorted(((k, v["score"]) for k, v in (area_scores or {}).items() if k not in skip),
                  key=lambda x: (-x[1], x[0]))
    return rows[:n] if n else rows


def good_areas(area_scores: dict, skip: tuple = (), n: int | None = None) -> list[tuple[str, int]]:
    """Areas where the CV scores at least POOL_MIN_SCORE and shows real evidence, best first."""
    rows = [(k, sc) for k, sc in top_areas(area_scores, skip)
            if sc >= config.POOL_MIN_SCORE and (area_scores[k].get("evidence"))]
    return rows[:n] if n else rows


def suggest_areas(area_scores: dict) -> list[str]:
    """The areas a CV is suggested for when a vacancy opens.

    Its best area, and also its second best area only when that one is nearly as strong
    (within POOL_CLOSE_POINTS). Both must score at least POOL_MIN_SCORE and show real
    evidence. So a CV is never spread over every area where its skills list happens to fit."""
    good = good_areas(area_scores)
    if not good:
        return []
    best = good[0][1]
    return [k for k, sc in good if sc >= best - config.POOL_CLOSE_POINTS][:config.POOL_SUGGEST_AREAS]


def shortlist_status(score: int) -> str:
    if score >= config.SHORTLIST_AT:
        return "shortlisted"
    if score >= config.BORDERLINE_AT:
        return "borderline"
    return "not_shortlisted_check"


def best_area_by_rules(profile: dict, cv_text: str, areas: list) -> tuple[str, int, float]:
    """The area with the highest score by rules. Returns (name, score, share of required skills found)."""
    best = ("", -1, 0.0)
    cv, proj_cvs, exp_cvs = _prepared_cv(profile, cv_text)
    shared = shared_terms(areas)
    for a in areas:
        total, _, _ = score_cv(profile, cv_text, a, shared, cv=cv, proj_cvs=proj_cvs, exp_cvs=exp_cvs)
        if total is None:
            continue
        req = a.required_skills or []
        share = sum(1 for s in req if cv.has(s)) / len(req)
        if total > best[1]:
            best = (a.name, total, share)
    return best


# ---- Interview slots -------------------------------------------------------------------
def pick_interview_slot(taken: set[str], today: date | None = None) -> tuple[str, str]:
    """Next free slot on a working day, starting tomorrow. Returns (iso, label)."""
    day = (today or date.today()) + timedelta(days=1)
    for _ in range(60):
        if day.weekday() < 5:
            for t in config.INTERVIEW_TIMES:
                iso = f"{day.isoformat()}T{t}"
                if iso not in taken:
                    dt = datetime.fromisoformat(iso)
                    label = f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B %Y')} at {t}"
                    return iso, label
        day += timedelta(days=1)
    raise RuntimeError("No free interview slots in the next 60 days.")


# ---- Onboarding --------------------------------------------------------------------------
def induction_checklist(area) -> list[dict]:
    name = area.name if area else "the team"
    items = [
        "Send the offer letter and get it signed",
        "Collect the documents needed before the start date",
        "Create the new team member's accounts and email",
        f"Assign a mentor in {name}",
        f"Add the new team member to the {name} team channel",
        "Book a place in the next onboarding session",
    ]
    for extra in (area.checklist if area else []) or []:
        if extra not in items:
            items.append(extra)
    return [{"item": i, "done": False} for i in items]


# ---- Reply templates (used when the model's draft fails the checks) ----------------
SIGN_OFF = "Kind regards,\nHiring Team"


def template_draft(kind: str, facts: dict) -> dict:
    first = (facts.get("name") or "there").split()[0]
    area = facts.get("area") or "the role"
    app_no = facts.get("app_no", "")
    if kind == "invitation":
        return {"subject": f"Interview invitation - {area}",
                "body": (f"Dear {first},\n\nThank you for your application. We are pleased to "
                         f"invite you to an interview for the {area} area.\n\nDate and time: {facts.get('interview')}\n"
                         f"Application number: {app_no}\n\nPlease reply to this email to confirm that you can "
                         f"attend. If the time does not suit you, tell us and we will find another slot.\n\n{SIGN_OFF}")}
    if kind == "acceptance":
        return {"subject": f"Offer - {area}",
                "body": (f"Dear {first},\n\nCongratulations. We are happy to tell you that you have been selected "
                         f"for the {area} position.\n\nYour work ID is {facts.get('work_id')}. "
                         f"Please use it in all messages with us. Your placement letter is attached to this "
                         f"email.\n\nOur team will contact you soon with the onboarding details.\n\n{SIGN_OFF}")}
    if kind == "hold":
        return {"subject": "Update on your interview",
                "body": (f"Dear {first},\n\nThank you for attending the interview for the {area} position. "
                         f"Your result is on hold for now, and we will contact you as soon as a decision is made. "
                         f"You do not need to do anything at this stage.\n\n{SIGN_OFF}")}
    if kind == "rejection":
        return {"subject": "Your interview result",
                "body": (f"Dear {first},\n\nThank you for attending the interview for the {area} position and for "
                         f"your interest in joining us. After careful thought, we are not able to offer you a place this "
                         f"time. We wish you every success in your future career.\n\n{SIGN_OFF}")}
    # not shortlisted
    return {"subject": "Your application",
            "body": (f"Dear {first},\n\nThank you for applying to join us. We have reviewed your "
                     f"application carefully, and we are not able to invite you to an interview this time. We "
                     f"encourage you to keep building your skills and to apply again in the future.\n\n{SIGN_OFF}")}


def template_questions(area_name: str, gaps: list[str], profile: dict) -> list[str]:
    qs = [f"What made you interested in working in {area_name}?"]
    projects = profile.get("projects") or []
    if projects:
        qs.append(f"Tell us about your project \"{projects[0][:80]}\". What was your own part in it?")
    else:
        qs.append("Tell us about a project you are proud of. What was your own part in it?")
    for g in gaps[:2]:
        qs.append(f"How much do you know about {g}, and how would you learn more about it?")
    qs.append("Describe a problem you could not solve at first. What did you do?")
    qs.append("How do you plan your work when you have several deadlines?")
    return qs[:8]
