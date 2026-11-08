"use strict";
const $ = (s) => document.querySelector(s);
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const pagePath = location.pathname.replace(/^\/|\/$/g, "") || "submit";
// /leaderboard is a team-workspace subpage; /admin/leaderboard is the staff one.
// `route` names the workspace shell ("admin", "submit" or "leaderboard").
const leaderboardView = pagePath === "leaderboard" || pagePath === "admin/leaderboard";
const route = pagePath.startsWith("admin") ? "admin" : pagePath;
const labels = {
  full: "Full evaluation",
  public: "Public test",
  infra: "Infrastructure test",
};
const totals = { full: 39, public: 5, infra: 3 };
const stateNames = {
  queued: ["Queued", "amber"],
  testing: ["Running", "blue"],
  attention: ["Needs attention", "red"],
  completed: ["Completed", "green"],
  cancelled: ["Cancelled", "gray"],
  cancelling: ["Cancelling", "amber"],
  approved: ["Approved", "green"],
  pending_review: ["Under review", "amber"],
  rejected: ["Rejected", "red"],
  cancelled: ["Cancelled", "gray"],
};
let platformConfig = {},
  user = null,
  versions = [],
  batches = [],
  adminData = null,
  pendingRequest = null,
  pollBusy = false,
  refreshAgain = false,
  submissionIntents = [],
  uploadBusy = false,
  uploadAttempt = null,
  serverOffset = 0,
  traceBatchId = null,
  tracePanelBusy = false,
  toastTimer;
function stamp(v) {
  return new Intl.DateTimeFormat(route === "admin" && window.staffLocale === "zh" ? "zh-CN" : "en-GB", {
    timeZone: "Asia/Singapore",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(v * 1000));
}
function badge(state) {
  const [name, cls] = stateNames[state] || [state, "gray"];
  return `<span class="pill ${cls}">${esc(name)}</span>`;
}
function modeLabel(mode) {
  return mode === "scientific"
    ? "Scientific evaluation"
    : mode === "scientific-smoke"
      ? "Scientific diagnostic · unranked"
      : mode === "codex"
        ? "Legacy Codex · mock"
        : "Simulation";
}
function toast(message, error = false) {
  const t = $("#toast");
  t.textContent = message;
  t.classList.toggle("error", error);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 6500);
}
async function api(path, options = {}) {
  const headers = { "X-QIQC-Request": "1", "X-QIQC-Workspace": route === "admin" ? "staff" : "team", ...(options.headers || {}) };
  if (options.json) {
    options.body = JSON.stringify(options.json);
    headers["Content-Type"] = "application/json";
    delete options.json;
  }
  let r;
  try {
    r = await fetch("/api" + path, { ...options, headers, signal: options.signal || AbortSignal.timeout(90000) });
  } catch (cause) {
    const error = new Error(cause.name === "TimeoutError"
      ? "The request timed out. Check the current status before retrying."
      : "Connection interrupted. Check your connection and try again.");
    error.uncertain = options.method && options.method !== "GET";
    throw error;
  }
  let result;
  try {
    result = await r.json();
  } catch {
    const error = new Error(
      r.status === 429
        ? "Too many requests. Wait a moment and try again."
        : "The service could not complete the request. Please try again.",
    );
    error.status = r.status;
    error.uncertain = !!options.method && options.method !== "GET" && r.status >= 500;
    throw error;
  }
  if (!r.ok) {
    const error = new Error(result.error || `Request failed (${r.status})`);
    error.status = r.status;
    error.uncertain = !!options.method && options.method !== "GET" && r.status >= 500;
    throw error;
  }
  return result;
}
function heading(eyebrow, title, subtitle) {
  return `<div class="page-heading"><div><span class="eyebrow">${eyebrow}</span><h1>${title}</h1><p>${subtitle}</p></div><div class="heading-aside"><div class="right"><span class="live-dot"></span> Asia/Singapore · UTC+8</div></div></div>`;
}
function calendarNotice() {
  const c = platformConfig.calendar;
  if (!c || c.mode !== "scheduled") return "";
  const fmt = (v) =>
    new Intl.DateTimeFormat(route === "admin" && window.staffLocale === "zh" ? "zh-CN" : "en-GB", {
      timeZone: "Asia/Singapore",
      dateStyle: "medium",
      timeStyle: "short",
      hour12: false,
    }).format(new Date(v));
  return `<p id="calendar-notice" class="help">Admission window (UTC+8): ${esc(fmt(c.starts_at))} to ${esc(fmt(c.ends_at))}; appeal deadline: ${esc(fmt(c.appeal_closes_at))}. Server acceptance time applies.</p>`;
}
function phaseNotice() {
 const phase=platformConfig.phase;
 if(!phase)return "Evaluation status is being checked.";
 const next=phase.next_at ? ` Next boundary: ${new Intl.DateTimeFormat("en-GB",{timeZone:"Asia/Singapore",dateStyle:"medium",timeStyle:"short",hour12:false}).format(new Date(phase.next_at))} (UTC+8).` : "";
 return phase.reason+next;
}
function banner() {
 return `<div class="banner" role="status"><strong>Event status</strong><span id="phase-notice">${esc(phaseNotice())}</span><span id="event-countdown"></span></div>`+calendarNotice();
}
// Participant pages show the whole schedule; the current stage is highlighted.
function eventStages() {
  const c = platformConfig.calendar;
  if (!c || c.mode !== "scheduled") return [];
  const quiet = c.infra_ends_at || new Date(Date.parse(c.starts_at) - 6 * 3600000).toISOString();
  return [
    { phase: "practice", title: "Infrastructure test", from: c.infra_starts_at, to: quiet, note: "3 infrastructure cases · unranked" },
    { phase: "quiet", title: "Quiet period", from: quiet, to: c.starts_at, note: "Uploads closed" },
    { phase: "competition", title: "Competition", from: c.starts_at, to: c.ends_at, note: "Public test and full evaluation" },
    { phase: "closed", title: "Appeals close", from: c.appeal_closes_at, note: "Results final after review" },
  ].filter((x) => x.from);
}
function eventTimeline(showStatus) {
  const stages = eventStages();
  if (!stages.length) return showStatus ? banner() : "";
  const fmt = (v) => new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Singapore", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(v));
  const current = platformConfig.phase?.name;
  const items = stages.map((x) => `<li class="${x.phase === current ? "current" : ""}"><span class="stage-title">${esc(x.title)}${x.phase === current ? ' <em class="stage-now">Now</em>' : ""}</span><time>${esc(fmt(x.from))}${x.to ? " – " + esc(fmt(x.to)) : ""}</time><span class="stage-note">${esc(x.note)}</span></li>`).join("");
  const status = showStatus ? `<p class="timeline-status"><strong>Event status</strong> <span id="phase-notice">${esc(platformConfig.phase?.reason || "Evaluation status is being checked.")}</span></p>` : "";
  return `<section class="banner event-timeline" role="status" aria-label="Event timeline"><div class="timeline-head"><strong>Event timeline</strong><span>UTC+8 · <span id="event-countdown"></span></span></div><ol>${items}</ol>${status}</section>`;
}
function updateEventClock(){
 const next=platformConfig.phase?.next_at;
 const node=$("#event-countdown");if(!node)return;
 if(!next){node.textContent="";return}
 const seconds=Math.max(0,Math.ceil((Date.parse(next)-Date.now()-serverOffset)/1000));
 const pad=n=>String(n).padStart(2,"0"),d=Math.floor(seconds/86400),h=Math.floor(seconds%86400/3600),m=Math.floor(seconds%3600/60);
 const clock=d?`${d}d ${pad(h)}h ${pad(m)}m`:`${h}h ${pad(m)}m ${pad(seconds%60)}s`;
 if(!$(".event-timeline")){node.textContent=clock;return}
 const stage=eventStages().find(x=>Date.parse(x.from)===Date.parse(next));
 node.textContent=stage?`${stage.title} starts in ${clock}`:`Current stage ends in ${clock}`;
}
function stat(label, number, caption, icon = "") {
  return `<div class="stat"><div class="label">${label}${icon ? `<span class="type-icon">${icon}</span>` : ""}</div><div class="number">${number}</div><span class="caption">${caption}</span></div>`;
}
function reviewReason(v) {
  if (v.status === "pending_review")
    return "Submission frozen; awaiting security review. No attempt charged.";
  if (v.status === "rejected")
    return "This version was not approved. Review the findings below or contact the organizers by email.";
  if (v.status === "cancelled")
    return "Submission cancelled before acceptance. No attempt charged.";
  const reason = String(v.reason || "");
  if (reason.startsWith("自动审查通过"))
    return "Automated review passed. Execution remains sandboxed.";
  if (reason.startsWith("已冻结提交"))
    return "Submission frozen; awaiting security review. No attempt charged.";
  if (reason.startsWith("自动审查未通过"))
    return "Automated review did not approve this version. No attempt charged.";
  return reason && !/[\u4e00-\u9fff]/.test(reason) ? reason : "Review status is recorded below.";
}
function versionReviewHTML(v) {
  const review = v.review;
  if (!review) return "";
  const staticRisks = Array.isArray(review.static_risks) ? review.static_risks : [];
  const advisory = review.ai_advisory || {};
  const findings = Array.isArray(advisory.findings) ? advisory.findings : [];
  const staticList = staticRisks.length
    ? `<h4>Static findings</h4><ul class="small">${staticRisks.map((risk) => `<li><strong>${esc(risk.rule || "Static risk")}</strong> · <code>${esc(risk.file || "Unknown file")}</code>${Number.isInteger(Number(risk.line)) && Number(risk.line) > 0 ? ` · line ${esc(String(risk.line))}` : ""}${risk.explanation ? `: ${esc(risk.explanation)}` : ""}</li>`).join("")}</ul>`
    : "";
  const aiList = findings.length
    ? `<h4>Automated review details</h4><ul class="small">${findings.map((finding) => `<li><strong>${esc(finding.rule || "Review finding")}</strong> · <code>${esc(finding.file || "Unknown file")}</code>${Number.isFinite(Number(finding.confidence)) ? ` · ${esc(String(finding.confidence))}` : ""}${finding.explanation ? `: ${esc(finding.explanation)}` : ""}</li>`).join("")}</ul>`
    : "";
  const summary = advisory.summary ? `<p class="small">${esc(advisory.summary)}</p>` : "";
  const followup = advisory.manual_followup_required
    ? `<p class="small muted">This review requires organizer follow-up. The result shown here is advisory evidence; the final version status is authoritative.</p>`
    : "";
  if (!staticList && !aiList && !summary && !followup) return "";
  return `<details class="version-review"><summary>View review details</summary>${staticList}${aiList}${summary}${followup}</details>`;
}
function empty(text) {
  return `<div class="empty">${text}</div>`;
}
function updateIdentity() {
  $("#identity").textContent = user ? user.team : "";
  $("#logout").hidden = !user;
}
// Password inputs can be revealed; the toggle never submits its form.
function passwordField(id, label, autocomplete, extra = "") {
  return `<label class="field"><span>${label}</span><span class="password-input"><input id="${id}" type="password" autocomplete="${autocomplete}" required ${extra}><button type="button" class="reveal" data-reveal="${id}" aria-label="Show password" aria-pressed="false">Show</button></span></label>`;
}
function bindReveal(root) {
  root.querySelectorAll("[data-reveal]").forEach((b) => (b.onclick = () => {
    const input = document.getElementById(b.dataset.reveal), show = input.type === "password";
    input.type = show ? "text" : "password";
    b.textContent = show ? "Hide" : "Show";
    b.setAttribute("aria-label", show ? "Hide password" : "Show password");
    b.setAttribute("aria-pressed", String(show));
  }));
}
function loginShell(panel) {
  const admin = route === "admin";
  $("#app").innerHTML =
    heading(
      "WELCOME TO QIQC",
      admin ? "Staff console" : leaderboardView ? "Leaderboard" : "Submit your harness",
      admin ? "Manage team access, review submissions and monitor competition operations." : leaderboardView ? "The leaderboard is visible to participating teams. Sign in with your team account." : "Manage your submissions, start an evaluation and follow your results.",
    ) +
    (admin ? banner() : eventTimeline(false)) +
    `<section class="panel login-panel"><div class="panel-body">${panel}</div></section>`;
  updateEventClock();
  bindReveal($("#app"));
}
function loginPage() {
  const admin = route === "admin";
  if (admin && typeof clearStaffAccountSecrets === "function") clearStaffAccountSecrets();
  loginShell(`<span class="eyebrow">${admin ? "ORGANIZER ACCESS" : "TEAM ACCESS"}</span><h2>${admin ? "Staff sign in" : "Team sign in"}</h2><p class="muted">Use the account issued by the organizers.</p><form id="login-form"><label class="field"><span>Email address</span><input id="email" type="email" autocomplete="username" required placeholder="name@example.com"></label>${passwordField("password", "Password", "current-password")}<button class="button primary full-width" type="submit">Sign in →</button></form>${admin ? "" : '<p class="login-note">Forgot your password? Email the organizers from your team account email and ask for a reset.</p>'}`);
  $("#login-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const b = ev.currentTarget.querySelector("button[type=submit]");
    b.disabled = true;
    try {
      user = await api("/login", {
        method: "POST",
        json: { email: $("#email").value, password: $("#password").value },
      });
      render();
    } catch (err) {
      toast(err.message, true);
    } finally {
      b.disabled = false;
    }
  };
}
function changePage() {
  $("#app").innerHTML =
    heading(
      "ACCOUNT SECURITY",
      "Set your password",
      "Replace your temporary password before using your account.",
    ) +
    `<section class="panel login-panel"><div class="panel-body"><h2>Change temporary password</h2><form id="password-form">${passwordField("new-password", "New password (at least 12 characters)", "new-password", 'minlength="12"')}${passwordField("confirm-password", "Confirm new password", "new-password", 'minlength="12"')}<button class="button primary full-width" type="submit">Save password →</button></form></div></section>`;
  bindReveal($("#app"));
  $("#password-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const button = ev.currentTarget.querySelector("button[type=submit]");
    if (button.disabled) return;
    if ($("#new-password").value !== $("#confirm-password").value) return toast("The two passwords do not match.", true);
    button.disabled = true;
    try {
      await api("/password", {
        method: "POST",
        json: { password: $("#new-password").value },
      });
      user = await api("/me");
      render();
      toast("Password updated.");
    } catch (err) {
      toast(err.message, true);
    } finally {
      button.disabled = false;
    }
  };
}
const kindOptions = [
  ["infra", "Infrastructure test", "3 infrastructure cases · unranked"],
  ["public", "Public test", "5 public tasks · unranked"],
  ["full", "Full evaluation", "39 normal tasks · counts for the leaderboard"],
];
const selectedKind = () => document.querySelector('input[name="kind"]:checked')?.value || "infra";
function submitPage() {
 if(user.role!=="team")return loginPage();
 const templates=[["codex","Codex"],["deepseek","DeepSeek Harness"],["pi","Pi"]].map(([id,name])=>`<a class="template-link" href="/adapters/${id}/harness.zip" download="qiqc-${id}-harness.zip">${name} <span aria-hidden="true">↓</span></a>`).join("");
 $("#app").innerHTML=heading("YOUR WORKSPACE / "+esc(user.team),"Submit your harness","Choose an available evaluation and upload. Review, admission and execution continue automatically.")+'<button type="button" class="leaderboard-cta" id="leaderboard-toggle"><span><strong>Leaderboard</strong><small>Show live standings in this workspace</small></span><span aria-hidden="true">→</span></button><section class="panel inline-leaderboard" id="inline-leaderboard" hidden><div class="panel-head"><h2>Leaderboard</h2><button type="button" class="button secondary compact" id="leaderboard-hide">Hide</button></div><div class="leader-hero"><div><span class="eyebrow">BENCHMARKING AGENT HARNESSES</span><p>Full evaluation results update automatically; public practice is unranked.</p></div><div class="hero-numbers"><div>39<span>NORMAL TASKS / EVALUATION</span></div><div>01<span>FULL ATTEMPT</span></div></div></div><div class="queue-counters" aria-live="polite"><div class="counter"><span class="counter-label"><i class="pending-dot"></i>Queued evaluations</span><strong id="inline-queued-count">–</strong></div><div class="counter"><span class="counter-label"><i class="running-dot"></i>Running evaluations</span><strong id="inline-running-count">–</strong></div><div class="counter-updated"><span class="live-dot"></span> Live · updated <span id="inline-leader-updated">–</span></div></div><div id="inline-leader-table"></div></section>'+eventTimeline(true)+
 `<section class="stats" id="quota-stats"></section>
 <section class="panel"><div class="panel-head"><h2>Upload and evaluate</h2></div>
 <div class="panel-body"><div class="template-row"><span>New here? Download a starter template, edit it, then upload it below:</span><span class="template-links">${templates}</span></div>
 <fieldset class="kind-picker"><legend>Evaluation type</legend>${kindOptions.map(([value,title,note],i)=>`<label class="kind-option"><input type="radio" name="kind" value="${value}"${i?"":" checked"}><span class="kind-title">${title}</span><span class="kind-note">${note}</span><span class="kind-left" data-kind-left="${value}"></span></label>`).join("")}</fieldset>
 <select id="mode-select" hidden><option value="scientific">Scientific</option></select><select id="version-select" hidden></select><button id="prepare-evaluation" hidden></button>
 <label class="upload-zone" id="drop-zone"><input id="zip-file" type="file" accept=".zip,application/zip" class="visually-hidden"><span class="upload-empty"><span class="upload-icon" aria-hidden="true">↥</span><strong>Drop harness.zip here or click to choose</strong><span class="upload-hint">One ZIP · harness/ + adapter/ + harness.json · up to 20 MiB</span></span></label>
 <div id="file-chip" class="file-chip" hidden><span class="file-icon" aria-hidden="true">ZIP</span><span class="file-meta"><strong id="file-name"></strong><small id="file-size"></small></span><button type="button" id="file-remove" class="file-remove" aria-label="Remove selected file" title="Remove file">×</button></div>
 <p id="run-hint" class="help"></p>
 <button id="upload-button" class="button primary">Upload and evaluate →</button>
 <div id="upload-feedback" class="submission-feedback" role="status" aria-live="polite">No upload in progress.</div><div id="submission-status" aria-live="polite"></div></div></section>
 <section class="panel"><div class="panel-head"><div><h2>Evaluation history</h2><span class="small muted">Complete results appear automatically</span></div><button type="button" class="button secondary compact" id="trace-history-open">View traces</button></div><div id="batch-list"></div></section>
 <section class="panel trace-panel" id="trace-panel" hidden aria-live="polite"></section>
 <section class="panel"><div class="panel-head"><h2>Submission versions</h2><span class="small muted">Immutable files · SHA-256 verified</span></div><div id="version-list"></div></section>`;
 $("#leaderboard-toggle").onclick=()=>{const panel=$("#inline-leaderboard");panel.hidden=false;$("#leaderboard-toggle").hidden=true;refresh();panel.scrollIntoView({behavior:"smooth",block:"start"})};
 $("#leaderboard-hide").onclick=()=>{$("#inline-leaderboard").hidden=true;$("#leaderboard-toggle").hidden=false};
 $("#upload-button").onclick=uploadFile;
 document.querySelectorAll('input[name="kind"]').forEach(r=>r.onchange=resetUploadSelection);
 const input=$("#zip-file"),zone=$("#drop-zone");
 input.onchange=resetUploadSelection;
 zone.ondragover=e=>{e.preventDefault();if(!uploadBusy)zone.classList.add("drag")};zone.ondragleave=()=>zone.classList.remove("drag");
 zone.ondrop=e=>{e.preventDefault();zone.classList.remove("drag");if(uploadBusy||!e.dataTransfer.files.length)return;if(e.dataTransfer.files.length>1)toast("Upload one ZIP file at a time.",true);const dt=new DataTransfer();dt.items.add(e.dataTransfer.files[0]);input.files=dt.files;resetUploadSelection()};
 $("#file-remove").onclick=()=>{if(uploadBusy)return;input.value="";resetUploadSelection()};
 updateRunHint();updateEventClock();
}
// Feedback belongs to the selected file/request, never to the team or filename.
// Changing bytes/type starts a new request; retrying an uncertain receipt keeps
// the existing key until the user explicitly changes the selection.
function resetUploadSelection(){
 if(uploadBusy)return;
 uploadAttempt=null;
 showSelectedFile();
 const file=$("#zip-file")?.files[0];
 $("#upload-feedback").textContent=file ? `Ready to upload ${file.name}. This file has not been checked yet.` : "No upload in progress.";
 clearTimeout(toastTimer);
 $("#toast").hidden=true;
 updateRunHint();
}
function uploadValidationError(message){
 $("#upload-feedback").textContent=message;
 toast(message,true);
}
function showSelectedFile(){
 const file=$("#zip-file")?.files[0];
 $("#file-chip").hidden=!file;$("#drop-zone").hidden=!!file;
 if(file){$("#file-name").textContent=file.name;$("#file-size").textContent=file.size<1048576?`${Math.max(1,Math.round(file.size/1024))} KiB`:`${(file.size/1048576).toFixed(1)} MiB`}
}
function evaluationAvailability(config, mode, kind) {
  if (mode === "scientific") {
    const taskSet = config.scientific_task_set || {};
    if (!taskSet.configured)
      return "The scientific task set is not configured.";
    if (!taskSet[`${kind}_ready`])
      return kind === "public"
        ? "Public tests are unavailable until all five tasks are configured."
        : "This evaluation type is not ready.";
  }
  if (mode === "scientific-smoke" && kind !== "infra")
    return "Legacy diagnostics support one infrastructure task only.";
  return "";
}
function updateRunHint() {
 if(!$("#upload-button"))return;
 const allowed=platformConfig.phase?.allowed_kinds||[];
 const radios=[...document.querySelectorAll('input[name="kind"]')];
 for(const r of radios){r.disabled=uploadBusy||!allowed.includes(r.value);const left=document.querySelector(`[data-kind-left="${r.value}"]`);if(left&&user?.quotas)left.textContent=`${user.quotas[r.value]} attempts left`}
 if(!allowed.includes(selectedKind())&&allowed.length){const r=radios.find(x=>x.value===allowed[0]);if(r)r.checked=true}
 const kind=selectedKind();
 const blocked=evaluationAvailability(platformConfig,"scientific",kind);
 const active=batches.some(b=>["queued","testing","attention","cancelling"].includes(b.status));
 const reviewing=submissionIntents.some(i=>i.state==="reviewing" || (i.state==="accepted" && !batches.some(b=>b.id===i.batch_id)));
 const exhausted=user?.quotas?.[kind]===0;
 $("#zip-file").disabled=uploadBusy;$("#drop-zone").classList.toggle("disabled",uploadBusy);
 $("#run-hint").textContent=(active ? "Your existing evaluation must finish or be resolved by staff before another upload." : reviewing ? "Your submission is under review. Evaluation will start automatically after approval." : exhausted ? "No attempts remain for this evaluation type." : "")||(!allowed.includes(kind)?"This evaluation type is not open now. See the event timeline above.":blocked)||`This request runs ${totals[kind]} tasks. ${kind==="full"?"A complete valid result updates your team's best score immediately.":"Results do not enter the leaderboard."} An attempt is charged only after acceptance.`;
 $("#upload-button").disabled=uploadBusy||active||reviewing||exhausted||!!blocked||!platformConfig.phase?.upload_allowed||!allowed.includes(kind);
}
function renderSubmissionStatus(){
 const container=$("#submission-status");if(!container)return;
 container.innerHTML=submissionIntents.map(i=>{
  const batch=batches.find(b=>b.id===i.batch_id);const state=batch?.status||i.state;
  const label={reviewing:"Uploaded · automated review",accepted:"Accepted",blocked:"Not accepted",rejected:"Review rejected"}[state]||stateNames[state]?.[0]||state;
  const canCancel = i.state === "reviewing";
  return `<article class="submission-status-item"><strong>${esc(label)}</strong><p>${esc(batch ? (batch.status==="completed" ? (batch.score ? `Evaluation completed: ${batch.score.passed}/${batch.score.total}.` : "Evaluation completed. Score details are being refreshed.") : batch.status==="queued" ? "Your evaluation is queued." : batch.status_message || "Your evaluation is in progress. Complete results will appear here.") : i.reason)}</p><small class="mono">${esc(i.sha256.slice(0,16))} · ${esc(stamp(i.updated))}</small>${canCancel ? `<button type="button" class="text-button small" data-cancel-intent="${esc(i.id)}">Cancel before acceptance</button>` : ""}</article>`
 }).join("");
 container.querySelectorAll("[data-cancel-intent]").forEach((button) => button.addEventListener("click", async () => {
   button.disabled = true;
   try {
     await api(`/submissions/${encodeURIComponent(button.dataset.cancelIntent)}/cancel`, {method:"POST"});
     toast("Submission cancelled before acceptance. No attempt was charged.");
     await refresh();
   } catch (err) {
     toast(err.message, true);
     button.disabled = false;
   }
 }));
}
async function uploadFile(){
 if(uploadBusy)return;
 const file=$("#zip-file").files[0];if(!file)return uploadValidationError("Choose harness.zip first.");
 if(!/\.zip$/i.test(file.name))return uploadValidationError("Choose a ZIP archive containing your harness and adapter.");
 if(file.size>20*1024*1024)return uploadValidationError("The ZIP archive exceeds the 20 MiB upload limit.");
 const kind=selectedKind();
 if(!uploadAttempt||uploadAttempt.file!==file||uploadAttempt.kind!==kind)uploadAttempt={file,kind,key:crypto.randomUUID()};
 uploadBusy=true;updateRunHint();const feedback=$("#upload-feedback");feedback.textContent="Uploading and checking package…";
 try{
  const intent=await api("/submissions",{method:"POST",body:file,headers:{"Content-Type":"application/zip","X-QIQC-Evaluation":kind,"Idempotency-Key":uploadAttempt.key}});
  feedback.textContent="Upload received. Review and evaluation continue automatically; you may close this page.";
  submissionIntents=[intent,...submissionIntents.filter(i=>i.id!==intent.id)];renderSubmissionStatus();await refresh();
 }catch(error){feedback.textContent=error.uncertain
   ? `Upload receipt not confirmed: ${error.message} Keep this page open and retry the same file with the same request key; accepted requests are not charged twice.`
   : `Upload or admission failed: ${error.message}`;toast(error.message,true)}
 finally{uploadBusy=false;updateRunHint()}
}
function prepare() {
  const vid = $("#version-select").value,
    kind = selectedKind(),
    mode = $("#mode-select").value;
  const v = versions.find((x) => x.id === vid);
  if (!v || v.status !== "approved")
    return toast("Upload a harness and wait for approval first.", true);
  const blocked = evaluationAvailability(platformConfig, mode, kind);
  if (blocked) return toast(blocked, true);
  pendingRequest = {
    version_id: vid,
    kind,
    mode,
    idempotency_key: crypto.randomUUID(),
  };
  $("#confirm-content").innerHTML =
    `<div class="dialog-row"><span>Version</span><strong>${esc(v.name)}</strong></div><div class="dialog-row"><span>SHA-256</span><code>${esc(v.sha256.slice(0, 16))}…</code></div><div class="dialog-row"><span>Evaluation</span><strong>${labels[kind]} · ${totals[kind]} tasks</strong></div><div class="dialog-row"><span>Execution</span><strong>${modeLabel(mode)}</strong></div><div class="dialog-row"><span>Attempt usage</span><strong>1 attempt / available: ${user.quotas[kind]} attempts</strong></div>`;
  $("#confirm-dialog").showModal();
}
$("#confirm-back").onclick = () => $("#confirm-dialog").close();
$("#confirm-evaluate").onclick = async () => {
  const b = $("#confirm-evaluate");
  b.disabled = true;
  try {
    await api("/batches", { method: "POST", json: pendingRequest });
    $("#confirm-dialog").close();
    toast("Evaluation accepted. This version is frozen.");
    await refresh();
  } catch (err) {
    toast(err.message, true);
  } finally {
    b.disabled = false;
  }
};
function publicTestNumber(batch) {
  if (Number.isInteger(batch?.public_test_number) && batch.public_test_number > 0)
    return batch.public_test_number;
  const rows = batches
    .filter((x) => x.kind === "public")
    .slice()
    .sort((a, b) => Number(a.accepted_at || 0) - Number(b.accepted_at || 0) || String(a.id).localeCompare(String(b.id)));
  const index = rows.findIndex((x) => x.id === batch?.id);
  return index >= 0 ? index + 1 : null;
}
function tracePanelTitle(batch) {
  if (batch?.kind === "public") {
    const attempt = publicTestNumber(batch);
    return attempt ? `Public test #${attempt} trace` : "Public test trace";
  }
  return "Public test traces";
}
function tracePanelDescription(batch) {
  if (batch?.kind === "public") return "Five filtered trajectory files from this Public test. The full rollout is never downloadable.";
  return "Only the three completed Public tests provide downloadable trajectories. Full evaluation never exposes traces.";
}
function renderTracePanel() {
  const panel = $("#trace-panel");
  if (!panel) return;
  const batch = batches.find((x) => x.id === traceBatchId);
  if (!batch) {
    if (traceBatchId !== "__empty__") {
      panel.hidden = true;
      panel.innerHTML = "";
      return;
    }
    panel.hidden = false;
    panel.innerHTML = `<div class="trace-panel-head"><div><span class="eyebrow">YOUR TEAM ONLY</span><h2>Public test traces</h2><p class="muted">Filtered trajectory files from your completed Public tests. The full rollout is never downloadable.</p></div><button type="button" class="trace-refresh" data-trace-refresh-empty aria-label="Refresh evaluation history" title="Refresh evaluation history"><span aria-hidden="true">↻</span></button></div><div class="trace-panel-body"><div class="empty trace-empty">No downloadable trace is available yet. Refresh after this Public test finishes.</div></div>`;
    panel.querySelector("[data-trace-refresh-empty]")?.addEventListener("click", () => refresh());
    return;
  }
  // Full evaluations deliberately have no participant trace surface, even if
  // their frozen task set contains public task identities.
  const files = batch.kind === "public" && Array.isArray(batch.public_runs) ? batch.public_runs : [];
  const attempt = publicTestNumber(batch);
  const folderLabel = batch.kind === "public" && attempt ? `Public test #${attempt} trace` : "Public-task trace folder";
  const filesHTML = files.length
    ? `<div class="trace-folder"><span class="trace-folder-icon" aria-hidden="true">📁</span><div><strong>${esc(folderLabel)}</strong><small>${files.length} filtered trace file${files.length === 1 ? "" : "s"} · no full rollout</small></div></div><ul class="trace-file-list">${files.map((r, i) => `<li><span class="trace-file-icon" aria-hidden="true">📄</span><span class="trace-file-name">${esc(r.label || `Public task ${i + 1}`)} trace</span><button type="button" class="button secondary compact" data-panel-trace-view="${esc(batch.id)}" data-panel-trace-ordinal="${r.ordinal}">View</button><button type="button" class="button secondary compact" data-panel-trace-download="${esc(batch.id)}" data-panel-trace-ordinal="${r.ordinal}" data-panel-trace-file="public-test-${attempt || "evaluation"}-task-${i + 1}-trace.txt">Download</button></li>`).join("")}</ul><div id="trace-preview" class="trace-preview" hidden><div class="trace-preview-head"><strong id="trace-preview-title"></strong><span class="small muted">Filtered public trace</span></div><pre id="trace-preview-content"></pre></div>`
    : `<div class="empty trace-empty">No downloadable trace is available yet. Refresh after this Public test finishes.</div>`;
  panel.hidden = false;
  panel.innerHTML = `<div class="trace-panel-head"><div><span class="eyebrow">YOUR TEAM ONLY</span><h2>${esc(tracePanelTitle(batch))}</h2><p class="muted">${esc(tracePanelDescription(batch))}</p></div><button type="button" class="trace-refresh${tracePanelBusy ? " is-spinning" : ""}" data-trace-refresh="${esc(batch.id)}" aria-label="Refresh public traces" title="Refresh public traces"><span aria-hidden="true">↻</span></button></div><div class="trace-panel-body">${filesHTML}</div>`;
  panel.querySelector("[data-trace-refresh]")?.addEventListener("click", () => refreshTracePanel(batch.id));
  panel.querySelectorAll("[data-panel-trace-view]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const trace = await api(`/batches/${encodeURIComponent(button.dataset.panelTraceView)}/traces/${encodeURIComponent(button.dataset.panelTraceOrdinal)}`);
      const preview = $("#trace-preview"), title = $("#trace-preview-title"), content = $("#trace-preview-content");
      if (!preview || !title || !content) return;
      title.textContent = `${button.closest("li")?.querySelector(".trace-file-name")?.textContent || "Public task trace"}`;
      content.textContent = trace.text || "No trace text was returned.";
      preview.hidden = false;
      preview.scrollIntoView({ block: "nearest", behavior: "smooth" });
    } catch (err) {
      toast(err.message, true);
    } finally {
      button.disabled = false;
    }
  }));
  panel.querySelectorAll("[data-panel-trace-download]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const trace = await api(`/batches/${encodeURIComponent(button.dataset.panelTraceDownload)}/traces/${encodeURIComponent(button.dataset.panelTraceOrdinal)}`);
      const url = URL.createObjectURL(new Blob([trace.text || ""], { type: "text/plain;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = button.dataset.panelTraceFile;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      toast(err.message, true);
    } finally {
      button.disabled = false;
    }
  }));
}
async function openTracePanel(batchID) {
  traceBatchId = batchID;
  tracePanelBusy = false;
  renderTeamData();
  await refreshTracePanel(batchID);
}
function openLatestTracePanel() {
  const latest = batches
    .filter((batch) => batch.kind === "public")
    .slice()
    .sort((a, b) => Number(b.accepted_at || 0) - Number(a.accepted_at || 0) || String(b.id).localeCompare(String(a.id)))[0];
  if (latest) return openTracePanel(latest.id);
  traceBatchId = "__empty__";
  tracePanelBusy = false;
  renderTeamData();
}
async function refreshTracePanel(batchID) {
  if (tracePanelBusy && traceBatchId === batchID && $("[data-trace-refresh]")) return;
  traceBatchId = batchID;
  tracePanelBusy = true;
  renderTracePanel();
  try {
    const latest = await api(`/batches/${encodeURIComponent(batchID)}`);
    const index = batches.findIndex((x) => x.id === batchID);
    if (index >= 0) batches[index] = latest;
    else batches = [latest, ...batches];
  } catch (err) {
    toast(err.message, true);
  } finally {
    tracePanelBusy = false;
    renderTeamData();
  }
}
function renderTeamData() {
  const q = user.quotas;
  $("#quota-stats").innerHTML =
    stat(
      "Full evaluation",
      `${q.full}<small>/ 1</small>`,
      "39 normal tasks · the one complete evaluation counts",
    ) +
    stat(
      "Public test",
      `${q.public}<small>/ 3</small>`,
      "5 public tasks · unranked",
    ) +
    stat(
      "Infrastructure test",
      `${q.infra}<small>/ 10</small>`,
      "Infrastructure allowance · unranked",
    );
  const select = $("#version-select"),
    previous = select.value;
  const approved = versions.filter((v) => v.status === "approved");
  select.innerHTML = approved.length
    ? approved
        .map(
          (v) =>
            `<option value="${v.id}">${esc(v.name)} · ${v.sha256.slice(0, 8)}</option>`,
        )
        .join("")
    : '<option value="">Upload a harness and wait for approval</option>';
  if (approved.some((v) => v.id === previous)) select.value = previous;
  const active = batches.some((b) =>
    ["queued", "testing", "attention", "cancelling"].includes(b.status),
  );
  $("#prepare-evaluation").disabled = active || !approved.length;
  $("#prepare-evaluation").textContent = active
    ? "Wait for your active evaluation to finish"
    : "Review evaluation →";
  updateRunHint();
  $("#version-list").innerHTML = versions.length
    ? `<div class="table-wrap"><table><thead><tr><th>Version / SHA-256</th><th>Review status</th><th>Uploaded (UTC+8)</th><th>Feedback</th></tr></thead><tbody>${versions.map((v) => `<tr><td><div class="primary-text">${esc(v.name)}</div><div class="sub mono">${v.sha256.slice(0, 16)}</div></td><td>${badge(v.status)}</td><td>${stamp(v.created)}</td><td><div class="small">${esc(reviewReason(v))}</div><div class="sub">${v.files} files · ${v.risk_count} static findings</div>${versionReviewHTML(v)}</td></tr>`).join("")}</tbody></table></div>`
    : empty("No submissions yet. Download an adapter template to get started.");
  $("#batch-list").innerHTML = batches.length
    ? `<div class="table-wrap"><table><thead><tr><th>Evaluation / version</th><th>Status</th><th>Result</th><th>Accepted (UTC+8)</th><th>Actions</th></tr></thead><tbody>${batches.map((b) => `<tr><td><div class="primary-text">${labels[b.kind]}</div><div class="sub mono" title="Evaluation ID">ID ${esc(b.id)}</div><div class="sub">${modeLabel(b.mode)}${b.task_time_limits === "native" ? " · original task time limits" : b.bounded_scientific ? ` · agent limit per task ${b.agent_budget_seconds} s` : b.catalog_acceptance ? ` · single-task check (${b.agent_budget_seconds === 180 ? 180 : 300} s）` : ""} · <span class="mono">${b.sha256.slice(0, 8)}</span></div></td><td>${badge(b.status)}${b.status_message ? `<div class="sub">${esc(b.status_message)}</div>` : ""}${b.status === "queued" ? `<div class="sub">Ahead: ${b.tasks_ahead} tasks awaiting dispatch</div>` : ""}</td><td>${b.score ? `<strong>${b.score.passed}/${b.score.total}</strong><div class="sub mono">${esc(b.score.mean_seconds)} s / task</div>` : '<span class="muted">—</span>'}${b.invalid ? '<div class="sub danger-text">Score invalidated</div>' : ""}</td><td>${stamp(b.accepted_at)}</td><td><div class="actions">${b.kind === "public" ? `<button class="button primary compact" data-trace-open="${esc(b.id)}">View traces</button>` : ""}${b.kind === "public" ? b.public_runs.map((r) => `<button class="button secondary compact" data-trace="${b.id}" data-ordinal="${r.ordinal}">${esc(r.label || `Public task ${r.ordinal + 1}`)}</button>`).join("") : ""}${["queued", "testing"].includes(b.status) ? `<button class="text-button small" data-cancel="${b.id}">Cancel</button>` : ""}</div>${b.kind === "public" && b.public_runs.length ? '<div class="sub">Your public-test traces</div>' : ""}</td></tr>`).join("")}</tbody></table></div>`
    : empty(
        "No evaluations yet. Upload a harness to start the automatic workflow.",
      );
  renderTracePanel();
  $("#trace-history-open")?.addEventListener("click", () => openLatestTracePanel());
  $("#batch-list")
    .querySelectorAll("[data-trace-open]")
    .forEach((b) => (b.onclick = () => openTracePanel(b.dataset.traceOpen)));
  $("#batch-list")
    .querySelectorAll("[data-cancel]")
    .forEach(
      (b) =>
        (b.onclick = async () => {
          b.disabled = true;
          try {
            await api(`/batches/${b.dataset.cancel}/cancel`, {
              method: "POST",
            });
            toast("Cancellation requested. A queued evaluation is cancelled before E2B and its attempt is returned; running evaluations keep their attempt.");
            await refresh();
          } catch (err) {
            toast(err.message, true);
          }
        }),
    );
  $("#batch-list")
    .querySelectorAll("[data-trace]")
    .forEach(
      (b) =>
        (b.onclick = async () => {
          try {
            const r = await api(
              `/batches/${b.dataset.trace}/traces/${b.dataset.ordinal}`,
            );
            $("#trace-title").textContent =
              `Public task ${Number(b.dataset.ordinal) + 1} · ${modeLabel(r.mode)}`;
            $("#trace-content").textContent = r.text;
            $("#trace-dialog").showModal();
          } catch (err) {
            toast(err.message, true);
          }
        }),
    );
}
function leaderboardPage() {
  const staff = route === "admin";
  $("#app").innerHTML =
    heading(
      "THE LEADERBOARD",
      "Leaderboard",
      "Each team is ranked by its one complete evaluation: tasks passed first, then mean runtime.",
    ) +
    `<div class="leader-actions"><a class="button secondary compact" href="${staff ? "/admin" : "/submit"}">← Back to ${staff ? "the console" : "your workspace"}</a></div><div class="leader-hero"><div><span class="eyebrow">BENCHMARKING AGENT HARNESSES</span><h2>Can your harness beat GPT-6 Astra?</h2><p>Full evaluation results update automatically; public practice is unranked.</p></div><div class="hero-numbers"><div>39<span>NORMAL TASKS / EVALUATION</span></div><div>01<span>FULL ATTEMPT</span></div></div></div><div class="queue-counters" aria-live="polite"><div class="counter"><span class="counter-label"><i class="pending-dot"></i>Queued evaluations</span><strong id="queued-count">–</strong></div><div class="counter"><span class="counter-label"><i class="running-dot"></i>Running evaluations</span><strong id="running-count">–</strong></div><div class="counter-updated"><span class="live-dot"></span> Live · updated <span id="leader-updated">–</span></div></div><section class="panel leaderboard-panel"><div class="panel-head"><h2>Leaderboard <span id="leaderboard-phase" class="pill gray">AWAITING COMPETITION</span></h2><span class="small muted">Provisional standings</span></div><div id="leader-table"></div></section>`;
}
function renderLeaderboardInto(root,data) {
  const q=root.querySelector("#queued-count,#inline-queued-count"), r=root.querySelector("#running-count,#inline-running-count"), u=root.querySelector("#leader-updated,#inline-leader-updated"), table=root.querySelector("#leader-table,#inline-leader-table");
  if(q)q.textContent=data.queued_batches;
  if(r)r.textContent=data.testing_batches;
  if(u)u.textContent = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Singapore", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date()) + " UTC+8";
  const base = data.baseline;
  if(!table)return;
  const baselineVerified = base?.verified === true;
  const baselineStatus = baselineVerified ? "Verified 39-task Astra xhigh baseline" : "Reference pending verification";
  const baselineAccepted = baselineVerified ? "Organizer verified" : "Awaiting measured baseline";
  table.innerHTML =
    `<div class="table-wrap leaderboard-scroll" tabindex="0" aria-label="Leaderboard standings"><table><thead><tr><th>Rank</th><th>Team / version</th><th>PASS RATE</th><th>Mean time / task</th><th>Accepted (UTC+8)</th></tr></thead><tbody><tr class="baseline-row"><td><span class="pill green">BASELINE</span></td><td><strong>${esc(base.label)}</strong><div class="sub">${baselineStatus}</div></td><td><strong>${base.passed == null ? "—" : base.passed + "/39"}</strong></td><td class="mono">${base.mean == null ? "—" : base.mean + " s"}</td><td class="muted">${baselineAccepted}</td></tr>${data.teams.map((r) => `<tr class="${r.challenger ? "best-row" : ""}"><td><span class="rank">${String(r.rank).padStart(2, "0")}</span>${r.tie ? '<div class="sub">Tied · judge review</div>' : ""}</td><td><strong class="team-name">${esc(r.team)}</strong><div class="sub mono">${r.sha256.slice(0, 12)}</div>${r.challenger ? '<span class="challenge-mark">First above baseline · provisional</span>' : ""}</td><td><strong>${r.passed}/39</strong></td><td class="mono">${esc(r.mean)} s</td><td>${stamp(r.accepted_at)}</td></tr>`).join("")}</tbody></table></div>${!data.teams.length ? empty("No official results published yet.") : ""}`;
  // Show the baseline plus ten teams; further rows scroll inside the box.
  const box = root.querySelector(".leaderboard-scroll"), rows = box?.querySelectorAll("tbody tr") || [];
  if (rows.length > 11) box.style.setProperty("--leaderboard-height", box.querySelector("thead").offsetHeight + [...rows].slice(0, 11).reduce((h, r) => h + r.offsetHeight, 0) + 2 + "px");
  else box?.style.removeProperty("--leaderboard-height");
}
function renderLeaderboard(data) {
  renderLeaderboardInto(document, data);
}
function adminPage() {
  if (user.role !== "admin") {
    $("#app").innerHTML = heading(
      "RESTRICTED",
      "Staff access required",
      "You are signed in with a team account. Sign out and use your staff account.",
    );
    return;
  }
  $("#app").innerHTML =
    heading(
      "ORGANIZER CONTROL ROOM",
      "Competition operations",
      "Review submissions, manage evaluations and record staff actions. Every change requires a reason and is recorded in the audit log.",
    ) +
    banner() +
    `<section class="stats four" id="admin-stats"></section><section class="panel"><div class="panel-body admin-operator"><label class="field"><span>Operator name (recorded with your signed-in account)</span><input id="operator" value="Staff operator" maxlength="80"></label><label class="field"><span>Reason for this action · required for every change</span><input id="admin-reason" placeholder="For example: reviewed submission or resolved incident" maxlength="1000"></label><button id="pause-button" class="button secondary">Pause dispatch</button></div></section><details class="disclosure"><summary>Team accounts and admission controls</summary><p>Fill in your operator name and reason first. Share temporary passwords privately. Email delivery is not configured.</p><form id="account-form"><div class="two-fields"><label class="field"><span>Team captain email</span><input id="account-email" type="email" required></label><label class="field"><span>Team name</span><input id="account-team" maxlength="100" required></label></div><label class="field"><span>Account role</span><select id="account-role"><option value="team">Team captain</option><option value="admin">Staff administrator</option></select><small>Staff accounts can manage all teams and competition settings. Issue one account per operator.</small></label><label class="field"><span>Temporary password (at least 12 characters)</span><input id="account-password" type="password" minlength="12" autocomplete="new-password" required></label><button class="button secondary">Create team account</button></form><form id="reset-form"><p class="help">Use this when a team emails to ask for a password reset. Verify the request comes from the account email first. The new password works immediately; the team is not asked to change it. All existing sessions of the account are signed out.</p><label class="field"><span>Account</span><select id="reset-user" required><option value="">Select an account</option></select></label><label class="field"><span>New password (at least 12 characters)</span><span class="password-input with-generate"><input id="reset-password" type="password" minlength="12" maxlength="200" autocomplete="new-password" required><button type="button" class="reveal" data-reveal="reset-password" aria-label="Show password" aria-pressed="false">Show</button></span></label><div class="actions"><button type="button" id="reset-generate" class="button secondary compact">Generate a strong password</button><button class="button primary compact" type="submit">Reset password</button></div><div id="reset-result" class="reset-result" hidden role="status"><p><strong>Password reset for <span id="reset-result-account" data-no-translate></span>.</strong> Send this password to the team by email. They can sign in with it right away.</p><div class="reset-secret"><code id="reset-result-password" data-no-translate></code><button type="button" id="reset-copy" class="button secondary compact">Copy</button><button type="button" id="reset-clear" class="button secondary compact">Clear</button></div></div></form><p>Closing admission lets accepted evaluations finish. This control cannot reopen admission.</p><button id="close-demo" class="button danger">Close evaluation admission</button></details><div class="split-admin"><section class="panel"><div class="panel-head"><h2>Submission review</h2><span id="review-count" class="pill amber">Pending review</span></div><div id="review-list"></div></section><section class="panel"><div class="panel-head"><h2>Runtime and costs</h2><span class="small muted">Current environment</span></div><div class="panel-body" id="health-summary"></div></section></div><section class="panel"><div class="panel-head"><h2>Evaluations and incidents</h2><span class="small muted">Staff-only internal status</span></div><div id="admin-batches"></div></section><div class="split-admin"><section class="panel"><div class="panel-head"><h2>Adjust attempts</h2><span class="small muted">Appends a ledger entry · preserves history</span></div><div class="panel-body"><form id="quota-form"><label class="field"><span>Team</span><select id="quota-team"></select></label><div class="two-fields"><label class="field"><span>Attempt type</span><select id="quota-kind"><option value="full">Full evaluation</option><option value="public">Public test</option><option value="infra">Infrastructure test</option></select></label><label class="field"><span>Change (+ adds, − removes)</span><input id="quota-delta" type="number" min="-20" max="20" value="1" required></label></div><button class="button secondary full-width">Record adjustment</button></form></div></section><section class="panel"><div class="panel-head"><h2>Formal appeals</h2><span class="small muted">One per team</span></div><div id="admin-appeals"></div></section></div><section class="panel"><div class="panel-head"><h2>Model usage and costs</h2><span class="small muted">Unknown prices remain unpriced</span></div><div id="usage-table"></div><div class="panel-body"><details><summary class="small muted">Configure estimated prices (USD / million tokens)</summary><p class="help">These are operator estimates, not provider invoices. Do not enter an unknown price. Saved rates apply to future calls and existing unpriced calls.</p><form id="pricing-form"><div class="stats"><label class="field"><span>Input</span><input id="price-input" type="number" min="0" step="any" required placeholder="Not confirmed"></label><label class="field"><span>Cached input</span><input id="price-cache" type="number" min="0" step="any" required placeholder="Not confirmed"></label><label class="field"><span>Output</span><input id="price-output" type="number" min="0" step="any" required placeholder="Not confirmed"></label></div><button class="button secondary">Save price version and calculate</button></form></details></div></section><section class="panel"><div class="panel-head"><h2>Audit log</h2><span class="small muted">Latest 100 entries</span></div><div id="audit-table"></div></section><details class="disclosure"><summary>Notification records (no email delivery)</summary><div id="outbox"></div></details>`;
  const calendarPanel = document.createElement("details");
  calendarPanel.className = "disclosure";
  calendarPanel.innerHTML = `<summary>Competition calendar</summary><p>Enter all times in UTC+8. Use a new calendar version and record an operator and reason. Accepted evaluations retain their original policy. Calendar changes do not reopen manually closed admission.</p><form id="calendar-form"><div class="two-fields"><label class="field"><span>Admission mode</span><select id="calendar-mode"><option value="demo">Open admission</option><option value="scheduled">Scheduled window</option></select></label><label class="field"><span>Configuration version</span><input id="calendar-version" value="calendar-2026-v1" maxlength="120" required></label></div><div class="two-fields"><label class="field"><span>Competition starts (UTC+8)</span><input id="calendar-start" type="datetime-local" value="2026-11-08T08:00"></label><label class="field"><span>Competition ends (UTC+8)</span><input id="calendar-end" type="datetime-local" value="2026-11-10T21:00"></label></div><label class="field"><span>Appeal deadline (UTC+8)</span><input id="calendar-appeal" type="datetime-local" value="2026-11-11T21:00"></label><div class="two-fields"><label class="field"><span>Infrastructure window starts (optional, UTC+8)</span><input id="calendar-infra-start" type="datetime-local"></label><label class="field"><span>Infrastructure window ends (optional, UTC+8)</span><input id="calendar-infra-end" type="datetime-local"></label></div><p class="help">In scheduled mode, infrastructure tests require their own window. Calendar settings do not enable an unvalidated executor.</p><button class="button secondary">Save calendar</button></form>`;
  $("#app").append(calendarPanel);
  const c = platformConfig.calendar;
  if (c) {
    $("#calendar-mode").value = c.mode;
    $("#calendar-version").value = c.version;
    const local = (v) =>
      new Date(new Date(v).getTime() + 8 * 3600000).toISOString().slice(0, 16);
    if (c.mode === "scheduled") {
      for (const [id, key] of [
        ["start", "starts_at"],
        ["end", "ends_at"],
        ["appeal", "appeal_closes_at"],
        ["infra-start", "infra_starts_at"],
        ["infra-end", "infra_ends_at"],
      ])
        if (c[key]) $("#calendar-" + id).value = local(c[key]);
    }
  }
  $("#calendar-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const calendar = {
      mode: $("#calendar-mode").value,
      version: $("#calendar-version").value.trim(),
    };
    if (calendar.mode === "scheduled") {
      for (const [id, key] of [
        ["start", "starts_at"],
        ["end", "ends_at"],
        ["appeal", "appeal_closes_at"],
      ]) {
        if (!$("#calendar-" + id).value)
          return toast("Enter competition and appeal dates.", true);
        calendar[key] = $("#calendar-" + id).value + ":00+08:00";
      }
      for (const [id, key] of [
        ["infra-start", "infra_starts_at"],
        ["infra-end", "infra_ends_at"],
      ])
        if ($("#calendar-" + id).value)
          calendar[key] = $("#calendar-" + id).value + ":00+08:00";
    }
    if (
      !window.confirm(
        "Save the new competition and appeal calendar? This change will be audited.",
      )
    )
      return;
    await adminAction({ action: "calendar", calendar });
    try {
      platformConfig = await api("/config");
    } catch {}
  };
  const context = () => ({
    actor: $("#operator").value.trim(),
    reason: $("#admin-reason").value.trim(),
  });
  $("#account-form").onsubmit = async (ev) => {
    ev.preventDefault();
    if ($("#account-role").value === "admin" && !window.confirm("Create a staff administrator with access to all teams and competition controls?")) return;
    const details = {role: $("#account-role").value, email: $("#account-email").value.trim(), team: $("#account-team").value.trim(), password: $("#account-password").value};
    const reason = $("#account-reason").value.trim();
    if ([...reason].length < 3) {toast("Enter a reason of at least 3 characters.",true);return;}
    if (!staffBeginAccountCreation()) return;
    try {
      const result = await api("/admin/accounts", {method:"POST",json:{...context(),...details,reason,role:$("#account-role").value}});
      $("#account-password").value = "";
      staffAccountIssued([details]);
      toast("Account created. Share the temporary password privately.");
      $("#account-issued").scrollIntoView({block:"nearest",behavior:"smooth"});
      await refresh();
    } catch (err) {
      if(err.uncertain){
        staffAccountIssued([details]);
        $("#account-issued-title").textContent="Creation not confirmed";
        $("#account-issued-count").textContent="Creation not confirmed";
        toast("Connection interrupted. Check the account directory before retrying. Download the prepared credentials for reconciliation; creation is not confirmed.",true);
      } else toast(err.message, true);
    }
    finally {staffEndAccountCreation();}
  };
  bindReveal($("#reset-form"));
  $("#reset-generate").onclick = () => {
    // Unambiguous characters; 16 symbols ≈ 90 bits.
    const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
    const bytes = crypto.getRandomValues(new Uint32Array(16));
    $("#reset-password").value = Array.from(bytes, (n) => chars[n % chars.length]).join("");
    $("#reset-password").type = "text";
    const reveal = $('[data-reveal="reset-password"]');
    reveal.textContent = "Hide"; reveal.setAttribute("aria-pressed", "true");
  };
  $("#reset-copy").onclick = () => navigator.clipboard?.writeText($("#reset-result-password").textContent).then(() => toast("Password copied."), () => toast("Copy failed; select the password and copy it manually.", true));
  $("#reset-clear").onclick = () => { $("#reset-result").hidden = true; $("#reset-result-password").textContent = ""; };
  $("#reset-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const select = $("#reset-user"), password = $("#reset-password").value;
    const label = select.selectedOptions[0]?.textContent || "";
    if (!select.value) return toast("Select the account to reset.", true);
    if (!window.confirm(window.staffLocale === "zh" ? `确认重置 ${label} 的密码？该账号所有会话都会退出。` : `Reset the password of ${label}? All of its sessions will be signed out.`)) return;
    try {
      await api("/admin/accounts/reset", {
        method: "POST",
        json: {
          ...context(),
          target: select.value,
          password,
        },
      });
      $("#reset-password").value = "";
      $("#reset-password").type = "password";
      Object.assign($('[data-reveal="reset-password"]'), { textContent: "Show" }).setAttribute("aria-pressed", "false");
      $("#reset-result-account").textContent = label;
      $("#reset-result-password").textContent = password;
      $("#reset-result").hidden = false;
      toast("Password reset. Existing sessions were signed out.");
      await refresh();
    } catch (err) {
      toast(err.message, true);
    }
  };
  $("#close-demo").onclick = () => {
    if (
      window.confirm(
        "Close evaluation admission? Accepted evaluations will finish. This action cannot be reversed from this console.",
      )
    )
      adminAction({ action: "close_demo" });
  };
  $("#pricing-form").onsubmit = (ev) => {
    ev.preventDefault();
    adminAction({
      action: "pricing",
      rates: {
        input_per_million: Number($("#price-input").value),
        cached_per_million: Number($("#price-cache").value),
        output_per_million: Number($("#price-output").value),
      },
    });
  };
  const scientificPanel = document.createElement("section");
  scientificPanel.className = "panel";
  scientificPanel.id = "science-target-panel";
  scientificPanel.innerHTML = `<div class="panel-head"><h2>Legacy single-task diagnostic</h2><span class="small muted">Staff only · unranked</span></div><div class="panel-body"><p class="help">Development diagnostics only: bind an approved official template to a task index. Use a new team and version for each check. The target cannot be changed. Normal contestant submissions do not need this step. Agent limit: ${platformConfig.scientific_agent_budget_seconds === 180 ? 180 : 300} seconds, followed by verification and archiving.</p><form id="science-target-form"><label class="field"><span>Approved version ID</span><input id="science-target-version" required maxlength="120"></label><label class="field"><span>Task index in the configured release</span><select id="science-target-index">${Array.from({ length: 49 }, (_, i) => `<option value="${i}">Diagnostic task ${String(i + 1).padStart(2, "0")}</option>`).join("")}</select></label><button id="science-target-submit" class="button secondary">Bind diagnostic target</button></form></div>`;
  $("#app").append(scientificPanel);
  $("#science-target-form").onsubmit = (ev) => {
    ev.preventDefault();
    adminAction({
      action: "scientific_target",
      target: $("#science-target-version").value.trim(),
      task_index: Number($("#science-target-index").value),
    });
  };
  $("#pause-button").onclick = () =>
    adminAction({ action: "pause", paused: !adminData.paused });
  $("#quota-form").onsubmit = (ev) => {
    ev.preventDefault();
    adminAction({
      action: "quota",
      target: $("#quota-team").value,
      kind: $("#quota-kind").value,
      delta: Number($("#quota-delta").value),
    });
  };
}
let adminBusy = false,
  adminPending = null;
async function adminAction(data) {
  if (adminBusy) return;
  let reason = $("#admin-reason").value.trim();
  const actor = $("#operator").value.trim();
  if (data.action === "retry_task" && reason.length < 3) {
    reason = "Force retry selected task after operator review";
    $("#admin-reason").value = reason;
  }
  if (reason.length < 3 || !actor)
    return toast(
      "Enter your operator name and an action reason at the top.",
      true,
    );
  const payload = { ...data, reason, actor },
    fingerprint = JSON.stringify(payload);
  if (adminPending?.fingerprint !== fingerprint)
    adminPending = { fingerprint, key: crypto.randomUUID() };
  adminBusy = true;
  try {
    await api("/admin/actions", {
      method: "POST",
      json: { ...payload, idempotency_key: adminPending.key },
    });
    adminPending = null;
    toast("Action completed and recorded.");
    await refresh();
  } catch (err) {
    toast(err.message, true);
  } finally {
    adminBusy = false;
  }
}
function detail(title, data) {
  $("#detail-summary").innerHTML = recordGrid(
    Object.entries(data)
      .filter(
        ([, v]) =>
          v !== null && ["string", "number", "boolean"].includes(typeof v),
      )
      .map(([k, v]) => [k.replaceAll("_", " "), v]),
  );
  $("#detail-title").textContent = title;
  $("#detail-content").textContent = JSON.stringify(data, null, 2);
  $("#detail-dialog").showModal();
}
function batchDetail(batch) {
  if (!batch) return;
  detail("Evaluation details · task runs", batch);
  const runs = Array.isArray(batch.runs) ? batch.runs : [];
  if (!runs.length) return;
  const host = $("#detail-summary");
  host.insertAdjacentHTML("beforeend", `<h3>Task runs</h3><div class="table-wrap"><table><thead><tr><th>Task</th><th>State</th><th>Attempt</th><th>Reward</th><th>Action</th></tr></thead><tbody>${runs.map((run) => `<tr><td>${esc(String(Number(run.ordinal) + 1))}</td><td>${badge(run.state)}</td><td>${esc(run.attempt ?? "—")}</td><td>${run.reward == null ? "—" : esc(run.reward)}</td><td>${run.state === "attention" ? `<button type="button" class="button primary compact" data-retry-task="${esc(batch.id)}:${esc(run.ordinal)}">Force retry this task</button>` : "—"}</td></tr>`).join("")}</tbody></table></div><p class="small muted">Force retry is reserved for confirmed infrastructure failures. It records an audit entry and clears the stale lease before queueing only this task.</p>`);
  host.querySelectorAll("[data-retry-task]").forEach((button) => button.addEventListener("click", async () => {
    const [id, ordinal] = button.dataset.retryTask.split(":");
    button.disabled = true;
    try {
      await adminAction({ action: "retry_task", target: id, task_index: Number(ordinal), force: true });
    } finally {
      button.disabled = false;
    }
  }));
}
function reviewAdviceLabel(a) {
  if (!a || a.outcome === "not_requested") return "Awaiting automated review";
  if (a.outcome === "provider_unavailable")
    return "AI unavailable · check the version status and reason";
  if (a.outcome === "retry_wait") return "Review service unavailable · automatic retry scheduled";
  if (a.outcome === "queued" || a.outcome === "running")
    return "Automated review in progress";
  if (a.outcome === "advice_ready")
    return (a.provider_version || "").startsWith("fake-")
      ? "Simulated advice · not a model review"
      : "AI risk advice · requires review";
  return "Review incomplete or stale · check the version status and reason";
}
function reviewAdviceHTML(a) {
  if (!a || a.outcome !== "advice_ready") return "";
  const uncovered =
    Number.isInteger(a.uncovered_scanner_risks) &&
    a.uncovered_scanner_risks >= 0
      ? a.uncovered_scanner_risks
      : "Unknown";
  const followup = a.manual_followup_required
    ? `<p class="small" data-review-followup>Advice may not cover every static finding. Check the final version status. Unexplained findings:${esc(String(uncovered))} .</p>`
    : "";
  return `${followup}<p class="small">${esc(a.summary || "")}</p><ul class="small">${(a.findings || []).map((f) => `<li>${esc(f.rule)} · ${esc(f.file)} · ${esc(String(f.confidence))}：${esc(f.explanation)}</li>`).join("")}</ul>`;
}
function renderAdmin(d) {
  adminData = d;
  const pending = d.versions.filter((v) => v.status === "pending_review"),
    running = d.batches.filter((b) =>
      ["testing", "attention", "cancelling"].includes(b.status),
    ),
    queued = d.batches.filter((b) => b.status === "queued"),
    failed = d.batches.filter((b) => b.status === "attention");
  $("#admin-stats").innerHTML =
    stat(
      "Under review",
      pending.length,
      "Submissions awaiting a decision",
      "◉",
    ) +
    stat(
      "Queued / running",
      `${queued.length}<small>/ ${running.length}</small>`,
      "Evaluation counts",
      "≋",
    ) +
    stat(
      "Needs attention",
      failed.length,
      d.paused ? "New dispatch paused" : "New dispatch enabled",
      "!",
    ) +
    stat(
      "Model calls",
      d.usage.calls,
      `${d.usage.input_tokens + d.usage.output_tokens} tokens · ${d.usage.unpriced || 0} unpriced calls`,
      "↗",
    );
  $("#pause-button").textContent = d.paused
    ? "Resume dispatch →"
    : "Pause dispatch";
  $("#review-count").textContent = `${pending.length} Under review`;
  $("#review-list").innerHTML = pending.length
    ? pending
        .map(
          (v) =>
            `<div class="review-item"><h3>${esc(v.team)} / ${esc(v.name)}</h3><p><span class="mono">${v.sha256.slice(0, 16)}</span> · ${v.report.risks.length} findings · ${esc(reviewAdviceLabel(v.ai_advisory))}</p>${reviewAdviceHTML(v.ai_advisory)}<div class="actions"><button class="button secondary compact" data-review-detail="${v.id}">Review findings and source</button><span class="small muted">Waiting for automated review</span></div></div>`,
        )
        .join("")
    : empty("No submissions are awaiting review.");
  $("#health-summary").innerHTML =
    `<ul class="note-list"><li><span>Execution</span><strong>${platformConfig.phase?.name === "competition" ? "Original task limits" : "Public-task practice configuration"}</strong></li><li><span>E2B / R2</span><strong>${esc(d.storage || "See configuration and validation records")}</strong></li><li><span>Test model</span><strong>${esc(d.execution_policy?.test_model || d.model)}</strong></li><li><span>Formal model</span><strong>${esc(d.execution_policy?.formal_configured ? d.execution_policy.formal_model : "Not configured · formal admission blocked")}</strong></li><li><span>Practice time limit / task</span><strong>${esc(d.execution_policy?.test_agent_budget_seconds ?? "Unknown")} s</strong></li><li><span>Formal task limits</span><strong>Original per-task configuration</strong></li><li><span>Global task slots</span><strong>${d.slots} configured</strong></li><li><span>Free disk space</span><strong>${d.operational_health?.disk_status === "unavailable" ? "Unavailable" : (d.disk_free_bytes / 1024 ** 3).toFixed(1) + " GiB"}</strong></li><li><span>Priced cost estimate</span><strong>${d.usage.known_amount === null ? "No priced total yet" : "$" + d.usage.known_amount.toFixed(4)}</strong></li></ul><div class="metric-warning">${d.usage.unpriced || 0} calls are unpriced, not free. No monetary hard limit is configured.</div>`;
  const h = d.operational_health;
  if (h) {
    const names = {
      disk_probe_failed: "Disk capacity unavailable; uploads paused",
      upload_disk_reserve: "Disk reserve reached; uploads paused",
      rollout_upload_backlog: "Rollouts awaiting upload",
      remote_cleanup_unconfirmed: "Remote resources await reconciliation",
      worker_lease_expired: "Expired worker leases",
      model_rate_limited: "Model rate limits in the last 15 minutes",
    };
    $("#health-summary").insertAdjacentHTML(
      "beforeend",
      `<div class="metric-warning">${h.alerts.length ? h.alerts.map((x) => esc(names[x] || x)).join("<br>") : "No current alerts from these checks"}<br>Alerts appear in this console. External notification delivery is not configured.</div>`,
    );
  }
  const delivery = d.notification_delivery;
  if (delivery) {
    const names = {
      local_only: "Local record only",
      pending: "Open",
      sending: "Processing",
      retry: "Waiting to retry",
      dead_letter: "Needs staff review",
      fake_delivered: "Simulated delivery",
    };
    const counts = (delivery.counts || [])
      .map(
        (x) =>
          `${esc(x.mode === "fake" ? "Simulation" : "Local")} / ${esc(names[x.status] || x.status)} ${esc(x.count)}`,
      )
      .join("；");
    $("#health-summary").insertAdjacentHTML(
      "beforeend",
      `<div id="notification-health" class="metric-warning">Notifications: email delivery is not configured.${counts || "No delivery records yet."}<br>Simulated delivery does not mean an email was sent.</div>`,
    );
  }
  $("#admin-batches").innerHTML = d.batches.length
    ? `<div class="table-wrap"><table><thead><tr><th>Team / request</th><th>Evaluation</th><th>Internal status</th><th>Task states</th><th>Actions</th></tr></thead><tbody>${d.batches
        .map(
          (b) =>
            `<tr><td><strong>${esc(b.team)}</strong><div class="sub mono">${b.id.slice(0, 12)}</div></td><td>${labels[b.kind]}<div class="sub">${modeLabel(b.mode)}</div></td><td>${badge(b.status)}${b.invalid ? '<div class="sub danger-text">Score invalidated</div>' : ""}</td><td><span class="small">${Object.entries(
              b.run_states,
            )
              .map(([k, n]) => `${esc(k)} ${n}`)
              .join(
                " / ",
              )}</span></td><td><div class="actions"><button class="button secondary compact" data-batch-detail="${b.id}">Details</button>${b.status === "attention" ? `<button class="button primary compact" data-retry="${b.id}">Force retry infrastructure failure</button>` : ""}${b.status === "completed" && !b.invalid ? `<button class="button danger compact" data-invalidate="${b.id}">Invalidate score</button>` : ""}</div></td></tr>`,
        )
        .join("")}</tbody></table></div>`
    : empty("No evaluation requests.");
  const rs = $("#reset-user"), chosen = rs.value;
  rs.innerHTML = '<option value="">Select an account</option>' + (d.accounts || [])
    .map((x) => `<option value="${esc(x.id)}">${esc(x.team)} · ${esc(x.email)}${x.role === "admin" ? " (staff)" : ""}</option>`)
    .join("");
  if ((d.accounts || []).some((x) => x.id === chosen)) rs.value = chosen;
  const qt = $("#quota-team"),
    old = qt.value;
  qt.innerHTML = d.teams
    .map(
      (t) =>
        `<option value="${t.id}">${esc(t.name)} · Full ${t.quotas.full} / Public ${t.quotas.public} / Infra ${t.quotas.infra}</option>`,
    )
    .join("");
  if (d.teams.some((t) => t.id === old)) qt.value = old;
  $("#admin-appeals").innerHTML = d.appeals.length
    ? d.appeals
        .map(
          (a) =>
            `<div class="review-item"><h3>${esc(a.team)} · ${a.status === "open" ? "Awaiting response" : "Answered"}</h3><p>${esc(a.body)}</p>${a.response ? `<p>Response:${esc(a.response)}</p>` : `<button class="button secondary compact" data-appeal="${a.id}">Respond using the action reason</button>`}</div>`,
        )
        .join("")
    : empty("No formal appeals.");
  $("#usage-table").innerHTML = d.usage_rows.length
    ? `<div class="table-wrap"><table><thead><tr><th>Time (UTC+8)</th><th>Run / attempt</th><th>Model</th><th>Input / output tokens</th><th>Cost</th><th>HTTP</th></tr></thead><tbody>${d.usage_rows.map((r) => `<tr><td>${stamp(r.created)}</td><td class="mono">${esc(String(r.run_id || "Unknown").slice(0, 10))} / ${r.attempt}</td><td>${esc(r.model)}</td><td>${r.input_tokens ?? "Unknown"} / ${r.output_tokens ?? "Unknown"}</td><td>${r.amount === null ? '<span class="pill amber">Unpriced</span>' : "$" + r.amount.toFixed(4)}</td><td>${r.status}</td></tr>`).join("")}</tbody></table></div>`
    : empty(
        "Real model calls record usage here. Simulated tasks do not generate model bills.",
      );
  $("#audit-table").innerHTML = d.audit.length
    ? `<div class="table-wrap"><table><thead><tr><th>Time (UTC+8)</th><th>Operator</th><th>Action</th><th>Reason</th><th>Before / after</th></tr></thead><tbody>${d.audit.map((a) => `<tr><td>${stamp(a.created)}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td><td>${esc(a.reason)}</td><td><button class="text-button small" data-audit="${a.id}">View</button></td></tr>`).join("")}</tbody></table></div>`
    : empty("Staff actions are recorded here.");
  $("#outbox").innerHTML =
    d.outbox
      .map(
        (m) =>
          `<p>${stamp(m.created)} · ${esc(m.event)} · Local record only</p>`,
      )
      .join("") || "No notification events.";
  const binds = [
    [
      "data-approve",
      (id) =>
        adminAction({ action: "review", target: id, decision: "approve" }),
    ],
    [
      "data-reject",
      (id) => adminAction({ action: "review", target: id, decision: "reject" }),
    ],
    ["data-retry", (id) => adminAction({ action: "retry", target: id, force: true })],
    [
      "data-invalidate",
      (id) => adminAction({ action: "invalidate", target: id }),
    ],
    ["data-appeal", (id) => adminAction({ action: "appeal", target: id })],
    [
      "data-review-detail",
      (id) =>
        detail("Submission review · package text is untrusted", {
          static_report: d.versions.find((v) => v.id === id).report,
          ai_advisory: d.versions.find((v) => v.id === id).ai_advisory,
        }),
    ],
    [
      "data-batch-detail",
      (id) => batchDetail(d.batches.find((b) => b.id === id)),
    ],
    [
      "data-audit",
      (id) =>
        detail(
          "Audit entry",
          d.audit.find((a) => a.id === id),
        ),
    ],
  ];
  for (const [attr, fn] of binds)
    document
      .querySelectorAll(`[${attr}]`)
      .forEach((b) => (b.onclick = () => fn(b.getAttribute(attr))));
}
// Public notices are visible to signed-in participants and staff. Participants
// Participants report problems by email; no ticket UI is mounted in the portal.
const communityState = {
  announcementPage: 0,
  pending: new Map(),
  owner: null,
};
const announcementScope = {
  public: "Public notice",
  teams: "Participants",
  team: "Selected team",
};
function communityTime(v) {
  return (
    new Intl.DateTimeFormat(route === "admin" && window.staffLocale === "zh" ? "zh-CN" : "en-GB", {
      timeZone: "Asia/Singapore",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date(v * 1000)) + " UTC+8"
  );
}
function communityOperator() {
  const actor = $("#operator")?.value.trim() || "",
    reason = $("#admin-reason")?.value.trim() || "";
  if (!actor || reason.length < 3)
    throw new Error(
      "Enter your operator name and a reason of at least 3 characters.",
    );
  return { actor, reason };
}
async function communityWrite(key, path, payload) {
  const fingerprint = JSON.stringify(payload);
  let pending = communityState.pending.get(key);
  if (pending?.busy) return null;
  if (!pending || pending.fingerprint !== fingerprint) {
    pending = { fingerprint, key: crypto.randomUUID(), busy: false };
    communityState.pending.set(key, pending);
  }
  pending.busy = true;
  try {
    const result = await api(path, {
      method: "POST",
      json: { ...payload, idempotency_key: pending.key },
    });
    communityState.pending.delete(key);
    return result;
  } finally {
    pending.busy = false;
  }
}
function mountCommunity() {
  if (user?.must_change) return;
  if (communityState.owner !== (user?.id || null)) {
    communityState.owner = user?.id || null;
    communityState.pending.clear();
    communityState.announcementPage = 0;
  }
  const admin = user?.role === "admin" && route === "admin";
  const section = document.createElement("section");
  section.className = "panel";
  section.id = "announcements-panel";
  section.innerHTML = `<div class="panel-head"><h2>Announcements</h2><span class="small muted">Versioned notices · Asia/Singapore UTC+8</span></div><div id="announcements-list"></div><div class="panel-body actions"><button id="announcements-prev" class="button secondary compact">Newer notices</button><span id="announcements-page" class="small"></span><button id="announcements-next" class="button secondary compact">Older notices</button></div>`;
  $("#app").append(section);
  $("#announcements-prev").onclick = () => {
    communityState.announcementPage = Math.max(
      0,
      communityState.announcementPage - 1,
    );
    refreshCommunity().catch((e) => toast(e.message, true));
  };
  $("#announcements-next").onclick = () => {
    communityState.announcementPage++;
    refreshCommunity().catch((e) => toast(e.message, true));
  };
  if (admin) {
    const publish = document.createElement("section");
    publish.className = "panel";
    publish.innerHTML = `<div class="panel-head"><h2>Publish announcement</h2><span class="small muted">Published notices are immutable; issue a new version to correct them</span></div><div class="panel-body"><p class="help">Record your operator name and reason first. Publish confirmed information only. Never include credentials, private team data or hidden tasks.</p><form id="admin-announcement-form"><div class="two-fields"><label class="field"><span>Notice version</span><input id="announcement-version" maxlength="80" required placeholder="notice-2026-001"></label><label class="field"><span>Rules version</span><input id="announcement-prd" readonly value="${esc(platformConfig.rule_version || "")}"></label></div><label class="field"><span>Title</span><input id="announcement-title" maxlength="120" required></label><label class="field"><span>Body (plain text, 3–8,000 characters)</span><textarea id="announcement-body" minlength="3" maxlength="8000" required></textarea></label><div class="two-fields"><label class="field"><span>Audience</span><select id="announcement-scope"><option value="public">Everyone</option><option value="teams">All teams</option><option value="team">Selected team only</option></select></label><label class="field"><span>Team ID (for a selected team only)</span><input id="announcement-team" maxlength="120"></label></div><button id="announcement-submit" class="button primary">Publish and record audit entry</button></form></div>`;
    $("#app").append(publish);
    $("#admin-announcement-form").onsubmit = async (ev) => {
      ev.preventDefault();
      const button = $("#announcement-submit");
      button.disabled = true;
      try {
        const result = await communityWrite(
          "announcement",
          "/admin/announcements",
          {
            version: $("#announcement-version").value.trim(),
            title: $("#announcement-title").value,
            body: $("#announcement-body").value,
            scope: $("#announcement-scope").value,
            team_id: $("#announcement-team").value.trim(),
            prd_version: $("#announcement-prd").value,
            ...communityOperator(),
          },
        );
        if (result) {
          toast("Announcement published. Its content and version are frozen.");
          communityState.announcementPage = 0;
          await refreshCommunity();
        }
      } catch (err) {
        toast(err.message, true);
      } finally {
        button.disabled = false;
      }
    };
  }
}
async function refreshCommunity() {
  if (!$("#announcements-list")) return;
  const announcements = await api(
    "/announcements?page=" + communityState.announcementPage,
  );
  $("#announcements-list").innerHTML = announcements.length
    ? announcements
        .map(
          (a) =>
            `<article class="review-item" data-announcement-id="${esc(a.id)}"><h3>${esc(a.title)}</h3><p class="small muted">${esc(a.version)} · ${esc(a.prd_version)} · ${esc(announcementScope[a.scope] || a.scope)}${a.team_id ? " · " + esc(a.team_id) : ""}<br>${esc(communityTime(a.published))}</p><p style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(a.body)}</p></article>`,
        )
        .join("")
    : empty("No announcements yet.");
  $("#announcements-prev").disabled = communityState.announcementPage === 0;
  $("#announcements-next").disabled = announcements.length < 50;
  $("#announcements-page").textContent =
    "Page " + (communityState.announcementPage + 1) + " ";
}

// Participants report problems by email.
function organizerEmail(subject = "QIQC Challenge") {
  fetch("/facts.json").then((r) => r.json()).then((f) => {
    if (!f.contact) return;
    document.querySelectorAll("a.organizer-email").forEach((a) => { a.textContent = f.contact; a.href = "mailto:" + f.contact + "?subject=" + encodeURIComponent(subject); });
  }).catch(() => {});
}
function mountHelpNote() {
  const note = document.createElement("section");
  note.className = "help-note";
  note.innerHTML = `<strong>Evaluation environment error or submission problem?</strong><p>Email the organizers promptly at <a class="organizer-email" href="mailto:qiqc@gauge-forge.com">qiqc@gauge-forge.com</a>. Include your team name, the evaluation ID and the submission SHA-256. Never include a password, model key or private trace.</p>`;
  $("#app").append(note);
  organizerEmail("QIQC submission issue · " + (user?.team || ""));
}
async function refresh() {
  if (pollBusy) { refreshAgain = true; return; }
  pollBusy = true;
  const refreshUser = user;
  const adminQuery = route === "admin" && typeof staffSnapshotQuery === "function" ? staffSnapshotQuery() : "";
  try {
    platformConfig=await api("/config");serverOffset=Date.parse(platformConfig.server_time)-Date.now();
    if($("#phase-notice"))$("#phase-notice").textContent=$(".event-timeline")?platformConfig.phase?.reason||"":phaseNotice();updateEventClock();
    if (leaderboardView) {
      if (user && !user.must_change) {
        const board = await api("/leaderboard");
        if (user === refreshUser && $("#leader-table")) renderLeaderboard(board);
      }
    } else if (route === "submit" && $("#inline-leaderboard:not([hidden])") && user && !user.must_change) {
      const board = await api("/leaderboard");
      if (user === refreshUser) renderLeaderboardInto($("#inline-leaderboard"), board);
    } else if (user && !user.must_change) {
      if (route === "submit" && user.role === "team") {
        const results = await Promise.all([
          api("/me"),
          api("/versions"),
          api("/batches"),
          api("/submissions"),
        ]);
        if(user !== refreshUser)return;
        [user, versions, batches, submissionIntents] = results;
        renderTeamData();renderSubmissionStatus();
      } else if (route === "admin" && user.role === "admin") {
        const data = await api("/admin" + adminQuery);
        if(user !== refreshUser)return;
        if(adminQuery !== staffSnapshotQuery()){refreshAgain=true;return;}
        renderAdmin(data);
        renderStaffWorkspace(data);
      }
    }
    await refreshCommunity();
  } catch (err) {
    if (err.status === 401 && user === refreshUser && user) {
      user = null;
      uploadAttempt = null;
      versions = []; batches = []; submissionIntents = [];
      render();
      toast("Your session has expired. Sign in again to continue.", true);
    } else toast(err.message, true);
  } finally {
    pollBusy = false;
    if(refreshAgain){refreshAgain=false;queueMicrotask(refresh);}
  }
}
function render() {
  if (user && (((route === "submit" || route === "leaderboard") && user.role !== "team") || (route === "admin" && user.role !== "admin"))) user = null;
  updateIdentity();
  if (user?.must_change) {
    changePage();
    return;
  }
  if (!user) {
    loginPage();
    return;
  }
  if (leaderboardView) {
    leaderboardPage();
    refresh();
    return;
  }
  if (route === "admin") adminPage();
  else submitPage();
  mountCommunity();
  if (route === "submit") mountHelpNote();
  if (route === "admin") mountStaffWorkspace();
  refresh();
}
$("#logout").onclick = async () => {
  try {
    await api("/logout", { method: "POST" });
    if (route === "admin") clearStaffAccountSecrets();
    user = null;
    render();
  } catch (err) {
    toast(err.message, true);
  }
};
document
  .querySelectorAll("[data-nav]")
  .forEach((a) => a.dataset.nav === route && a.setAttribute("aria-current", "page"));
(async () => {
  try {
    platformConfig = await api("/config");
  } catch {}
  try {
    user = await api("/me");
  } catch {}
  render();
  setInterval(
    () => {
      if (!document.hidden) refresh();
    },
    leaderboardView ? 3000 : route === "admin" ? 30000 : 2000,
  );
})();

// Navigation collapses at tablet widths and remains operable without hover.
const consoleMenu = document.querySelector('#console-menu-toggle');
consoleMenu?.addEventListener('click', () => {
  const navigation = document.querySelector('#console-navigation');
  const open = navigation.classList.toggle('open');
  consoleMenu.setAttribute('aria-expanded', String(open));
});

setInterval(updateEventClock,1000);
window.addEventListener("online",refresh);
document.addEventListener("visibilitychange",()=>{if(!document.hidden)refresh()});
