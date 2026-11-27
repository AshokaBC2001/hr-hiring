/* HR Hiring - the pages for the HR officer and the Interviewer.
   Plain JavaScript, no framework. Every value from the server is escaped before it is shown. */
"use strict";

// ---------------------------------------------------------------- helpers
const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } },
};
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const AI_ACTORS = new Set(["Recruiter", "Coordinator", "Onboarding", "Chief of staff"]);
const APP_NAME = "HR Hiring";

let ROLE = store.get("hr-hiring-role", "hr");
if (!["hr", "interviewer"].includes(ROLE)) ROLE = "hr";
let STATE = null;
let lastHTML = "";
let current = { page: "", arg: "" };
const UI = { showCv: false, showRuns: false, editDraft: null, editEmail: false, letterDates: null, candFilter: "selected", candQuery: "",
             poolArea: "", poolShow: "all", poolQuery: "", cvEmail: "", cvSource: "", inviteEdit: null, regEdit: null,
             regShow: "all", regQuery: "", candidateFrom: "",
             vacShow: "open", internShow: "all", internQuery: "",
             repliesTab: "waiting", showAll: "", files: [], uploadResults: null, areas: null };

async function api(path, { method = "GET", body, form } = {}) {
  const opt = { method, headers: { "X-Role": ROLE } };
  if (form) opt.body = form;
  else if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch { /* no body */ }
  if (!r.ok) throw new Error((data && data.detail) || `The server answered with error ${r.status}.`);
  return data;
}

function toast(msg, bad = false) {
  const t = document.createElement("div");
  t.className = "toast" + (bad ? " bad" : "");
  t.textContent = msg;
  $("#toasts").appendChild(t);
  while ($("#toasts").children.length > 3) $("#toasts").firstChild.remove();
  setTimeout(() => t.remove(), bad ? 7000 : 4500);
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// Colour is used only for Pass, Fail and "Needs you".
const HR_NEEDS = new Set(["needs_person", "needs_area", "borderline", "not_shortlisted_check", "invite_ready", "hold_note_ready", "reply_ready", "offer_ready"]);
const INT_NEEDS = new Set(["awaiting_result", "on_hold"]);
const needsMe = (status) => (ROLE === "hr" ? HR_NEEDS : INT_NEEDS).has(status);
function statusTag(c) {
  const cls = c.status === "passed" ? "pass" : c.status === "failed" ? "fail" : needsMe(c.status) ? "need" : "";
  return `<span class="tag ${cls}">${esc(c.status_label)}</span>`;
}
const resultTag = (r) => (r ? `<span class="tag ${r === "Pass" ? "pass" : r === "Fail" ? "fail" : ""}">${esc(r)}</span>` : "");
const tag = (text, cls = "") => `<span class="tag ${cls}">${esc(text)}</span>`;
const byText = (text) => (text ? `<span class="by">(${esc(text)})</span>` : "");
const actorText = (actor) => esc(AI_ACTORS.has(actor) ? `AI ${actor}` : actor);
const empty = (text) => `<div class="empty">${esc(text)}</div>`;
const candLink = (appNo, text) => `<a href="#/candidate/${encodeURIComponent(appNo)}">${esc(text || appNo)}</a>`;

function title(t) { $("#page-title").textContent = t; document.title = `${t} - ${APP_NAME}`; }
function setView(html) {
  if (html === lastHTML) return;
  const y = window.scrollY;
  lastHTML = html;
  view.innerHTML = html;
  window.scrollTo(0, y);
}
function busy() {
  const a = document.activeElement;
  return $("#dialog").open || document.hidden ||
    (a && ["INPUT", "TEXTAREA", "SELECT"].includes(a.tagName)) || UI.editDraft !== null || UI.editEmail;
}

// ---------------------------------------------------------------- dialogs
function openDialog(html) {
  $("#dialog-body").innerHTML = html;
  const d = $("#dialog");
  if (!d.open) d.showModal();
  const f = d.querySelector("input, textarea, select");
  if (f) f.focus();
}
function closeDialog() { $("#dialog").close(); }
function confirmBox({ heading, text, ok = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    openDialog(`<h3>${esc(heading)}</h3><p class="muted">${esc(text)}</p>
      <div class="actions"><button class="btn" data-dlg="no">Cancel</button>
      <button class="btn ${danger ? "danger" : "primary"}" data-dlg="yes">${esc(ok)}</button></div>`);
    const d = $("#dialog");
    const done = (v) => { d.removeEventListener("close", onClose); closeDialog(); resolve(v); };
    const onClose = () => resolve(false);
    d.addEventListener("close", onClose, { once: true });
    d.querySelector('[data-dlg="yes"]').onclick = () => done(true);
    d.querySelector('[data-dlg="no"]').onclick = () => done(false);
  });
}

// ---------------------------------------------------------------- frame: top menu, model, role
const NAV = {
  hr: [["dashboard", "Dashboard"], ["add", "Add CV"], ["vacancies", "Vacancies", "vacancies"],
       ["candidates", "Candidates"], ["review", "Review queue", "review"], ["replies", "Replies", "release"],
       ["interns", "Interns"], ["areas", "Areas"], ["team", "AI team"]],
  interviewer: [["interviews", "Interviews", "interview"], ["team", "AI team"]],
};
NAV.mail = [["add", "Add CV"], ["register", "Register"], ["mailsettings", "Mail settings"]];
const HOME = { hr: "#/dashboard", interviewer: "#/interviews", mail: "#/add" };
const MAILMODE = () => !!(STATE && STATE.mode === "mail");
const NK = () => (MAILMODE() ? "mail" : ROLE);       // which menu, home page and routes to use

function renderFrame() {
  const t = STATE ? STATE.tiles : {};
  $("#nav").innerHTML = NAV[NK()].map(([key, label, countKey]) => {
    const count = countKey && t[countKey] ? `<span class="count">${esc(t[countKey])}</span>` : "";
    const onCandidate = current.page === "candidate" && (key === (MAILMODE() ? UI.candidateFrom || "add" : UI.candidateInPool ? "add" : "candidates"));
    const on = current.page === key || onCandidate || (key === "interviews" && current.page === "interview") || (key === "add" && current.page === "pool");
    return `<a class="${on ? "on" : ""}" href="#/${key}">${esc(label)}${count}</a>`;
  }).join("");
  $("#brand").href = HOME[NK()];
  $("#role-select").value = ROLE;
  const roleBox = $("#role-select").closest("label") || $("#role-select");
  roleBox.style.display = MAILMODE() ? "none" : "";
  if (!STATE) return;
  const m = STATE.model;
  const down = !m.ok || (STATE.notice && STATE.notice.text);
  const el = $("#model");
  el.textContent = down ? "Model: not running" : "Model: running";
  el.className = "model" + (down ? " down" : "");
  el.title = down ? ((STATE.notice && STATE.notice.text) || m.detail || "") : m.model;
  const working = Object.values(STATE.team).find((x) => x.state === "working");
  const ol = STATE.outlook || {};
  const eff = ol.effective_mode || ol.mode || "eml";
  const acct = ol.account?.username || "";
  const olText = eff === "outlook_app"
    ? "Outlook Desktop / Mail app (no login needed)"
    : eff === "graph_draft"
    ? `Outlook: Graph Drafts (${acct || "sign in required"})`
    : eff === "graph" || eff === "graph_send"
    ? `Outlook: Graph Send (${acct || "sign in required"})`
    : eff === "smtp"
    ? `${MAILMODE() ? (SMTP_NAMES[ol.provider] || "SMTP") : "Outlook: Office 365 SMTP"} (${ol.smtp_user || ol.smtp_host})`
    : "Outlook: Draft files (.eml) & Outlook Web";
  const redir = ol.test_recipient ? ` -> demo: ${ol.test_recipient}` : "";
  mailBanner(ol);
  $("#foot").textContent = MAILMODE()
    ? `${APP_NAME} - mail-only mode (${olText}${redir}). AI team: ${working ? working.name + " is working" : "waiting for work"}.`
    : `${APP_NAME} - ${olText}${redir}. AI team: ${STATE.paused ? "paused" : working ? working.name + " is working" : "waiting for work"}.`;
}
// Mail-only mode: a warning on every page while all mails go to the test inbox.
const SMTP_NAMES = { microsoft: "Microsoft SMTP", google: "Google SMTP", custom: "SMTP" };
function mailBanner(ol) {
  let el = $("#mail-banner");
  if (!el) { el = document.createElement("div"); el.id = "mail-banner"; $("main").prepend(el); }
  const on = MAILMODE() && ol && ol.test_recipient;
  el.hidden = !on;
  el.innerHTML = on ? `<div class="notice error" style="margin-top:16px"><div class="grow"><b>Test inbox is on:</b>
    <span>every mail goes to ${esc(ol.test_recipient)}, not to the students. Test sends are not counted in the register.</span></div>
    <a class="btn small" href="#/mailsettings">Change in Mail settings</a></div>` : "";
}
async function refreshState() {
  try { STATE = await api("/api/state"); } catch { return; }
  renderFrame();
}

function openQuickGuide() {
  const mailHtml = `
    <h3>How to Use HR Hiring (Quick Guide)</h3>
    <p class="small muted" style="margin-top:0">No email passwords or Azure setup needed. Clicking <b>Send mail</b> opens the email directly in your Outlook or Mail app with all the attachments already attached. To send directly from Microsoft, Google or another mail server, set it up once in <b>Mail settings</b>.</p>
    <ol class="clean" style="line-height:1.55">
      <li><b>Add CV:</b> Drag and drop student CVs (PDF, Word or TXT), optionally enter the <b>Source</b>, and click <b>Add CVs</b>.</li>
      <li><b>Open Candidate:</b> Click <b>Register</b> in the top menu, then click any student row. Add their email if the CV didn't have one.</li>
      <li><b>Check &amp; Send Mail:</b> Verify the suggested Tuesday visit date, click <b>Preview</b> if desired, then click <b>Send mail</b>. If your Outlook / Mail window opens, click <b>Send</b> there, then click <b>I sent it</b> here so the register gets the mail date. To mail several students, tick them on the <b>Register</b> page and click <b>Prepare invitations</b>.</li>
      <li><b>Save Replies &amp; Export:</b> When the student replies, open <b>Paste the student's reply</b> on their page to fill NIC, Tel No &amp; Laptop Serial (or type them), use <b>Use next: ...</b> for reference numbers, and click <b>Export to Excel</b> or <b>Export CSV</b> on the <b>Register</b> page.</li>
    </ol>
    <div class="actions" style="margin-top:14px"><button type="button" class="btn primary" data-dlg="close-guide">Got it</button></div>`;
  const fullHtml = `
    <h3>How to Use HR Hiring (Quick Guide)</h3>
    <ol class="clean" style="line-height:1.55">
      <li><b>Add CV:</b> Upload candidate CVs; the Recruiter scores them automatically into the pool.</li>
      <li><b>Review Queue &amp; Vacancies:</b> Approve vacancy matches or shortlist borderline CVs; Coordinator drafts interview invites.</li>
      <li><b>Replies:</b> Release emails and placement letters. In Automatic mode they open directly in Outlook/Mail (or send automatically if signed in to Microsoft).</li>
      <li><b>Interviewer Role:</b> Switch Role (top right) to record interview Pass, Fail or Hold.</li>
    </ol>
    <div class="actions" style="margin-top:14px"><button type="button" class="btn primary" data-dlg="close-guide">Got it</button></div>`;
  openDialog(MAILMODE() ? mailHtml : fullHtml);
  $("#dialog-body").querySelector('[data-dlg="close-guide"]').onclick = closeDialog;
}
if ($("#help-btn")) $("#help-btn").onclick = openQuickGuide;

$("#role-select").onchange = (e) => {
  ROLE = e.target.value;
  store.set("hr-hiring-role", ROLE);
  e.target.blur();
  toast(`You are now using the system as ${ROLE === "hr" ? "the HR Officer" : "the Interviewer"}.`);
  lastHTML = "";
  location.hash = HOME[ROLE];
  renderFrame();
};

// ---------------------------------------------------------------- shared pieces
function notice(kind, head, text = "", action = "") {
  return `<div class="notice ${kind}"><div class="grow"><b>${esc(head)}</b>${text ? ` <span class="muted">${esc(text)}</span>` : ""}</div>${action}</div>`;
}
function boxHead(text, extra = "") {
  return `<div class="box-head"><h2>${esc(text)}</h2><div class="spacer"></div>${extra}</div>`;
}
function modelNotice() {
  if (!STATE) return "";
  const n = STATE.notice && STATE.notice.text;
  if (STATE.model.ok && !n) return "";
  return notice("error", "The AI employees cannot work: the model server is not running.",
    "Open a second terminal in the project folder and run ./start_model.sh, then wait until it says the server is listening. " + (n || STATE.model.detail || ""));
}
function mailBlock(d, { showCandidate = false } = {}) {
  const editing = UI.editDraft === d.id;
  const ol = STATE?.outlook || {};
  const state = d.status === "waiting" ? tag("Waiting for release", ROLE === "hr" ? "need" : "") : d.status === "released" ? tag("Released") : tag(d.status);
  const deliveryBadge = d.delivery_status === "sent" ? tag("Sent via Outlook", "pass")
    : d.delivery_status === "opened_app" ? tag("Opened in Outlook / Mail", "pass")
    : d.delivery_status === "outlook_draft" || (d.status === "waiting" && d.outlook_web_link) ? tag("Outlook Draft", "pass")
    : d.delivery_status === "failed" ? tag("Outlook Failed", "fail")
    : "";
  const notes = (d.check_notes || []).map((n) => `<div class="warn">Warning: ${esc(n)}</div>`).join("");
  const desktopOpenBtn = ROLE === "hr" && d.email
    ? `<button class="btn small" data-act="open-draft-app" data-id="${d.id}" title="Open ready-to-send email in Microsoft Outlook / Mail app">Open in Outlook App</button>`
    : "";
  const emlBtn = `<a class="btn small" href="${esc(d.eml_url || `/api/drafts/${d.id}/eml`)}" title="Download .eml draft (with PDF attachment if acceptance) for Outlook Desktop">.eml</a>`;
  const webBtn = d.compose_links?.outlook_office
    ? `<a class="btn small" href="${esc(d.compose_links.outlook_office)}" target="_blank" rel="noopener" title="Open pre-filled draft in Microsoft 365 Outlook Web">Outlook Web</a>`
    : "";
  const openBtn = d.outlook_web_link
    ? `<a class="btn small" href="${esc(d.outlook_web_link)}" target="_blank" rel="noopener">Open in Outlook</a>`
    : "";
  const graphDraftBtn = ROLE === "hr" && d.email && ol.signed_in
    ? `<button class="btn small" data-act="outlook-draft" data-id="${d.id}">${d.outlook_web_link ? "Update Outlook Draft" : "Save to Outlook Drafts"}</button>`
    : "";
  let foot = "";
  if (d.status === "waiting" && ROLE === "hr") {
    foot = editing
      ? `<div class="actions"><button class="btn primary" data-act="save-draft" data-id="${d.id}">Save changes</button><button class="btn" data-act="cancel-edit">Cancel</button></div>`
      : `<div class="actions"><button class="btn primary" data-act="release" data-id="${d.id}" data-name="${esc(d.name)}" data-kind="${esc(d.kind_label)}" ${d.can_release ? "" : "disabled"}>Release</button>
         <button class="btn" data-act="edit-draft" data-id="${d.id}">Edit</button>${desktopOpenBtn}${graphDraftBtn}${openBtn}${webBtn}${emlBtn}
         ${d.can_release ? "" : `<span class="small muted">${esc(d.release_blocker || "The candidate is not at this step now.")}</span>`}</div>`;
  } else if (d.status === "released") {
    let info = `Released ${esc(fmtTime(d.released_at))}. Outbox: ${esc(d.file)}.`;
    if (d.delivery_status === "sent") info = `Sent via Outlook to ${esc(d.sent_to)} (${esc(fmtTime(d.sent_at || d.released_at))}). Archived: ${esc(d.file)}.`;
    else if (d.delivery_status === "opened_app") info = `Opened in Outlook / Mail ready to send to ${esc(d.sent_to)}. Archived: ${esc(d.file)}.`;
    else if (d.delivery_status === "outlook_draft") info = `Saved to Outlook Drafts for ${esc(d.sent_to)}. Archived: ${esc(d.file)}.`;
    else if (d.delivery_status === "failed") info = `Saved to outbox, Outlook error: ${esc(d.delivery_error || "failed")}`;
    const effMode = ol.effective_mode || ol.mode;
    const retryBtn = ROLE === "hr" && d.email && d.delivery_status !== "sent" && effMode !== "eml" && effMode !== "outlook_app"
      ? `<button class="btn small" data-act="resend-draft" data-id="${d.id}">${d.delivery_status === "failed" ? "Retry Outlook" : "Send via Outlook"}</button>`
      : "";
    foot = `<div class="actions"><span class="small muted" style="flex:1">${info}</span>${desktopOpenBtn}${retryBtn}${graphDraftBtn}${openBtn}${webBtn}${emlBtn}</div>`;
  }
  const attach = d.attachment ? `<div class="mail-attach small">Attachment: <a href="/api/candidates/${encodeURIComponent(d.app_no)}/letter" target="_blank" rel="noopener">${esc(d.attachment)}</a> <span class="muted">(placement letter, made by code)</span></div>` : "";
  const body = editing
    ? `<div class="mail-body editing"><div class="form-row"><label for="ed-subject">Subject</label><input type="text" id="ed-subject" value="${esc(d.subject)}"></div>
       <div class="form-row"><label for="ed-body">Email</label><textarea id="ed-body" rows="12">${esc(d.body)}</textarea></div></div>`
    : `<div class="mail-body"><b>${esc(d.subject)}</b>\n\n${esc(d.body)}</div>`;
  return `<div class="mail" id="mail-${d.id}">
    <div class="mail-head"><b>${esc(d.kind_label)}</b>${state}${deliveryBadge}${byText(d.written_by)}<span class="grow"></span>
      ${showCandidate ? candLink(d.app_no) : ""}<span class="small muted">To: ${esc(d.name || "")} ${d.email ? "&lt;" + esc(d.email) + "&gt;" : "(no email yet)"}</span></div>
    ${notes}${body}${attach}${foot ? `<div class="mail-foot">${foot}</div>` : ""}</div>`;
}

let _authTimer = null;
function _stopAuthPoll() {
  if (_authTimer) { clearInterval(_authTimer); _authTimer = null; }
}

function _effectiveOutlookMode(c) {
  if (c.effective_mode) return c.effective_mode;
  if (c.mode && c.mode !== "auto") return c.mode;
  if (c.signed_in) return "graph";
  if (c.smtp_user && c.has_smtp_password) return "smtp";
  return "outlook_app";
}

function _outlookControlsMarkup(c, prefix = "ol") {
  const acct = c.account;
  const a = c.auth || {};
  const mode = c.mode || "auto";
  const eff = _effectiveOutlookMode(c);
  const isGraph = mode === "auto" || mode === "graph" || mode === "graph_send" || mode === "graph_draft";
  const showSmtp = mode === "smtp" || (isGraph && c.smtp_fallback !== false);
  const openAdv = !!a.error;
  const savedEmail = c.sender_email || c.account?.username || c.smtp_user || "";
  const customSmtpUser = c.smtp_user && c.smtp_user !== savedEmail ? c.smtp_user : "";

  const statusBanner = (c.signed_in && acct)
    ? `<div class="notice ok" style="padding:10px 12px"><div class="grow small"><b>Connected to Outlook:</b> ${esc(acct.name)} (${esc(acct.username)}).<br>Emails and attachments are sent automatically from your Outlook account.</div><button type="button" class="btn small" data-dlg="ol-signout">Sign out</button></div>`
    : a.status === "pending" && a.user_code
    ? `<div class="notice need" style="padding:10px 12px"><div class="grow small">Open <a href="${esc(a.verification_uri || "https://microsoft.com/devicelogin")}" target="_blank" rel="noopener"><b>${esc(a.verification_uri || "microsoft.com/devicelogin")}</b></a> and enter code:<br><span class="code-pill" style="margin-top:4px">${esc(a.user_code)}</span></div></div>`
    : a.status === "pending"
    ? `<div class="notice need" style="padding:10px 12px"><div class="grow small"><b>Waiting for Microsoft sign-in...</b> Finish signing in on the Microsoft browser window that just opened.</div></div>`
    : eff === "smtp"
    ? `<div class="notice ok" style="padding:10px 12px"><div class="grow small"><b>Saved sender:</b> ${esc(c.from_name || "HR Hiring Team")} &lt;${esc(savedEmail)}&gt;.<br>Ready to send directly via Outlook / Office 365 SMTP with all attachments.</div></div>`
    : eff === "eml"
    ? `<div class="notice ok" style="padding:10px 12px"><div class="grow small"><b>Draft file mode (.eml).</b> Ready-to-open emails are saved to <code>data/outbox/</code>.</div></div>`
    : savedEmail
    ? `<div class="notice ok" style="padding:10px 12px"><div class="grow small"><b>Saved sender:</b> ${esc(c.from_name || "HR Hiring Team")} &lt;${esc(savedEmail)}&gt;.<br>Clicking <b>Send mail</b> sends via your Outlook / Mail app with all attachments included.</div>${c.has_client_id ? `<button type="button" class="btn primary small" data-dlg="ol-signin">Sign in with Microsoft</button>` : ""}</div>`
    : `<div class="notice ok" style="padding:10px 12px"><div class="grow small">Enter your name and Outlook email above, then click <b>Save Outlook settings</b>. (Password is optional &mdash; leave it blank to send through your Outlook / Mail app.)</div></div>`;

  const authBlock = acct
    ? `<div class="notice ok" style="padding:8px 10px"><div class="grow small"><b>Signed in sender:</b> ${esc(acct.name)} (${esc(acct.username)})</div><button type="button" class="btn small" data-dlg="ol-signout">Sign out</button></div>`
    : a.status === "pending" && a.user_code
    ? `<div class="notice need" style="padding:8px 10px"><div class="grow small">Waiting for code <span class="code-pill">${esc(a.user_code)}</span> at microsoft.com/devicelogin</div></div>`
    : `<div class="actions"><button type="button" class="btn primary small" data-dlg="ol-signin">Sign in with Microsoft (Browser)</button><button type="button" class="btn small" data-dlg="ol-device">Use sign-in code</button>${a.error ? `<span class="small" style="color:var(--fail)">${esc(a.error)}</span>` : ""}</div>`;

  const mailUI = prefix === "ms-ol";          // the Mail settings page of mail-only mode
  const provider = c.provider || "microsoft";
  const pwStore = c.password_store === "keychain" ? "Saved in this computer's keychain."
    : c.password_store === "database" ? "Saved in the app's local database (install the keyring package to keep it in the keychain)." : "";
  const topFields = mailUI ? `
    <div class="grid two">
      <div class="form-row" style="margin:0"><label for="${prefix}-fname">Sender Name</label><input type="text" id="${prefix}-fname" value="${esc(c.from_name || "Digital Platforms Development Section")}" placeholder="e.g. your name or your section"></div>
      <div class="form-row" style="margin:0"><label for="${prefix}-semail">Your email</label>
        <div class="actions" style="flex-wrap:nowrap"><input type="email" id="${prefix}-semail" value="${esc(savedEmail)}" placeholder="you@your-organisation.com">
        <button type="button" class="btn small" id="${prefix}-detect" title="Find the provider from the email address">Detect</button></div></div>
    </div>
    <div class="form-row" style="margin-top:10px"><label for="${prefix}-provider">Email provider</label>
      <select id="${prefix}-provider">
        <option value="microsoft" ${provider === "microsoft" ? "selected" : ""}>Microsoft (Outlook, Microsoft 365, Outlook.com)</option>
        <option value="google" ${provider === "google" ? "selected" : ""}>Google (Gmail, Google Workspace)</option>
        <option value="custom" ${provider === "custom" ? "selected" : ""}>Other mail server (SMTP)</option>
        <option value="desktop" ${provider === "desktop" ? "selected" : ""}>Outlook or Mail app on this computer (no password)</option>
      </select>
      <div class="help" id="${prefix}-provider-help">${esc(PROVIDER_HELP[provider] || "")}</div></div>
    <div class="form-row" id="${prefix}-pass-row" style="margin-top:10px;${provider === "desktop" ? "display:none" : ""}">
      <label for="${prefix}-spass" id="${prefix}-pass-label">${PASS_LABEL[provider] || PASS_LABEL.microsoft}</label>
      <input type="password" id="${prefix}-spass" placeholder="${c.has_smtp_password ? "Saved (leave blank to keep the saved password)" : "Optional — leave blank to send through your mail app"}">
      ${pwStore ? `<div class="help">${esc(pwStore)}</div>` : ""}
      ${c.has_smtp_password ? `<label class="check" style="margin-top:6px"><input type="checkbox" id="${prefix}-clear-pw"> Clear saved password</label>` : ""}
    </div>` : `
    <div class="grid two">
      <div class="form-row" style="margin:0"><label for="${prefix}-fname">Sender Name</label><input type="text" id="${prefix}-fname" value="${esc(c.from_name || "Digital Platforms Development Section")}" placeholder="e.g. Giridaran / SLT HR"></div>
      <div class="form-row" style="margin:0"><label for="${prefix}-semail">Your Outlook Email</label><input type="email" id="${prefix}-semail" value="${esc(savedEmail)}" placeholder="you@slt.com.lk or you@outlook.com"></div>
    </div>
    <div class="form-row" style="margin-top:10px">
      <label for="${prefix}-spass">Outlook Password / App Password <span class="small muted">(optional &mdash; leave blank to use your Outlook / Mail app)</span></label>
      <input type="password" id="${prefix}-spass" placeholder="${c.has_smtp_password ? "Saved (leave blank to keep existing password)" : "Optional — leave blank if you don't use SMTP password"}">
      ${c.has_smtp_password ? `<label class="check" style="margin-top:6px"><input type="checkbox" id="${prefix}-clear-pw"> Clear saved password</label>` : ""}
    </div>`;
  const showGraph = isGraph && (!mailUI || provider === "microsoft");
  const showSmtpSec = showSmtp || (mailUI && provider === "custom");
  return `${topFields}
    <div id="${prefix}-status-banner" style="margin-top:10px">${statusBanner}</div>
    <details id="${prefix}-adv" style="margin-top:12px" ${openAdv || (mailUI && provider === "custom") ? "open" : ""}>
      <summary class="small muted" style="cursor:pointer;user-select:none">Advanced options (delivery mode, Microsoft App ID &amp; custom SMTP server)</summary>
      <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--line-soft)">
        <div class="form-row">
          <label for="${prefix}-mode">Delivery mode</label>
          <select id="${prefix}-mode">
            <option value="auto" ${mode === "auto" ? "selected" : ""}>Automatic (Recommended &mdash; Graph if signed in, SMTP if password saved, else Outlook app)</option>
            <option value="outlook_app" ${mode === "outlook_app" ? "selected" : ""}>Open / Send with Outlook Desktop or Mail app (no password needed)</option>
            <option value="eml" ${mode === "eml" ? "selected" : ""}>Save Draft file (.eml) &amp; Outlook Web links</option>
            <option value="graph" ${mode === "graph" || mode === "graph_send" ? "selected" : ""}>Send via Microsoft Graph (OAuth2 + Upload Session for &gt;3 MB)</option>
            <option value="graph_draft" ${mode === "graph_draft" ? "selected" : ""}>Save to Outlook Drafts (Microsoft Graph + MSAL)</option>
            <option value="smtp" ${mode === "smtp" ? "selected" : ""}>Direct Office 365 / Outlook SMTP</option>
          </select>
        </div>
        <div id="${prefix}-graph-sec" style="margin-top:12px;padding:10px;border:1px solid var(--line-soft);border-radius:4px;${showGraph ? "" : "display:none"}">
          <div class="label" style="margin-bottom:6px">Microsoft 365 / Outlook Graph (OAuth2 Sign-In)</div>
          <div class="grid two">
            <div class="form-row" style="margin:0"><label for="${prefix}-client">Application (Client) ID</label><input type="text" id="${prefix}-client" value="${esc(c.client_id)}" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"></div>
            <div class="form-row" style="margin:0"><label for="${prefix}-tenant">Tenant ID</label><input type="text" id="${prefix}-tenant" value="${esc(c.tenant_id || "common")}" placeholder="common"></div>
          </div>
          <div class="help" style="margin:4px 0 8px">Keep <code>common</code> for both Microsoft 365 work accounts and Outlook.com.</div>
          <div id="${prefix}-auth-box">${authBlock}</div>
        </div>
        <div id="${prefix}-smtp-sec" style="margin-top:12px;padding:10px;border:1px solid var(--line-soft);border-radius:4px;${showSmtpSec ? "" : "display:none"}">
          <div class="label" style="margin-bottom:6px">${mailUI ? "SMTP server" : "Custom Office 365 SMTP Settings"}</div>
          <div class="grid two">
            <div class="form-row" style="margin:0"><label for="${prefix}-shost">SMTP Host</label><input type="text" id="${prefix}-shost" value="${esc(c.smtp_host)}" placeholder="${mailUI ? "smtp.your-organisation.com" : "smtp.office365.com"}"></div>
            <div class="form-row" style="margin:0"><label for="${prefix}-sport">SMTP Port</label><input type="text" id="${prefix}-sport" value="${esc(c.smtp_port)}" placeholder="587"></div>
          </div>
          ${mailUI ? `<div class="form-row" style="margin:8px 0 0"><label for="${prefix}-ssec">Security</label><select id="${prefix}-ssec">
            <option value="starttls" ${c.smtp_security === "starttls" ? "selected" : ""}>STARTTLS (usually port 587)</option>
            <option value="ssl" ${c.smtp_security === "ssl" ? "selected" : ""}>SSL / TLS (usually port 465)</option>
            <option value="none" ${c.smtp_security === "none" ? "selected" : ""}>None (not recommended)</option></select>
            <div class="help">Microsoft and Google fill these in for you. For another server, ask your IT team.</div></div>` : ""}
          <div class="form-row" style="margin-top:8px"><label for="${prefix}-suser">Custom SMTP Username (only if different from Your Outlook Email)</label><input type="email" id="${prefix}-suser" value="${esc(customSmtpUser)}" placeholder="Same as Your Outlook Email"></div>
          <label class="check" style="margin-top:6px"><input type="checkbox" id="${prefix}-sfallback" ${c.smtp_fallback !== false ? "checked" : ""}> Use Office 365 SMTP automatically as a fallback if Microsoft Graph fails</label>
        </div>
        <div class="form-row" style="margin-top:12px">
          <label for="${prefix}-test-to">Safe Demo Recipient Override (optional)</label>
          <input type="email" id="${prefix}-test-to" value="${esc(c.test_recipient)}" placeholder="Leave blank to send to candidate email">
          <div class="help">If set, all outgoing emails redirect to this inbox (useful for testing with a test inbox).</div>
        </div>
      </div>
    </details>`;
}

const PROVIDER_HELP = {
  microsoft: "With a password, mails go out through smtp.office365.com (port 587, STARTTLS). Leave the password blank to send through your Outlook / Mail app. You can also sign in with Microsoft under Advanced options.",
  google: "With a Google App Password, mails go out through smtp.gmail.com (port 587, STARTTLS). Make one at Google Account > Security > 2-Step Verification > App passwords. Leave it blank to send through your mail app.",
  custom: "Enter your mail server under Advanced options below. Ask your IT team for the server name, port and security.",
  desktop: "Send mail opens the mail with all its attachments in Outlook or Apple Mail. No password is stored.",
};
const PASS_LABEL = {
  microsoft: `Password / App Password <span class="small muted">(optional &mdash; leave blank to use your Outlook / Mail app)</span>`,
  google: `Google App Password <span class="small muted">(16 letters, not your normal password &mdash; optional)</span>`,
  custom: `Password <span class="small muted">(for your mail server)</span>`,
};
const SMTP_PRESETS = { microsoft: ["smtp.office365.com", "587", "starttls"], google: ["smtp.gmail.com", "587", "starttls"] };
function _applyProvider(prefix, provider) {
  const help = $(`#${prefix}-provider-help`), label = $(`#${prefix}-pass-label`), row = $(`#${prefix}-pass-row`);
  if (help) help.textContent = PROVIDER_HELP[provider] || "";
  if (label) label.innerHTML = PASS_LABEL[provider] || PASS_LABEL.microsoft;
  if (row) row.style.display = provider === "desktop" ? "none" : "";
  const preset = SMTP_PRESETS[provider];
  if (preset) {
    $(`#${prefix}-shost`).value = preset[0]; $(`#${prefix}-sport`).value = preset[1];
    if ($(`#${prefix}-ssec`)) $(`#${prefix}-ssec`).value = preset[2];
  } else if (provider === "custom" && Object.values(SMTP_PRESETS).some((p) => p[0] === $(`#${prefix}-shost`).value)) {
    $(`#${prefix}-shost`).value = "";
  }
  if (provider === "custom" && $(`#${prefix}-adv`)) $(`#${prefix}-adv`).open = true;
  _syncOutlookModeUI(prefix);
}
function _syncOutlookModeUI(prefix = "ol") {
  const mode = $(`#${prefix}-mode`)?.value || "auto";
  const provider = $(`#${prefix}-provider`)?.value || "";
  const isGraph = mode === "auto" || mode === "graph" || mode === "graph_send" || mode === "graph_draft";
  const fb = !!$(`#${prefix}-sfallback`)?.checked;
  const showSmtp = mode === "smtp" || (isGraph && fb) || provider === "custom";
  const graphSec = $(`#${prefix}-graph-sec`), smtpSec = $(`#${prefix}-smtp-sec`);
  if (graphSec) graphSec.style.display = isGraph && (!provider || provider === "microsoft") ? "" : "none";
  if (smtpSec) smtpSec.style.display = showSmtp ? "" : "none";
}

function _collectOutlookPayload(prefix = "ol") {
  const semail = $(`#${prefix}-semail`)?.value.trim() || "";
  const advUser = $(`#${prefix}-suser`)?.value.trim() || "";
  const p = {
    mode: $(`#${prefix}-mode`).value,
    from_name: $(`#${prefix}-fname`)?.value.trim() || "HR Hiring Team",
    sender_email: semail,
    client_id: $(`#${prefix}-client`).value.trim(),
    tenant_id: $(`#${prefix}-tenant`).value.trim() || "common",
    test_recipient: $(`#${prefix}-test-to`).value.trim(),
    smtp_host: $(`#${prefix}-shost`).value.trim(),
    smtp_port: $(`#${prefix}-sport`).value.trim(),
    smtp_user: advUser || semail,
    smtp_use_tls: true,
    smtp_fallback: !!$(`#${prefix}-sfallback`).checked,
  };
  if ($(`#${prefix}-provider`)) p.provider = $(`#${prefix}-provider`).value;
  if ($(`#${prefix}-ssec`)) p.smtp_security = $(`#${prefix}-ssec`).value;
  if ($(`#${prefix}-clear-pw`)?.checked) p.clear_smtp_password = true;
  else if ($(`#${prefix}-spass`).value) p.smtp_password = $(`#${prefix}-spass`).value;
  return p;
}

function _startOutlookAuthPoll(onUpdate) {
  _stopAuthPoll();
  _authTimer = setInterval(async () => {
    try {
      const cur = await api("/api/outlook/auth/status");
      if (cur.signed_in) {
        _stopAuthPoll();
        toast(`Signed in to Outlook as ${cur.account?.username || "Microsoft account"}.`);
        const latest = await api("/api/outlook/settings");
        await refreshState();
        onUpdate(latest);
      } else if (cur.status === "error") {
        _stopAuthPoll();
        const latest = await api("/api/outlook/settings");
        onUpdate({ ...latest, auth: cur });
      }
    } catch { /* ignore transient poll failure */ }
  }, 2500);
}

function _bindOutlookAuthActions(root, prefix, onUpdate, onAfterSignOut) {
  const startAuth = async (endpoint) => {
    try {
      const payload = _collectOutlookPayload(prefix);
      if (!payload.client_id) {
        toast("Open Advanced settings and enter your Application (Client) ID first.", true);
        const adv = $(`#${prefix}-adv`);
        if (adv) adv.open = true;
        return;
      }
      await api("/api/outlook/settings", { method: "POST", body: payload });
      const st = await api(endpoint, { method: "POST" });
      const latest = await api("/api/outlook/settings");
      onUpdate({ ...latest, auth: st });
      _startOutlookAuthPoll(onUpdate);
    } catch (e) { toast(e.message, true); }
  };
  root.querySelectorAll('[data-dlg="ol-signin"]').forEach((btn) => { btn.onclick = () => startAuth("/api/outlook/auth/interactive"); });
  root.querySelectorAll('[data-dlg="ol-device"]').forEach((btn) => { btn.onclick = () => startAuth("/api/outlook/auth/start"); });
  root.querySelectorAll('[data-dlg="ol-signout"]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        _stopAuthPoll();
        const latest = await api("/api/outlook/auth/signout", { method: "POST" });
        toast("Signed out of Microsoft Outlook.");
        onAfterSignOut(latest);
      } catch (e) { toast(e.message, true); }
    };
  });
}

async function openOutlookSettings() {
  _stopAuthPoll();
  openDialog(`<h3>Microsoft Outlook Settings</h3><p class="muted">Loading settings...</p>`);
  const dlg = $("#dialog");
  dlg.addEventListener("close", _stopAuthPoll, { once: true });
  try {
    const cfg = await api("/api/outlook/settings");
    const renderBody = (c) => {
      $("#dialog-body").innerHTML = `
        <h3>Microsoft Outlook Settings</h3>
        <p class="small muted" style="margin:0 0 12px">Choose how emails leave the system. Ready-to-open .eml copies are always saved to data/outbox/.</p>
        ${_outlookControlsMarkup(c, "ol")}
        <div class="form-row" style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line-soft)">
          <label for="ol-probe">Test Outlook connection</label>
          <div class="actions">
            <input type="email" id="ol-probe" style="flex:1" value="${esc(c.test_recipient || c.account?.username || c.smtp_user)}" placeholder="testinbox@outlook.com">
            <button type="button" class="btn" data-dlg="ol-test">Test Outlook</button>
          </div>
        </div>
        <div class="actions"><button type="button" class="btn" data-dlg="cancel">Cancel</button><button type="button" class="btn primary" data-dlg="save">Save settings</button></div>`;

      $("#ol-mode").onchange = () => _syncOutlookModeUI("ol");
      $("#ol-sfallback").onchange = () => _syncOutlookModeUI("ol");

      const bodyEl = $("#dialog-body");
      bodyEl.querySelector('[data-dlg="cancel"]').onclick = () => { _stopAuthPoll(); closeDialog(); };
      bodyEl.querySelector('[data-dlg="save"]').onclick = async () => {
        try {
          await api("/api/outlook/settings", { method: "POST", body: _collectOutlookPayload("ol") });
          _stopAuthPoll(); closeDialog(); toast("Saved Outlook settings."); lastHTML = ""; await refreshState(); render();
        } catch (e) { toast(e.message, true); }
      };
      bodyEl.querySelector('[data-dlg="ol-test"]').onclick = async () => {
        try {
          await api("/api/outlook/settings", { method: "POST", body: _collectOutlookPayload("ol") });
          const r = await api("/api/outlook/test", { method: "POST", body: { to: $("#ol-probe").value } });
          toast(r.delivery_status === "opened_app"
            ? `Opened in ${r.opened_in || "Outlook / Mail"} for ${r.sent_to} — just click Send.`
            : r.delivery_status === "outlook_draft" ? `Test draft saved in Outlook for ${r.sent_to}.`
            : `Test email sent via Outlook to ${r.sent_to}.`);
        } catch (e) { toast(e.message, true); }
      };
      _bindOutlookAuthActions(bodyEl, "ol", renderBody, (latest) => { renderBody(latest); refreshState(); });
    };
    renderBody(cfg);
  } catch (e) {
    $("#dialog-body").innerHTML = `<h3>Outlook settings</h3><p class="muted">${esc(e.message)}</p><div class="actions"><button class="btn" data-dlg="cancel">Close</button></div>`;
    $("#dialog-body").querySelector('[data-dlg="cancel"]').onclick = closeDialog;
  }
}

// ================================================================ HR officer pages
async function pDashboard(_arg, { reuseState = false } = {}) {
  title("Dashboard");
  const [s, all] = await Promise.all([
    reuseState && STATE ? Promise.resolve(STATE) : api("/api/state"),
    api("/api/candidates"),
  ]);
  STATE = s;
  renderFrame();
  const t = s.tiles;
  let next;
  if (t.vacancies) next = notice("need", `${t.vacancies} candidate(s) are suggested for ${s.vacancies.open} open vacancy(ies).`, `${s.vacancies.places} place(s) to fill.`, `<a class="btn" href="#/vacancies">Open Vacancies</a>`);
  else if (t.review) next = notice("need", `${t.review} item(s) need your decision or a quick check.`, "", `<a class="btn" href="#/review">Open Review queue</a>`);
  else if (t.release) next = notice("need", `${t.release} email(s) are waiting for your release.`, "Nothing reaches a candidate until you release it.", `<a class="btn" href="#/replies">Open Replies</a>`);
  else if (!s.total) next = notice("ok", "No CVs yet.", "Add a CV to start, or load the sample CVs.", `<a class="btn primary" href="#/add">Add CV</a>`);
  else next = notice("ok", "Nothing needs you right now.", "The AI team is handling the rest.");
  const nums = [
    ["New CVs", t.new, "#/add/recent", false],
    ["Vacancies to fill", s.vacancies.places, "#/vacancies", t.vacancies > 0],
    ["Needs your review", t.review, "#/review", t.review > 0],
    ["Emails to release", t.release, "#/replies", t.release > 0],
    ["In interview", t.interview, "#/candidates/interview", false],
    ["Selected", t.selected, "#/candidates/selected", false],
  ].map(([label, n, href, need]) => `<a class="num ${need ? "need" : ""}" href="${href}"><b>${esc(n)}</b><span>${esc(label)}</span></a>`).join("");

  // Selected = passed the interview, counted per area
  const passed = {};
  all.filter((c) => c.result === "Pass").forEach((c) => { passed[c.area] = (passed[c.area] || 0) + 1; });
  const areaNames = s.areas_chart.map((a) => a.area);
  Object.keys(passed).forEach((a) => { if (!areaNames.includes(a)) areaNames.push(a); });
  const totalPassed = Object.values(passed).reduce((x, y) => x + y, 0);
  const areaTable = areaNames.length ? `<table class="t"><thead><tr><th>Area</th><th class="num-col">Selected</th></tr></thead><tbody>
      ${areaNames.map((a) => `<tr><td>${esc(a)}</td><td class="num-col">${esc(passed[a] || 0)}</td></tr>`).join("")}
      <tr class="total"><td>All areas</td><td class="num-col">${esc(totalPassed)}</td></tr></tbody></table>` : empty("No areas yet.");

  const recent = s.recent.length ? `<table class="t"><thead><tr><th style="width:110px">Time</th><th>Activity</th></tr></thead><tbody>
      ${s.recent.slice(0, 6).map((r) => `<tr ${r.app_no ? `class="click" data-href="#/candidate/${encodeURIComponent(r.app_no)}"` : ""}><td class="small muted">${esc(fmtTime(r.at))}</td>
        <td>${esc(r.text)}<div class="small muted">${actorText(r.actor)}${r.app_no ? " - " + esc(r.app_no) + " " + esc(r.name || "") : ""}</div></td></tr>`).join("")}</tbody></table>`
    : empty("Nothing has happened yet.");
  const team = `<table class="t"><thead><tr><th>Employee</th><th>Status</th><th>Last run</th></tr></thead><tbody>
      ${Object.values(s.team).map((e) => `<tr><td>${esc(e.name)}</td>
        <td>${s.paused ? "Paused" : e.state === "working" ? "Working: " + esc(e.doing) : "Ready"}</td>
        <td class="small muted">${e.last_run ? esc(fmtTime(e.last_run)) + "<br>" + esc(e.last_note) : "Not yet"}</td></tr>`).join("")}</tbody></table>`;
  const brief = s.brief ? `<p style="margin:0 0 6px">${esc(s.brief.text)}</p><div class="small muted">Chief of staff (${esc(s.brief.written_by)}) - ${esc(fmtTime(s.brief.created_at))}</div>`
    : `<p class="muted" style="margin:0">The Chief of staff writes a brief every morning at 08:00.</p>`;

  setView(`${modelNotice()}
    ${next}
    <div class="nums six">${nums}</div>
    <div class="box">${boxHead("Morning brief", `<button class="btn small" data-act="brief">Write brief now</button>`)}${brief}</div>
    <div class="box">${boxHead("Selected candidates by area", `<span class="small muted">Passed the interview</span> <a class="small" href="#/candidates/selected">View list</a>`)}${areaTable}</div>
    <div class="grid two even">
      <div class="box">${boxHead("Recent activity")}${recent}</div>
      <div class="box">${boxHead("AI team", `<a class="small" href="#/team">Open AI team</a>`)}${team}</div>
    </div>`);
}

const POOL_SHOW = [["all", "All"], ["recent", "Recently added"], ["no_area", "No matching area"], ["archived", "Archived"]];
async function pAdd(arg) {
  title("Add CV");
  UI.candidateFrom = "add";
  if (POOL_SHOW.some((x) => x[0] === arg)) UI.poolShow = arg;
  const qs = new URLSearchParams({ area: UI.poolArea, show: UI.poolShow, q: UI.poolQuery });
  const r = await api(`/api/pool?${qs}`);
  if (UI.poolArea && !r.area) UI.poolArea = "";
  const files = UI.files;
  const results = UI.uploadResults || [];
  const added = new Set(results.filter((x) => x.ok).map((x) => x.app_no));
  const notAdded = results.filter((x) => !x.ok);
  const fileTable = files.length ? `<table class="t" style="margin-top:10px"><thead><tr><th>Chosen file</th><th>Size</th><th></th></tr></thead><tbody>
      ${files.map((f, i) => `<tr><td>${esc(f.name)}</td><td class="small muted">${Math.ceil(f.size / 1024)} KB</td>
        <td style="text-align:right"><button class="btn small" data-act="remove-file" data-i="${i}">Remove</button></td></tr>`).join("")}</tbody></table>` : "";
  const resultLine = results.length ? `<div class="upload-result">
      ${added.size ? `<p class="small" style="margin:0">${added.size} CV(s) added. They are at the top of the list below, marked New. The Recruiter reads each one and fills in its area and score.${MAILMODE() ? " Open a CV to prepare its invitation mail." : ""}</p>` : ""}
      ${notAdded.length ? `<table class="t" style="margin-top:6px"><tbody>${notAdded.map((x) => `<tr><td>${esc(x.file)}</td><td>${tag("Not added", "fail")} <span class="small muted">${esc(x.reason)}</span></td></tr>`).join("")}</tbody></table>` : ""}</div>` : "";

  const st = r.stats;
  const counts = `<span class="small muted">${esc(st.in_pool)} in pool, ${esc(st.being_read)} being read, ${esc(st.added_today)} added today</span>`;
  const hint = UI.poolArea ? `Ranked by score for ${UI.poolArea}. Only CVs scoring ${r.min_score} or more with a related project, job or studies are shown.`
    : UI.poolShow === "recent" ? "CVs added in the last 24 hours, newest first."
    : UI.poolShow === "archived" ? "Archived CVs are not deleted. Open one to restore it."
    : UI.poolShow === "no_area" ? "No area on the list fits these CVs. They are read again when you add or change an area."
    : "All waiting CVs, sorted by their best score. Choose an area to see its ranking.";
  const rows = r.rows.length ? `<table class="t list pool-list"><thead><tr><th class="num-col">Rank</th><th>Candidate</th><th>Best area</th>
      <th class="num-col">${UI.poolArea ? "Score for " + esc(UI.poolArea) : "Score"}</th><th>Other good areas</th><th>Skill gaps</th><th>Added</th><th>Status</th></tr></thead><tbody>
    ${r.rows.map((c) => `<tr class="click" data-href="#/candidate/${encodeURIComponent(c.app_no)}">
      <td class="num-col">${c.rank ?? "-"}</td>
      <td>${esc(c.name || "Reading…")} ${added.has(c.app_no) ? tag("New") : ""}<div class="small muted">${esc(c.app_no)}</div></td>
      <td>${c.best_area ? esc(c.best_area) : c.suggested_new_area ? `<span class="small muted">Suggested: ${esc(c.suggested_new_area)}</span>` : `<span class="muted">-</span>`}</td>
      <td class="num-col">${(UI.poolArea ? c.area_score : c.best_score) ?? "-"}</td>
      <td class="small">${c.other_areas.length ? c.other_areas.map((o) => `${esc(o.area)} ${esc(o.score)}`).join(", ") : `<span class="muted">-</span>`}</td>
      <td class="small muted">${c.gaps.length ? esc(c.gaps.join(", ")) : "-"}</td>
      <td class="small muted">${esc(fmtTime(c.created_at))}</td>
      <td>${statusTag(c)}</td></tr>`).join("")}</tbody></table>`
    : empty(UI.poolArea ? `No CV in the pool fits ${UI.poolArea} yet.` : UI.poolQuery ? "No CV matches your search." : "No CVs here.");

  setView(`<div class="box">${boxHead("Add CVs")}
      <div class="add-grid">
        <div>
          <label class="drop" id="drop" for="file-input"><span class="btn">Choose files</span><div style="margin-top:8px">or drop CV files here</div>
            <div class="small muted">PDF, Word (.docx) or text files, up to 5 MB each. You can add several at once.</div></label>
          <input type="file" id="file-input" accept=".pdf,.docx,.txt" multiple hidden>
          ${fileTable}
        </div>
        <div>
          <div class="form-row"><label for="cv-email">Candidate's email (optional)</label>
            <input type="email" id="cv-email" value="${files.length > 1 ? "" : esc(UI.cvEmail)}" placeholder="${files.length > 1 ? "Only for a single CV" : "name@example.com"}" ${files.length > 1 ? "disabled" : ""}>
            <div class="help">If you type it, replies use this address. If you leave it empty, the Recruiter reads it from the CV and you confirm it.</div></div>
          <div class="form-row"><label for="cv-source">Source (optional)</label>
            <input type="text" id="cv-source" value="${esc(UI.cvSource)}" placeholder="Who sent the CV, for example a name or an email address">
            <div class="help">Saved with every CV in this upload. It is the Source column of the register.</div></div>
          <p class="small muted">${files.length ? `${files.length} file(s) chosen.` : "No files chosen yet."}</p>
          <div class="actions"><button class="btn primary" data-act="upload" ${files.length ? "" : "disabled"}>Add ${files.length > 1 ? files.length + " CVs" : "CV"}</button>
            ${files.length ? `<button class="btn" data-act="clear-files">Clear</button>` : ""}
            <span class="spacer"></span><button class="btn" data-act="samples" title="17 made-up CVs in PDF, Word and text files, for demos and testing">Load the sample CVs</button></div>
        </div>
      </div>${resultLine}${MAILMODE() ? handAddBox() : ""}</div>
    <div class="box">${boxHead("CV pool", counts)}
      <div class="filters"><div class="tabs">${POOL_SHOW.map(([k, l]) => `<button class="${UI.poolShow === k ? "on" : ""}" data-act="pool-show" data-s="${k}">${esc(l)}${k === "no_area" && st.no_area ? ` (${esc(st.no_area)})` : ""}${k === "archived" && st.archived ? ` (${esc(st.archived)})` : ""}</button>`).join("")}</div>
        <select id="pool-area" style="width:auto"><option value="">All areas</option>${r.areas.map((a) => `<option ${a === UI.poolArea ? "selected" : ""}>${esc(a)}</option>`).join("")}</select>
        <input type="search" class="search" id="pool-q" placeholder="Search name or number" value="${esc(UI.poolQuery)}"></div>
      <p class="small muted" style="margin:0 0 8px">${esc(hint)}</p>${rows}</div>`);
  const input = $("#file-input");
  input.onchange = () => { addFiles(input.files); input.value = ""; };
  const drop = $("#drop");
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); addFiles(e.dataTransfer.files); };
  const em = $("#cv-email");
  em.oninput = () => { UI.cvEmail = em.value; };
  const so = $("#cv-source");
  so.oninput = () => { UI.cvSource = so.value; };
  if (MAILMODE()) bindHandAdd();
  $("#pool-area").onchange = (e) => { UI.poolArea = e.target.value; e.target.blur(); lastHTML = ""; pAdd(); };
  const qi = $("#pool-q");
  qi.oninput = () => { UI.poolQuery = qi.value; const pos = qi.selectionStart; lastHTML = ""; pAdd().then(() => { const n = $("#pool-q"); n.focus(); n.setSelectionRange(pos, pos); }); };
}
// Mail-only mode: a student typed in by hand (no CV file, or the model is not running).
const HAND_FIELDS = [["name", "Name", "text", "As it should appear in the mail"], ["email", "Email", "email", "name@example.com"],
  ["university", "University", "text", ""], ["technology", "Technology", "text", "For example Java, AI/ML"], ["source", "Source", "text", "Who sent the student"]];
function handInputs(prefix, values) {
  return `<div class="grid two" style="margin-top:8px">${HAND_FIELDS.map(([k, label, type, ph]) => `<div class="form-row" style="margin:0 0 10px">
    <label for="${prefix}-${k}">${esc(label)}${k === "name" || k === "email" ? " *" : ""}</label>
    <input type="${type}" id="${prefix}-${k}" value="${esc(values[k] || "")}" placeholder="${esc(ph)}"></div>`).join("")}</div>`;
}
function readHand(prefix) {
  const out = {};
  HAND_FIELDS.forEach(([k]) => { out[k] = $(`#${prefix}-${k}`)?.value.trim() || ""; });
  return out;
}
function handAddBox() {
  const down = STATE && !STATE.model.ok;
  const v = UI.hand || { source: UI.cvSource };
  return `<details class="field" id="hand-box" style="margin-top:14px;padding-top:10px;border-top:1px solid var(--line-soft)" ${UI.handOpen || (down && UI.handOpen !== false) ? "open" : ""}>
    <summary class="label" style="cursor:pointer">Add a student without a CV${down ? " (the model is not running)" : ""}</summary>
    ${handInputs("hand", v)}
    <div class="actions"><button class="btn primary" data-act="hand-add">Add student</button>
      <span class="small muted">Use this when there is no CV file, or when the Recruiter cannot read CVs because the model is not running.</span></div></details>`;
}
function bindHandAdd() {
  const box = $("#hand-box");
  if (!box) return;
  box.ontoggle = () => { UI.handOpen = box.open; };
  HAND_FIELDS.forEach(([k]) => { const el = $(`#hand-${k}`); if (el) el.oninput = () => { UI.hand = readHand("hand"); }; });
}
function handFillBox(c) {
  if (!["new", "needs_person"].includes(c.status)) return "";
  const down = STATE && !STATE.model.ok;
  const head = c.status === "needs_person" ? "The Recruiter could not read this CV."
    : down ? "The model is not running, so the Recruiter cannot read this CV." : "The Recruiter is reading this CV.";
  const open = UI.handFor === c.app_no;
  const v = UI.handFill && UI.handFill.app === c.app_no ? UI.handFill : { email: c.email || c.email_in_cv || "", source: c.source || "" };
  return `<div class="box" id="hand-fill">${notice(down || c.status === "needs_person" ? "need" : "ok", head,
      open ? "Type the details from the CV, then click Save. The mail can be sent after that." : "The mail can be sent when it has finished, or you can type the details yourself.",
      open ? "" : `<button class="btn small" data-act="hand-open">Enter details by hand</button>`)}
    ${open ? `${handInputs("handc", v)}${c.email_in_cv && !c.email ? `<div class="help" style="margin-top:-4px">The email was found in the CV text. Check it.</div>` : ""}
      <div class="actions"><button class="btn primary" data-act="hand-fill">Save details</button><button class="btn" data-act="hand-cancel">Cancel</button></div>` : ""}</div>`;
}
function bindHandForm(c) {
  HAND_FIELDS.forEach(([k]) => { const el = $(`#handc-${k}`); if (el) el.oninput = () => { UI.handFill = { app: c.app_no, ...readHand("handc") }; }; });
}
function addFiles(list) {
  for (const f of list) if (!UI.files.some((x) => x.name === f.name && x.size === f.size)) UI.files.push(f);
  UI.uploadResults = null;
  lastHTML = "";
  pAdd();
}

const FILTERS = [["selected", "Selected candidates"], ["", "All"], ["new", "New"], ["interview", "In interview"], ["closed", "Closed"], ["review", "Needs you"], ["release", "Emails to release"]];
async function pCandidates(arg) {
  title("Candidates");
  if (arg !== undefined && arg !== "" && FILTERS.some((f) => f[0] === arg)) UI.candFilter = arg;
  const selected = UI.candFilter === "selected";
  let rows = await api(`/api/candidates?status=${selected ? "" : encodeURIComponent(UI.candFilter)}`);
  if (selected) rows = rows.filter((c) => c.result === "Pass");   // passed the interview
  const q = UI.candQuery.toLowerCase();
  const shown = q ? rows.filter((c) => [c.name, c.app_no, c.area, c.email].join(" ").toLowerCase().includes(q)) : rows;
  const body = shown.length ? `<table class="t list"><thead><tr><th>Application</th><th>Candidate</th><th>Area</th><th class="num-col">Score</th><th>Status</th><th>Next</th></tr></thead><tbody>
    ${shown.map((c) => `<tr class="click" data-href="#/candidate/${encodeURIComponent(c.app_no)}">
      <td><b>${esc(c.app_no)}</b><div class="small muted">${esc(fmtTime(c.created_at))}</div></td>
      <td>${esc(c.name)}<div class="small muted">${esc(c.email || "no email yet")}</div></td>
      <td>${c.area ? esc(c.area) : c.suggested_new_area ? `<span class="small muted">Suggested: ${esc(c.suggested_new_area)}</span>` : `<span class="muted">-</span>`}</td>
      <td class="num-col">${c.score == null ? "-" : esc(c.score)}</td>
      <td>${statusTag(c)} ${c.open_flags ? tag(`${c.open_flags} note(s)`, ROLE === "hr" ? "need" : "") : ""}</td>
      <td class="small muted">${esc(c.next_actor || (c.outcome || "Done"))}</td></tr>`).join("")}</tbody></table>`
    : empty(rows.length ? "No candidate matches your search." : selected ? "No one has passed an interview yet." : "No candidates here.");
  setView(`<div class="box">${boxHead("Candidates", `<a class="btn primary" href="#/add">Add CV</a>`)}
    <div class="filters"><div class="tabs">${FILTERS.map(([k, l]) => `<button class="${UI.candFilter === k ? "on" : ""}" data-act="filter" data-f="${k}">${esc(l)}</button>`).join("")}</div>
    <input type="search" class="search" id="cand-q" placeholder="Search name, number, area" value="${esc(UI.candQuery)}"></div>${body}</div>`);
  const qi = $("#cand-q");
  qi.oninput = () => { UI.candQuery = qi.value; const pos = qi.selectionStart; lastHTML = ""; pCandidates().then(() => { const n = $("#cand-q"); n.focus(); n.setSelectionRange(pos, pos); }); };
}

const VAC_SHOW = [["open", "Open"], ["done", "Filled and closed"], ["all", "All"]];
async function pVacancies(arg) {
  title("Vacancies");
  if (VAC_SHOW.some((x) => x[0] === arg)) UI.vacShow = arg;
  const r = await api(`/api/vacancies?show=${UI.vacShow}`);
  const waiting = r.rows.reduce((n, v) => n + v.waiting, 0);
  const places = r.rows.filter((v) => v.status === "open").reduce((n, v) => n + Math.max(0, v.places - v.filled), 0);
  const nums = [["Open vacancies", r.counts.open, "open"], ["Places to fill", places, "open"],
    ["Waiting for you", waiting, "open"], ["Filled", r.counts.filled, "done"]]
    .map(([label, n, show]) => `<a class="num ${label === "Waiting for you" && n ? "need" : ""}" href="#/vacancies/${show}"><b>${esc(n)}</b><span>${esc(label)}</span></a>`).join("");
  const card = (v) => {
    const rows = v.suggestions.map((sg) => `<tr>
      <td>${candLink(sg.app_no, sg.name)}<div class="small muted">${esc(sg.app_no)}</div></td>
      <td class="num-col">${esc(sg.score ?? "-")}</td>
      <td class="small muted">${sg.gaps && sg.gaps.length ? esc(sg.gaps.join(", ")) : "-"}</td>
      <td>${sg.state === "passed" ? tag(sg.state_label, "pass") : sg.state === "suggested" ? tag(sg.state_label, "need") : tag(sg.state_label)}
        ${sg.work_id ? `<div class="small muted">Work ID ${esc(sg.work_id)}</div>` : sg.state === "approved" && sg.status_label ? `<div class="small muted">${esc(sg.status_label)}</div>` : ""}</td>
      <td style="text-align:right;white-space:nowrap">${sg.state === "suggested" && v.status === "open"
        ? `<button class="btn small primary" data-act="vac-approve" data-v="${v.id}" data-a="${esc(sg.app_no)}" data-n="${esc(sg.name)}" data-area="${esc(v.area)}">Approve</button>
           <button class="btn small" data-act="vac-decline" data-v="${v.id}" data-a="${esc(sg.app_no)}" data-n="${esc(sg.name)}">Not this one</button>` : ""}</td></tr>`).join("");
    const head = `<span class="tag ${v.status === "filled" ? "pass" : ""}">${esc(v.status === "open" ? `${v.filled} of ${v.places} filled` : v.status === "filled" ? "Filled" : "Closed")}</span>
      <span class="small muted">From ${esc(v.opens_on)} - opened by ${esc(v.created_by)}</span>
      ${v.status === "open" ? `<button class="btn small" data-act="vac-close" data-v="${v.id}" data-area="${esc(v.area)}">Close</button>` : ""}`;
    return `<div class="box">${boxHead(`${v.area} - ${v.places} place(s)`, head)}
      <p class="small muted" style="margin:0 0 10px">${esc(v.reason)}</p>
      ${v.suggestions.length ? `<table class="t vac-list"><thead><tr><th>Suggested candidate</th><th class="num-col">Score for ${esc(v.area)}</th><th>Skill gaps</th><th>State</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
        : empty(`No CV in the pool fits ${v.area} yet. A CV is suggested only for its best area, or a second area that is nearly as strong.`)}</div>`;
  };
  setView(`<div class="nums four">${nums}</div>
    <div class="box">${boxHead("Vacancies", `<button class="btn primary" data-act="vac-new">Add vacancy</button>`)}
      <div class="filters"><div class="tabs">${VAC_SHOW.map(([k, l]) => `<button class="${UI.vacShow === k ? "on" : ""}" data-act="vac-show" data-s="${k}">${esc(l)}</button>`).join("")}</div></div>
      <p class="small muted" style="margin:0">A place opens ${esc(r.notice_days)} days before an intern's last day. For each vacancy the Planner suggests the best waiting CVs for that area, plus ${esc(r.reserves)} reserves. Nobody is contacted until you approve.</p></div>
    ${r.rows.length ? r.rows.map(card).join("") : `<div class="box">${empty(UI.vacShow === "open" ? "No open vacancies. The Planner checks the intern records every morning." : "Nothing here yet.")}</div>`}`);
}

function vacancyForm(areas) {
  openDialog(`<h3>Add vacancy</h3><p class="small muted">For a place the intern records do not show yet, for example when someone leaves early.</p>
    <div class="form-row"><label for="vf-area">Area</label><select id="vf-area">${areas.map((a) => `<option>${esc(a)}</option>`).join("")}</select></div>
    <div class="form-row"><label for="vf-places">How many places</label><input type="text" id="vf-places" value="1"></div>
    <div class="form-row"><label for="vf-date">Open from</label><input type="text" id="vf-date" placeholder="YYYY-MM-DD, or leave empty for today"></div>
    <div class="form-row"><label for="vf-reason">Reason</label><textarea id="vf-reason" rows="2" style="min-height:50px" placeholder="For example: an intern left early."></textarea></div>
    <div class="actions"><button class="btn" data-dlg="cancel">Cancel</button><button class="btn primary" data-dlg="save">Add vacancy</button></div>`);
  const d = $("#dialog");
  d.querySelector('[data-dlg="cancel"]').onclick = closeDialog;
  d.querySelector('[data-dlg="save"]').onclick = async () => {
    try {
      await api("/api/vacancies", { method: "POST", body: { area: $("#vf-area").value, places: $("#vf-places").value,
        opens_on: $("#vf-date").value, reason: $("#vf-reason").value } });
      closeDialog(); toast("Vacancy added."); lastHTML = ""; render();
    } catch (e) { toast(e.message, true); }
  };
}

const INTERN_SHOW = [["all", "All"], ["soon", "Ending soon"], ["unknown", "Unknown area"], ["finished", "Finished"]];
async function pInterns(arg) {
  title("Interns");
  if (INTERN_SHOW.some((x) => x[0] === arg)) UI.internShow = arg;
  const r = await api(`/api/interns?show=${UI.internShow}&q=${encodeURIComponent(UI.internQuery)}`);
  const st = r.stats;
  const nums = [["Interns", st.total, "all"], [`Ending in ${r.notice_days} days`, st.ending_soon, "soon"],
    ["Finished", st.finished, "finished"], ["Unknown area", st.unknown_area, "unknown"]]
    .map(([label, n, show]) => `<a class="num" href="#/interns/${show}"><b>${esc(n)}</b><span>${esc(label)}</span></a>`).join("");
  const rows = r.rows.length ? `<table class="t intern-list"><thead><tr><th>Trainee ID</th><th>Name</th><th>Specialization</th><th>Area</th><th>Start</th><th>Last day</th><th>State</th></tr></thead><tbody>
      ${r.rows.map((i) => `<tr><td>${esc(i.trainee_id)}</td><td>${esc(i.name || "-")}</td><td>${esc(i.specialization || "-")}</td>
        <td>${i.area ? esc(i.area) : `<span class="tag need">No matching area</span>`}</td>
        <td class="small muted">${esc(i.start_date || "-")}</td><td class="small muted">${esc(i.end_date || "not set")}</td>
        <td>${i.ending_soon ? tag("Ending soon", "need") : i.finished ? tag("Finished") : tag("Working")}</td></tr>`).join("")}</tbody></table>`
    : empty(UI.internQuery ? "No intern matches your search." : "No intern records yet. Use Sync now to read them.");
  setView(`<div class="nums four">${nums}</div>
    <div class="box">${boxHead("Intern records from Talent Hub", `<button class="btn primary" data-act="sync-interns">Sync now</button>`)}
      <div class="filters"><div class="tabs">${INTERN_SHOW.map(([k, l]) => `<button class="${UI.internShow === k ? "on" : ""}" data-act="intern-show" data-s="${k}">${esc(l)}</button>`).join("")}</div>
        <input type="search" class="search" id="intern-q" placeholder="Search name or trainee ID" value="${esc(UI.internQuery)}"></div>
      <p class="small muted" style="margin:0 0 8px">Read only: the system never writes to Talent Hub. Source: ${esc(r.source === "mongodb" ? "Talent Hub (MongoDB)" : "example records, for testing")}. ${r.synced_at ? "Last read " + esc(fmtTime(r.synced_at)) + "." : "Not read yet."}</p>${rows}</div>`);
  const qi = $("#intern-q");
  qi.oninput = () => { UI.internQuery = qi.value; const pos = qi.selectionStart; lastHTML = ""; pInterns().then(() => { const n = $("#intern-q"); n.focus(); n.setSelectionRange(pos, pos); }); };
}

function nextStep(c, areas) {
  const areaSelect = (label) => `<select id="area-pick" style="width:auto;max-width:220px">${areas.filter((a) => a.active).map((a) => `<option ${a.name === c.area ? "selected" : ""}>${esc(a.name)}</option>`).join("")}</select>
      <button class="btn ${label ? "primary" : "small"}" data-act="set-area">${esc(label || "Use this area")}</button>`;
  const scoreOf = {};
  (c.area_scores || []).forEach((x) => { scoreOf[x.area] = x.score; });
  const inviteSelect = () => `<select id="invite-area" style="width:auto;max-width:260px">${(c.active_areas || []).slice()
      .sort((a, b) => (scoreOf[b] ?? -1) - (scoreOf[a] ?? -1))
      .map((a) => `<option value="${esc(a)}" ${a === c.area ? "selected" : ""}>${esc(a)}${scoreOf[a] != null ? " (" + esc(scoreOf[a]) + ")" : ""}</option>`).join("")}</select>
      <button class="btn primary" data-act="invite">Invite to interview</button>`;
  switch (c.status) {
    case "new": return notice("ok", "The Recruiter will read this CV in a moment.", "The page updates by itself.");
    case "in_pool": return notice("ok", "Waiting in the pool.", `Best area: ${c.area} (${c.score}/100). Invite this candidate when there is a place. The Coordinator then books the interview.`,
      `<div class="actions">${inviteSelect()}<button class="btn" data-act="archive">Archive</button></div>`);
    case "no_area": return notice("ok", "No matching area.",
      (c.suggested_new_area ? `The AI suggested a new area: "${c.suggested_new_area}". ` : "") + "The CV is read again when you add or change an area. You can still invite it for an area.",
      `<div class="actions">${c.suggested_new_area ? `<a class="btn" href="#/areas/new:${encodeURIComponent(c.suggested_new_area)}">Add "${esc(c.suggested_new_area)}"</a>` : ""}${inviteSelect()}<button class="btn" data-act="archive">Archive</button></div>`);
    case "archived": return notice("ok", "Archived.", `${c.outcome || "Removed from the pool"}. Restore it to put it back in the pool.`,
      `<button class="btn" data-act="restore">Restore to pool</button>`);
    case "needs_person": return notice("need", "This CV could not be read.", "It may be a scanned PDF. Upload it again as Word or text, or try again.",
      `<div class="actions"><button class="btn primary" data-act="retry">Try again</button><button class="btn danger" data-act="close">Close</button></div>`);
    case "needs_area": return notice("need", "Choose the area for this candidate.",
      c.suggested_new_area ? `The AI suggested a new area: "${c.suggested_new_area}". Add it on the Areas page first if you agree.` : "The Recruiter could not choose one with confidence.",
      `<div class="actions">${areaSelect("Save area")}${c.suggested_new_area ? `<a class="btn" href="#/areas/new:${encodeURIComponent(c.suggested_new_area)}">Add "${esc(c.suggested_new_area)}"</a>` : ""}<button class="btn danger" data-act="close">Close</button></div>`);
    case "borderline": return notice("need", `Score ${c.score}/100 is borderline (50 to 59). Please decide.`, "",
      `<div class="actions"><button class="btn primary" data-act="decide" data-d="shortlist">Shortlist</button><button class="btn" data-act="decide" data-d="not_shortlist">Not shortlisted</button></div>`)
      + `<div class="actions small muted" style="margin-top:8px">Wrong area? ${areaSelect("")}</div>`;
    case "not_shortlisted_check": return notice("need", `Score ${c.score}/100 is below 50. Confirm Not shortlisted?`, "A polite reply is drafted only after you confirm.",
      `<div class="actions"><button class="btn primary" data-act="decide" data-d="confirm">Confirm Not shortlisted</button><button class="btn" data-act="decide" data-d="shortlist">Shortlist instead</button></div>`)
      + `<div class="actions small muted" style="margin-top:8px">Wrong area? ${areaSelect("")}</div>`;
    case "invite_ready": case "reply_ready": case "hold_note_ready": case "offer_ready":
      return notice("need", "An email is waiting for your release.", "Read it below, edit it if needed, then release it.", `<a class="btn" href="#emails" data-act="jump">Go to the email</a>`);
    case "awaiting_result": return notice("ok", "Waiting for the interviewer's result.", `Interview: ${c.interview_label}.`);
    case "on_hold": return notice("ok", "The result is on hold.", "The interviewer will enter Pass or Fail later.");
    case "closed": return notice("ok", `Closed: ${c.outcome || "done"}.`);
    default: return notice("ok", `The ${c.next_actor} is working on this.`, "The page updates by itself.");
  }
}

async function pCandidate(appNo) {
  title("Candidate");
  if (MAILMODE() && !UI.mailFiles) {
    try { const s = await api("/api/mail/settings"); UI.mailFiles = s.attachments; UI.mailStatuses = s.statuses; UI.refSuggestion = s.ref_suggestion; } catch { /* shown as empty */ }
  }
  const [c, areas] = await Promise.all([api(`/api/candidates/${encodeURIComponent(appNo)}`), UI.areas ? Promise.resolve(UI.areas) : api("/api/areas")]);
  UI.areas = areas;
  const p = c.profile || {};
  const list = (items, none) => (items && items.length ? `<ul class="clean">${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<span class="muted">${esc(none)}</span>`);
  const flags = c.flags.filter((f) => f.open);
  const parts = Object.entries(c.score_parts || {});
  const scoreTable = parts.length ? `<table class="t score-table"><thead><tr><th>Part</th><th>Why</th><th class="num-col">Points</th></tr></thead><tbody>
      ${parts.map(([k, v]) => `<tr><td>${esc(k[0].toUpperCase() + k.slice(1))}</td><td class="small muted">${esc(v.why)}</td><td class="num-col">${esc(v.points)} / ${esc(v.max)}</td></tr>`).join("")}
      <tr class="total"><td>Total</td><td></td><td class="num-col">${esc(c.score)} / 100</td></tr></tbody></table>
      <div class="small muted" style="margin-top:6px">60 or more: shortlisted. 50 to 59: you decide. Below 50: not shortlisted.</div>` : "";
  const emailField = UI.editEmail
    ? `<div class="actions"><input type="email" id="email-input" value="${esc(c.email)}" style="max-width:280px"><button class="btn primary small" data-act="save-email">Save</button><button class="btn small" data-act="cancel-email">Cancel</button></div>`
    : `<div>${c.email ? esc(c.email) : `<span class="muted">No email yet</span>`} ${ROLE === "hr" ? `<button class="btn link small" data-act="edit-email">${c.email ? "Change" : "Add email"}</button>` : ""}</div>
       ${c.email_source ? `<div class="small muted">${esc(c.email_source)}</div>` : ""}`;
  const hasInterview = c.interview_label || (c.questions && c.questions.length) || c.result;
  const inPool = ["new", "in_pool", "no_area", "archived"].includes(c.status);
  if (UI.candidateInPool !== inPool) { UI.candidateInPool = inPool; renderFrame(); }
  const allScores = (c.area_scores || []).length ? `<details class="field"><summary class="label" style="cursor:pointer">Scores for every area (${c.area_scores.length})</summary>
      <table class="t"><tbody>${c.area_scores.map((x) => `<tr><td>${esc(x.area)}</td><td class="num-col">${esc(x.score)}</td></tr>`).join("")}</tbody></table></details>` : "";
  const back = MAILMODE() ? (UI.candidateFrom === "register" ? `<a class="back" href="#/register">&larr; Register</a>` : `<a class="back" href="#/add">&larr; CV pool</a>`)
    : inPool ? `<a class="back" href="#/add">&larr; CV pool</a>` : `<a class="back" href="#/candidates">&larr; All candidates</a>`;
  setView(`${back}
    <div class="box"><div class="box-head"><h2 style="font-size:18px">${esc(c.name)}</h2><div class="spacer"></div>
      ${c.cv_filename ? `<a class="btn" href="/api/candidates/${encodeURIComponent(c.app_no)}/cv">Download CV</a>` : `<span class="small muted">Added by hand, no CV file</span>`}</div>
      <div class="actions">${esc(c.app_no)} ${statusTag(c)}${c.area ? ` <span>Area: ${esc(c.area)}</span>` : ""}${c.score != null ? ` <span>Score: ${esc(c.score)}/100</span>` : ""}${c.work_id ? ` <b>Work ID ${esc(c.work_id)}</b>` : ""}</div></div>
    ${MAILMODE() ? handFillBox(c) : ROLE === "hr" ? nextStep(c, areas.areas) : notice("ok", "You are viewing this as the Interviewer.", "Open it from the Interviews page to enter a result.")}
    ${flags.length ? `<div class="box">${boxHead("Notes to check", `<span class="small muted">From the AI team</span>`)}<table class="t"><tbody>
      ${flags.map((f) => `<tr><td>${esc(f.text)}<div class="small muted">${esc(f.by)} - ${esc(fmtTime(f.at))}</div></td>
        <td style="text-align:right">${ROLE === "hr" ? `<button class="btn small" data-act="resolve" data-i="${f.index}">Mark as checked</button>` : ""}</td></tr>`).join("")}</tbody></table></div>` : ""}
    <div class="grid two">
      <div class="box">${boxHead("Profile", c.profile_source ? byText(c.profile_source === "AI" ? "read by AI, checked against the CV" : "made by rules") : "")}
        <div class="field"><div class="label">Email</div>${emailField}</div>
        <div class="field"><div class="label">Phone</div><div>${esc(p.phone || "-")}</div></div>
        <div class="field"><div class="label">Education</div>${list(p.education, "Not found")}</div>
        <div class="field"><div class="label">Skills</div>${p.skills && p.skills.length ? esc(p.skills.join(", ")) : `<span class="muted">Not found</span>`}</div>
        <div class="field"><div class="label">Projects</div>${list(p.projects, "Not found")}</div>
        <div class="field"><div class="label">Experience</div>${list(p.experience, "No experience listed")}</div>
        <div class="actions" style="margin-top:12px"><button class="btn small" data-act="toggle-cv">${UI.showCv ? "Hide" : "Show"} the CV text</button><span class="small muted">${esc(c.cv_filename)}</span></div>
        ${UI.showCv ? `<div class="cvtext">${esc(c.cv_text)}</div>` : ""}</div>
      <div class="box">${boxHead("Area and score", c.area_source ? byText(c.area_source) : "")}
        <div class="field"><div class="label">Area</div>${c.area ? `<div class="big">${esc(c.area)}</div>` : `<div class="muted">${c.suggested_new_area ? "AI suggested a new area: " + esc(c.suggested_new_area) : "Not chosen yet"}</div>`}</div>
        ${c.area_reasons.length ? `<div class="field"><div class="label">Why</div>${list(c.area_reasons, "")}</div>` : ""}
        ${c.area_evidence.length ? `<div class="field"><div class="label">Evidence from the CV</div>${c.area_evidence.map((e) => `<div class="quote">"${esc(e)}"</div>`).join("")}</div>` : ""}
        ${c.score != null ? `<div class="field"><div class="label">Score (calculated by code)</div>${scoreTable}</div>` : ""}
        ${c.gaps.length ? `<div class="field"><div class="label">Skill gaps</div>${esc(c.gaps.join(", "))}</div>` : ""}
        ${allScores}</div>
    </div>
    ${hasInterview ? `<div class="box">${boxHead("Interview", c.questions_source ? byText(c.questions_source === "AI" ? "questions by AI" : c.questions_source) : "")}
      <div class="grid two"><div><div class="field"><div class="label">Date and time</div><div>${esc(c.interview_label || "-")}</div></div>
        <div class="field"><div class="label">Result</div><div>${c.result ? `${resultTag(c.result)} <span class="small muted">${esc(fmtTime(c.result_at))} - decided by the interviewer</span>` : `<span class="muted">Not entered yet</span>`}</div></div>
        ${c.result_comment ? `<div class="field"><div class="label">Interviewer's comment</div><div>${esc(c.result_comment)}</div></div>` : ""}</div>
        <div><div class="label">Questions for the interviewer</div>${c.questions.length ? `<ol class="clean">${c.questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ol>` : `<span class="muted">-</span>`}</div></div></div>` : ""}
    ${MAILMODE() && c.invite ? inviteBox(c) + registerBox(c) : ""}
    ${c.letter ? letterBox(c) : ""}
    ${c.drafts.length ? `<div class="box" id="emails">${boxHead("Emails", `<button class="btn small" data-act="outlook-settings">Outlook settings</button>`)}${c.drafts.slice().reverse().map((d) => mailBlock(d)).join("")}</div>` : ""}
    ${c.work_id ? `<div class="box">${boxHead("Onboarding")}
      <div class="grid two"><div><div class="label">Work ID</div><div class="big">${esc(c.work_id)}</div><div class="small muted">Issued by plain code after the Pass. Never reused.</div></div>
      <div><div class="label">Onboarding checklist</div>${c.checklist.length ? `<ul class="clean">${c.checklist.map((i) => `<li>${esc(i.item)}</li>`).join("")}</ul>` : `<span class="muted">Being prepared</span>`}</div></div></div>` : ""}
    <div class="box">${boxHead("Timeline", `<button class="btn small" data-act="toggle-runs">${UI.showRuns ? "Hide" : "Show"} AI steps</button>`)}
      <table class="t"><thead><tr><th style="width:120px">Time</th><th style="width:170px">Who</th><th>What</th></tr></thead><tbody>
      ${c.events.slice().reverse().map((e) => `<tr><td class="small muted">${esc(fmtTime(e.at))}</td><td>${actorText(e.actor)}</td><td>${esc(e.text)}</td></tr>`).join("")}</tbody></table>
      ${UI.showRuns ? `<table class="t" style="margin-top:12px"><thead><tr><th>Time</th><th>Employee</th><th>Routine</th><th>Outcome</th><th class="num-col">Model calls</th><th class="num-col">Seconds</th></tr></thead><tbody>
        ${c.runs.map((r) => `<tr><td class="small muted">${esc(fmtTime(r.at))}</td><td>${esc(r.employee)}</td><td>${esc(r.routine)}</td><td>${outcomeText(r.outcome)}${r.note ? `<div class="small muted">${esc(r.note)}</div>` : ""}</td><td class="num-col">${esc(r.model_calls)}</td><td class="num-col">${esc(r.seconds)}</td></tr>`).join("")}</tbody></table>` : ""}</div>`);
  view.dataset.app = c.app_no;
  if (MAILMODE()) bindInvite(c);
  const ls = $("#letter-start"), le = $("#letter-end");
  if (ls && le) {      // keep typed dates when the page refreshes itself
    const keepDates = () => { UI.letterDates = { app: c.app_no, start: ls.value, end: le.value }; };
    ls.oninput = ls.onchange = le.oninput = le.onchange = keepDates;
  }
}
// ---------------------------------------------------------------- mail-only mode
const MAIL_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
const WEEKDAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
function visitText(iso) {            // 29-DEC-2026  (Tuesday), the same as the server
  if (!iso) return "";
  const d = new Date(iso + "T00:00:00");
  return `${String(d.getDate()).padStart(2, "0")}-${MAIL_MONTHS[d.getMonth()]}-${d.getFullYear()}  (${WEEKDAY_NAMES[d.getDay()]})`;
}
function shortDate(iso) {            // 5-Sep-26, like the section's sheet
  if (!iso) return "";
  const d = new Date(iso + "T00:00:00");
  return `${d.getDate()}-${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getMonth()]}-${String(d.getFullYear()).slice(2)}`;
}
const REG_TAG = { "Not mailed": "need", "Opened, not confirmed": "need", "Not coming": "fail", "Confirmed": "pass", "Visited": "pass" };
function regTag(s) { return tag(s, REG_TAG[s] || ""); }

function inviteValues(c) {
  return UI.inviteEdit && UI.inviteEdit.app === c.app_no ? UI.inviteEdit : { app: c.app_no, ...c.invite };
}
const HOW_LABEL = { outlook_app: "Mail app", eml: ".eml file", graph: "Microsoft Outlook", graph_send: "Microsoft Outlook",
  graph_draft: "Outlook Drafts", smtp: "SMTP", failed: "Failed" };
function inviteHistory(inv) {
  const h = inv.history || [];
  if (!h.length) return "";
  return `<details class="field"><summary class="label" style="cursor:pointer">Send history (${h.length})</summary>
    <table class="t"><thead><tr><th>Time</th><th>How</th><th>To</th><th>Result</th></tr></thead><tbody>
    ${h.slice().reverse().map((x) => `<tr><td class="small muted">${esc(fmtTime(x.at))}</td><td>${esc(HOW_LABEL[x.how] || x.how)}</td>
      <td>${esc(x.sent_to || "")}${x.test ? " " + tag("Test inbox") : ""}</td>
      <td class="small">${x.how === "failed" ? `<span style="color:var(--fail)">${esc(x.error || "Failed")}</span>`
        : x.status === "sent" ? tag("Sent", "pass") : x.status === "opened" ? tag("Opened, not confirmed", "need") : esc(x.status || "")}</td></tr>`).join("")}
    </tbody></table></details>`;
}
function inviteBox(c) {
  const v = inviteValues(c);
  const inv = c.invite;
  const url = `/api/candidates/${encodeURIComponent(c.app_no)}/mail/preview`;
  const files = UI.mailFiles || [];
  const to = esc(inv.sent_to || c.email);
  const emlLink = inv.file ? `<a class="btn" href="/api/candidates/${encodeURIComponent(c.app_no)}/mail/eml">Download .eml</a>` : "";
  const outlookLink = inv.web_link ? `<a class="btn" href="${esc(inv.web_link)}" target="_blank" rel="noopener">Open in Outlook</a>` : "";
  const fields = (when, label) => `
      <div class="field"><div class="label">To</div><div>${esc(c.name)} &lt;${to}&gt;</div></div>
      <div class="field"><div class="label">Subject</div><div>${esc(inv.subject)}</div></div>
      <div class="field"><div class="label">${label}</div><div>${esc(fmtTime(when))}</div></div>
      <div class="field"><div class="label">Attachments</div><div>${inv.attachments.length ? esc(inv.attachments.join(", ")) : `<span class="muted">No attachments</span>`}</div></div>`;
  if (inv.status === "sent") {
    const how = inv.how || "eml";
    let sentInfo = "";
    if (inv.confirmed_at) sentInfo = ` Sent from ${esc(inv.opened_in || HOW_LABEL[how] || "your mail app")} (you confirmed it).`;
    else if (how === "outlook_app") sentInfo = ` Sent by ${esc(inv.opened_in || "Outlook")} to ${to}.`;
    else if (how === "graph" || how === "graph_send") sentInfo = ` Sent via Microsoft Outlook${inv.sender ? " from " + esc(inv.sender) : ""} to ${to}.`;
    else if (how === "smtp") sentInfo = ` Sent by SMTP (${esc(inv.sender || "SMTP")}) to ${to}.`;
    if (inv.file) sentInfo += ` Copy saved as data/outbox/${esc(inv.file)}.`;
    return `<div class="box" id="invite">${boxHead("Invitation mail", tag("Mail sent", "pass"))}
      ${fields(inv.sent_at, "Sent")}
      <p class="small muted" style="margin:0">${sentInfo}</p>
      ${inviteHistory(inv)}
      <div class="actions"><a class="btn" href="${url}" target="_blank" rel="noopener">View the mail</a>${inv.file ? `<button class="btn" data-act="inv-open">Open in Outlook / Mail app</button>` : ""}${outlookLink}${emlLink}
        <span class="spacer"></span><button class="btn" data-act="inv-resend">Resend</button></div></div>`;
  }
  if (inv.status === "opened") {
    const how = inv.how || "eml";
    const what = inv.saved_only ? notice("error", "No mail app could open this mail.", inv.note || "Download the .eml file and open it in your mail app, send it there, then click I sent it.")
      : how === "graph_draft" ? notice("need", "Saved in your Outlook Drafts folder.", "Send it from Outlook, then click I sent it here.")
      : how === "eml" ? notice("need", "Saved as an .eml file.", "Open it in your mail app and send it, then click I sent it here.")
      : notice("need", `Opened in ${inv.opened_in || "your mail app"}. Did you click Send there?`,
          (inv.note ? inv.note + " " : "") + "The register shows the mail date only after you click I sent it.");
    return `<div class="box" id="invite">${boxHead("Invitation mail", tag("Opened, not confirmed", "need"))}
      ${what}
      ${fields(inv.opened_at, "Opened")}
      ${inviteHistory(inv)}
      <div class="actions"><button class="btn primary" data-act="inv-confirm">I sent it</button>
        ${inv.file && how !== "graph_draft" ? `<button class="btn" data-act="inv-open">Open again</button>` : ""}${outlookLink}${emlLink}
        <a class="btn" href="${url}" target="_blank" rel="noopener">View the mail</a>
        <span class="spacer"></span><button class="btn" data-act="inv-back">Not sent: edit mail</button></div></div>`;
  }
  const ol = STATE?.outlook || {};
  const testTo = ol.test_recipient || "";
  const noEmail = !c.email ? notice("need", "No email address yet.", "Add it in the Profile box before sending.") : "";
  const failed = inv.last_error ? notice("error", `The last try did not send (${fmtTime(inv.last_error_at)}).`, `${inv.last_error} Fix it, then click Send mail again.`) : "";
  const lt = inv.last_test;
  const tested = lt ? notice("ok", `Test copy ${lt.status === "sent" ? "sent" : "opened"} for ${lt.sent_to} (${fmtTime(lt.at)}).`, "The student has not received this mail. Turn the test inbox off in Mail settings to send it for real.") : "";
  const resending = inv.resend_of ? notice("ok", `Resending the mail first sent ${fmtTime(inv.resend_of)}.`, "Change it if needed, then click Send mail.") : "";
  const L = c.mail_links || {};
  const textOnly = c.email && (L.gmail || L.outlook_office) ? `<div class="small muted" style="margin-top:8px">Or write it yourself in
      ${L.gmail ? `<a href="${esc(L.gmail)}" target="_blank" rel="noopener">Gmail</a>` : ""}${L.gmail && L.outlook_office ? " or " : ""}${L.outlook_office ? `<a href="${esc(L.outlook_office)}" target="_blank" rel="noopener">Outlook on the web</a>` : ""}
      (uses the saved text; add the attachments yourself, then click Send mail here or mark it sent later).</div>` : "";
  return `<div class="box" id="invite">${boxHead("Invitation mail", tag(inv.status === "draft" ? "Saved, not sent" : "Not sent yet", "need"))}
    ${noEmail}${failed}${tested}${resending}
    <div class="grid two" style="align-items:start">
      <div class="form-row" style="margin:0"><label>To</label><div>${c.email ? `${esc(c.name)} &lt;${esc(c.email)}&gt;` : `<span class="muted">No email yet</span>`}</div></div>
      <div class="form-row" style="margin:0"><label for="inv-date">Visit date</label><input type="date" id="inv-date" value="${esc(v.visit_date || "")}">
        <div class="help">Suggested: the next Tuesday. Changing it updates the date in the subject and the mail.</div></div>
    </div>
    <div class="form-row"><label for="inv-subject">Subject</label><input type="text" id="inv-subject" value="${esc(v.subject)}"></div>
    <div class="form-row"><label for="inv-body">Mail</label><textarea id="inv-body" rows="22" style="font-family:inherit;font-size:13.5px">${esc(v.body)}</textarea>
      <div class="help">Edit anything. Links become clickable in the sent mail.</div></div>
    <div class="form-row"><label>Attachments</label>
      ${files.length ? files.map((f) => `<label class="check"><input type="checkbox" class="inv-file" value="${esc(f.name)}" ${(v.attachments || []).includes(f.name) ? "checked" : ""}> ${esc(f.name)} <span class="small muted">${fmtSize(f.size)}</span></label>`).join("")
        : `<div class="muted">No attachments uploaded yet. Add them in <a href="#/mailsettings">Mail settings</a>.</div>`}</div>
    ${inviteHistory(inv)}
    <div class="actions"><button class="btn" data-act="inv-save">Save</button><button class="btn" data-act="inv-preview">Preview</button>
      <span class="spacer"></span>${testTo ? `<span class="small" style="color:var(--fail)">Test inbox is on</span>` : ""}
      <button class="btn primary" data-act="inv-send" ${c.email && c.status !== "new" ? "" : "disabled"}>${testTo ? `Send to TEST inbox (${esc(testTo)})` : "Send mail"}</button></div>
    ${textOnly}</div>`;
}
function fmtSize(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB"; }
function readInvite(app) {
  return { app, visit_date: $("#inv-date").value, subject: $("#inv-subject").value, body: $("#inv-body").value,
           attachments: [...document.querySelectorAll(".inv-file:checked")].map((x) => x.value) };
}
function bindInvite(c) {
  const d = $("#inv-date");
  if (d) {
    const keep = () => { UI.inviteEdit = readInvite(c.app_no); };
    ["#inv-subject", "#inv-body"].forEach((s) => { $(s).oninput = keep; });
    document.querySelectorAll(".inv-file").forEach((x) => { x.onchange = keep; });
    let before = d.value;
    d.onchange = () => {
      const oldText = visitText(before), newText = visitText(d.value);
      const sub = $("#inv-subject"), body = $("#inv-body");
      if (newText) {
        let found = false;
        for (const el of [sub, body]) {
          for (const old of [oldText, "{visit_date}"]) {
            if (old && el.value.includes(old)) { el.value = el.value.split(old).join(newText); found = true; }
          }
        }
        if (!found) toast("The old date was not found in the mail. Check the Date line yourself.", true);
      }
      before = d.value;
      keep();
    };
  }
  document.querySelectorAll("#reg .grid input, #reg .grid select").forEach((x) => { x.oninput = x.onchange = () => { UI.regEdit = readRegister(c.app_no); }; });
  const rt = $("#reply-text"), rb = $("#reply-box");
  if (rt) rt.oninput = () => { UI.replyFor = c.app_no; UI.replyText = rt.value; };
  if (rb) rb.ontoggle = () => { UI.replyFor = c.app_no; UI.replyOpen = rb.open; };
  bindHandForm(c);
}
const REG_FIELDS = [["ref_no", "No (reference)"], ["source", "Source"], ["university", "University"], ["technology", "Technology"],
  ["name", "Name"], ["nic", "NIC / PP"], ["address", "Address / Company"], ["vehicle", "Vehicle No"], ["tel", "Tel No"], ["laptop", "Laptop Serial No"]];
function readRegister(app) {
  const out = { app };
  REG_FIELDS.forEach(([k]) => { out[k] = $(`#reg-${k}`).value; });
  out.status = $("#reg-status").value;
  return out;
}
function registerBox(c) {
  const r = UI.regEdit && UI.regEdit.app === c.app_no ? { ...c.register, ...UI.regEdit } : c.register;
  const st = UI.mailStatuses || ["Not mailed", "Opened, not confirmed", "Mail sent", "Replied", "Confirmed", "Not coming", "Visited"];
  const sug = UI.refSuggestion && !r.ref_no ? `<button class="btn link small" data-act="reg-ref" data-ref="${esc(UI.refSuggestion)}">Use next: ${esc(UI.refSuggestion)}</button>` : "";
  const sugg = UI.regSuggested && UI.regSuggested.app === c.app_no ? UI.regSuggested.keys : [];
  const hl = (k) => (sugg.includes(k) ? ` style="background:var(--need-bg);border-color:var(--need-line)"` : "");
  const reply = UI.replyFor === c.app_no ? UI.replyText : "";
  return `<div class="box" id="reg">${boxHead("Register details", regTag(c.register.status))}
    <p class="small muted" style="margin-top:0">One row of the intern register. Fill NIC, Tel No and Laptop Serial No from the student's reply.
      Mail date: <b>${c.register.mail_date ? esc(shortDate(c.register.mail_date)) : "not sent yet"}</b></p>
    <details class="field" id="reply-box" ${UI.replyFor === c.app_no && UI.replyOpen ? "open" : ""}><summary class="label" style="cursor:pointer">Paste the student's reply</summary>
      <textarea id="reply-text" rows="6" style="margin-top:6px" placeholder="Paste the student's reply mail here">${esc(reply)}</textarea>
      <div class="actions" style="margin-top:6px"><button class="btn small" data-act="reply-read">Fill the details from the reply</button>
        <span class="small muted">The details found are marked. Check them, then click Save details.</span></div></details>
    <div class="grid two">${REG_FIELDS.map(([k, label]) => `<div class="form-row" style="margin:0 0 10px"><label for="reg-${k}">${esc(label)}</label>
      <input type="text" id="reg-${k}" value="${esc(r[k] || "")}"${hl(k)}>${k === "ref_no" ? sug : ""}${k === "nic" && c.register.nic_hint ? `<div class="help">${esc(c.register.nic_hint)}</div>` : ""}</div>`).join("")}
      <div class="form-row" style="margin:0 0 10px"><label for="reg-status">Status</label><select id="reg-status"${hl("status")}>${st.map((s) => `<option ${s === r.status ? "selected" : ""}>${esc(s)}</option>`).join("")}</select></div></div>
    <div class="actions"><button class="btn primary" data-act="reg-save">Save details</button></div></div>`;
}

const REG_SHOW = [["all", "All"], ["sent", "Mail sent"], ["not_sent", "Not mailed yet"], ["waiting", "Waiting for I sent it"]];
function regQuery() {
  return new URLSearchParams({ show: UI.regShow, q: UI.regQuery, date_from: UI.regFrom || "", date_to: UI.regTo || "",
                               status: UI.regStatus || "", source: UI.regSource || "" });
}
async function pRegister(arg) {
  title("Register");
  UI.candidateFrom = "register";
  if (REG_SHOW.some((x) => x[0] === arg)) UI.regShow = arg;
  if (!UI.regSel) UI.regSel = new Set();
  const qs = regQuery();
  const r = await api(`/api/register?${qs}`);
  const cols = r.columns;
  const mailable = r.rows.filter((x) => x.can_mail).map((x) => x.app_no);
  for (const a of [...UI.regSel]) if (!mailable.includes(a)) UI.regSel.delete(a);
  const sel = UI.regSel;
  const allOn = mailable.length > 0 && mailable.every((a) => sel.has(a));
  const cell = (row, k) => k === "mail_date" ? esc(shortDate(row[k])) : k === "status" ? regTag(row[k]) + (row.test_sent ? " " + tag("Test sent") : "")
    : k === "name" ? `<b>${esc(row[k] || "Reading…")}</b>` : esc(row[k] || "");
  const table = r.rows.length ? `<div class="wide"><table class="t list reg-list"><thead><tr>
      <th style="width:28px"><input type="checkbox" id="reg-all" title="Select every student who can be mailed" ${allOn ? "checked" : ""} ${mailable.length ? "" : "disabled"}></th>
      ${cols.map(([, l]) => `<th>${esc(l)}</th>`).join("")}</tr></thead><tbody>
    ${r.rows.map((row) => `<tr class="click" data-href="#/candidate/${encodeURIComponent(row.app_no)}">
      <td>${row.can_mail ? `<input type="checkbox" class="reg-sel" value="${esc(row.app_no)}" ${sel.has(row.app_no) ? "checked" : ""} title="Select for invitations">` : ""}</td>
      ${cols.map(([k]) => `<td>${cell(row, k)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`
    : empty(UI.regQuery || UI.regFrom || UI.regTo || UI.regStatus || UI.regSource ? "No row matches the filters." : "No CVs yet. Add them on the Add CV page.");
  const opt = (list, val, all) => `<option value="">${esc(all)}</option>` + list.map((x) => `<option ${x === val ? "selected" : ""}>${esc(x)}</option>`).join("");
  const filtered = UI.regFrom || UI.regTo || UI.regStatus || UI.regSource;
  setView(`<div class="box">${boxHead("Intern register", `<a class="btn" href="/api/register.xlsx?${qs}">Export to Excel</a><a class="btn" href="/api/register.csv?${qs}">Export CSV</a>`)}
    <div class="filters"><div class="tabs">${REG_SHOW.map(([k, l]) => `<button class="${UI.regShow === k ? "on" : ""}" data-act="reg-show" data-s="${k}">${esc(l)} (${esc(r.counts[k] ?? 0)})</button>`).join("")}</div>
      <input type="search" class="search" id="reg-q" placeholder="Search any column" value="${esc(UI.regQuery)}"></div>
    <div class="filters">
      <label class="small muted" for="regf-from">Mail date from</label><input type="date" id="regf-from" style="width:auto" value="${esc(UI.regFrom || "")}">
      <label class="small muted" for="regf-to">to</label><input type="date" id="regf-to" style="width:auto" value="${esc(UI.regTo || "")}">
      <select id="regf-status" style="width:auto">${opt(r.statuses || [], UI.regStatus, "Any status")}</select>
      <select id="regf-source" style="width:auto">${opt(r.sources || [], UI.regSource, "Any source")}</select>
      ${filtered ? `<button class="btn small" data-act="reg-filters-clear">Clear filters</button>` : ""}</div>
    <p class="small muted" style="margin:0 0 8px">Name, University and Technology come from the CV. Mail date is filled when the mail is sent. Open a row to fill the details from the student's reply. The exports use the filters shown here.</p>
    <div class="actions" style="margin:0 0 8px"><button class="btn primary" data-act="bulk-open" ${sel.size ? "" : "disabled"}>Prepare invitations${sel.size ? ` (${sel.size})` : ""}</button>
      ${sel.size ? `<button class="btn small" data-act="bulk-clear">Clear selection</button>` : `<span class="small muted">Tick students who have not been mailed to send them the invitation together.</span>`}</div>
    ${table}</div>`);
  const qi = $("#reg-q");
  qi.oninput = () => { UI.regQuery = qi.value; const pos = qi.selectionStart; lastHTML = ""; pRegister().then(() => { const n = $("#reg-q"); n.focus(); n.setSelectionRange(pos, pos); }); };
  const refilter = () => { lastHTML = ""; pRegister(); };
  $("#regf-from").onchange = (e) => { UI.regFrom = e.target.value; refilter(); };
  $("#regf-to").onchange = (e) => { UI.regTo = e.target.value; refilter(); };
  $("#regf-status").onchange = (e) => { UI.regStatus = e.target.value; e.target.blur(); refilter(); };
  $("#regf-source").onchange = (e) => { UI.regSource = e.target.value; e.target.blur(); refilter(); };
  document.querySelectorAll(".reg-sel").forEach((x) => { x.onchange = () => { x.checked ? sel.add(x.value) : sel.delete(x.value); refilter(); }; });
  const all = $("#reg-all");
  if (all) all.onchange = () => { mailable.forEach((a) => (all.checked ? sel.add(a) : sel.delete(a))); refilter(); };
}

// ---- several invitations at once
async function openBulkDialog() {
  const apps = [...(UI.regSel || [])];
  if (!apps.length) return;
  const s = await api("/api/mail/settings");
  UI.mailFiles = s.attachments;
  openDialog(`<h3>Prepare invitations for ${apps.length} student(s)</h3>
    <div class="form-row"><label for="bulk-date">Visit date</label><input type="date" id="bulk-date" value="${esc(s.next_visit_day)}">
      <div class="help">Written into every mail, for example ${esc(visitText(s.next_visit_day))}.</div></div>
    <div class="form-row"><label>Attachments</label>${s.attachments.map((f) => `<label class="check"><input type="checkbox" class="bulk-file" value="${esc(f.name)}" checked> ${esc(f.name)} <span class="small muted">${fmtSize(f.size)}</span></label>`).join("") || `<div class="muted">No attachments.</div>`}</div>
    <p class="small muted">Each mail is made from the template. A mail you already saved keeps its own text; only its visit date changes. Students who were already mailed are left out.</p>
    <div class="actions"><button class="btn" data-dlg="no">Cancel</button><button class="btn primary" data-dlg="go">Prepare ${apps.length} mail(s)</button></div>`);
  const body = $("#dialog-body");
  body.querySelector('[data-dlg="no"]').onclick = closeDialog;
  body.querySelector('[data-dlg="go"]').onclick = async () => {
    try {
      const r = await api("/api/mail/bulk/prepare", { method: "POST", body: { app_nos: apps, visit_date: $("#bulk-date").value,
        attachments: [...document.querySelectorAll(".bulk-file:checked")].map((x) => x.value) } });
      bulkReady(r);
    } catch (e) { toast(e.message, true); }
  };
}
function bulkResultRows(list) {
  return `<table class="t" style="margin:8px 0"><tbody>${list.map((x) => `<tr><td>${esc(x.name || x.app_no)}</td>
    <td>${x.ok ? tag(x.state || "Ready", x.state === "Failed" ? "fail" : x.state === "Opened" ? "need" : "pass") : tag("Left out", "need")} <span class="small muted">${esc(x.reason || x.detail || "")}</span></td></tr>`).join("")}</tbody></table>`;
}
function bulkReady(r) {
  const n = r.ready.length;
  const testTo = STATE?.outlook?.test_recipient || "";
  const warn = testTo ? notice("error", "Test inbox is on.", `Every mail goes to ${testTo}, not to the students.`) : "";
  if (!n) {
    openDialog(`<h3>No mail is ready</h3>${bulkResultRows(r.results)}<div class="actions"><button class="btn primary" data-dlg="no">Close</button></div>`);
    $("#dialog-body").querySelector('[data-dlg="no"]').onclick = closeDialog;
    return;
  }
  if (r.direct) {
    openDialog(`<h3>${n} mail(s) ready</h3>${warn}${bulkResultRows(r.results)}
      <p class="small muted">They are sent one after another, a few seconds apart, from your ${r.mode === "smtp" ? "SMTP account" : "Microsoft account"}. You can close this window; sending goes on.</p>
      <div class="actions"><button class="btn" data-dlg="no">Not now</button><button class="btn primary" data-dlg="go">Send ${n} mail(s)</button></div>`);
    const body = $("#dialog-body");
    body.querySelector('[data-dlg="no"]').onclick = () => { closeDialog(); lastHTML = ""; render(); };
    body.querySelector('[data-dlg="go"]').onclick = async () => {
      try { bulkProgress(await api("/api/mail/bulk/send", { method: "POST", body: { app_nos: r.ready } })); }
      catch (e) { toast(e.message, true); }
    };
    return;
  }
  // The mail app opens one mail at a time.
  const queue = r.results.filter((x) => x.ok).map((x) => ({ ...x, state: "Ready" }));
  const show = () => {
    const next = queue.find((x) => x.state === "Ready");
    const opened = queue.filter((x) => x.state === "Opened");
    openDialog(`<h3>Open the mails one at a time</h3>${warn}
      <p class="small muted" style="margin-top:0">Each click opens the next mail in your mail app. Click Send there, then come back here. When all are sent, click I sent them all.</p>
      ${bulkResultRows(queue.map((x) => ({ ...x, ok: true, detail: x.detail || "" })))}
      <div class="actions"><button class="btn" data-dlg="no">Close</button>
        ${opened.length ? `<button class="btn" data-dlg="all">I sent them all (${opened.length})</button>` : ""}
        ${next ? `<button class="btn primary" data-dlg="next">Open next: ${esc(next.name)}</button>` : ""}</div>`);
    const body = $("#dialog-body");
    body.querySelector('[data-dlg="no"]').onclick = () => { closeDialog(); lastHTML = ""; render(); };
    const nb = body.querySelector('[data-dlg="next"]');
    if (nb) nb.onclick = async () => {
      nb.disabled = true;
      try {
        const res = await api(`/api/candidates/${encodeURIComponent(next.app_no)}/mail/send`, { method: "POST", body: {} });
        const i = res.invite;
        next.state = i.test_only ? "Test copy" : i.status === "sent" ? "Sent" : "Opened";
        next.detail = i.saved_only ? (i.note || "Download the .eml from the student's page.") : i.opened_in || "";
      } catch (e) { next.state = "Failed"; next.detail = e.message; }
      show();
    };
    const ab = body.querySelector('[data-dlg="all"]');
    if (ab) ab.onclick = async () => {
      for (const x of opened) {
        try { await api(`/api/candidates/${encodeURIComponent(x.app_no)}/mail/confirm`, { method: "POST" }); x.state = "Sent"; }
        catch (e) { x.detail = e.message; }
      }
      toast("Marked as sent."); show();
    };
  };
  show();
}
function bulkProgress(job) {
  const done = !job.running;
  openDialog(`<h3>${done ? "Finished" : "Sending…"} ${esc(job.done)} of ${esc(job.total)}</h3>
    <p class="small muted" style="margin-top:0">${esc(job.sent)} sent, ${esc(job.failed)} not sent.</p>
    ${bulkResultRows(job.results.map((x) => ({ ...x, ok: true, state: x.ok ? "Sent" : "Failed" })))}
    <div class="actions"><button class="btn ${done ? "primary" : ""}" data-dlg="no">Close</button></div>`);
  $("#dialog-body").querySelector('[data-dlg="no"]').onclick = () => { closeDialog(); UI.regSel = new Set(); lastHTML = ""; render(); };
  if (!done) setTimeout(async () => {
    if (!$("#dialog").open) return;
    try { bulkProgress(await api(`/api/mail/bulk/${encodeURIComponent(job.id)}`)); } catch { /* try again next time */ }
  }, 1500);
}

async function pMailSettings() {
  title("Mail settings");
  const s = await api("/api/mail/settings");
  const ol = s.outlook || STATE?.outlook || { mode: "eml", tenant_id: "common", smtp_host: "smtp.office365.com", smtp_port: 587 };
  UI.mailFiles = s.attachments;
  const files = s.attachments.length ? `<table class="t"><thead><tr><th>File</th><th>Size</th><th>Kind</th><th></th></tr></thead><tbody>
    ${s.attachments.map((f) => `<tr><td><a href="/api/mail/attachments/${encodeURIComponent(f.name)}">${esc(f.name)}</a></td><td class="small muted">${fmtSize(f.size)}</td>
      <td class="small muted">${f.standard ? (f.replaced ? "Required, newer version uploaded" : "Required, comes with the template") : "Added by you"}</td>
      <td style="text-align:right">${!f.standard || f.replaced ? `<button class="btn small" data-act="att-remove" data-n="${esc(f.name)}">${f.replaced ? "Use the original" : "Remove"}</button>` : ""}</td></tr>`).join("")}</tbody></table>`
    : empty("No attachments yet.");
  const total = s.attachments.reduce((n, f) => n + f.size, 0);
  const eff = _effectiveOutlookMode(ol);
  const senderAddr = ol.sender_email || ol.account?.username || ol.smtp_user || "";
  const acctText = ol.account?.username
    ? `Connected: ${esc(ol.account.username)}`
    : eff === "smtp"
    ? `${esc(SMTP_NAMES[ol.provider] || "SMTP")} (${esc(senderAddr)})`
    : eff === "outlook_app" && senderAddr
    ? `Sender: ${esc(senderAddr)} (Outlook / Mail app)`
    : eff === "outlook_app"
    ? "Enter your Outlook email below & save"
    : ".eml draft mode";
  const limit = ol.size_limit || 0;
  const packed = Math.round(total * 4 / 3);
  const sizeText = `${s.attachments.length} file(s), ${fmtSize(total)} in total` +
    (limit ? ` (about ${fmtSize(packed)} in the mail; the limit is about ${fmtSize(limit)})` : "");
  const ob = s.outbox || { files: 0, bytes: 0, keep_days: 90 };
  setView(`<div class="box">${boxHead("Attachments", `<span class="small muted" style="${limit && packed > limit ? "color:var(--fail);font-weight:600" : ""}">${sizeText}</span>`)}
      <p class="small muted" style="margin-top:0">These files are attached to every invitation mail. The required files come with the template; add more files here if needed. You can untick a file for a single mail on the candidate page. To update a required file, add a newer file with the same name.</p>
      ${files}
      <div class="actions" style="margin-top:12px"><label class="btn" for="att-input">Add files</label>
        <input type="file" id="att-input" accept=".pdf,.xlsx,.xls,.docx,.doc" multiple hidden>
        <span class="small muted">PDF, Excel or Word, up to 10 MB each.</span></div></div>
    <div class="box" id="outlook-box">${boxHead("Mail sending", `<span class="small muted">${acctText}</span>`)}
      ${_outlookControlsMarkup(ol, "ms-ol")}
      <div class="form-row" style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line-soft)">
        <label for="ms-ol-probe">Send a test email (includes all ${s.attachments.length} attachments)</label>
        <div class="actions">
          <input type="email" id="ms-ol-probe" style="flex:1" value="${esc(ol.test_recipient || senderAddr)}" placeholder="your.email@outlook.com">
          <button type="button" class="btn" id="ms-ol-test">Send test mail</button>
          <button type="button" class="btn primary" id="ms-ol-save">Save mail settings</button>
        </div>
      </div>
    </div>
    <div class="box">${boxHead("Saved mail copies", `<span class="small muted">${esc(ob.files)} file(s), ${fmtSize(ob.bytes || 0)}</span>`)}
      <p class="small muted" style="margin-top:0">A copy of every invitation is kept in data/outbox/. Once a day, copies of sent mails older than ${esc(ob.keep_days)} days are made smaller: the attachments are taken out and their names are kept in the text. Mails that are not confirmed as sent are never changed.</p>
      <div class="actions"><button class="btn" data-act="ms-shrink">Shrink old copies now</button></div></div>
    <div class="box">${boxHead("Mail template", `<span class="small muted">From: ${esc(s.sender)}</span>`)}
      <div class="form-row"><label for="ms-subject">Subject</label><input type="text" id="ms-subject" value="${esc(s.subject)}"></div>
      <div class="form-row"><label for="ms-body">Mail text</label><textarea id="ms-body" rows="26" style="font-family:inherit;font-size:13.5px">${esc(s.template)}</textarea>
        <div class="help">{visit_date} is replaced with the visit date picked for each mail, for example ${esc(visitText(s.next_visit_day))}. A change here is used for new mails, not for mails already saved.</div></div>
      <div class="actions"><button class="btn primary" data-act="ms-save">Save template</button><button class="btn" data-act="ms-reset">Back to the original</button></div></div>`);
  const input = $("#att-input");
  input.onchange = async () => {
    const form = new FormData();
    [...input.files].forEach((f) => form.append("files", f, f.name));
    try {
      const r = await api("/api/mail/attachments", { method: "POST", form });
      const bad = r.results.filter((x) => !x.ok);
      toast(bad.length ? bad.map((x) => `${x.file}: ${x.reason}`).join(" ") : `${r.results.length} file(s) added.`, !!bad.length);
    } catch (e) { toast(e.message, true); }
    lastHTML = ""; pMailSettings();
  };
  const olBox = $("#outlook-box");
  $("#ms-ol-provider").onchange = (e) => _applyProvider("ms-ol", e.target.value);
  $("#ms-ol-detect").onclick = async () => {
    try {
      const d = await api("/api/mail/detect", { method: "POST", body: { email: $("#ms-ol-semail").value.trim() } });
      if (!d.found || !d.provider) { toast(`Could not tell the provider of ${d.domain}. Choose it from the list.`, true); return; }
      $("#ms-ol-provider").value = d.provider;
      _applyProvider("ms-ol", d.provider);
      const names = { google: "Google", microsoft: "Microsoft", custom: "its own mail server" };
      toast(d.provider === "custom"
        ? `${d.domain} uses its own mail server (${(d.mx || []).slice(0, 2).join(", ")}). Ask IT for the SMTP server name, port and security, then Save.`
        : `${d.domain} uses ${names[d.provider]}. Settings filled in — click Save mail settings.`);
    } catch (e) { toast(e.message, true); }
  };
  $("#ms-ol-mode").onchange = () => _syncOutlookModeUI("ms-ol");
  $("#ms-ol-sfallback").onchange = () => _syncOutlookModeUI("ms-ol");
  $("#ms-ol-save").onclick = async () => {
    try {
      await api("/api/outlook/settings", { method: "POST", body: _collectOutlookPayload("ms-ol") });
      toast("Saved mail settings."); lastHTML = ""; await refreshState(); pMailSettings();
    } catch (e) { toast(e.message, true); }
  };
  $("#ms-ol-test").onclick = async () => {
    try {
      await api("/api/outlook/settings", { method: "POST", body: _collectOutlookPayload("ms-ol") });
      const r = await api("/api/mail/test", { method: "POST", body: { to: $("#ms-ol-probe").value } });
      toast(r.saved_only ? (r.note || `Saved to data/outbox/${r.file}. Open it in your mail app.`)
        : r.how === "outlook_app" && r.sent_by_app ? `Test mail sent by ${r.opened_in || "Outlook"} to ${r.sent_to} (${r.attachments.length} attachments).`
        : r.how === "outlook_app" ? `Opened in ${r.opened_in || "Outlook / Mail"} for ${r.sent_to} (${r.attachments.length} attachments) — just click Send.${r.note ? " " + r.note : ""}`
        : r.how === "eml" ? `Test .eml saved to data/outbox/${r.file} (${r.attachments.length} attachments).`
        : r.how === "graph_draft" ? `Test draft saved in Outlook for ${r.sent_to} (${r.attachments.length} attachments).`
        : `Test mail sent via ${r.how.toUpperCase()} to ${r.sent_to} (${r.attachments.length} attachments).`);
      lastHTML = ""; await refreshState(); pMailSettings();
    } catch (e) { toast(e.message, true); }
  };
  const reloadMailSettings = async () => { if (current.page === "mailsettings") { lastHTML = ""; await refreshState(); pMailSettings(); } };
  _bindOutlookAuthActions(olBox, "ms-ol", reloadMailSettings, reloadMailSettings);
}

function letterBox(c) {
  const L = c.letter;
  const url = `/api/candidates/${encodeURIComponent(c.app_no)}/letter`;
  const keep = UI.letterDates && UI.letterDates.app === c.app_no ? UI.letterDates : { start: L.start_date, end: L.end_date };
  const longDate = (v) => (v ? new Date(v + "T00:00:00").toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" }) : "To be confirmed");
  const dates = L.can_edit && ROLE === "hr"
    ? `<div class="actions"><div class="form-row" style="margin:0"><label for="letter-start">Start date</label><input type="date" id="letter-start" value="${esc(keep.start)}"></div>
        <div class="form-row" style="margin:0"><label for="letter-end">End date</label><input type="date" id="letter-end" value="${esc(keep.end)}"></div>
        <button class="btn primary" data-act="save-letter" style="align-self:flex-end">Save dates</button></div>
       <div class="help">${L.automatic ? "Filled in automatically: the Monday after the interview, for six months. Change them if needed." : "Dates changed by the HR officer. Clear both and save to go back to the automatic dates."}</div>`
    : `<div class="field"><div class="label">Start date</div><div>${esc(longDate(L.start_date))}</div></div>
       <div class="field"><div class="label">End date</div><div>${esc(longDate(L.end_date))}</div></div>`;
  return `<div class="box" id="letter">${boxHead("Placement letter", L.ready ? tag(L.automatic ? "Automatic dates" : "Dates changed") : ROLE === "hr" && L.can_edit ? tag("Needs dates", "need") : "")}
    <div class="grid two"><div>${dates}</div>
      <div><div class="label">The letter</div><div>${esc(L.file)}</div>
        <div class="small muted">A plain A4 letter made by code from the letter template, attached to the acceptance email.${L.made_at ? " Made " + esc(fmtTime(L.made_at)) + "." : ""}</div>
        <div class="actions" style="margin-top:8px"><a class="btn" href="${url}" target="_blank" rel="noopener">Preview</a><a class="btn" href="${url}?download=1">Download PDF</a></div></div></div></div>`;
}
function outcomeText(o) {
  const m = { ok: ["", "Passed checks"], fixed: ["", "Fixed after a check"], fallback: ["", "Fell back to rules"], stopped: ["fail", "Stopped: model not running"], error: ["fail", "Error"] }[o] || ["", o];
  return m[0] ? tag(m[1], m[0]) : esc(m[1]);
}

async function pReview() {
  title("Review queue");
  const r = await api("/api/review");
  const total = r.decisions.length + r.notes.length;
  setView(`${total ? notice("need", `${total} item(s) need you.`, "Decisions first, then quick checks.") : notice("ok", "The review queue is empty.", "Nothing needs a decision right now.")}
    <div class="box">${boxHead("Emails waiting for release")}
      <div class="notice ${r.replies_waiting ? "need" : ""}"><div class="grow"><b>${esc(r.replies_waiting)} email(s) waiting for your release.</b>
        <span class="muted">Invitations, replies and acceptance emails drafted by the AI team. Nothing reaches a candidate until you release it.</span></div><a class="btn" href="#/replies">Open Replies</a></div></div>
    <div class="grid two">
      <div class="box">${boxHead("Decisions for you", `<span class="small muted">${r.decisions.length}</span>`)}
        ${r.decisions.length ? `<table class="t"><thead><tr><th>Candidate</th><th>What to decide</th><th>Status</th></tr></thead><tbody>
          ${r.decisions.map((c) => `<tr class="click" data-href="#/candidate/${encodeURIComponent(c.app_no)}"><td>${esc(c.name)}<div class="small muted">${esc(c.app_no)}</div></td>
            <td>${esc(c.reason)}</td><td>${statusTag(c)}</td></tr>`).join("")}</tbody></table>` : empty("No decisions waiting.")}</div>
      <div class="box">${boxHead("Notes to check", `<span class="small muted">${r.notes.length}</span>`)}
        ${r.notes.length ? `<table class="t"><thead><tr><th>Candidate</th><th>Note</th><th></th></tr></thead><tbody>
          ${r.notes.map((n) => `<tr><td>${esc(n.name)}<div class="small">${candLink(n.app_no)}</div></td><td>${esc(n.text)}<div class="small muted">${esc(n.by)} - ${esc(fmtTime(n.at))}</div></td>
            <td style="text-align:right"><button class="btn small" data-act="resolve" data-app="${esc(n.app_no)}" data-i="${n.index}">Checked</button></td></tr>`).join("")}</tbody></table>` : empty("No notes to check.")}</div>
    </div>
    ${r.suggested_areas.length ? `<div class="box">${boxHead("New areas suggested by the AI")}<table class="t"><thead><tr><th>Area</th><th>Suggested for</th><th></th></tr></thead><tbody>
      ${r.suggested_areas.map((s) => `<tr><td>${esc(s.name)}</td><td>${esc(s.app_nos.join(", "))}</td><td style="text-align:right"><a class="btn small" href="#/areas/new:${encodeURIComponent(s.name)}">Add this area</a></td></tr>`).join("")}</tbody></table></div>` : ""}`);
}

async function pReplies(arg) {
  title("Replies");
  if (arg === "released" || arg === "waiting") UI.repliesTab = arg;
  const drafts = await api(`/api/drafts?state=${UI.repliesTab}`);
  const ol = STATE?.outlook || {};
  const modeHint = ol.mode === "graph_draft"
    ? `Microsoft Graph Drafts mode (${ol.account?.username || "sign in via Outlook settings"}). Released emails also save to your Outlook Drafts folder.`
    : (ol.mode === "graph" || ol.mode === "graph_send")
    ? `Microsoft Graph Direct Send (${ol.account?.username || "sign in via Outlook settings"}). Released emails send directly via Outlook and archive to data/outbox/.`
    : ol.mode === "smtp"
    ? `Office 365 SMTP mode (${ol.smtp_user || ol.smtp_host}). Released emails send live and archive to data/outbox/.`
    : `Draft files (.eml) & Outlook Web mode. Click Release to save an Outlook-ready .eml (with PDF attached) in data/outbox/, or use Outlook Web / configure Microsoft Graph.`;
  setView(`<div class="box">${boxHead("Emails drafted by the AI team", `<button class="btn small" data-act="outlook-settings">Outlook settings</button>`)}
    <div class="filters"><div class="tabs"><button class="${UI.repliesTab === "waiting" ? "on" : ""}" data-act="tab" data-t="waiting">Waiting for release</button><button class="${UI.repliesTab === "released" ? "on" : ""}" data-act="tab" data-t="released">Released</button></div></div>
    <p class="small muted" style="margin-top:0">${esc(modeHint)}</p>
    ${drafts.length ? drafts.map((d) => mailBlock(d, { showCandidate: true })).join("") : empty(UI.repliesTab === "waiting" ? "No emails are waiting for release." : "No emails released yet.")}</div>`);
}

async function pAreas(arg) {
  title("Areas");
  const a = UI.areas = await api("/api/areas");
  setView(`${a.suggested.length ? notice("need", "The AI suggested new areas.", "Add one only if your organisation really offers it. " + a.suggested.map((s) => `${s.name} (${s.count})`).join(", ") + ".",
      `<div class="actions">${a.suggested.map((s) => `<button class="btn small" data-act="area-new" data-name="${esc(s.name)}">Add ${esc(s.name)}</button>`).join("")}</div>`) : ""}
    <div class="box">${boxHead("Areas", `<button class="btn primary" data-act="area-new">Add area</button>`)}
      <p class="small muted" style="margin-top:0">The Recruiter chooses from these areas. CVs are scored against the required skills (skills 40, projects 30, education 20, experience 10). Education gets full points only for the area's related studies.</p>
      <table class="t"><thead><tr><th>Area</th><th>Required skills</th><th>Preferred skills</th><th class="num-col">Candidates</th><th>Status</th><th></th></tr></thead><tbody>
      ${a.areas.map((x) => `<tr><td><b>${esc(x.name)}</b><div class="small muted">${esc(x.description || "No description.")}</div><div class="small muted">Added by ${esc(x.added_by)}</div></td>
        <td>${esc(x.required_skills.join(", ") || "-")}</td><td>${esc(x.preferred_skills.join(", ") || "-")}</td>
        <td class="num-col">${esc(x.candidates)}</td><td>${x.active ? "Active" : "Turned off"}</td>
        <td style="text-align:right;white-space:nowrap"><button class="btn small" data-act="area-edit" data-id="${x.id}">Edit</button>
          <button class="btn small" data-act="area-toggle" data-id="${x.id}">${x.active ? "Turn off" : "Turn on"}</button></td></tr>`).join("")}</tbody></table></div>`);
  if (arg && arg.startsWith("new:")) { history.replaceState(null, "", "#/areas"); areaForm(null, arg.slice(4)); }
}
function areaForm(area, name = "") {
  const v = area || { name, description: "", required_skills: [], preferred_skills: [], studies: [], keywords: [], checklist: [] };
  openDialog(`<h3>${area ? "Edit area" : "Add area"}</h3><p class="small muted">Separate skills with commas. Only add an area that your organisation really offers.</p>
    <div class="form-row"><label for="af-name">Name</label><input type="text" id="af-name" value="${esc(v.name)}" maxlength="40"></div>
    <div class="form-row"><label for="af-desc">Description</label><textarea id="af-desc" rows="2" style="min-height:50px">${esc(v.description)}</textarea></div>
    <div class="form-row"><label for="af-req">Required skills</label><input type="text" id="af-req" value="${esc(v.required_skills.join(", "))}" placeholder="For example: Recruitment, Interviewing, Excel"><div class="help">Used for the score. At least one.</div></div>
    <div class="form-row"><label for="af-pref">Preferred skills</label><input type="text" id="af-pref" value="${esc(v.preferred_skills.join(", "))}"></div>
    <div class="form-row"><label for="af-studies">Related studies</label><input type="text" id="af-studies" value="${esc((v.studies || []).join(", "))}" placeholder="For example: Human Resource Management, Psychology, Business"><div class="help">Degrees or subjects that fit this area. They give full education points.</div></div>
    <div class="form-row"><label for="af-kw">Keywords for related projects</label><input type="text" id="af-kw" value="${esc(v.keywords.join(", "))}"></div>
    <div class="form-row"><label for="af-check">Extra onboarding checklist items (one per line)</label><textarea id="af-check" rows="3" style="min-height:60px">${esc(v.checklist.join("\n"))}</textarea></div>
    <div class="actions"><button class="btn" data-dlg="cancel">Cancel</button><button class="btn primary" data-dlg="save">Save area</button></div>`);
  const d = $("#dialog");
  d.querySelector('[data-dlg="cancel"]').onclick = closeDialog;
  d.querySelector('[data-dlg="save"]').onclick = async () => {
    const body = { name: $("#af-name").value, description: $("#af-desc").value, required_skills: $("#af-req").value,
      preferred_skills: $("#af-pref").value, studies: $("#af-studies").value, keywords: $("#af-kw").value, checklist: $("#af-check").value, active: area ? area.active : true };
    try {
      if (area) await api(`/api/areas/${area.id}`, { method: "PUT", body });
      else await api("/api/areas", { method: "POST", body });
      closeDialog(); toast(`Saved the area ${body.name}.`); UI.areas = null; lastHTML = ""; render();
    } catch (e) { toast(e.message, true); }
  };
}

async function pTeam() {
  title("AI team");
  const t = await api("/api/team");
  const down = !t.model.ok || (t.notice && t.notice.text);
  setView(`${notice(down ? "error" : "ok", down ? "The model server is not running, so the AI team is waiting." : t.paused ? "The AI team is paused." : "Four AI employees work on their own schedule.",
      "Each one is a folder of Markdown files (ROLE, SCHEDULE, RELEASES and one SKILL file per routine). The status of a candidate hands the work from one to the next.",
      ROLE === "hr" ? `<div class="actions"><button class="btn primary" data-act="run" data-who="all">Run all now</button><button class="btn" data-act="pause" data-p="${t.paused ? "0" : "1"}">${t.paused ? "Resume" : "Pause"}</button></div>` : "")}
    <div class="grid two">${t.employees.map((e) => `<div class="box emp">
      ${boxHead(e.name, `<span>${t.paused ? "Paused" : e.state === "working" ? "Working: " + esc(e.doing) : "Ready"}</span>`)}
      <div class="small muted">${esc((e.schedule.text || "").split("\n")[0])}${e.next_run ? " - next run " + esc(fmtTime(e.next_run)) : ""}</div>
      <p>${esc(e.summary)}</p>
      <p class="small">Last 24 hours: ${esc(e.day.ok)} passed checks, ${esc(e.day.fixed)} fixed, ${esc(e.day.fallback)} fell back to rules${e.day.error || e.day.stopped ? `, ${tag((e.day.error + e.day.stopped) + " stopped", "fail")}` : ""}.</p>
      <div class="label">Routines</div>
      <table class="t"><thead><tr><th>Routine</th><th>Done by</th><th>Starts when</th></tr></thead><tbody>
        ${e.routines.map((r) => `<tr><td>${esc(r.name)}</td><td>${r.uses_model ? "Model + checks" : "Plain code"}</td><td class="small muted">${esc(r.starts_when || "-")}</td></tr>`).join("")}</tbody></table>
      ${e.lessons.length ? `<div class="label" style="margin-top:12px">What it has learned</div><ul class="clean small">${e.lessons.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : ""}
      <div class="label" style="margin-top:12px">Recent steps</div>
      ${e.runs.length ? `<table class="t"><thead><tr><th>Time</th><th>Routine</th><th>Outcome</th><th class="num-col">Seconds</th></tr></thead><tbody>
        ${e.runs.slice(0, 5).map((r) => `<tr><td class="small muted">${esc(fmtTime(r.at))}</td><td>${esc(r.routine)}</td><td>${outcomeText(r.outcome)}</td><td class="num-col">${esc(r.seconds)}</td></tr>`).join("")}</tbody></table>` : `<span class="muted small">No runs yet.</span>`}
      ${ROLE === "hr" ? `<div class="actions" style="margin-top:12px"><button class="btn small" data-act="run" data-who="${esc(e.key)}">Run ${esc(e.name)} now</button></div>` : ""}
    </div>`).join("")}</div>`);
}

// ================================================================ Interviewer pages
async function pInterviews() {
  title("Interviews");
  const r = await api("/api/interviews");
  const LIMIT = 5;
  const table = (items, key, click, none, last) => {
    if (!items.length) return empty(none);
    const all = UI.showAll === key;
    const rows = (all ? items : items.slice(0, LIMIT)).map((c) => `<tr ${click ? `class="click" data-href="#/interview/${encodeURIComponent(c.app_no)}"` : ""}>
      <td>${esc(c.name)}<div class="small muted">${esc(c.app_no)}</div></td><td class="small">${esc(c.interview_label || "-")}</td><td>${esc(c.area)}</td>
      <td style="white-space:nowrap">${last === "result" ? resultTag(c.result) : last === "released" ? `<span class="small muted">Not released</span>` : esc(c.score ?? "-")}</td></tr>`).join("");
    const head = `<thead><tr><th>Candidate</th><th>Interview</th><th>Area</th><th>${last === "result" ? "Result" : last === "released" ? "Invitation" : "Score"}</th></tr></thead>`;
    const btn = items.length > LIMIT ? `<button class="btn link small" data-act="show-all" data-k="${all ? "" : key}" style="margin-top:8px">${all ? "Show fewer" : "Show all " + items.length}</button>` : "";
    return `<table class="t">${head}<tbody>${rows}</tbody></table>${btn}`;
  };
  setView(`${r.waiting.length ? notice("need", `${r.waiting.length} interview(s) are waiting for your result.`, "Open one, do the interview and enter Pass, Fail or Hold.")
      : notice("ok", "No results are waiting for you.", `${r.on_hold.length} on hold. Your result is final; the AI cannot change it.`)}
    <div class="grid two even">
      <div class="box">${boxHead("Waiting for your result", `<span class="small muted">${r.waiting.length}</span>`)}${table(r.waiting, "waiting", true, "Nothing waiting.", "score")}</div>
      <div class="box">${boxHead("Coming up", `<span class="small muted">${r.invitation_not_released.length}, invitation not released yet</span>`)}${table(r.invitation_not_released, "coming", false, "None.", "released")}</div>
      <div class="box">${boxHead("On hold", `<span class="small muted">${r.on_hold.length}</span>`)}${table(r.on_hold, "hold", true, "No one is on hold.", "result")}</div>
      <div class="box">${boxHead("Your recent results")}${table(r.done, "done", false, "No results yet.", "result")}</div>
    </div>`);
}

async function pInterview(appNo) {
  title("Interview");
  const c = await api(`/api/candidates/${encodeURIComponent(appNo)}`);
  const p = c.profile || {};
  const open = c.status === "awaiting_result" || c.status === "on_hold";
  const form = open ? `<div class="result-pick">
      <label class="pass"><input type="radio" name="result" value="Pass"><b>Pass</b> <span class="small muted">Make an offer</span></label>
      <label class="fail"><input type="radio" name="result" value="Fail"><b>Fail</b> <span class="small muted">No offer</span></label>
      ${c.status === "awaiting_result" ? `<label class="hold"><input type="radio" name="result" value="Hold"><b>Hold</b> <span class="small muted">Decide later</span></label>` : ""}</div>
    <div class="form-row" style="margin-top:12px"><label for="result-comment">Comment (optional)</label><textarea id="result-comment" rows="4" placeholder="Short notes for the HR officer. Not sent to the candidate."></textarea></div>
    <div class="actions" style="margin-top:12px"><button class="btn primary" data-act="save-result">Save result</button><span class="small muted">The AI team takes over after you save.</span></div>`
    : c.result ? `<p>${resultTag(c.result)}</p><p class="muted">Saved ${esc(fmtTime(c.result_at))}. ${c.result_comment ? "Comment: " + esc(c.result_comment) : ""}</p><p class="small muted">Now: ${esc(c.status_label)}.</p>`
    : `<p class="muted">The result can be entered after the HR officer releases the invitation.</p>`;
  setView(`<a class="back" href="#/interviews">&larr; All interviews</a>
    <div class="box"><div class="box-head"><h2 style="font-size:18px">${esc(c.name)}</h2><div class="spacer"></div><span class="small muted">${esc(c.interview_label || "")}</span></div>
      <div class="actions">${esc(c.app_no)} ${statusTag(c)} <span>Area: ${esc(c.area)}</span>${c.score != null ? ` <span>Score: ${esc(c.score)}/100</span>` : ""}</div></div>
    <div class="grid two">
      <div class="stack">
        <div class="box">${boxHead("Questions to ask", c.questions_source ? byText(c.questions_source === "AI" ? "prepared by AI" : c.questions_source) : "")}
          ${c.questions.length ? `<ol class="clean">${c.questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ol>` : `<span class="muted">No questions prepared.</span>`}</div>
        <div class="box">${boxHead("About the candidate", `<a class="btn small" href="/api/candidates/${encodeURIComponent(c.app_no)}/cv">Download CV</a>`)}
          <div class="field"><div class="label">Education</div>${(p.education || []).map((x) => `<div>${esc(x)}</div>`).join("") || "-"}</div>
          <div class="field"><div class="label">Skills</div>${esc((p.skills || []).join(", ") || "-")}</div>
          <div class="field"><div class="label">Projects</div><ul class="clean">${(p.projects || []).map((x) => `<li>${esc(x)}</li>`).join("") || "<li>-</li>"}</ul></div>
          ${c.gaps.length ? `<div class="field"><div class="label">Skill gaps for ${esc(c.area)}</div>${esc(c.gaps.join(", "))}</div>` : ""}</div>
      </div>
      <div class="box">${boxHead("Your result")}${form}</div>
    </div>`);
}

// ================================================================ actions (one click handler for every button)
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(typeof okMsg === "function" ? okMsg(r) : okMsg); lastHTML = ""; await refreshState(); await render(); }
  catch (e) { toast(e.message, true); }
}
const ACTS = {
  "brief": () => act(() => api("/api/brief", { method: "POST" }), "The Chief of staff is writing the brief. It appears here in a moment."),
  "remove-file": (b) => { UI.files.splice(+b.dataset.i, 1); lastHTML = ""; pAdd(); },
  "clear-files": () => { UI.files = []; UI.uploadResults = null; lastHTML = ""; pAdd(); },
  "upload": async (b) => {
    const form = new FormData();
    UI.files.forEach((f) => form.append("files", f, f.name));
    const email = $("#cv-email");
    if (email && !email.disabled && email.value.trim()) form.append("email", email.value.trim());
    if (UI.cvSource.trim()) form.append("source", UI.cvSource.trim());
    b.disabled = true;
    try {
      const r = await api("/api/cvs", { method: "POST", form });
      UI.uploadResults = r.results; UI.files = []; UI.cvEmail = ""; UI.poolShow = "recent"; UI.poolArea = "";
      const ok = r.results.filter((x) => x.ok).length;
      toast(ok ? `${ok} CV(s) added. The Recruiter will read them now.` : "No CV was added.", !ok);
    } catch (e) { toast(e.message, true); }
    lastHTML = ""; refreshState(); pAdd();
  },
  "samples": () => act(async () => { const r = await api("/api/samples", { method: "POST" }); UI.uploadResults = r.results; UI.poolShow = "recent"; UI.poolArea = ""; return r; },
    (r) => `${r.results.filter((x) => x.ok).length} sample CV(s) added.`),
  "filter": (b) => { UI.candFilter = b.dataset.f; history.replaceState(null, "", "#/candidates"); current.arg = ""; lastHTML = ""; pCandidates(); },
  "show-all": (b) => { UI.showAll = b.dataset.k; lastHTML = ""; render(); },
  "tab": (b) => { UI.repliesTab = b.dataset.t; lastHTML = ""; pReplies(); },
  "vac-show": (b) => { UI.vacShow = b.dataset.s; history.replaceState(null, "", "#/vacancies"); current.arg = ""; lastHTML = ""; pVacancies(); },
  "intern-show": (b) => { UI.internShow = b.dataset.s; history.replaceState(null, "", "#/interns"); current.arg = ""; lastHTML = ""; pInterns(); },
  "vac-new": async () => { const a = await api("/api/areas"); vacancyForm(a.areas.filter((x) => x.active).map((x) => x.name)); },
  "vac-approve": async (b) => {
    if (!(await confirmBox({ heading: `Approve ${b.dataset.n} for ${b.dataset.area}?`, text: "The candidate leaves the pool. The Coordinator books an interview and drafts the invitation for you to release.", ok: "Approve" }))) return;
    act(() => api(`/api/vacancies/${b.dataset.v}/approve`, { method: "POST", body: { app_no: b.dataset.a } }), (r) => `${b.dataset.n} approved for ${r.area} (score ${r.score}).`);
  },
  "vac-decline": async (b) => {
    if (!(await confirmBox({ heading: `Pass over ${b.dataset.n}?`, text: "The next best CV from the pool is suggested instead. The candidate stays in the pool for other areas.", ok: "Not this one" }))) return;
    act(() => api(`/api/vacancies/${b.dataset.v}/decline`, { method: "POST", body: { app_no: b.dataset.a } }), "Suggestion passed over.");
  },
  "vac-close": async (b) => {
    if (!(await confirmBox({ heading: `Close the ${b.dataset.area} vacancy?`, text: "It stops being suggested for. Candidates already invited keep their interviews.", ok: "Close vacancy", danger: true }))) return;
    act(() => api(`/api/vacancies/${b.dataset.v}/close`, { method: "POST" }), "Vacancy closed.");
  },
  "sync-interns": (b) => { b.disabled = true; act(() => api("/api/interns/sync", { method: "POST" }),
    (r) => `${r.read} record(s) read: ${r.added} new, ${r.updated} changed. ${r.created || 0} new vacancy(ies).`); },
  "pool-show": (b) => { UI.poolShow = b.dataset.s; history.replaceState(null, "", "#/add"); current = { page: "add", arg: "" }; lastHTML = ""; pAdd(); },
  "invite": async () => {
    const area = $("#invite-area").value;
    if (!(await confirmBox({ heading: `Invite to interview for ${area}?`, text: "The candidate leaves the pool. The Coordinator books an interview slot and drafts the invitation for you to release.", ok: "Invite" }))) return;
    act(() => api(`/api/candidates/${encodeURIComponent(current.arg)}/invite`, { method: "POST", body: { area } }), (r) => `Invited for ${r.area} (score ${r.score}). The Coordinator takes it from here.`);
  },
  "archive": async () => {
    if (!(await confirmBox({ heading: "Archive this CV?", text: "It is removed from the pool but not deleted. You can restore it from Add CV, CV pool, Archived.", ok: "Archive" }))) return;
    act(() => api(`/api/candidates/${encodeURIComponent(current.arg)}/archive`, { method: "POST" }), "Archived.");
  },
  "restore": () => act(() => api(`/api/candidates/${encodeURIComponent(current.arg)}/restore`, { method: "POST" }), "Restored. The Recruiter reads it again."),
  "hand-add": () => act(async () => {
    const r = await api("/api/mail/students", { method: "POST", body: readHand("hand") });
    UI.hand = { source: UI.cvSource }; UI.handOpen = false; UI.poolShow = "recent";
    return r;
  }, (r) => `${r.app_no} added. Open it to send the invitation mail.`),
  "hand-open": () => { UI.handFor = current.arg; lastHTML = ""; render(); },
  "hand-cancel": () => { UI.handFor = null; UI.handFill = null; lastHTML = ""; render(); },
  "hand-fill": () => act(async () => {
    const r = await api(`/api/candidates/${encodeURIComponent(current.arg)}/by-hand`, { method: "POST", body: readHand("handc") });
    UI.handFor = null; UI.handFill = null; UI.inviteEdit = null;
    return r;
  }, "Details saved. You can send the invitation mail now."),
  "toggle-cv": () => { UI.showCv = !UI.showCv; lastHTML = ""; render(); },
  "toggle-runs": () => { UI.showRuns = !UI.showRuns; lastHTML = ""; render(); },
  "edit-email": () => { UI.editEmail = true; lastHTML = ""; render().then(() => $("#email-input")?.focus()); },
  "cancel-email": () => { UI.editEmail = false; lastHTML = ""; render(); },
  "inv-save": () => act(async () => {
    const v = readInvite(current.arg);
    await api(`/api/candidates/${encodeURIComponent(current.arg)}/mail`, { method: "POST", body: v });
    UI.inviteEdit = null;
  }, "Mail saved."),
  "inv-preview": async () => {
    const w = window.open("about:blank", "_blank");
    try {
      await api(`/api/candidates/${encodeURIComponent(current.arg)}/mail`, { method: "POST", body: readInvite(current.arg) });
      UI.inviteEdit = null;
      if (w) w.location = `/api/candidates/${encodeURIComponent(current.arg)}/mail/preview`;
      lastHTML = ""; render();
    } catch (e) { if (w) w.close(); toast(e.message, true); }
  },
  "inv-send": async () => {
    const v = readInvite(current.arg);
    const testTo = STATE?.outlook?.test_recipient || "";
    const ok = await confirmBox(testTo
      ? { heading: "Send a TEST copy?", ok: "Send test copy", danger: true,
          text: `The test inbox is on in Mail settings, so this mail goes to ${testTo}, NOT to the student. The register does not change.` }
      : { heading: "Send this mail?", ok: "Send mail",
          text: `To ${$("#invite .form-row div").textContent.trim()}, visit date ${visitText(v.visit_date) || "as written in the mail"}, with ${v.attachments.length} attachment(s).` });
    if (!ok) return;
    act(async () => {
      const r = await api(`/api/candidates/${encodeURIComponent(current.arg)}/mail/send`, { method: "POST", body: v });
      UI.inviteEdit = null;
      return r;
    }, (r) => {
      const i = r.invite, how = i.how, to = i.sent_to || "the student";
      if (i.test_only) return `Test copy for ${to}. The student did not get it.`;
      if (i.saved_only) return i.note || "No mail app could open the mail. Download the .eml file.";
      if (how === "outlook_app" && i.status === "sent") return `Sent by ${i.opened_in || "Outlook"} to ${to}.`;
      if (how === "outlook_app") return `Opened in ${i.opened_in || "Outlook / Mail"} — click Send there, then click I sent it here.${i.note ? " " + i.note : ""}`;
      if (how === "graph" || how === "graph_send") return `Mail sent via Microsoft Outlook to ${to}.`;
      if (how === "graph_draft") return `Saved to Outlook Drafts for ${to}. Send it there, then click I sent it.`;
      if (how === "smtp") return `Mail sent by SMTP to ${to}.`;
      return "Saved as an .eml file in the outbox. Send it from your mail app, then click I sent it.";
    });
  },
  "inv-open": () => act(() => api(`/api/candidates/${encodeURIComponent(current.arg)}/mail/open`, { method: "POST" }),
    (r) => `Opened in ${r.opened_in || "Outlook / Mail"}.${r.note ? " " + r.note : ""}`),
  "inv-confirm": () => act(() => api(`/api/candidates/${encodeURIComponent(current.arg)}/mail/confirm`, { method: "POST" }),
    "Marked as sent. The register now shows the mail date."),
  "inv-back": async () => {
    if (!(await confirmBox({ heading: "The mail was not sent?", text: "It goes back to a draft so you can change it and send it again. Nothing is sent now.", ok: "Back to draft" }))) return;
    act(() => api(`/api/candidates/${encodeURIComponent(current.arg)}/mail/back-to-draft`, { method: "POST" }), "The mail is a draft again.");
  },
  "inv-resend": async () => {
    if (!(await confirmBox({ heading: "Send this mail again?", text: "The mail becomes a draft again so you can change it, then click Send mail. The earlier send stays in the history.", ok: "Resend" }))) return;
    act(() => api(`/api/candidates/${encodeURIComponent(current.arg)}/mail/resend`, { method: "POST" }), "Change the mail if needed, then click Send mail.");
  },
  "open-draft-app": (b) => act(async () => {
    const r = await api(`/api/drafts/${b.dataset.id}/open`, { method: "POST" });
    if (!r.ok) throw new Error(r.error || "Could not open Outlook / Mail.");
    return r;
  }, (r) => `Opened in ${r.opened_in || "Outlook / Mail"} — click Send.`),
  "outlook-settings": () => openOutlookSettings(),
  "outlook-draft": (b) => act(async () => {
    const r = await api(`/api/drafts/${b.dataset.id}/outlook-draft`, { method: "POST" });
    if (!r.ok) throw new Error(r.error || "Could not create Outlook draft.");
    return r;
  }, (r) => `Saved draft in Outlook for ${r.sent_to || "candidate"}.`),
  "resend-draft": (b) => act(async () => {
    const r = await api(`/api/drafts/${b.dataset.id}/resend`, { method: "POST" });
    if (!r.ok) throw new Error(r.error || "Outlook delivery failed.");
    return r;
  }, (r) => r.delivery_status === "opened_app" ? `Opened in ${r.opened_in || "Outlook / Mail"} — click Send.`
    : r.delivery_status === "outlook_draft" ? `Saved Outlook draft for ${r.sent_to}.` : `Sent via Outlook to ${r.sent_to}.`),
  "reg-ref": (b) => { $("#reg-ref_no").value = b.dataset.ref; UI.regEdit = readRegister(current.arg); },
  "reg-save": () => act(async () => {
    const r = await api(`/api/candidates/${encodeURIComponent(current.arg)}/register`, { method: "POST", body: readRegister(current.arg) });
    UI.regEdit = null; UI.refSuggestion = r.ref_suggestion; UI.regSuggested = null;
    return r;
  }, (r) => (r.register.warnings || []).length ? `Saved. Please check: ${r.register.warnings.join(" ")}` : "Register details saved."),
  "reply-read": async () => {
    const app = current.arg;
    const text = $("#reply-text").value;
    try {
      const r = await api(`/api/candidates/${encodeURIComponent(app)}/reply`, { method: "POST", body: { text } });
      const keys = Object.keys(r.fields);
      UI.regEdit = { ...readRegister(app), ...r.fields };
      UI.regSuggested = { app, keys };
      UI.replyFor = app; UI.replyText = text; UI.replyOpen = true;
      const n = Object.keys(r.found_by).length;
      toast(n ? `${n} detail(s) filled from the reply. Check the marked fields, then click Save details.${r.notes.length ? " " + r.notes.join(" ") : ""}`
        : `No details were found in the reply. ${r.notes.join(" ")}`, !n);
      lastHTML = ""; render();
    } catch (e) { toast(e.message, true); }
  },
  "bulk-open": () => openBulkDialog().catch((e) => toast(e.message, true)),
  "bulk-clear": () => { UI.regSel = new Set(); lastHTML = ""; pRegister(); },
  "reg-filters-clear": () => { UI.regFrom = UI.regTo = UI.regStatus = UI.regSource = ""; lastHTML = ""; pRegister(); },
  "reg-show": (b) => { UI.regShow = b.dataset.s; history.replaceState(null, "", "#/register"); current = { page: "register", arg: "" }; lastHTML = ""; pRegister(); },
  "att-remove": async (b) => {
    const original = b.textContent.trim() === "Use the original";
    if (!(await confirmBox(original ? { heading: "Use the original file?", text: `Your uploaded version of ${b.dataset.n} is removed and the one that came with the template is used again.`, ok: "Use the original" }
      : { heading: "Remove this attachment?", text: `${b.dataset.n} will no longer be attached to new mails.`, ok: "Remove", danger: true }))) return;
    act(async () => { const r = await api(`/api/mail/attachments/${encodeURIComponent(b.dataset.n)}/remove`, { method: "POST" }); UI.mailFiles = r.attachments; }, "Attachment removed.");
  },
  "ms-shrink": () => act(() => api("/api/mail/outbox/shrink", { method: "POST" }),
    (r) => r.shrunk ? `${r.shrunk} old copy(ies) made smaller, ${fmtSize(r.saved_bytes)} saved.` : "No copies are old enough to shrink yet."),
  "ms-save": () => act(() => api("/api/mail/settings", { method: "POST", body: { subject: $("#ms-subject").value, template: $("#ms-body").value } }), "Template saved. New mails use it."),
  "ms-reset": async () => {
    if (!(await confirmBox({ heading: "Go back to the original template?", text: "Your changes to the subject and the mail text will be removed.", ok: "Go back", danger: true }))) return;
    act(() => api("/api/mail/settings", { method: "POST", body: { reset: true } }), "The original template is back.");
  },
  "save-letter": () => act(async () => {
    await api(`/api/candidates/${encodeURIComponent(current.arg)}/letter`, { method: "POST", body: { start_date: $("#letter-start").value, end_date: $("#letter-end").value } });
    UI.letterDates = null;
  }, "Dates saved. The placement letter was made again."),
  "save-email": () => act(async () => { await api(`/api/candidates/${encodeURIComponent(current.arg)}/email`, { method: "POST", body: { email: $("#email-input").value } }); UI.editEmail = false; }, "Email saved."),
  "set-area": () => act(() => api(`/api/candidates/${encodeURIComponent(current.arg)}/area`, { method: "POST", body: { area: $("#area-pick").value } }),
    (r) => `Area saved. Score ${r.score}/100.`),
  "decide": async (b) => {
    const d = b.dataset.d;
    const text = { shortlist: ["Shortlist this candidate?", "The Coordinator will book an interview and draft the invitation for you to release.", "Shortlist"],
      not_shortlist: ["Mark as Not shortlisted?", "The Coordinator will draft a polite reply for you to release.", "Not shortlisted"],
      confirm: ["Confirm Not shortlisted?", "The Coordinator will draft a polite reply for you to release.", "Confirm"] }[d];
    if (!(await confirmBox({ heading: text[0], text: text[1], ok: text[2] }))) return;
    act(() => api(`/api/candidates/${encodeURIComponent(current.arg)}/decision`, { method: "POST", body: { decision: d } }), "Saved. The AI team takes it from here.");
  },
  "retry": () => act(() => api(`/api/candidates/${encodeURIComponent(current.arg)}/retry`, { method: "POST" }), "Sent back to the Recruiter."),
  "close": async () => {
    if (!(await confirmBox({ heading: "Close this application?", text: "No reply will be drafted. You can still see it under Closed.", ok: "Close application", danger: true }))) return;
    act(() => api(`/api/candidates/${encodeURIComponent(current.arg)}/close`, { method: "POST" }), "Application closed.");
  },
  "resolve": (b) => act(() => api(`/api/candidates/${encodeURIComponent(b.dataset.app || current.arg)}/flags/${b.dataset.i}/resolve`, { method: "POST" }), "Marked as checked."),
  "edit-draft": (b) => { UI.editDraft = +b.dataset.id; lastHTML = ""; render(); },
  "cancel-edit": () => { UI.editDraft = null; lastHTML = ""; render(); },
  "save-draft": async (b) => {
    try {
      const r = await api(`/api/drafts/${b.dataset.id}`, { method: "POST", body: { subject: $("#ed-subject").value, body: $("#ed-body").value } });
      UI.editDraft = null;
      toast(r.check_notes.length ? "Saved, but please read the warnings above the email." : "Saved.", r.check_notes.length > 0);
    } catch (e) { toast(e.message, true); }
    lastHTML = ""; render();
  },
  "release": async (b) => {
    const ol = STATE?.outlook || {};
    const eff = _effectiveOutlookMode(ol);
    const info = eff === "outlook_app"
      ? `Email will open ready to send in Outlook Desktop / Mail (and archive to data/outbox/).`
      : eff === "graph_draft"
      ? `Email will be archived to data/outbox/ AND saved directly into your Outlook Drafts folder.`
      : (eff === "graph" || eff === "graph_send")
      ? (ol.test_recipient ? `Email will be archived and sent via Outlook to demo override (${ol.test_recipient}).` : `Email will be archived and sent directly from your Outlook account to ${b.dataset.name}.`)
      : eff === "smtp"
      ? `Email will be archived and sent via Office 365 SMTP.`
      : `The .eml draft (with PDF attached if acceptance) is saved to data/outbox/ for Outlook Desktop.`;
    if (!(await confirmBox({ heading: `Release the ${b.dataset.kind}?`, text: `To ${b.dataset.name}. ${info}`, ok: "Release" }))) return;
    act(() => api(`/api/drafts/${b.dataset.id}/release`, { method: "POST" }),
      (r) => r.delivery_status === "opened_app" ? `Released and opened in Outlook / Mail for ${r.sent_to}.`
        : r.delivery_status === "sent" ? `Released and sent via Outlook to ${r.sent_to}.`
        : r.delivery_status === "outlook_draft" ? `Released and saved to Outlook Drafts (${r.sent_to}).`
        : r.delivery_status === "failed" ? `Released to outbox (Outlook failed: ${r.delivery_error}).`
        : "Released.");
  },
  "jump": (b, e) => { e.preventDefault(); $("#emails")?.scrollIntoView({ behavior: "smooth" }); },
  "area-new": (b) => areaForm(null, b.dataset.name || ""),
  "area-edit": (b) => areaForm(UI.areas.areas.find((x) => x.id === +b.dataset.id)),
  "area-toggle": async (b) => {
    const a = UI.areas.areas.find((x) => x.id === +b.dataset.id);
    act(() => api(`/api/areas/${a.id}`, { method: "PUT", body: { ...a, required_skills: a.required_skills, active: !a.active } }), a.active ? `${a.name} turned off.` : `${a.name} turned on.`);
  },
  "run": (b) => act(() => api("/api/run", { method: "POST", body: { employee: b.dataset.who } }), "Started. Watch the status update."),
  "pause": (b) => act(() => api("/api/pause", { method: "POST", body: { paused: b.dataset.p === "1" } }), b.dataset.p === "1" ? "The AI team is paused." : "The AI team is working again."),
  "save-result": async () => {
    const picked = document.querySelector('input[name="result"]:checked');
    if (!picked) { toast("Choose Pass, Fail or Hold first.", true); return; }
    const comment = $("#result-comment").value;
    const words = { Pass: "The Onboarding employee will issue a work ID and draft the acceptance email.", Fail: "The Coordinator will draft a polite reply.", Hold: "The Coordinator will draft a short hold note." }[picked.value];
    if (!(await confirmBox({ heading: `Save the result: ${picked.value}?`, text: `${words} The AI team cannot change your result.`, ok: `Save ${picked.value}` }))) return;
    act(() => api(`/api/candidates/${encodeURIComponent(current.arg)}/result`, { method: "POST", body: { result: picked.value, comment } }), "Result saved.");
  },
};
view.addEventListener("click", (e) => {
  const b = e.target.closest("[data-act]");
  if (b && !b.disabled) { ACTS[b.dataset.act]?.(b, e); return; }
  if (e.target.closest("a, button, input, select, textarea, label")) return;
  const row = e.target.closest("[data-href]");
  if (row) location.hash = row.dataset.href;
});

// ================================================================ router
const ROUTES = {
  hr: { dashboard: pDashboard, add: pAdd, pool: pAdd, vacancies: pVacancies, interns: pInterns, candidates: pCandidates, candidate: pCandidate, review: pReview, replies: pReplies, areas: pAreas, team: pTeam },
  interviewer: { interviews: pInterviews, interview: pInterview, team: pTeam, candidate: pCandidate },
  mail: { add: pAdd, pool: pAdd, register: pRegister, mailsettings: pMailSettings, candidate: pCandidate },
};
const AUTO = new Set(["register", "dashboard", "add", "pool", "vacancies", "interns", "candidates", "candidate", "review", "replies", "team", "interviews", "interview"]);
function parseHash() {
  const h = location.hash.replace(/^#\/?/, "");
  const i = h.indexOf("/");
  return i < 0 ? { page: h, arg: "" } : { page: h.slice(0, i), arg: decodeURIComponent(h.slice(i + 1)) };
}
let rendering = false;
async function render() {
  const { page, arg } = parseHash();
  const fn = ROUTES[NK()][page];
  if (!fn) { location.replace(HOME[NK()]); return; }
  if (page !== current.page || arg !== current.arg) { UI.showCv = false; UI.showRuns = false; UI.editDraft = null; UI.editEmail = false; lastHTML = ""; window.scrollTo(0, 0); }
  current = { page, arg };
  renderFrame();
  rendering = true;
  try { await fn(arg); }
  catch (e) { setView(`<div class="box">${empty(e.message)}<a class="btn" href="${HOME[NK()]}">Back to start</a></div>`); }
  finally { rendering = false; }
}
window.addEventListener("hashchange", render);

async function tick() {
  if (document.hidden) return;
  await refreshState();
  if (!rendering && AUTO.has(current.page) && !busy()) {
    try { await ROUTES[NK()][current.page]?.(current.arg, { reuseState: true }); } catch { /* keep the page as it is */ }
  }
}
function scheduleTick() {
  const working = STATE && Object.values(STATE.team || {}).some((x) => x.state === "working");
  const hasNew = STATE && (STATE.tiles?.new || 0) > 0;
  const delay = working || hasNew ? 5000 : 10000;
  setTimeout(async () => { await tick(); scheduleTick(); }, delay);
}
(async function start() {
  await refreshState();
  if (MAILMODE()) ROLE = "hr";                         // mail-only mode has the HR officer's view only
  await render();
  scheduleTick();
})();
