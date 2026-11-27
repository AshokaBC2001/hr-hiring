# HR Hiring (Mail Mode) — Supervisor Quick Guide

Everything works straight away: just save your Outlook email once, upload CVs, and click **Send mail**.

---

## 1. Starting the System
1. Double-click **`Start_HR_Mail.command`** inside the `hr-hiring` folder (keep the small window open).
2. Your browser will open **[http://localhost:8000](http://localhost:8000)** automatically.
3. Wait a moment until the badge at the top right shows **Model: running** (green).
   *(Click **Quick guide** at the top right anytime for an on-screen reminder.)*

---

## 2. One-Time Email Setup (30 Seconds)
1. Click **Mail settings** in the top menu and scroll to **Outlook sending**.
2. Enter:
   - **Sender Name** (for example `Giridaran` or `Digital Platforms Development Section`)
   - **Your Outlook Email** (for example `you@slt.com.lk` or `you@outlook.com`)
   - **Outlook Password / App Password** — *optional*: leave it blank if you want mails to send through your Outlook / Mail app on your computer, or enter it if IT uses Office 365 SMTP.
3. Click **Save Outlook settings**.
4. *(Optional check)* Type your own email under **Send a test email** and click **Send test mail** to verify all 4 SLT attachments arrive cleanly.

---

## 3. Sending an Invitation Mail
1. **Add CV:** Drag and drop student CV files (PDF, Word `.docx`, or `.txt`) into **Add CV**, optionally type **Source**, and click **Add CVs**.
2. **Open Student:** Go to **Register** and click the student's row. If no email was inside the CV, click **Edit** next to Email and add it.
3. **Check & Send:** Verify the suggested **Tuesday** visit date and attachments (click **Preview** if desired), then click **Send mail**.
   - If your Outlook app handles sending, it opens/sends cleanly from your saved Outlook address (just click **Send** if a draft window opens).
   - The **Register** automatically records today's **Mail date** and marks the student **Mail sent**.

---

## 4. Recording Student Replies & Downloading Excel
1. When a student replies, open their row in **Register** and scroll to **Register details**.
2. Click **Use next: ...** to fill the next reference number automatically, enter **NIC / PP**, **Tel No**, and **Laptop Serial No**, choose Status (**Replied** / **Confirmed**), and click **Save register details**.
3. On the **Register** page, click **Export to Excel** anytime to download the SLT `.xlsx` spreadsheet.

---

## Quick Troubleshooting
- **Need to open a sent email again?** Open the student's page and click **Open in Outlook / Mail app** (or **Download .eml**).
- **IT asks you to connect with Microsoft 365 login?** Expand **Advanced options** under **Outlook sending**, paste the Application ID IT gives you, and click **Sign in with Microsoft**.
- **Done for the day?** Close the terminal window (or press `Control+C`). All records stay saved on your computer.
