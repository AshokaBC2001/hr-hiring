"""Synthetic Sri Lankan CV PDF generator (400 unique candidate profiles & skills).

Mirrors `talent_hub.generate_sample_interns()` for the candidate CV Pool side:
- Deterministically generates N (default 400) realistic Sri Lankan IT and business CVs
  rendered cleanly as searchable single-page A4 PDF files (pure Python PDF 1.4, zero extra deps).
- Enforces strict uniqueness of candidate names, emails, phone numbers, and skill sets
  and guarantees zero identity/email overlap with MongoDB's 400 sample interns.
- Aligns skills, projects, education, and experience wording with `shared/areas.json`
  and `hiring/checks.py` so `pypdf` extraction and deterministic/LLM screening both pass cleanly.
"""
from __future__ import annotations

import json
import math
import random
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import actions, checks, config, db, talent_hub, tools, vacancies

# Strictly disjoint first/surname sets from `talent_hub._NAME_COMMUNITIES` (plus extra communities)
_FIRST_NAMES = (
    # Sinhala
    "Amaya", "Bhanuka", "Chamodi", "Danushka", "Eranga", "Gayan", "Harshani", "Isuru",
    "Janith", "Kalpani", "Lahiru", "Madhavi", "Nadeesha", "Oshan", "Pasindu", "Rashmi",
    "Sachini", "Thilina", "Udesh", "Vishwa", "Yasiru", "Dilshan", "Gimhani", "Hasitha",
    # Tamil
    "Abinaya", "Brinthavan", "Darshini", "Gajendran", "Harini", "Kajenthiran", "Lavanya", "Mithushan",
    "Nitharsana", "Prashanth", "Roshini", "Sangeetha", "Tharshini", "Vithushan", "Anushan", "Kishanth",
    # Muslim
    "Aflal", "Fairoz", "Hana", "Imran", "Mushraf", "Nafeesa", "Razeena", "Safras",
    "Tariq", "Zahara", "Fawaz", "Shazna",
    # Burgher & Other
    "Ashane", "Chantal", "Devika", "Gehan", "Jehan", "Natalie", "Roshan", "Tashya",
)

_LAST_NAMES = (
    "Adikari", "Balasuriya", "Cooray", "Dharmasena", "Edirisinghe", "Fonseka", "Gamage", "Hapuarachchi",
    "Illangasinghe", "Jayasuriya", "Kodituwakku", "Liyanage", "Munasinghe", "Nanayakkara", "Obeysekera",
    "Pathirana", "Ratnayake", "Samarasinghe", "Tennakoon", "Udugama", "Vithanage", "Wijayaratne",
    "Kandasamy", "Maheswaran", "Navaratnam", "Ponnambalam", "Ratnasingam", "Sivanesan", "Thiruchelvam",
    "Azeez", "Imtiaz", "Latiff", "Mowlana", "Sathar", "Balthazar", "Gomes", "Loos", "Melder",
    "Weerasinghe", "Abeykoon",
)

_CITIES = (
    "Colombo", "Kandy", "Galle", "Gampaha", "Kurunegala", "Negombo",
    "Matara", "Jaffna", "Batticaloa", "Ratnapura", "Kalutara", "Badulla",
)

_UNIVERSITIES = (
    "University of Moratuwa",
    "University of Colombo School of Computing (UCSC)",
    "SLIIT (Sri Lanka Institute of Information Technology)",
    "Informatics Institute of Technology (IIT)",
    "NSBM Green University",
    "National Institute of Business Management (NIBM)",
    "University of Peradeniya",
    "University of Kelaniya",
    "Sabaragamuwa University of Sri Lanka",
    "Wayamba University of Sri Lanka",
    "University of Ruhuna",
    "University of Jaffna",
    "Open University of Sri Lanka",
    "APIIT Sri Lanka",
)

_DEGREES_BY_FAMILY = {
    "it": (
        "B.Sc. (Hons) in Computer Science",
        "B.Sc. Engineering in Computer Engineering",
        "B.Sc. (Hons) in Information Technology",
        "B.Eng. (Hons) in Software Engineering",
        "B.Sc. in Data Science and Computing",
    ),
    "design": (
        "B.Sc. (Hons) in Interactive Media and UI/UX Design",
        "B.Sc. in Multimedia and Computing",
        "B.Des. in Digital Product Design",
    ),
    "biz": (
        "BBA (Hons) in Business Analytics",
        "B.Sc. in Business Information Systems",
        "B.Sc. (Hons) in Accounting and Finance",
        "BBA in Human Resource Management and Digital Marketing",
    ),
    "emerging": (
        "B.Sc. Engineering in Mechatronics and Embedded Robotics",
        "B.Sc. in Electronics and Industrial IoT Systems",
    ),
}

# Cross-pollination bonus skills so each CV builds a unique real-world skill fingerprint
_CROSS_SKILLS = (
    "FastAPI", "PostgreSQL", "Redis", "GraphQL", "Tailwind CSS", "Unit Testing",
    "Linux Shell", "Prometheus", "Grafana", "RabbitMQ", "gRPC", "Swagger",
    "Clean Architecture", "Technical Writing", "Problem Solving", "Team Collaboration",
    "Sri Lankan Sinhala Unicode NLP", "Figma", "REST API", "Git", "Docker", "Python", "SQL",
)

_PROJECT_DOMAINS = (
    ("SLT Fiber Fault Triage System", "automated ticket routing and SLA tracking"),
    ("LankaTransit Passenger Forecast Platform", "real-time commuter demand prediction"),
    ("Ayurveda E-Pharmacy Inventory Portal", "multi-branch stock reconciliation and billing"),
    ("Campus Internship Placement Matcher", "student skill indexing and cohort analytics"),
    ("Smart Solar Microgrid Telemetry Hub", "inverter anomaly detection and reporting"),
    ("Hospital OPD Queue Optimizer", "appointment slot allocation and SMS alerts"),
    ("AgriMarket Tea Auction Analytics Suite", "export price trend modeling and dashboards"),
    ("FinGuard Fraud Anomaly Monitor", "rule and ML-based transaction screening"),
    ("EduLanka Exam Portal", "proctored assessment workflow and grading"),
    ("CloudDeploy CI Runner", "containerized staging pipelines and zero-downtime releases"),
    ("CeylonCourier Fleet Tracker", "delivery route optimization and live webhooks"),
    ("Citizen Service Feedback Desk", "bilingual complaint classification and escalation"),
)

_EMERGING_SPECS = (
    {
        "title": "Robotics & Embedded IoT Engineering",
        "skills": ["ROS2", "Arduino", "ESP32", "Embedded C", "MQTT", "PCB Design", "PID Control", "KiCad"],
        "project": "Autonomous Greenhouse Moisture Rover using ESP32, MQTT telemetry, and PID Control",
    },
    {
        "title": "Game & Graphics Simulation Engineering",
        "skills": ["Unity 3D", "Unreal Engine", "GLSL Shaders", "Blender", "3D Modeling", "Physics Simulation"],
        "project": "Physics-Based Coastal Disaster Visualizer in Unity 3D with custom GLSL Shaders",
    },
)

# Weighted distribution across areas (~80% IT/Tech, ~15% corporate/HR/finance, ~5% emerging unmapped)
_AREA_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("AI/ML", 11),
    ("Web Development", 11),
    ("Backend Development", 11),
    ("DevOps", 9),
    ("MLOps", 9),
    ("Flutter", 8),
    ("Cybersecurity", 8),
    ("Data Engineering", 8),
    ("Data Analytics", 8),
    ("Software Quality Assurance", 7),
    ("UI/UX Design", 6),
    ("Networking", 6),
    ("Business Analysis", 5),
    ("Project Management", 4),
    ("Human Resources", 3),
    ("Digital Marketing", 2),
    ("Accounting", 2),
    ("Financial Analysis", 2),
)


@dataclass
class CandidateCVSpec:
    index: int
    filename: str
    name: str
    email: str
    phone: str
    city: str
    target_area: str
    tier: str
    degree: str
    institute: str
    grad_year: int
    skills: list[str]
    projects: list[str]
    experience: list[str]
    activities: list[str]
    text: str


def _ascii_pdf_safe(text: str) -> str:
    clean = (
        (text or "")
        .replace("—", "-")
        .replace("–", "-")
        .replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("•", "-")
    )
    clean = clean.encode("latin-1", errors="ignore").decode("latin-1")
    return clean.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap_lines(raw_lines: list[tuple[str, bool]], max_chars: int = 88) -> list[tuple[str, bool]]:
    wrapped: list[tuple[str, bool]] = []
    for line, bold in raw_lines:
        if not line.strip():
            wrapped.append(("", False))
            continue
        cur = line
        indent = "  " if line.startswith("- ") else ""
        while len(cur) > max_chars:
            cut = cur.rfind(" ", 0, max_chars)
            if cut <= 12:
                cut = max_chars
            wrapped.append((cur[:cut].rstrip(), bold))
            cur = indent + cur[cut:].lstrip()
        wrapped.append((cur, bold))
    return wrapped


def render_cv_pdf(cv_text: str) -> bytes:
    """Render plain structured CV text into a clean, text-extractable PDF 1.4 document."""
    raw_lines: list[tuple[str, bool]] = []
    headings = {"Profile", "Education", "Skills", "Projects", "Experience", "Activities"}
    for i, line in enumerate(cv_text.splitlines()):
        stripped = line.strip()
        is_bold = i == 0 or stripped in headings
        raw_lines.append((stripped, is_bold))

    lines = _wrap_lines(raw_lines)
    page_w, page_h = 595, 842  # A4 points
    top_y, bottom_y, left_x = 795, 48, 48

    pages: list[list[tuple[str, bool, int, int]]] = [[]]
    y = top_y
    for idx, (text, is_bold) in enumerate(lines):
        if idx == 0:
            size, step = 14, 20
        elif is_bold:
            size, step = 11, 16
        elif not text:
            size, step = 9, 9
        else:
            size, step = 9, 13
        if y - step < bottom_y:
            pages.append([])
            y = top_y
        if text:
            pages[-1].append((text, is_bold, size, y))
        y -= step

    objects: list[bytes] = []

    def add_obj(body: bytes | str) -> int:
        data = body.encode("latin-1") if isinstance(body, str) else body
        objects.append(data)
        return len(objects)

    f_regular = add_obj("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    f_bold = add_obj("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    page_ids: list[int] = []
    pages_placeholder_idx = len(objects)
    objects.append(b"")  # reserved for /Pages root (id = 3)
    pages_obj_id = pages_placeholder_idx + 1

    for items in pages:
        cmds = ["BT"]
        for text, is_bold, size, ypos in items:
            font = "/F2" if is_bold else "/F1"
            safe = _ascii_pdf_safe(text)
            cmds.append(f"{font} {size} Tf 1 0 0 1 {left_x} {ypos} Tm ({safe}) Tj")
        cmds.append("ET")
        stream = "\n".join(cmds).encode("latin-1")
        content_id = add_obj(
            f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream"
        )
        page_id = add_obj(
            f"<< /Type /Page /Parent {pages_obj_id} 0 R /MediaBox [0 0 {page_w} {page_h}] "
            f"/Resources << /Font << /F1 {f_regular} 0 R /F2 {f_bold} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        )
        page_ids.append(page_id)

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[pages_placeholder_idx] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("latin-1")
    catalog_id = add_obj(f"<< /Type /Catalog /Pages {pages_obj_id} 0 R >>")

    buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for obj_id, payload in enumerate(objects, start=1):
        offsets.append(len(buf))
        buf.extend(f"{obj_id} 0 obj\n".encode("latin-1"))
        buf.extend(payload)
        buf.extend(b"\nendobj\n")

    xref_pos = len(buf)
    buf.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("latin-1"))
    for off in offsets[1:]:
        buf.extend(f"{off:010d} 00000 n \n".encode("latin-1"))
    buf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("latin-1")
    )
    return bytes(buf)


def _load_areas_data() -> dict[str, dict[str, Any]]:
    raw = json.loads(config.AREAS_FILE.read_text(encoding="utf-8"))
    return {a["name"]: a for a in raw}


def _existing_forbidden_identities() -> tuple[set[str], set[str]]:
    """Collect MongoDB sample intern names/emails + existing samples so CVs stay 100% distinct."""
    names: set[str] = set()
    emails: set[str] = set()
    for row in talent_hub.generate_sample_interns(400):
        names.add(row["Trainee_Name"].strip().lower())
        emails.add(row["Trainee_Email"].strip().lower())
    for p in config.SAMPLES_DIR.glob("*.txt"):
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        if lines:
            names.add(lines[0].strip().lower())
        for m in checks.EMAIL_RE.findall("\n".join(lines[:6])):
            emails.add(m.strip().lower())
    return names, emails


def _unique_candidate_names(count: int, forbidden_names: set[str]) -> list[str]:
    combos = [
        f"{fn} {ln}"
        for ln in _LAST_NAMES
        for fn in _FIRST_NAMES
        if f"{fn} {ln}".lower() not in forbidden_names
    ]
    n = len(combos)
    stride = next(s for s in (313, 271, 199, 137, 89, 53, 29, 13, 1) if s < n and math.gcd(s, n) == 1)
    ordered = [combos[(idx * stride) % n] for idx in range(n)]
    if count <= len(ordered):
        return ordered[:count]
    out = list(ordered)
    k = len(out)
    while len(out) < count:
        base = ordered[k % len(ordered)]
        mid = chr(ord("A") + (k % 26))
        parts = base.split(" ", 1)
        cand = f"{parts[0]} {mid}. {parts[1]}"
        if cand.lower() not in forbidden_names and cand not in out:
            out.append(cand)
        k += 1
    return out


def _pick_degree(area_name: str, is_emerging: bool, rng: random.Random) -> str:
    if is_emerging:
        return rng.choice(_DEGREES_BY_FAMILY["emerging"])
    if area_name == "UI/UX Design":
        return rng.choice(_DEGREES_BY_FAMILY["design"])
    if area_name in ("Human Resources", "Digital Marketing", "Accounting", "Financial Analysis", "Project Management"):
        return rng.choice(_DEGREES_BY_FAMILY["biz"])
    return rng.choice(_DEGREES_BY_FAMILY["it"])


def _area_schedule(count: int) -> list[str]:
    weighted: list[str] = []
    for name, w in _AREA_WEIGHTS:
        weighted.extend([name] * w)
    # Mix ~95% weighted area targets and ~5% unmapped emerging specializations
    plan: list[str] = []
    for i in range(count):
        if i % 20 == 19:
            plan.append("__EMERGING__")
        else:
            plan.append(weighted[(i * 7) % len(weighted)])
    return plan


def generate_cv_specs(count: int = 400, seed: int = 20261116) -> list[CandidateCVSpec]:
    """Generate `count` unique structured CV specs aligned with `shared/areas.json`."""
    if count < 1:
        raise ValueError("count must be at least 1.")
    areas_map = _load_areas_data()
    forbidden_names, forbidden_emails = _existing_forbidden_identities()
    names = _unique_candidate_names(count, forbidden_names)
    schedule = _area_schedule(count)

    seen_skill_sigs: set[tuple[str, ...]] = set()
    used_emails = set(forbidden_emails)
    used_phones: set[str] = set()
    specs: list[CandidateCVSpec] = []

    operators = ("77", "71", "76", "70", "75", "78", "72", "74")

    for idx in range(1, count + 1):
        rng = random.Random(seed + idx * 7919)
        name = names[idx - 1]
        slug = re.sub(r"[^a-z0-9]+", ".", name.lower()).strip(".")
        email = f"{slug}.cv{idx:03d}@candidates.slt.lk"
        suffix_num = idx
        while email in used_emails:
            suffix_num += 1
            email = f"{slug}.cv{suffix_num:03d}@candidates.slt.lk"
        used_emails.add(email)

        op = operators[idx % len(operators)]
        mid3 = 100 + ((idx * 37) % 899)
        last4 = 1000 + ((idx * 173 + 57) % 8999)
        phone = f"+94 {op} {mid3:03d} {last4:04d}"
        while phone in used_phones:
            last4 = (last4 + 1) % 9000 + 1000
            phone = f"+94 {op} {mid3:03d} {last4:04d}"
        used_phones.add(phone)

        city = _CITIES[(idx * 3 + rng.randint(0, 5)) % len(_CITIES)]
        institute = _UNIVERSITIES[(idx * 5 + rng.randint(0, 4)) % len(_UNIVERSITIES)]
        grad_year = 2024 + (idx % 4)
        target = schedule[idx - 1]
        is_emerging = target == "__EMERGING__"

        if is_emerging:
            em = _EMERGING_SPECS[idx % len(_EMERGING_SPECS)]
            target_area = em["title"]
            tier = "unmapped"
            base_skills = list(em["skills"])
            extra_pool = [s for s in _CROSS_SKILLS if s not in base_skills and s not in ("Python", "Git", "Docker", "SQL")]
            extras = rng.sample(extra_pool, rng.randint(2, 4))
            skills = base_skills + extras
            degree = _pick_degree(target_area, True, rng)
            dom_a, desc_a = _PROJECT_DOMAINS[(idx + 2) % len(_PROJECT_DOMAINS)]
            projects = [
                f"{em['project']} ({dom_a})",
                f"Prototype controller board for {desc_a} with {skills[-1]} and {skills[-2]}",
            ]
            experience = (
                [f"Robotics Lab Assistant, {institute} ({grad_year - 1}) - calibrated embedded sensor nodes"]
                if idx % 2 == 0 else []
            )
        else:
            target_area = target
            area = areas_map[target]
            req = list(area.get("required_skills") or [])
            pref = list(area.get("preferred_skills") or [])
            mod = idx % 10
            if mod < 5:
                tier = "strong"
                chosen_req = rng.sample(req, max(4, len(req) - (idx % 2)))
                chosen_pref = rng.sample(pref, min(len(pref), rng.randint(3, 5)))
                proj_count = rng.randint(2, 3)
            elif mod < 8:
                tier = "borderline"
                chosen_req = rng.sample(req, min(len(req), max(2, len(req) - 2)))
                chosen_pref = rng.sample(pref, min(len(pref), rng.randint(1, 3)))
                proj_count = rng.randint(1, 2)
            else:
                tier = "developing"
                chosen_req = rng.sample(req, min(len(req), rng.randint(1, 2)))
                chosen_pref = rng.sample(pref, min(len(pref), rng.randint(1, 2)))
                proj_count = 1

            # Ensure every CV has a strictly unique combination of skills
            cross_candidates = [s for s in _CROSS_SKILLS if s not in chosen_req and s not in chosen_pref]
            chosen_cross = rng.sample(cross_candidates, rng.randint(2, 4))
            combined = list(dict.fromkeys(chosen_req + chosen_pref + chosen_cross))
            while tuple(sorted(combined)) in seen_skill_sigs:
                spare = [s for s in (pref + cross_candidates) if s not in combined]
                if spare:
                    combined.append(rng.choice(spare))
                else:
                    combined.append(f"Lab Track {rng.randint(10, 999)}")
            seen_skill_sigs.add(tuple(sorted(combined)))
            skills = combined

            degree = _pick_degree(target, False, rng)
            projects = []
            used_doms: set[int] = set()
            for p_idx in range(proj_count):
                d_idx = (idx * 3 + p_idx * 5 + rng.randint(0, 4)) % len(_PROJECT_DOMAINS)
                while d_idx in used_doms:
                    d_idx = (d_idx + 1) % len(_PROJECT_DOMAINS)
                used_doms.add(d_idx)
                dom_name, dom_goal = _PROJECT_DOMAINS[d_idx]
                used_stack = ", ".join(rng.sample(skills, min(len(skills), 3)))
                kw_hint = (area.get("keywords") or ["solution"])[p_idx % max(1, len(area.get("keywords") or ["solution"]))]
                projects.append(f"{dom_name} ({kw_hint}): built {dom_goal} using {used_stack}")

            if tier == "strong" or (tier == "borderline" and idx % 2 == 0):
                primary_kw = ", ".join(skills[:3])
                experience = [
                    f"Trainee {target} Intern, SLT Digital Ventures, {city} ({grad_year - 1} - 6 months) - worked with {primary_kw}"
                ]
                if idx % 4 == 0:
                    experience.append(f"Part-time Peer Mentor in {target} Lab, {institute}")
            elif tier == "borderline":
                experience = [f"Undergraduate Teaching Assistant, {institute} ({grad_year - 1}) - supported campus lab sessions"]
            else:
                experience = []

        # Ensure emerging specs also carry strictly unique skill sets
        if is_emerging:
            while tuple(sorted(skills)) in seen_skill_sigs:
                extra_s = rng.choice([s for s in _CROSS_SKILLS if s not in skills])
                skills.append(extra_s)
            seen_skill_sigs.add(tuple(sorted(skills)))

        activities = [
            f"Member, FOSS & Computing Society at {institute}",
            f"Finalist, Sri Lanka National Inter-University Hackathon ({2024 + (idx % 2)})",
        ]
        if tier == "strong":
            activities.append("Volunteer workshop mentor for first-year undergraduate coding bootcamps")

        gpa = 3.15 + ((idx * 17) % 80) / 100.0
        profile_line = (
            f"Sri Lankan undergraduate and emerging {target_area} practitioner in {city} "
            f"seeking an internship placement to apply practical engineering and teamwork skills."
        )

        cv_lines = [
            name,
            f"{email} | {phone} | {city}, Sri Lanka",
            "",
            "Profile",
            profile_line,
            "",
            "Education",
            f"- {degree}, {institute} (Expected/Graduated {grad_year}, GPA {gpa:.2f}/4.00)",
            "",
            "Skills",
            ", ".join(skills),
            "",
            "Projects",
            *[f"- {p}" for p in projects],
        ]
        if experience:
            cv_lines.extend(["", "Experience", *[f"- {e}" for e in experience]])
        cv_lines.extend(["", "Activities", *[f"- {a}" for a in activities]])
        text = "\n".join(cv_lines)

        file_slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        specs.append(
            CandidateCVSpec(
                index=idx,
                filename=f"cv_{idx:03d}_{file_slug}.pdf",
                name=name,
                email=email,
                phone=phone,
                city=city,
                target_area=target_area,
                tier=tier,
                degree=degree,
                institute=institute,
                grad_year=grad_year,
                skills=skills,
                projects=projects,
                experience=experience,
                activities=activities,
                text=text,
            )
        )
    return specs


def write_cv_pdfs(
    out_dir: Path | str | None = None,
    count: int = 400,
    seed: int = 20261116,
    reset: bool = False,
) -> dict[str, Any]:
    """Generate and write `count` searchable PDF CVs + manifest.json to `out_dir`."""
    target_dir = Path(out_dir) if out_dir else (db.paths.data / "cv_pool_pdfs")
    target_dir.mkdir(parents=True, exist_ok=True)
    if reset:
        for old in target_dir.glob("cv_*.pdf"):
            old.unlink()

    specs = generate_cv_specs(count=count, seed=seed)
    paths: list[Path] = []
    manifest_rows: list[dict[str, Any]] = []
    for sp in specs:
        dest = target_dir / sp.filename
        dest.write_bytes(render_cv_pdf(sp.text))
        paths.append(dest)
        row = asdict(sp)
        row.pop("text", None)
        manifest_rows.append(row)

    manifest_path = target_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps({"count": len(specs), "seed": seed, "cvs": manifest_rows}, indent=2),
        encoding="utf-8",
    )
    return {
        "out_dir": str(target_dir),
        "manifest": str(manifest_path),
        "written": len(paths),
        "paths": [str(p) for p in paths],
        "specs": specs,
    }


def load_pdfs_into_register(
    pdf_paths: list[Path],
    *,
    pending_ai: int = 0,
    specs_by_filename: dict[str, CandidateCVSpec] | None = None,
) -> dict[str, int]:
    """Load generated PDF CVs into SQLite (`data/register.db`), skipping already-present CV hashes.

    - The last `pending_ai` CVs remain in status `"new"` so the live AI Recruiter agent can process them.
    - All other CVs are deterministically profiled and scored across every active area cleanly
      into `"in_pool"` or `"no_area"` (and Planner suggestions are topped up automatically).
    """
    db.setup()
    areas = db.active_areas()
    vocab = tools.skill_vocabulary(areas)
    total = len(pdf_paths)
    new_cutoff = max(0, total - max(0, pending_ai))

    added_pool = added_new = skipped = 0
    for idx, p in enumerate(pdf_paths):
        res = actions.add_cv(p.name, p.read_bytes())
        if not res.get("ok"):
            skipped += 1
            continue
        if idx >= new_cutoff:
            added_new += 1
            continue
        c = db.get_candidate(res["app_no"])
        if c is None:
            continue
        sp = (specs_by_filename or {}).get(p.name)
        profile = tools.rule_profile(c.cv_text, vocab)
        if sp and sp.name and not profile.get("name"):
            profile["name"] = sp.name
        if sp and sp.email and not profile.get("email"):
            profile["email"] = sp.email

        area_scores = tools.score_all_areas(profile, c.cv_text, areas)
        best_name, best_score, best_share = tools.best_area_by_rules(profile, c.cv_text, areas)

        is_unmapped = (sp and sp.tier == "unmapped") or not best_name or best_share <= 0
        if is_unmapped:
            suggested = sp.target_area if (sp and sp.tier == "unmapped") else ""
            db.update_candidate(
                c.id,
                status="no_area",
                name=profile.get("name") or (sp.name if sp else c.app_no),
                email=profile.get("email") or (sp.email if sp else ""),
                email_source="Read from CV",
                profile=profile,
                profile_source="Rules",
                area="",
                area_source="",
                suggested_new_area=suggested,
                score=None,
                score_parts={},
                gaps=[],
                area_scores=area_scores,
            )
            db.log_event(c.id, "Recruiter", 'Added generated PDF CV to pool as "No matching area".')
        else:
            picked = area_scores.get(best_name, {})
            score = picked.get("score", best_score)
            db.update_candidate(
                c.id,
                status="in_pool",
                name=profile.get("name") or (sp.name if sp else c.app_no),
                email=profile.get("email") or (sp.email if sp else ""),
                email_source="Read from CV",
                profile=profile,
                profile_source="Rules",
                area=best_name,
                area_source="Chosen by rules",
                area_reasons=[f"Matched required and preferred skills for {best_name}."],
                area_evidence=profile.get("skills", [])[:3],
                score=score,
                score_parts=picked.get("parts", {}),
                gaps=picked.get("gaps", []),
                area_scores=area_scores,
            )
            db.log_event(c.id, "Recruiter", f"Added generated PDF CV to pool ({best_name}, score {score} of 100).")
        added_pool += 1

    if added_pool:
        vacancies.suggest_for_open()

    return {
        "added_pool": added_pool,
        "added_new": added_new,
        "skipped": skipped,
        "total_loaded": added_pool + added_new,
    }
