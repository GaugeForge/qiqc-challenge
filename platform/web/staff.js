"use strict";
// Staff workspace presentation. All mutations use the existing authenticated,
// audited Go endpoints; no shell commands or privileged host actions are exposed.
const staffView = {
  tab: "overview",
  pages: {versions:0,batches:0,usage:0,audit:0},
  batchQuery: "",
  batchState: "",
  teamQuery: "",
  reviewQuery: "",
  reviewState: "", reviewFrom: "", reviewUntil: "",
};
let staffFilterTimer;
function staffSnapshotQuery() {
 const q=new URLSearchParams();
 for(const [key,page] of Object.entries(staffView.pages))q.set(key+"_page",page);
 for(const [key,value] of Object.entries({version_query:staffView.reviewQuery,version_state:staffView.reviewState,batch_query:staffView.batchQuery,batch_state:staffView.batchState}))if(value)q.set(key,value);
 for(const [key,value] of [["version_from",staffView.reviewFrom],["version_until",staffView.reviewUntil]])if(value){const n=Date.parse(value+"+08:00")/1000;if(Number.isFinite(n))q.set(key,n);}
 return "?"+q.toString();
}
function refreshStaffFilter(section) {
 staffView.pages[section]=0;
 clearTimeout(staffFilterTimer);
 staffFilterTimer=setTimeout(refresh,300);
}
function renderStaffPagination(data) {
 for(const [key,selector] of [["versions","#review-list"],["batches","#admin-batches"],["usage","#usage-table"],["audit","#audit-table"]]){
  const p=data.pagination?.[key];if(!p)continue;
  staffView.pages[key]=p.page;
  let node=document.querySelector(`[data-history-page="${key}"]`);
  if(!node){node=document.createElement("div");node.className="record-pagination";node.dataset.historyPage=key;staffPanelFor(selector).append(node);}
  node.innerHTML=`<button type="button" class="button secondary compact" data-page-prev ${p.page===0?"disabled":""}>Previous</button><span>Page ${p.page+1} of ${p.pages} · ${p.total} records</span><button type="button" class="button secondary compact" data-page-next ${p.page+1>=p.pages?"disabled":""}>Next</button>`;
  for(const [selector,delta] of [["[data-page-prev]",-1],["[data-page-next]",1]])node.querySelector(selector).onclick=()=>{staffView.pages[key]=p.page+delta;node.querySelectorAll("button").forEach(b=>b.disabled=true);refresh();};
 }
}
const staffSections = [
  [
    "overview",
    "Overview",
    "Start with current health and work that needs attention.",
  ],
  [
    "reviews",
    "Submissions",
    "Review immutable packages and record a decision when staff follow-up is needed.",
  ],
  [
    "evaluations",
    "Evaluations",
    "Follow recent evaluations, inspect failures and retry eligible infrastructure errors.",
  ],
  [
    "teams",
    "Teams & access",
    "Find team accounts, issue temporary credentials and adjust attempt allowances.",
  ],
  [
    "announcements",
    "Announcements",
    "Publish versioned notices to everyone, all teams or a selected team.",
  ],
  [
    "operations",
    "Operations",
    "Check service health, verified backups, model usage and the event calendar.",
  ],
  ["audit", "Audit log", "Inspect who changed what, when, and why."],
];
function staffPanelFor(selector) {
  return document
    .querySelector(selector)
    ?.closest("section.panel,details.disclosure");
}
function showStaffTab(tab) {
  if (!staffSections.some((x) => x[0] === tab)) tab = "overview";
  staffView.tab = tab;
  const context = document.querySelector('.staff-action-context');
  if (context) context.hidden = tab === 'overview';
  document
    .querySelectorAll("[data-staff-section]")
    .forEach((x) => (x.hidden = x.dataset.staffSection !== tab));
  document.querySelectorAll("[data-staff-tab]").forEach((x) => {
    x.setAttribute("aria-selected", String(x.dataset.staffTab === tab));
    x.tabIndex = x.dataset.staffTab === tab ? 0 : -1;
  });
  history.replaceState(null, "", "#" + tab);
}
function mountStaffWorkspace() {
  if (route !== "admin" || user?.role !== "admin" || user.must_change) return;
  const app = $("#app"),
    operator = $("#operator");
  operator.value = user.email;
  operator.readOnly = true;
  const tabs = document.createElement("nav");
  tabs.className = "staff-tabs";
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "Staff workspace");
  tabs.innerHTML = staffSections
    .map(
      ([id, label]) =>
        `<button type="button" id="staff-tab-${id}" role="tab" data-staff-tab="${id}" aria-controls="staff-section-${id}" aria-selected="false"><span class="staff-nav-index" aria-hidden="true">${String(staffSections.findIndex(x=>x[0]===id)+1).padStart(2,"0")}</span><span>${label}</span></button>`,
    )
    .join("");
  const operatorPanel = staffPanelFor("#operator");
  operatorPanel.classList.add('staff-action-context');
  operatorPanel.after(tabs);
  tabs.after(operatorPanel);
  const panels = {};
  for (const [id, label, description] of staffSections) {
    const section = document.createElement("section");
    section.id = "staff-section-" + id;
    section.className = "staff-section";
    section.dataset.staffSection = id;
    section.setAttribute("role", "tabpanel");
    section.setAttribute("aria-labelledby", "staff-tab-" + id);
    section.innerHTML = `<h2>${label}</h2><p class="help">${description}</p>`;
    panels[id] = section;
    app.append(section);
  }
  panels.overview.querySelector('.help').after(document.querySelector('#admin-stats'));
  const move = (selector, tab) => {
    const panel = staffPanelFor(selector);
    if (panel) panels[tab].append(panel);
  };
  for (const [selector, tab] of [
    ["#review-list", "reviews"],
    ["#admin-batches", "evaluations"],
    ["#account-form", "teams"],
    ["#quota-form", "teams"],
    ["#admin-appeals", "evaluations"],
    ["#announcements-panel", "announcements"],
    ["#admin-announcement-form", "announcements"],
    ["#health-summary", "operations"],
    ["#usage-table", "operations"],
    ["#calendar-form", "operations"],
    ["#science-target-form", "operations"],
    ["#audit-table", "audit"],
    ["#outbox", "audit"],
  ])
    move(selector, tab);
  app.querySelectorAll(".split-admin").forEach((x) => {
    if (!x.children.length) x.remove();
  });
  // Accounts and calendar are normal operator workflows, not hidden disclosures.
  for (const selector of ["#account-form", "#calendar-form"]) {
    const p = staffPanelFor(selector);
    if (p?.tagName === "DETAILS") p.open = true;
  }
  const legacy = staffPanelFor("#science-target-form");
  if (legacy) {
    const disclosure = document.createElement("details");
    disclosure.className = "disclosure";
    disclosure.innerHTML =
      "<summary>Advanced development diagnostics</summary>";
    legacy.before(disclosure);
    disclosure.append(legacy);
  }
  panels.overview.insertAdjacentHTML(
    "beforeend",
    `<div class="staff-quick-actions"><div><h3>Participant accounts</h3><p>Create access for one team or import your participant list.</p></div><div class="actions"><button type="button" class="button primary" data-account-shortcut="single">Create account</button><button type="button" class="button secondary" data-account-shortcut="bulk">Import accounts</button></div></div><div id="staff-overview-health"></div><div class="staff-summary">${[
      [
        "reviews",
        "Review submissions",
        "Inspect scanner findings and AI advice.",
      ],
      [
        "evaluations",
        "Follow evaluations",
        "Check queue, failures and completed results.",
      ],
      [
        "teams",
        "Manage team access",
        "Create accounts, reset passwords and adjust attempts.",
      ],
      [
        "operations",
        "Check services & backups",
        "Read monitored status and recovery evidence.",
      ],
      [
        "audit",
        "Inspect the audit trail",
        "Verify changes and their recorded reasons.",
      ],
    ]
      .map(
        ([tab, title, text]) =>
          `<a href="#${tab}" data-staff-link="${tab}"><strong>${title} ↗</strong><span>${text}</span></a>`,
      )
      .join(
        "",
      )}</div><div class="staff-guide" style="margin-top:24px"><h3>Before changing competition state</h3><ol><li>Identify the team, submission SHA or evaluation ID. Open its details before taking action.</li><li>Select a work section and enter a specific action reason. Your signed-in staff account is recorded automatically.</li><li>Use retry only for eligible platform failures. Unconfirmed remote cleanup must be reconciled first; do not create a duplicate run.</li><li>Confirm the result in the audit log. Notification records do not mean an email was delivered.</li></ol></div>`,
  );
  panels.teams.insertAdjacentHTML(
    "beforeend",
    `<section class="panel"><div class="panel-head"><h2>Account directory</h2><span id="staff-team-count" class="small muted"></span></div><div class="staff-toolbar"><input id="staff-team-search" type="search" placeholder="Find a team, email or account ID" aria-label="Search team accounts"></div><div id="staff-team-list"></div></section>`,
  );
  $("#staff-team-search").value = staffView.teamQuery;
  $("#staff-team-search").oninput = (e) => {
    staffView.teamQuery = e.target.value;
    renderStaffTeams(adminData);
  };
  const batchesPanel = staffPanelFor("#admin-batches");
  batchesPanel
    .querySelector(".panel-head")
    .insertAdjacentHTML(
      "afterend",
      `<div class="staff-toolbar"><input id="staff-batch-search" type="search" placeholder="Find a team or evaluation ID across all records" aria-label="Search evaluations"><select id="staff-batch-state" aria-label="Filter evaluation status"><option value="">All statuses</option><option value="attention">Needs attention</option><option value="queued">Queued</option><option value="testing">Running</option><option value="completed">Completed</option><option value="cancelled">Cancelled</option></select></div><p id="staff-batch-count" class="help" style="padding:0 24px"></p>`,
    );
  $("#staff-batch-search").value = staffView.batchQuery;
  $("#staff-batch-state").value = staffView.batchState;
  $("#staff-batch-search").oninput = (e) => {
    staffView.batchQuery = e.target.value;
    refreshStaffFilter("batches");
  };
  $("#staff-batch-state").onchange = (e) => {
    staffView.batchState = e.target.value;
    refreshStaffFilter("batches");
  };
  const reviewsPanel = staffPanelFor("#review-list");
  reviewsPanel
    .querySelector(".panel-head")
    .insertAdjacentHTML(
      "afterend",
      '<div class="staff-toolbar"><input id="staff-review-search" type="search" placeholder="Find a submission by team, name or SHA" aria-label="Search submissions"><select id="staff-review-state" aria-label="Review status"><option value="">All review states</option><option value="pending_review">Under review</option><option value="approved">Approved</option><option value="rejected">Rejected</option></select><label>From (UTC+8)<input id="staff-review-from" type="datetime-local"></label><label>Until (UTC+8)<input id="staff-review-until" type="datetime-local"></label></div>',
    );
  $("#staff-review-search").oninput = (e) => {
    staffView.reviewQuery = e.target.value;
    refreshStaffFilter("versions");
  };
  for(const [id,key] of [["staff-review-state","reviewState"],["staff-review-from","reviewFrom"],["staff-review-until","reviewUntil"]]){
    $("#"+id).onchange=e=>{staffView[key]=e.target.value;refreshStaffFilter("versions")};
  }
  const host = document.createElement("section");
  host.className = "panel";
  host.innerHTML =
    '<div class="panel-head"><h2>Server health & recovery</h2><span class="small muted">Read-only monitor · refreshed every minute</span></div><div class="panel-body" id="staff-host-health"></div>';
  panels.operations
    .querySelector("h2")
    .parentNode.insertBefore(host, staffPanelFor("#health-summary"));
  tabs.querySelectorAll("button").forEach((button, index) => {
    button.onclick = () => showStaffTab(button.dataset.staffTab);
    button.onkeydown = (e) => {
      if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(e.key)) return;
      e.preventDefault();
      const n = staffSections.length;
      const next =
        e.key === "Home"
          ? 0
          : e.key === "End"
            ? n - 1
            : (index + (e.key === "ArrowRight" ? 1 : -1) + n) % n;
      showStaffTab(staffSections[next][0]);
      tabs.querySelectorAll("button")[next].focus();
    };
  });
  app.querySelectorAll("[data-staff-link]").forEach(
    (x) =>
      (x.onclick = (e) => {
        e.preventDefault();
        showStaffTab(x.dataset.staffLink);
      }),
  );
  mountStaffAccounts(panels.teams);
  showStaffTab(location.hash.slice(1) || staffView.tab);
}
function filterStaffBatches() {
  const list = $("#admin-batches");
  if (!list || !adminData) return;
  let shown = 0;
  const query = staffView.batchQuery.toLowerCase();
  list.querySelectorAll("tbody tr").forEach((row) => {
    const id = row.querySelector("[data-batch-detail]")?.dataset.batchDetail;
    const b = adminData.batches.find((x) => x.id === id);
    const matches =
      b &&
      (!staffView.batchState || b.status === staffView.batchState) &&
      `${b.team} ${b.id}`.toLowerCase().includes(query);
    row.hidden = !matches;
    if (matches) shown++;
  });
  $("#staff-batch-count").textContent =
    `Showing ${shown} evaluations on this page. Search and filters cover all records.`;
}
function renderStaffTeams(d) {
  if (!d || !$("#staff-team-list")) return;
  const q = staffView.teamQuery.toLowerCase();
  const rows = (d.accounts || []).filter((x) =>
    `${x.team} ${x.email} ${x.id}`.toLowerCase().includes(q),
  );
  $("#staff-team-count").textContent =
    `${rows.length} of ${(d.accounts || []).length} accounts`;
  $("#staff-team-list").innerHTML = rows.length
    ? `<div class="table-wrap"><table><thead><tr><th>Team / account</th><th>Role & access</th><th>Remaining attempts</th><th>Actions</th></tr></thead><tbody>${rows
        .map((a) => {
          const team = d.teams.find((t) => t.id === a.team_id);
          return `<tr><td><strong>${esc(a.team)}</strong><div class="sub">${esc(a.email)}</div><div class="sub mono">${esc(a.id)}</div></td><td data-ui-text>${esc(a.role === "admin" ? "Staff" : "Team")}<div class="sub">${a.must_change ? "Temporary password must be changed" : "Password set"}</div></td><td data-ui-text>${team ? `Full ${team.quotas.full} · Public ${team.quotas.public} · Infra ${team.quotas.infra}` : "—"}</td><td><div class="actions"><button class="button secondary compact" data-account-reset="${esc(a.id)}">Reset password</button>${team ? `<button class="button secondary compact" data-team-quota="${esc(a.team_id)}">Adjust attempts</button>` : ""}</div></td></tr>`;
        })
        .join("")}</tbody></table></div>`
    : empty("No matching accounts.");
  $("#staff-team-list")
    .querySelectorAll("[data-account-reset]")
    .forEach(
      (b) =>
        (b.onclick = () => {
          $("#reset-user").value = b.dataset.accountReset;
          $("#reset-result").hidden = true;
          $("#reset-password").scrollIntoView({
            block: "center",
            behavior: "smooth",
          });
          $("#reset-password").focus();
        }),
    );
  $("#staff-team-list")
    .querySelectorAll("[data-team-quota]")
    .forEach(
      (b) =>
        (b.onclick = () => {
          $("#quota-team").value = b.dataset.teamQuota;
          $("#quota-form").scrollIntoView({
            block: "center",
            behavior: "smooth",
          });
          $("#quota-delta").focus();
        }),
    );
}
function renderStaffReviews(d) {
  if (!d) return;
  const q = staffView.reviewQuery.toLowerCase();
  const rows = d.versions.filter((v) =>
    `${v.team} ${v.name} ${v.sha256} ${v.id}`.toLowerCase().includes(q) &&
      (!staffView.reviewState || v.status===staffView.reviewState) &&
      (!staffView.reviewFrom || v.created*1000>=Date.parse(staffView.reviewFrom+"+08:00")) &&
      (!staffView.reviewUntil || v.created*1000<=Date.parse(staffView.reviewUntil+"+08:00")),
  );
  $("#review-list").innerHTML = rows.length
    ? rows
        .map(
          (v) =>
            `<article class="review-item"><h3>${esc(v.team)} / ${esc(v.name)} ${badge(v.status)}</h3><p class="mono">${esc(v.sha256)}</p><p>${v.report.risks.length} static findings · ${esc(reviewAdviceLabel(v.ai_advisory))}</p>${reviewAdviceHTML(v.ai_advisory)}<div class="actions"><button class="button secondary compact" data-review-download="${esc(v.id)}">Download review report</button><button class="button secondary compact" data-review-open="${esc(v.id)}">Review package</button>${v.status === "pending_review" ? `<button class="button primary compact" data-review-approve="${esc(v.id)}">Approve</button><button class="button danger compact" data-review-reject="${esc(v.id)}">Reject</button>` : ""}</div></article>`,
        )
        .join("")
    : empty("No matching submissions.");
  $("#review-count").textContent =
    `${d.counts?.pending_reviews ?? 0} pending · pending first`;
  $("#review-list")
    .querySelectorAll("[data-review-open]")
    .forEach(
      (b) =>
        (b.onclick = () =>
          showReviewDetails(
            d.versions.find((v) => v.id === b.dataset.reviewOpen),
          )),
    );
  for (const decision of ["approve", "reject"])
    $("#review-list")
      .querySelectorAll(`[data-review-${decision}]`)
      .forEach(
        (b) =>
          (b.onclick = () => {
            const id = b.getAttribute("data-review-" + decision);
            if (
              confirm(
                `${decision === "approve" ? "Approve" : "Reject"} this exact submission version? Record your review reason above. This action is audited.`,
              )
            )
              adminAction({ action: "review", target: id, decision });
          }),
      );
}
function recordGrid(items) {
  return `<div class="record-grid">${items.map(([k, v]) => `<div><span data-ui-text>${esc(k)}</span><b data-no-translate>${esc(v ?? "Not available")}</b></div>`).join("")}</div>`;
}
function showReviewDetails(v) {
  detail("Submission review", v);
  $("#detail-summary").innerHTML =
    recordGrid([
      ["Team", v.team],
      ["Version", v.name],
      ["Status", v.status],
      ["SHA-256", v.sha256],
    ]) +
    `<p class="notice">Package text is untrusted. Review code and findings before deciding; AI advice is not proof of safety.</p><h3>Static findings</h3>${v.report.risks.length ? `<ul>${v.report.risks.map((r) => `<li>${esc(r.rule)} · ${esc(r.file)}</li>`).join("")}</ul>` : "<p>No static findings recorded.</p>"}<h3>AI advice</h3>${reviewAdviceHTML(v.ai_advisory) || "<p>No completed AI advice is available.</p>"}<h3>Captured source excerpts</h3>${(v.report.snippets || []).map((x) => `<h4>${esc(x.file)}</h4><pre>${esc(x.text)}</pre>`).join("") || "<p>No source excerpt is available.</p>"}`;
}
function showBatchDetails(b) {
  detail("Evaluation details", b);
  $("#detail-summary").innerHTML =
    recordGrid([
      ["Team", b.team],
      ["Evaluation ID", b.id],
      ["Version ID", b.version_id],
      ["Mode", modeLabel(b.mode)],
      ["Status", b.status],
      ["Accepted (UTC+8)", stamp(b.accepted_at)],
      ["Passed", b.passed],
      ["Mean runtime", b.mean],
    ]) +
    `<h3>Task progress</h3><p>${Object.entries(b.run_states)
      .map(([k, v]) => esc(k) + ": " + v)
      .join(
        " · ",
      )}</p><h3>Recorded failures</h3>${b.errors?.length ? `<div class="table-wrap"><table><thead><tr><th>Task number</th><th>Attempt</th><th>Error code</th></tr></thead><tbody>${b.errors.map((e) => `<tr><td>${e.ordinal + 1}</td><td>${e.attempt}</td><td class="mono">${esc(e.error)}</td></tr>`).join("")}</tbody></table></div>` : "<p>No task errors are recorded.</p>"}<p class="notice">Retry is available only for eligible infrastructure failures. Unconfirmed remote resources must be reconciled first. A retry preserves the original evaluation and attempt ledger.</p>`;
}
function showAuditDetails(a) {
  detail("Audit entry", a);
  $("#detail-summary").innerHTML =
    recordGrid([
      ["Time (UTC+8)", stamp(a.created)],
      ["Signed-in user ID", a.user_id],
      ["Operator", a.actor],
      ["Action", a.action],
      ["Target", a.object_id],
      ["Reason", a.reason],
    ]) +
    `<h3>Before</h3><pre>${esc(a.before_json)}</pre><h3>After</h3><pre>${esc(a.after_json)}</pre>`;
}
const hostCheckNames = {
  host_clock_synchronized: "Server clock synchronization",
  http_api: "Go API response",
  https_entry: "HTTPS entry",
  https_certificate_expiry: "Certificate expiry",
  disk_capacity: "Disk capacity",
  sqlite_integrity: "Database integrity",
  expired_worker_lease: "Expired worker leases",
  queue_wait_over_30m: "Queue waiting over 30 minutes",
  runs_need_attention: "Historical / unresolved run records",
  backup_under_8h: "Verified backup within 8 hours",
  last_backup_service: "Last backup job",
};
function renderStaffHost(d) {
  const h = d.host_status;
  if (!h?.available) {
    $("#staff-host-health").innerHTML =
      '<p class="notice">No host monitor report is available. Service and backup health cannot be confirmed.</p>';
    return;
  }
  const b = h.backup,
    r = h.reboot;
  $("#staff-host-health").innerHTML =
    `<p>Sampled ${stamp(h.sampled_at)} (UTC+8) ${h.stale ? '<span class="pill red">Stale · check monitoring</span>' : `<span class="pill ${h.status === "ok" ? "green" : "amber"}">${h.status === "ok" ? "Checks healthy" : "Attention recorded"}</span>`}</p><ul class="note-list">${Object.entries(
      h.checks || {},
    )
      .map(
        ([k, v]) =>
          `<li><span>${esc(hostCheckNames[k] || k.replace("service:qiqc-native-", "Service: ").replace("service:", "Service: "))}</span><strong>${esc(v)}</strong></li>`,
      )
      .join(
        "",
      )}</ul><h3 style="margin-top:24px">Backup and restart evidence</h3>${recordGrid(
      [
        [
          "Latest backup",
          b.verified ? "Restore checks passed" : "No verified backup",
        ],
        ["Backup time (UTC+8)", b.created_at ? stamp(b.created_at) : "Unknown"],
        [
          "Files / submission ZIPs checked",
          `${b.files_checked || 0} / ${b.zips_checked || 0}`,
        ],
        ["Storage location", b.offsite ? "Offsite" : "Same VPS · not offsite"],
        ["Controlled host reboot", r.passed ? "Passed" : "Not verified"],
        [
          "Reboot checked (UTC+8)",
          r.checked_at ? stamp(r.checked_at) : "Unknown",
        ],
      ],
    )}<p class="notice">${h.external_delivery ? "External notifications configured." : "Alerts are recorded locally. No email, SMS or chat delivery is configured."} Backups on this VPS do not protect against losing the host or its disk. Historical attention records remain visible until reviewed.</p>`;
}
function renderStaffWorkspace(d) {
  renderStaffPagination(d);
  if (!$("#staff-team-list")) return;
  const c = d.counts || {};
  $("#admin-stats").innerHTML =
    stat("Awaiting review", c.pending_reviews ?? 0, "All submission versions") +
    stat(
      "Queued / running",
      `${c.queued_batches ?? 0}<small>/ ${c.running_batches ?? 0}</small>`,
      `${c.queued_tasks ?? 0} queued tasks · ${c.running_tasks ?? 0} running tasks`,
    ) +
    stat(
      "Needs attention",
      c.attention_batches ?? 0,
      "Includes historical unresolved evaluations",
    ) +
    stat(
      "Model calls",
      d.usage.calls,
      `${d.usage.unpriced || 0} unpriced · no cost assumed`,
    );
  $("#staff-overview-health").innerHTML =
    `<div class="staff-guide"><h3>${d.paused ? "Dispatch is paused" : "Dispatch is enabled"}</h3><p>${c.running_tasks ?? 0} tasks currently running across ${d.slots} configured slots. ${c.pending_reviews ?? 0} submissions await a decision. ${c.attention_batches ?? 0} evaluations have attention status; these are separate from active execution.</p><p>Host monitor: ${!d.host_status?.available ? "unavailable" : d.host_status.stale ? "stale" : d.host_status.status === "ok" ? "checks healthy" : "attention recorded"}. Open Operations for individual checks and backup evidence.</p></div>`;
  renderStaffTeams(d);
  renderStaffReviews(d);
  renderStaffHost(d);
  filterStaffBatches();
  document
    .querySelectorAll("[data-batch-detail]")
    .forEach(
      (b) =>
        (b.onclick = () =>
          showBatchDetails(
            d.batches.find((x) => x.id === b.dataset.batchDetail),
          )),
    );
  document
    .querySelectorAll("[data-audit]")
    .forEach(
      (b) =>
        (b.onclick = () =>
          showAuditDetails(d.audit.find((x) => x.id === b.dataset.audit))),
    );
}

// Reports are staff-authenticated API reads; no public object URLs are issued.
document.addEventListener("click", async (event) => {
 const button=event.target.closest("[data-review-download]"); if(!button)return;
 button.disabled=true;
 try { const report=await api("/admin/review-reports/"+encodeURIComponent(button.dataset.reviewDownload));
 const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:"application/json"}));
 const a=document.createElement("a");a.href=url;a.download="qiqc-review-report.json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
 }catch(error){toast(error.message,true)}finally{button.disabled=false}
});
