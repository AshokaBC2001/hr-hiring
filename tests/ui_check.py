"""Clicks through every page in a real browser and saves screenshots (optional check).

Needs Playwright (pip install playwright, then python -m playwright install chromium),
the fake model (python tests/fake_model.py --port 8080) and the app (python app.py)
running with an empty data folder.

    python tests/ui_check.py [--out screenshots]
"""
import argparse
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
errors: list[str] = []
checks: list[tuple[str, bool]] = []


def ok(name: str, cond: bool) -> None:
    checks.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


def wait_for(page, fn, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="screenshots")
    out = Path(ap.parse_args().out)
    out.mkdir(exist_ok=True)
    with sync_playwright() as p:
        exe = os.environ.get("CHROMIUM_PATH")
        browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        shot = lambda name, full=True: page.screenshot(path=str(out / f"{name}.png"), full_page=full)

        page.goto(BASE + "/#/dashboard")
        page.wait_for_selector(".nums")
        ok("dashboard loads", page.locator(".num").count() == 6)
        ok("name in the top bar is HR Hiring", page.inner_text("#brand") == "HR Hiring")
        shot("01_dashboard_empty")

        ok("no separate Pool item in the menu", page.locator("#nav a[href='#/pool']").count() == 0)
        page.click("#nav a[href='#/add']")
        page.wait_for_selector("#drop")
        ok("Add CV page has the CV pool and no 'What happens next'", "CV pool" in page.inner_text("#view")
           and "What happens next" not in page.inner_text("#view"))
        shot("02_add_cv")
        page.click("[data-act=samples]")
        ok("samples added and marked New in the pool", wait_for(page, lambda: page.locator(
            ".pool-list tbody tr td:nth-child(2) .tag", has_text="New").count() >= 6))
        ok("Recently added is shown after adding", "Recently added" in page.inner_text(".tabs button.on"))
        shot("03_add_cv_result")

        ok("employees processed the CVs", wait_for(page, lambda: page.evaluate(
            "fetch('/api/pool').then(r=>r.json()).then(p=>p.stats.being_read + ':' + p.stats.in_pool)") == "0:17", 240))
        page.goto(BASE + "/#/dashboard")
        page.wait_for_selector(".nums")
        time.sleep(1)
        shot("04_dashboard")

        page.click("#nav a[href='#/add']")
        page.wait_for_selector(".pool-list")
        page.click("[data-act=pool-show][data-s=all]")
        ok("pool shows 17 CVs", wait_for(page, lambda: page.locator(".pool-list tbody tr").count() == 17))
        ok("pool shows its counts", "17 in pool" in page.inner_text("#view"))
        shot("05_pool", full=False)
        page.select_option("#pool-area", "DevOps")
        ok("area ranking only shows 50 or more", wait_for(page, lambda: "Score for DevOps" in page.inner_text(".pool-list")
            and all(int(x) >= 50 for x in page.locator(".pool-list tbody tr td:nth-child(4)").all_inner_texts())))
        shot("05b_pool_devops")
        page.select_option("#pool-area", "AI/ML")
        wait_for(page, lambda: "Score for AI/ML" in page.inner_text(".pool-list"))
        page.click("[data-act=pool-show][data-s=no_area]")
        ok("no matching area list shows 3", wait_for(page, lambda: page.locator(".pool-list tbody tr").count() == 3)
           or "No CV in the pool" in page.inner_text("#view"))
        page.select_option("#pool-area", "")
        ok("no matching area list shows 3 (all areas)", wait_for(page, lambda: page.locator(".pool-list tbody tr").count() == 3))
        page.click("[data-act=pool-show][data-s=all]")
        wait_for(page, lambda: page.inner_text(".tabs button.on") == "All" and page.locator(".pool-list tbody tr").count() == 17)
        page.select_option("#pool-area", "AI/ML")
        ok("AI/ML ranking shown", wait_for(page, lambda: "Score for AI/ML" in page.inner_text(".pool-list")))

        page.click(".pool-list tbody tr:first-child")
        page.wait_for_selector("text=Area and score")
        ok("best AI/ML candidate is Arjun", "Arjun Mehta" in page.inner_text("#view"))
        ok("candidate page shows the score table", wait_for(page, lambda: page.locator(".score-table tbody tr").count() == 5, 10))
        shot("06_candidate_in_pool")
        page.click("[data-act=invite]")
        page.click("#dialog [data-dlg=yes]")
        ok("invitation drafted after invite", wait_for(page, lambda: page.locator(".mail").count() >= 1, 60))
        shot("06b_candidate_invited")

        page.click("[data-act=release]")
        page.click("#dialog [data-dlg=yes]")
        ok("invitation released", wait_for(page, lambda: "Waiting for the interviewer" in page.inner_text("#view")))
        shot("07_candidate_released")

        page.click("#nav a[href='#/interns']")
        page.wait_for_selector("#view")
        page.click("[data-act=sync-interns]")
        ok("intern records read from Talent Hub", wait_for(page, lambda: page.locator(".intern-list tbody tr").count() > 50, 60))
        shot("05c_interns")
        page.click("[data-act=intern-show][data-s=soon]")
        ok("ending soon list", wait_for(page, lambda: 0 < page.locator(".intern-list tbody tr").count() < 30))
        page.click("[data-act=intern-show][data-s=unknown]")
        ok("unknown specialization is kept, not guessed", wait_for(page, lambda: "No matching area" in page.inner_text("#view")))

        page.click("#nav a[href='#/vacancies']")
        page.wait_for_selector(".vac-list")
        ok("vacancies found", page.locator(".box .vac-list").count() >= 3)
        ok("suggestions are waiting", "Waiting for you" in page.inner_text("#view"))
        shot("05d_vacancies")
        page.locator("[data-act=vac-approve]").first.click()
        page.click("#dialog [data-dlg=yes]")
        ok("suggestion approved", wait_for(page, lambda: "Invited" in page.inner_text("#view"), 60))
        shot("05e_vacancy_approved")

        page.goto(BASE + "/#/review")
        page.wait_for_selector("text=Decisions for you")
        ok("review queue has no pool CVs", "No decisions waiting." in page.inner_text("#view"))
        shot("08_review")

        page.goto(BASE + "/#/areas")
        page.wait_for_selector("#view table.t")
        page.click(".box-head [data-act=area-new]")
        page.fill("#af-name", "Legal")
        page.fill("#af-req", "Contract Law, Legal Research, Drafting")
        page.fill("#af-studies", "Law, LLB")
        page.click("#dialog [data-dlg=save]")
        ok("new area added", wait_for(page, lambda: "Added by HR officer" in page.inner_text("#view")))
        shot("09_areas")

        page.goto(BASE + "/#/add/all")
        page.select_option("#pool-area", "")
        page.wait_for_selector(".pool-list")
        page.click(".pool-list >> text=Laura Bennett")
        ok("HR CV matched to Human Resources", wait_for(page, lambda: "Area: Human Resources" in page.inner_text("#view")))
        shot("10_candidate_area_chosen")

        page.goto(BASE + "/#/replies")
        page.wait_for_selector("text=Emails drafted by the AI team")
        shot("11_replies")

        page.goto(BASE + "/#/team")
        page.wait_for_selector("text=Routines")
        ok("team page shows 5 employees", page.locator(".box.emp").count() == 5)
        shot("12_team")

        # Interviewer
        page.select_option("#role-select", "interviewer")
        page.wait_for_selector("text=Waiting for your result")
        shot("13_interviews")
        page.click("tr.click >> text=Arjun Mehta")
        page.wait_for_selector("text=Your result")
        shot("14_interview")
        page.click("label.pass")
        page.fill("#result-comment", "Strong project work and clear answers.")
        page.click("[data-act=save-result]")
        page.click("#dialog [data-dlg=yes]")
        ok("result saved", wait_for(page, lambda: "Saved" in page.inner_text("#view")))
        shot("15_interview_saved")

        # Back to HR: onboarding should have drafted the acceptance with a work ID
        page.select_option("#role-select", "hr")
        page.goto(BASE + "/#/candidates/selected")
        page.wait_for_selector("table.list")
        page.click("text=Arjun Mehta")
        ok("work ID issued", wait_for(page, lambda: "Work ID 1001" in page.inner_text("#view"), 40))
        shot("16_candidate_work_id")
        ok("placement letter has automatic dates", wait_for(page, lambda: "Automatic dates" in page.inner_text("#letter"), 20)
           and page.input_value("#letter-start") and page.input_value("#letter-end"))
        ok("acceptance email shows the letter attached", "Placement_Letter_" in page.inner_text("#emails"))
        ok("acceptance email can be released", page.locator("#emails [data-act=release]").is_enabled())
        page.fill("#letter-start", "2026-12-07")
        page.fill("#letter-end", "2027-06-06")
        page.click("[data-act=save-letter]")
        ok("dates changed and letter made again", wait_for(page, lambda: "Dates changed" in page.inner_text("#letter"), 15))
        pdf = page.request.get(BASE + page.get_attribute("#letter a.btn >> nth=0", "href"))
        ok("letter preview is a PDF", pdf.ok and pdf.body()[:4] == b"%PDF")
        page.locator("#letter").scroll_into_view_if_needed()
        shot("16b_placement_letter", full=False)
        page.click("#emails [data-act=release]")
        page.click("#dialog [data-dlg=yes]")
        ok("acceptance released with the letter", wait_for(page, lambda: ".eml" in page.inner_text("#emails"), 15))
        page.goto(BASE + "/#/candidates")
        page.wait_for_selector(".tabs")
        ok("Selected candidates is the default filter", "Selected candidates" in page.inner_text(".tabs button.on"))
        page.click("[data-act=filter][data-f='']")
        # Arjun (invited from the pool) and the CV approved for a vacancy; none of the pool CVs
        ok("candidates page shows only the hiring flow", wait_for(page, lambda: page.locator("table.list tbody tr").count() == 2))

        page.goto(BASE + "/#/dashboard")
        ok("selected count on dashboard", wait_for(page, lambda: "Selected candidates by area" in page.inner_text("#view")))
        shot("17_dashboard_after")
        page.select_option("#role-select", "interviewer")
        page.wait_for_selector("text=Your recent results")
        shot("18_interviews_after")
        page.select_option("#role-select", "hr")
        page.goto(BASE + "/#/review")
        page.wait_for_selector("text=Decisions for you")
        shot("19_review_after")
        page.goto(BASE + "/#/add")
        page.wait_for_selector("#drop")
        shot("20_add_after")
        page.set_viewport_size({"width": 820, "height": 1000})
        page.goto(BASE + "/#/dashboard")
        page.wait_for_selector(".nums")
        shot("21_dashboard_narrow")
        browser.close()

    ok("no browser errors", not errors)
    for e in errors[:10]:
        print("  browser error:", e)
    failed = [n for n, c in checks if not c]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} checks passed. Screenshots in {out}/")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
