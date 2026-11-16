"""Generate 400 unique Sri Lankan CV PDFs and optionally load them into the CV Pool & AI queue.

Usage:
    # Write 400 unique CV PDFs + manifest to data/cv_pool_pdfs/:
    .venv/bin/python scripts/generate_cv_pool.py

    # Write 400 PDFs and ingest into data/register.db (395 scored in CV Pool + 5 'new' for Recruiter AI):
    .venv/bin/python scripts/generate_cv_pool.py --load-db --pending-ai 5
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hiring import cv_generator  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate unique Sri Lankan CV PDFs for the HR Hiring CV Pool")
    ap.add_argument("--count", type=int, default=400, help="Number of unique CV PDFs to generate (default: 400)")
    ap.add_argument("--seed", type=int, default=20261116, help="Deterministic RNG seed")
    ap.add_argument("--out", default="", help="Output folder (default: data/cv_pool_pdfs)")
    ap.add_argument("--reset", action="store_true", help="Clear existing cv_*.pdf files in output folder first")
    ap.add_argument("--load-db", action="store_true", help="Load generated PDFs into data/register.db CV Pool / queue")
    ap.add_argument("--pending-ai", type=int, default=5, help="Leave N CVs in 'new' status for live Recruiter AI (default: 5)")
    args = ap.parse_args()

    out = cv_generator.write_cv_pdfs(out_dir=args.out or None, count=args.count, seed=args.seed, reset=args.reset)
    specs = out["specs"]
    unique_names = len({s.name for s in specs})
    unique_skills = len({tuple(sorted(s.skills)) for s in specs})
    print(
        f"Generated {out['written']} CV PDFs in {out['out_dir']} "
        f"(unique_names={unique_names}, unique_skill_sets={unique_skills})"
    )
    print(f"Manifest: {out['manifest']}")

    if args.load_db:
        by_name = {s.filename: s for s in specs}
        stats = cv_generator.load_pdfs_into_register(
            [Path(p) for p in out["paths"]],
            pending_ai=args.pending_ai,
            specs_by_filename=by_name,
        )
        print(
            f"Loaded into register.db: pool={stats['added_pool']}, "
            f"pending_ai_new={stats['added_new']}, skipped_duplicates={stats['skipped']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
