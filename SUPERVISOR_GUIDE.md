# HR Hiring (Mail Mode) — Supervisor Quick Guide

Welcome! You do **not** need to configure passwords, servers, or developer IDs to send internship invitations.

---

## 1. Starting the System
1. Double-click **`Start_HR_Mail.command`** in the `hr-hiring` folder (or run `./start_mail.sh` after `./start_model.sh`).
2. Keep the window open—your web browser will open **[http://localhost:8000](http://localhost:8000)** automatically.
3. Wait until the badge in the top-right says **Model: running** (green).
   *(Need a reminder anytime inside the app? Click **Quick guide** at the top right.)*

---

## 2. Sending an Internship Invitation (4 Steps)
1. **Add CV**
   - Drag and drop one or more student CV files (PDF, Word `.docx`, or `.txt`) into the upload box.
   - Optionally type the **Source** (who forwarded the CV) and click **Add CVs**.
   - Wait a few seconds while the system reads the student's **Name, Email, University, and Technology** and places them in the **Register**.
2. **Open the Student**
   - Click **Register** in the top menu, then click the student's row.
   - If the CV did not include an email address, click **Edit** next to Email and type it in.
3. **Check the Mail & Visit Date**
   - Scroll to **Invitation mail**. The next **Tuesday** date and all 4 required SLT attachments (`Hospital_system.pdf`, `individual user manual.pdf`, `POS Task Backlog.xlsx`, `Trainee_Guidelines_Agreement.pdf`) are already filled in.
   - Click **Preview** if you want to check how the mail looks.
4. **Click Send mail → Send in Outlook**
   - Click **Send mail** and confirm.
   - Your **Microsoft Outlook** (or Apple Mail) app will pop open with the email, recipient, subject, formatted text, and all 4 attachments already attached.
   - Just click **Send** inside Outlook. The Register automatically updates the **Mail date** and sets Status to **Mail sent**.

---

## 3. Recording Replies & Exporting to Excel
1. When the student replies with their NIC, Contact No, and Laptop Serial No:
   - Open **Register**, click the student's row, and scroll to **Register details**.
   - Click **Use next: ...** to fill the next reference number automatically, fill in **NIC / PP**, **Tel No**, and **Laptop Serial No**, change Status to **Replied** or **Confirmed**, and click **Save register details**.
2. On the **Register** page, click **Export to Excel** anytime to download the formatted `.xlsx` sheet matching SLT's template.

---

## 4. Changing Attachments or Mail Text (Optional)
- Open **Mail settings** in the top bar to:
  - Add extra attachments or replace one of the 4 standard files.
  - Edit the standard invitation wording or subject (or reset back to the original).
  - **(Optional 1-click automatic sending):** If your IT admin has configured a Microsoft sign-in app for the team, click **Sign in with Microsoft** in **Mail settings** once so emails send straight in the background without even opening the Outlook window.

---

## Quick Troubleshooting
- **Need to open a sent mail again?** Open the student's page and click **Open in Outlook / Mail app** (or **Download .eml**).
- **The email didn't open Outlook?** Check the green/red message in the bottom-right corner, or click **Download .eml** and double-click the downloaded file.
- **Finished for the day?** Close the terminal window (or press `Control+C`). All records stay saved on your computer.
