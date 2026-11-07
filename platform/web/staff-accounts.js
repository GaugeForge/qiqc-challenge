"use strict";
// Account provisioning is staff-only. Plaintext temporary credentials stay in
// this page's memory until downloaded/cleared; they never enter storage or logs.
let clearStaffAccountSecrets = () => {};
let staffAccountIssued = () => {};
let staffBeginAccountCreation = () => true;
let staffEndAccountCreation = () => {};
function accountPassword() {
  const bytes = crypto.getRandomValues(new Uint8Array(24));
  return 'Q-' + btoa(String.fromCharCode(...bytes)).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');
}
function parseAccountCSV(text) {
  if (new TextEncoder().encode(text).length > 256 * 1024) throw Error('CSV must be smaller than 256 KiB.');
  const rows = []; let row = [], field = '', quoted = false, closed = false;
  text = text.replace(/^\uFEFF/, '');
  const pushField = () => { row.push(field); field = ''; closed = false; };
  const pushRow = () => { pushField(); if (row.some(v => v.trim())) rows.push(row); row = []; };
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (ch === '"') { quoted = false; closed = true; }
      else field += ch;
    } else if (ch === '"') {
      if (field || closed) throw Error('Invalid CSV quoting. Use the template and try again.');
      quoted = true;
    } else if (ch === ',') pushField();
    else if (ch === '\n' || ch === '\r') { if (ch === '\r' && text[i + 1] === '\n') i++; pushRow(); }
    else { if (closed && ch.trim()) throw Error('Invalid CSV quoting. Use the template and try again.'); if (!closed) field += ch; }
  }
  if (quoted) throw Error('Invalid CSV quoting. Use the template and try again.');
  pushRow();
  if (rows[0]?.[0].trim().toLowerCase() === 'email' && rows[0]?.[1]?.trim().toLowerCase() === 'team') rows.shift();
  if (rows.length < 1 || rows.length > 200) throw Error('Provide between 1 and 200 team accounts.');
  const seen = new Set();
  return rows.map((r, i) => {
    const email = (r[0] || '').trim().toLowerCase(), team = (r[1] || '').trim();
    if (r.length !== 2 || !/^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(email) || email.length > 200 || !team || [...team].length > 100)
      throw Error(`Row ${i + 1}: enter a valid email and a team name of 1–100 characters.`);
    if (seen.has(email)) throw Error(`Row ${i + 1}: duplicate email in this import. No accounts were created.`);
    seen.add(email); return { email, team };
  });
}
function accountCSVCell(value, protectFormula = true) {
  const s = String(value);
  // Protect display names from spreadsheet formulas. Identity/password columns
  // must remain exact; credential CSV files should be imported as text.
  return '"' + (protectFormula && /^\s*[=+@-]/.test(s) ? "'" + s : s).replaceAll('"', '""') + '"';
}
function downloadAccountCSV(name, rows, exactColumns = []) {
  const content = '\uFEFF' + rows.map(row => row.map((value, column) => accountCSVCell(value, !exactColumns.includes(column))).join(',')).join('\r\n') + '\r\n';
  const url = URL.createObjectURL(new Blob([content], { type: 'text/csv;charset=utf-8' }));
  const a = document.createElement('a'); a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function mountStaffAccounts(section) {
  const form = $('#account-form'), previous = staffPanelFor('#account-form');
  const reset = $('#reset-form'), close = $('#close-demo');
  let prepared = [], issued = [], busy = false, downloaded = false;
  const container = document.createElement('section'); container.className = 'panel account-provisioning';
  container.innerHTML = `<div class="panel-head"><div><span class="eyebrow">ACCOUNT PROVISIONING</span><h3>Create accounts</h3><p class="help">Create one account or import a team list. Every account gets its own temporary password.</p></div></div>
    <div class="panel-body"><div class="account-mode-switch" role="group" aria-label="Account creation mode"><button type="button" class="button secondary" data-account-mode="single" aria-pressed="true">Single account</button><button type="button" class="button secondary" data-account-mode="bulk" aria-pressed="false">Import team accounts</button></div>
    <label class="field"><span>Reason for account creation</span><input id="account-reason" value="Participant onboarding" maxlength="1000" required></label>
    <div id="account-single"></div><div id="account-bulk" hidden>
      <div class="import-instructions"><span class="index">01</span><div><h3>Prepare your team list</h3><p>Two columns: email and team. Up to 200 accounts per import. Passwords are generated separately for each account.</p><button type="button" id="account-template" class="text-button">Download CSV template</button></div></div>
      <label class="field"><span>Upload a CSV file</span><input type="file" id="accounts-file" accept=".csv,text/csv"></label>
      <label class="field"><span>Or paste your CSV</span><textarea id="accounts-csv" rows="6" spellcheck="false" placeholder="email,team&#10;captain@example.com,Team Orion"></textarea></label>
      <button type="button" id="accounts-preview" class="button primary">Review accounts →</button>
      <section id="accounts-preview-panel" class="import-preview" hidden><div class="panel-head"><h3>Review before creating</h3><span id="accounts-count" class="pill"></span></div><p class="help">All rows are validated together. If any email already exists, no accounts in this import are created.</p><div id="accounts-preview-table"></div><button type="button" id="accounts-create" class="button primary">Create all team accounts</button></section>
    </div><p id="accounts-error" class="form-error" role="alert" hidden></p>
    <section id="account-issued" class="account-issued" hidden><div class="panel-head"><h3 id="account-issued-title">Accounts created</h3><span id="account-issued-count" class="pill green"></span></div><p>Download the credentials before leaving this page. Share each account privately. First sign-in requires a password change; no email is sent.</p><div class="actions"><button type="button" id="accounts-download" class="button primary">Download credentials CSV</button><button type="button" id="accounts-clear" class="button secondary">Clear credentials from this page</button></div></section></div>`;
  section.querySelector(":scope > p.help").after(container);
  container.querySelector('#account-single').append(form);
  const resetPanel = document.createElement('section'); resetPanel.className = 'panel';
  resetPanel.innerHTML = '<div class="panel-head"><h3>Reset account access</h3><span class="small muted">Select an account in the directory below</span></div><div class="panel-body"></div>';
  resetPanel.querySelector('.panel-body').append(reset);section.append(resetPanel);
  const admission = document.createElement('section');admission.className = 'panel';
  admission.innerHTML = '<div class="panel-head"><h3>Evaluation admission</h3></div><div class="panel-body"><p>Closing admission lets accepted evaluations finish. This control cannot reopen admission.</p></div>';
  admission.querySelector('.panel-body').append(close);$('#staff-section-operations').append(admission);previous.remove();
  const password = $('#account-password');
  password.value = accountPassword(); password.maxLength = 200;
  const controls = document.createElement('div');controls.className = 'password-tools';
  controls.innerHTML = '<button type="button" id="account-generate" class="button secondary compact">Generate password</button><button type="button" id="account-reveal" class="text-button" aria-pressed="false">Show password</button><span class="help">Unique temporary password · change required at first sign-in</span>';
  password.closest('label').after(controls);
  $('#account-generate').onclick = () => {password.value = accountPassword();};
  $('#account-reveal').onclick = e => { const reveal = password.type === 'password';password.type = reveal ? 'text' : 'password';e.currentTarget.setAttribute('aria-pressed', String(reveal));e.currentTarget.textContent = reveal ? 'Hide password' : 'Show password'; };
  const error = message => {$('#accounts-error').textContent = message;$('#accounts-error').hidden = !message;};
  const resetPreview = () => {prepared = [];$('#accounts-preview-panel').hidden = true;error('');};
  const guardIssued = () => !issued.length || downloaded || window.confirm('Credentials have not been downloaded. Clear them from this page anyway?');
  staffBeginAccountCreation = () => {
    if (busy || !guardIssued()) return false;
    busy = true;container.querySelectorAll('button,input,textarea,select').forEach(e => e.disabled = true);return true;
  };
  staffEndAccountCreation = () => {busy = false;container.querySelectorAll('button,input,textarea,select').forEach(e => e.disabled = false);};
  const submit = form.querySelector('button:not([type=button])');
  submit.type = 'submit'; submit.className = 'button primary';
  const showMode = mode => {
    if (busy) return;
    container.querySelectorAll('[data-account-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.accountMode === mode)));
    $('#account-single').hidden = mode !== 'single';$('#account-bulk').hidden = mode !== 'bulk';error('');
  };
  container.querySelectorAll('[data-account-mode]').forEach(b => b.onclick = () => showMode(b.dataset.accountMode));
  $('#account-template').onclick = () => downloadAccountCSV('qiqc-team-template.csv', [['email','team'],['captain@example.com','Team Orion']]);
  $('#accounts-csv').oninput = resetPreview;
  $('#accounts-file').onchange = async e => {
    resetPreview();const f = e.target.files[0];if (!f) return;
    if (f.size > 256 * 1024) {error('CSV must be smaller than 256 KiB.');return;}
    try {$('#accounts-csv').value = await f.text();} catch {error('Could not read the CSV file. Please try again.');}
  };
  $('#accounts-preview').onclick = () => {
    resetPreview();
    try {
      prepared = parseAccountCSV($('#accounts-csv').value).map(row => ({...row,password:accountPassword()}));
      $('#accounts-count').textContent = `${prepared.length} accounts`;
      $('#accounts-preview-table').innerHTML = `<div class="table-wrap"><table><thead><tr><th>#</th><th>Email address</th><th>Team name</th></tr></thead><tbody>${prepared.map((r,i)=>`<tr data-no-translate><td>${i+1}</td><td>${esc(r.email)}</td><td>${esc(r.team)}</td></tr>`).join('')}</tbody></table></div>`;
      $('#accounts-preview-panel').hidden = false;
    } catch (e) {error(e.message);}
  };
  const publishIssued = rows => {
    issued = rows;downloaded = false;$('#account-issued-title').textContent = 'Accounts created';$('#account-issued-count').textContent = `${rows.length} accounts`;
    $('#account-issued').hidden = false;error('');
  };
  staffAccountIssued = publishIssued;
  $('#accounts-create').onclick = async () => {
    if (busy || !prepared.length) return;
    const reason = $('#account-reason').value.trim();
    if ([...reason].length < 3) {error('Enter a reason of at least 3 characters.');return;}
    if (!staffBeginAccountCreation()) return;
    error('');const rows = prepared;
    try {
      const result = await api('/admin/accounts/bulk', {method:'POST',json:{actor:user.email,reason,accounts:rows}});
      if (result.count !== rows.length) throw Error('Unexpected account receipt. Check the account directory before retrying.');
      publishIssued(rows);prepared = [];$('#accounts-preview-panel').hidden = true;$('#accounts-csv').value = '';$('#accounts-file').value = '';
      toast('Accounts created. Download and share the credentials privately.');await refresh();
    } catch(e) {
      // A transport failure cannot prove whether the transaction committed.
      // Keep the prepared credentials available while staff reconcile the directory.
      error(e.message);
      if (e.uncertain || e instanceof TypeError || /temporarily unavailable|network|fetch/i.test(e.message)) {
        publishIssued(rows);$('#account-issued-title').textContent = 'Creation not confirmed';$('#account-issued-count').textContent = 'Creation not confirmed';
        error('Connection interrupted. Check the account directory before retrying. Download the prepared credentials for reconciliation; creation is not confirmed.');
      }
    } finally {staffEndAccountCreation();}
  };
  $('#accounts-download').onclick = () => {
    if (!issued.length) return;
    downloadAccountCSV('qiqc-account-credentials.csv', [['email','team','temporary_password','sign_in_url'],...issued.map(r=>[r.email,r.team,r.password,location.origin+(r.role === 'admin' ? '/admin' : '/submit')])], [0,2,3]);downloaded = true;
  };
  const clear = () => {issued=[];prepared=[];downloaded=false;staffAccountIssued=()=>{};staffBeginAccountCreation=()=>true;staffEndAccountCreation=()=>{};};
  clearStaffAccountSecrets = clear;
  $('#accounts-clear').onclick = () => {if(guardIssued()){issued=[];downloaded=false;$('#account-issued').hidden=true;}};
  section.querySelector('#staff-team-list')?.closest('section').classList.add('account-directory');
  // Overview provides direct entry to either provisioning workflow.
  document.querySelectorAll('[data-account-shortcut]').forEach(b => b.onclick = () => {
    showStaffTab('teams');showMode(b.dataset.accountShortcut);container.scrollIntoView({block:'start',behavior:'smooth'});
  });
}
