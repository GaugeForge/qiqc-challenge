"use strict";
// Staff help assistant: read-only questions about the console, answered by the
// server from the operations manuals and the current admin snapshot.
(() => {
  if (location.pathname.replace(/\/$/, "") !== "/admin") return;
  const history = [];
  let busy = false;
  const escText = (v) => String(v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  // Minimal, escaped Markdown: headings, lists, bold, inline code, code blocks.
  function markdown(text) {
    const blocks = escText(text).split(/```[a-z]*\n?/);
    return blocks.map((block, i) => {
      if (i % 2) return `<pre>${block}</pre>`;
      const inline = (s) => s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>");
      let html = "", list = null;
      for (const line of block.split("\n")) {
        const item = line.match(/^\s*(?:[-*]|(\d+)\.)\s+(.*)$/);
        if (item) {
          const tag = item[1] ? "ol" : "ul";
          if (list !== tag) { if (list) html += `</${list}>`; html += `<${tag}>`; list = tag; }
          html += `<li>${inline(item[2])}</li>`;
          continue;
        }
        if (list) { html += `</${list}>`; list = null; }
        const h = line.match(/^#{1,4}\s+(.*)$/);
        if (h) html += `<p><strong>${inline(h[1])}</strong></p>`;
        else if (line.trim()) html += `<p>${inline(line)}</p>`;
      }
      return html + (list ? `</${list}>` : "");
    }).join("");
  }
  const root = document.createElement("div");
  root.className = "assistant-root";
  root.innerHTML = `<button type="button" id="assistant-toggle" class="assistant-toggle" aria-expanded="false" aria-controls="assistant-panel" hidden>Ask the assistant</button>
<section id="assistant-panel" class="assistant-panel" hidden aria-label="Console assistant">
 <header><div><strong>Console assistant</strong><small>Answers from the staff manual and live console data. Read-only: it cannot change anything.</small></div><button type="button" id="assistant-close" class="assistant-close" aria-label="Close assistant">×</button></header>
 <div id="assistant-log" class="assistant-log" data-no-translate aria-live="polite"><div class="assistant-empty"><p>Ask about any part of this console, for example:</p><ul><li data-assistant-example>这个页面的“需要处理”是什么意思？我该怎么做？</li><li data-assistant-example>How do I give a team one more full evaluation attempt?</li><li data-assistant-example>现在有哪些评测排队或失败？</li></ul></div></div>
 <form id="assistant-form"><textarea id="assistant-input" rows="2" maxlength="4000" placeholder="Ask a question… (Enter to send, Shift+Enter for a new line)" required></textarea><div class="assistant-actions"><button type="button" id="assistant-reset" class="button secondary compact">New conversation</button><button id="assistant-send" class="button primary compact">Send</button></div></form>
</section>`;
  document.body.append(root);
  const $a = (s) => root.querySelector(s);
  const panel = $a("#assistant-panel"), toggle = $a("#assistant-toggle"), log = $a("#assistant-log"), input = $a("#assistant-input");
  function open(show) {
    panel.hidden = !show;
    toggle.setAttribute("aria-expanded", String(show));
    if (show) input.focus();
  }
  function addMessage(role, html) {
    $a(".assistant-empty")?.remove();
    const item = document.createElement("div");
    item.className = "assistant-msg " + role;
    item.innerHTML = html;
    log.append(item);
    log.scrollTop = log.scrollHeight;
    return item;
  }
  // Outline of the rendered console (tabs, headings, labels, buttons) so answers
  // name real controls. Data rows are excluded; the server sends live data.
  function uiOutline() {
    const lines = [];
    document.querySelectorAll("[data-staff-section]").forEach((section) => {
      lines.push(`## Tab: ${section.querySelector("h2")?.textContent.trim() || section.dataset.staffSection}`);
      const seen = new Set();
      section.querySelectorAll("h3,.panel-head h2,summary,legend,.field > span,button,label > span:first-child").forEach((el) => {
        if (el.closest("table,.review-item,[data-no-translate]")) return;
        const kind = el.tagName === "BUTTON" ? "button" : /^H|SUMMARY|LEGEND/.test(el.tagName) ? "heading" : "field";
        const text = el.textContent.replace(/\s+/g, " ").trim().slice(0, 120);
        if (text && !seen.has(kind + text)) { seen.add(kind + text); lines.push(`- ${kind}: ${text}`); }
      });
    });
    return lines.join("\n").slice(0, 15000);
  }
  async function ask(question) {
    if (busy || !question.trim()) return;
    busy = true;
    $a("#assistant-send").disabled = true;
    history.push({ role: "user", content: question.trim() });
    addMessage("user", escText(question.trim()));
    const pending = addMessage("assistant pending", window.staffLocale === "zh" ? "正在查阅手册和当前数据…" : "Checking the manual and current data…");
    try {
      const r = await fetch("/api/admin/assistant", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-QIQC-Request": "1", "X-QIQC-Workspace": "staff" },
        body: JSON.stringify({ messages: history.slice(-20), ui_outline: uiOutline() }),
        signal: AbortSignal.timeout(300000),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
      history.push({ role: "assistant", content: data.reply });
      pending.className = "assistant-msg assistant";
      pending.innerHTML = markdown(data.reply);
    } catch (err) {
      history.pop();
      pending.className = "assistant-msg error";
      pending.textContent = err.name === "TimeoutError" ? "The assistant took too long. Try a narrower question." : err.message;
    } finally {
      busy = false;
      $a("#assistant-send").disabled = false;
      log.scrollTop = log.scrollHeight;
    }
  }
  toggle.onclick = () => open(panel.hidden);
  $a("#assistant-close").onclick = () => { open(false); toggle.focus(); };
  $a("#assistant-reset").onclick = () => { history.length = 0; log.innerHTML = ""; input.focus(); };
  root.querySelectorAll("[data-assistant-example]").forEach((li) => (li.onclick = () => ask(li.textContent)));
  $a("#assistant-form").onsubmit = (e) => { e.preventDefault(); const q = input.value; input.value = ""; ask(q); };
  input.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $a("#assistant-form").requestSubmit(); } };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) open(false); });
  // Only signed-in staff with a changed password can use it; the API enforces this too.
  setInterval(() => { toggle.hidden = !document.querySelector(".staff-tabs"); if (toggle.hidden) open(false); }, 1000);
})();
