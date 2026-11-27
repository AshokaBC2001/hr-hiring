"""Clicks through mail-only mode in a real browser and saves screenshots (optional check).

Needs Playwright, the fake model (python tests/fake_model.py --port 8080) and the app started in
mail-only mode with an empty data folder:  HR_MODE=mail python app.py

    python tests/ui_check_mail.py --files <folder with the mail attachments> [--out screenshots_mail]
"""
import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
SAMPLES = Path(__file__).resolve().parent.parent / "samples"
errors: list[str] = []
checks: list[tuple[str, bool]] = []


def ok(name: str, cond: bool) -> None:
    checks.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


def wait_for(fn, timeout=40):
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
    ap.add_argument("--out", default="screenshots_mail")
    ap.add_argument("--files", default="", help="folder with PDF or Excel files to attach")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    with sync_playwright() as p:
        exe = os.environ.get("CHROMIUM_PATH")
        browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        shot = lambda name, full=True: page.screenshot(path=str(out / f"{name}.png"), full_page=full)

        page.goto(BASE + "/")
        page.wait_for_selector("#drop")
        menu = page.inner_text("#nav")
        ok("only Add CV, Register and Mail settings in the menu",
           menu.split() == ["Add", "CV", "Register", "Mail", "settings"])
        ok("no role switch in mail-only mode", not page.is_visible("#role-select"))

        # the 4 required files come with the template; extra files can be added
        page.click("#nav a[href='#/mailsettings']")
        page.wait_for_selector("#ms-body")
        ok("the 4 required files are already there", page.locator("#view table.t tbody tr").count() == 4
           and "Hospital_system.pdf" in page.inner_text("#view"))
        files = sorted(Path(a.files).glob("*")) if a.files else []
        if files:
            page.set_input_files("#att-input", [str(f) for f in files])
            ok("an extra file is added", wait_for(lambda: page.locator("[data-act=att-remove]").count() == len(files)))
        n_files = page.locator("#view table.t tbody tr").count()
        ok("template has the visit date blank", "{visit_date}" in page.input_value("#ms-body"))
        shot("01_mail_settings")

        # add 2 CVs with a source
        page.click("#nav a[href='#/add']")
        page.wait_for_selector("#drop")
        page.set_input_files("#file-input", [str(SAMPLES / "arjun_mehta.txt"), str(SAMPLES / "laura_bennett.pdf")])
        page.fill("#cv-source", "Mr.Dinesh")
        page.click("[data-act=upload]")
        ok("CVs added", wait_for(lambda: page.locator(".pool-list tbody tr").count() == 2))
        shot("02_add_cv")

        # the register fills from the CVs
        page.click("#nav a[href='#/register']")
        ok("register shows the CVs read by the Recruiter", wait_for(
            lambda: "Arjun Mehta" in page.inner_text("#view") and "Laura Bennett" in page.inner_text("#view"), 60))
        text = page.inner_text("#view")
        ok("university and source in the register", "Vellore Institute of Technology" in text and "Mr.Dinesh" in text)
        shot("03_register_before")

        # the invitation mail
        page.click("tr.click >> text=Arjun Mehta")
        page.wait_for_selector("#inv-body")
        today = date.today()
        tuesday = today + timedelta(days=(1 - today.weekday()) % 7 or 7)
        ok("visit date suggests the next Tuesday", page.input_value("#inv-date") == tuesday.isoformat())
        body = page.input_value("#inv-body")
        ok("mail is filled from the template", "Dear Intern" in body and "sltinternguide.vercel.app" in body
           and "(Tuesday)" in body and "{visit_date}" not in body)
        later = tuesday + timedelta(days=14)
        page.fill("#inv-date", later.isoformat())
        page.dispatch_event("#inv-date", "change")
        months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
        new_text = f"{later.day:02d}-{months[later.month - 1]}-{later.year}"
        ok("changing the date updates the mail", new_text in page.input_value("#inv-body"))
        page.fill("#inv-subject", page.input_value("#inv-subject") + " - test")
        ok("all attachments ticked", page.locator(".inv-file:checked").count() == n_files)
        page.locator("#invite").scroll_into_view_if_needed()
        shot("04_invitation_mail", full=False)

        prev = page.request.post(BASE + f"/api/candidates/{page.url.split('/')[-1]}/mail",
                                 headers={"X-Role": "hr"},
                                 data={"subject": page.input_value("#inv-subject"), "body": page.input_value("#inv-body"),
                                       "visit_date": later.isoformat(),
                                       "attachments": [x.input_value() for x in page.locator(".inv-file").all()]})
        ok("mail saved", prev.ok)
        html = page.request.get(BASE + f"/api/candidates/{page.url.split('/')[-1]}/mail/preview").text()
        ok("preview has clickable links and the attachments", '<a href="https://ternboard.slt.com.lk/">' in html
           and "Hospital_system.pdf" in html and all(f.name in html for f in files))
        page.click("[data-act=inv-send]")
        page.click("#dialog [data-dlg=yes]")
        ok("mail sent (saved to the outbox)", wait_for(lambda: "Mail sent" in page.inner_text("#invite")))

        # register details from the student's reply
        page.fill("#reg-ref_no", "Refno :TEST-2026/11/03/06")
        page.fill("#reg-nic", "200012345678")
        page.fill("#reg-tel", "0771234567")
        page.fill("#reg-laptop", "TESTSERIAL01")
        page.select_option("#reg-status", "Replied")
        page.click("[data-act=reg-save]")
        ok("register details saved", wait_for(lambda: "Replied" in page.inner_text("#reg .box-head")))
        shot("05_candidate_after", full=True)

        page.click("#nav a[href='#/register']")
        page.wait_for_selector(".reg-list")
        ok("register shows the mail date and the reply details", wait_for(
            lambda: "TESTSERIAL01" in page.inner_text(".reg-list") and "Replied" in page.inner_text(".reg-list")))
        page.click("tr.click >> text=Laura Bennett")
        page.wait_for_selector("#reg")
        ok("next reference number is suggested", "Use next: Refno :TEST-2026/11/03/07" in page.inner_text("#reg"))
        page.click("#nav a[href='#/register']")
        page.wait_for_selector(".reg-list")
        shot("06_register_after")
        x = page.request.get(BASE + "/api/register.xlsx")
        ok("Excel export works", x.ok and x.body()[:2] == b"PK")
        page.set_viewport_size({"width": 820, "height": 1000})
        page.reload()
        page.wait_for_selector(".reg-list")
        shot("07_register_narrow")
        browser.close()

    ok("no browser errors", not errors)
    for e in errors[:10]:
        print("  browser error:", e)
    failed = [n for n, c in checks if not c]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} checks passed. Screenshots in {out}/")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
