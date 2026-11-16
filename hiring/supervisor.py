"""Supervisor evaluation feedback, hosting diagnostics, and pipeline telemetry reports.

Provides hosted readiness checks, AI agent performance metrics, MongoDB/SQLite
diagnostics, and structured feedback capture for supervisor review — without requiring
changes to the frontend UI.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from . import config, db, llm, mailer, talent_hub
from .actions import ActionError

VALID_CATEGORIES = (
    "general",
    "workflow",
    "agents",
    "scoring",
    "email",
    "mongodb",
    "hosting",
    "bug",
)


def _feedback_json(row: db.SupervisorFeedback) -> dict[str, Any]:
    return {
        "id": row.id,
        "reviewer": row.reviewer,
        "role": row.role,
        "category": row.category,
        "rating": row.rating,
        "target": row.target,
        "comment": row.comment,
        "status": row.status,
        "resolution_note": row.resolution_note,
        "created_at": row.created_at,
        "resolved_at": row.resolved_at,
    }


def submit_feedback(
    *,
    reviewer: str = "Supervisor",
    role: str = "supervisor",
    category: str = "general",
    comment: str = "",
    rating: int | None = None,
    target: str = "",
) -> dict[str, Any]:
    """Validate and persist a supervisor or team evaluation note."""
    clean_comment = (comment or "").strip()[:3000]
    if not clean_comment:
        raise ActionError("Write feedback comments before submitting.")
    clean_cat = (category or "general").strip().lower()
    if clean_cat not in VALID_CATEGORIES:
        raise ActionError(f"Unknown feedback category. Choose one of: {', '.join(VALID_CATEGORIES)}.")
    clean_rating: int | None = None
    if rating is not None and str(rating).strip() != "":
        try:
            clean_rating = int(rating)
        except (TypeError, ValueError) as exc:
            raise ActionError("Rating must be a whole number from 1 to 5.") from exc
        if not 1 <= clean_rating <= 5:
            raise ActionError("Rating must be between 1 and 5.")
    clean_reviewer = (reviewer or "Supervisor").strip()[:120] or "Supervisor"
    clean_role = (role or "supervisor").strip()[:40] or "supervisor"
    clean_target = (target or "").strip()[:80]
    row = db.SupervisorFeedback(
        reviewer=clean_reviewer,
        role=clean_role,
        category=clean_cat,
        rating=clean_rating,
        target=clean_target,
        comment=clean_comment,
        status="open",
        resolution_note="",
        created_at=db.now(),
        resolved_at="",
    )
    with db.Session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
    db.log_event(None, clean_reviewer, f"Submitted {clean_cat} feedback (#{row.id}).")
    return _feedback_json(row)


def list_feedback(category: str = "", status: str = "") -> dict[str, Any]:
    """List recorded supervisor feedback along with summary counts and ratings."""
    with db.Session() as s:
        q = select(db.SupervisorFeedback).order_by(db.SupervisorFeedback.id.desc())
        if category:
            q = q.where(db.SupervisorFeedback.category == category.strip().lower())
        if status:
            q = q.where(db.SupervisorFeedback.status == status.strip().lower())
        rows = [_feedback_json(r) for r in s.scalars(q)]
        all_rows = list(s.scalars(select(db.SupervisorFeedback)))
    rated = [r.rating for r in all_rows if r.rating is not None]
    by_cat: dict[str, int] = {}
    for r in all_rows:
        by_cat[r.category] = by_cat.get(r.category, 0) + 1
    return {
        "rows": rows,
        "categories": list(VALID_CATEGORIES),
        "summary": {
            "total": len(all_rows),
            "open": sum(1 for r in all_rows if r.status == "open"),
            "resolved": sum(1 for r in all_rows if r.status == "resolved"),
            "avg_rating": round(sum(rated) / len(rated), 2) if rated else None,
            "by_category": by_cat,
        },
    }


def resolve_feedback(feedback_id: int, resolution_note: str = "", status: str = "resolved") -> dict[str, Any]:
    """Mark a supervisor feedback entry as resolved or reopened."""
    clean_status = (status or "resolved").strip().lower()
    if clean_status not in ("open", "resolved"):
        raise ActionError("Status must be 'open' or 'resolved'.")
    with db.Session() as s:
        row = s.get(db.SupervisorFeedback, feedback_id)
        if row is None:
            raise ActionError(f"There is no feedback entry #{feedback_id}.")
        row.status = clean_status
        row.resolution_note = (resolution_note or "").strip()[:1000]
        row.resolved_at = db.now() if clean_status == "resolved" else ""
        s.commit()
        s.refresh(row)
        out = _feedback_json(row)
    db.log_event(None, "HR officer", f"Marked feedback #{feedback_id} as {clean_status}.")
    return out


def pipeline_analytics() -> dict[str, Any]:
    """Compute hiring funnel, AI agent accuracy/speed, SMTP delivery, and MongoDB cohort telemetry."""
    with db.Session() as s:
        counts = dict(s.execute(select(db.Candidate.status, func.count(db.Candidate.id)).group_by(db.Candidate.status)).all())
        total_candidates = sum(counts.values())
        scored = [x for x in s.scalars(select(db.Candidate.score).where(db.Candidate.score.is_not(None)))]
        selected_count = s.scalar(select(func.count(db.Candidate.id)).where(db.Candidate.work_id.is_not(None))) or 0
        runs = list(s.scalars(select(db.Run)))
        draft_counts = dict(
            s.execute(
                select(db.Draft.delivery_status, func.count(db.Draft.id))
                .where(db.Draft.status == "released")
                .group_by(db.Draft.delivery_status)
            ).all()
        )
        vacancies = list(s.scalars(select(db.Vacancy)))
        fb_rows = list(s.scalars(select(db.SupervisorFeedback)))

    by_agent: dict[str, dict[str, Any]] = {}
    for r in runs:
        bucket = by_agent.setdefault(
            r.employee,
            {"runs": 0, "ok": 0, "fixed": 0, "fallback": 0, "stopped": 0, "error": 0, "total_seconds": 0.0, "model_calls": 0},
        )
        bucket["runs"] += 1
        bucket[r.outcome] = bucket.get(r.outcome, 0) + 1
        bucket["total_seconds"] += float(r.seconds or 0.0)
        bucket["model_calls"] += int(r.model_calls or 0)
    for bucket in by_agent.values():
        cnt = max(bucket["runs"], 1)
        bucket["avg_seconds"] = round(bucket.pop("total_seconds") / cnt, 3)

    rated = [f.rating for f in fb_rows if f.rating is not None]
    intern_summary: dict[str, Any] = {"ok": False, "mode": talent_hub.resolve_mode()}
    try:
        if talent_hub.resolve_mode() == "mongo":
            intern_summary = {"ok": True, "mode": "mongo", **talent_hub.mongo_analytics()}
        else:
            areas = [a.name for a in db.active_areas()]
            enriched = talent_hub.interns_with_area(areas, limit=0)
            intern_summary = {"ok": True, "mode": talent_hub.resolve_mode(), **talent_hub.analytics(enriched)}
    except (talent_hub.TalentHubUnavailable, talent_hub.TalentHubAuthError) as exc:
        intern_summary["detail"] = str(exc)

    return {
        "generated_at": db.now(),
        "candidates": {
            "total": total_candidates,
            "by_status": counts,
            "avg_score": round(sum(scored) / len(scored), 1) if scored else None,
            "selected": selected_count,
        },
        "agents": by_agent,
        "vacancies": {
            "open": sum(1 for v in vacancies if v.status == "open"),
            "filled": sum(1 for v in vacancies if v.status == "filled"),
            "closed": sum(1 for v in vacancies if v.status == "closed"),
            "total_places": sum(v.places for v in vacancies),
            "filled_places": sum(v.filled for v in vacancies),
        },
        "email_delivery": draft_counts,
        "mongodb_interns": intern_summary,
        "feedback": {
            "total": len(fb_rows),
            "open": sum(1 for f in fb_rows if f.status == "open"),
            "avg_rating": round(sum(rated) / len(rated), 2) if rated else None,
        },
    }


def diagnostics(force: bool = True) -> dict[str, Any]:
    """Run hosting, security, database, MongoDB/TalentHub, LLM, and SMTP readiness checks."""
    wal_mode = "unknown"
    integrity = "unknown"
    sqlite_counts: dict[str, int] = {}
    with db.Session() as s:
        conn = s.connection()
        wal_mode = str(conn.exec_driver_sql("PRAGMA journal_mode").scalar() or "")
        integrity = str(conn.exec_driver_sql("PRAGMA quick_check").scalar() or "")
        sqlite_counts = {
            "candidates": s.scalar(select(func.count(db.Candidate.id))) or 0,
            "vacancies": s.scalar(select(func.count(db.Vacancy.id))) or 0,
            "areas": s.scalar(select(func.count(db.Area.id))) or 0,
            "drafts": s.scalar(select(func.count(db.Draft.id))) or 0,
            "feedback": s.scalar(select(func.count(db.SupervisorFeedback.id))) or 0,
        }

    th_health = talent_hub.health(force=force)
    mongo_direct = talent_hub.mongo_health()
    model_health = llm.health(force=force)
    email_cfg = mailer.public_settings()
    auth_enabled = bool((config.AUTH_USER and config.AUTH_PASS) or config.ACCESS_TOKEN)
    public_bind = config.APP_HOST not in ("127.0.0.1", "localhost", "::1")

    warnings: list[str] = []
    if (public_bind or config.PUBLIC_HOSTING) and not auth_enabled:
        warnings.append("Hosting publicly without authentication: set HR_AUTH_USER/HR_AUTH_PASS or HR_ACCESS_TOKEN.")
    if (public_bind or config.PUBLIC_HOSTING) and email_cfg["enabled"] and not email_cfg["test_recipient"] and not config.DEMO_EMAIL_LOCK:
        warnings.append("Live SMTP enabled on hosted server without Demo Recipient Override or HR_DEMO_EMAIL_LOCK=1.")
    if (public_bind or config.PUBLIC_HOSTING) and config.TALENT_HUB_API_TOKEN == "talenthub-local-dev-token":
        warnings.append("TALENT_HUB_API_TOKEN still uses the local default token.")
    if not model_health["ok"] and not config.ALLOW_RULES_ONLY:
        warnings.append("LLM server is unreachable and HR_ALLOW_RULES_ONLY=0 (AI steps will pause until LLM starts).")
    if not th_health["ok"]:
        warnings.append(f"TalentHub data source is not reachable: {th_health.get('detail', '')}")

    return {
        "checked_at": db.now(),
        "ready": integrity == "ok",
        "sqlite": {
            "path": str(db.paths.db),
            "journal_mode": wal_mode,
            "integrity": integrity,
            "counts": sqlite_counts,
        },
        "talenthub": {
            "mode": talent_hub.resolve_mode(),
            "api_health": th_health,
            "mongo_health": mongo_direct,
            "mongo_uri": talent_hub._mask_uri(config.TALENT_HUB_MONGO_URI),
            "sample_write_allowed": talent_hub.is_sample_mongo_target(),
        },
        "llm": {
            **model_health,
            "url": config.MODEL_URL,
            "has_api_key": bool(config.MODEL_API_KEY),
            "rules_only_fallback": config.ALLOW_RULES_ONLY,
        },
        "email": {
            **email_cfg,
            "demo_email_lock": config.DEMO_EMAIL_LOCK,
        },
        "hosting": {
            "host": config.APP_HOST,
            "port": config.APP_PORT,
            "public_hosting": config.PUBLIC_HOSTING,
            "allowed_hosts": config.ALLOWED_HOSTS,
            "auth_enabled": auth_enabled,
            "scheduler_enabled": config.SCHEDULER_ENABLED,
        },
        "warnings": warnings,
    }


def supervisor_report_markdown() -> str:
    """Build a standalone Markdown evaluation report combining metrics, health, and supervisor notes."""
    diag = diagnostics(force=False)
    stats = pipeline_analytics()
    fb = list_feedback()
    lines = [
        "# HR Hiring — Supervisor Evaluation & System Report",
        f"Generated: `{stats['generated_at']}`",
        "",
        "## 1. Environment & Hosting Readiness",
        f"- **App bind:** `{diag['hosting']['host']}:{diag['hosting']['port']}` | Auth enabled: `{diag['hosting']['auth_enabled']}` | Public hosting: `{diag['hosting']['public_hosting']}`",
        f"- **SQLite (`register.db`):** integrity=`{diag['sqlite']['integrity']}`, journal=`{diag['sqlite']['journal_mode']}`, candidates=`{stats['candidates']['total']}`",
        f"- **TalentHub / MongoDB:** mode=`{diag['talenthub']['mode']}`, ok=`{diag['talenthub']['api_health']['ok']}`, uri=`{diag['talenthub']['mongo_uri']}`",
        f"- **LLM Endpoint:** ok=`{diag['llm']['ok']}`, model=`{diag['llm']['model']}`, rules_only_fallback=`{diag['llm']['rules_only_fallback']}`",
        f"- **Email Pipeline:** mode=`{diag['email']['mode']}`, demo_email_lock=`{diag['email']['demo_email_lock']}`",
    ]
    if diag["warnings"]:
        lines.append("- **Warnings:**")
        for w in diag["warnings"]:
            lines.append(f"  - {w}")

    lines.extend([
        "",
        "## 2. Hiring Pipeline & Vacancies",
        f"- **Total CVs:** {stats['candidates']['total']} (avg score: {stats['candidates']['avg_score']})",
        f"- **Vacancies:** {stats['vacancies']['open']} open, {stats['vacancies']['filled']} filled, {stats['vacancies']['closed']} closed "
        f"({stats['vacancies']['filled_places']}/{stats['vacancies']['total_places']} places filled)",
        f"- **Email Deliveries:** {stats['email_delivery'] or {'none': 0}}",
        f"- **MongoDB Interns Cohort:** {stats['mongodb_interns'].get('total', 'offline')} total, "
        f"{stats['mongodb_interns'].get('active', '-')} active, {stats['mongodb_interns'].get('ending_soon', '-')} ending soon",
        "",
        "## 3. AI Agent Performance",
    ])
    if stats["agents"]:
        lines.append("| Agent | Runs | OK | Fixed | Fallback | Errors | Avg Seconds |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for name, a in sorted(stats["agents"].items()):
            lines.append(f"| {name} | {a['runs']} | {a['ok']} | {a['fixed']} | {a['fallback']} | {a['error']} | {a['avg_seconds']} |")
    else:
        lines.append("_No agent runs recorded yet._")

    lines.extend([
        "",
        "## 4. Supervisor Feedback",
        f"- **Summary:** {fb['summary']['total']} total ({fb['summary']['open']} open, {fb['summary']['resolved']} resolved, "
        f"avg rating: {fb['summary']['avg_rating'] or '-'})",
    ])
    for item in fb["rows"]:
        rating_txt = f" ({item['rating']}/5)" if item["rating"] is not None else ""
        target_txt = f" [{item['target']}]" if item["target"] else ""
        lines.append(f"- **#{item['id']} [{item['status']}] {item['category']}{rating_txt}{target_txt}** — {item['reviewer']}: {item['comment']}")
        if item["resolution_note"]:
            lines.append(f"  - Resolution: {item['resolution_note']}")
    return "\n".join(lines) + "\n"
