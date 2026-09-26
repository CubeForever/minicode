/* minicode web — 原生 JS，无构建、无依赖 */
"use strict";

const TOKEN = "__TOKEN__";
const $ = (s, el) => (el || document).querySelector(s);
const chat = $("#chat");

/* ---------------- markdown 渲染（自研，安全转义优先） ---------------- */
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function inlineMd(raw) {
  let s = esc(raw);
  s = s.replace(/`([^`]+)`/g, (_m, c) => `<code>${c}</code>`);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  s = s.replace(/\*([^*\n]+)\*/g, "<i>$1</i>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s;
}
function mdBlocks(text) {
  const lines = String(text).split("\n");
  const out = [];
  let para = [], list = null;
  const flushP = () => {
    if (para.length) { out.push(`<p>${inlineMd(para.join(" "))}</p>`); para = []; }
  };
  const flushL = () => {
    if (list) {
      out.push(`<${list.tag}>` + list.items.map(x => `<li>${inlineMd(x)}</li>`).join("")
        + `</${list.tag}>`);
      list = null;
    }
  };
  for (const line of lines) {
    let m;
    if (!line.trim()) { flushP(); flushL(); continue; }
    if ((m = line.match(/^(#{1,4})\s+(.*)/))) {
      flushP(); flushL();
      const h = Math.min(m[1].length + 1, 5);
      out.push(`<h${h}>${inlineMd(m[2])}</h${h}>`);
    } else if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) {
      flushP(); flushL(); out.push("<hr>");
    } else if ((m = line.match(/^>\s?(.*)/))) {
      flushP(); flushL(); out.push(`<blockquote>${inlineMd(m[1])}</blockquote>`);
    } else if ((m = line.match(/^\s*[-*]\s+(.+)/))) {
      flushP();
      if (!list || list.tag !== "ul") { flushL(); list = { tag: "ul", items: [] }; }
      list.items.push(m[1]);
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.+)/))) {
      flushP();
      if (!list || list.tag !== "ol") { flushL(); list = { tag: "ol", items: [] }; }
      list.items.push(m[1]);
    } else {
      para.push(line.trim());
    }
  }
  flushP(); flushL();
  return out.join("");
}
function md(src) {
  const parts = String(src == null ? "" : src).split("```");
  let html = "";
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) {                       // 围栏代码块
      const body = parts[i];
      const nl = body.indexOf("\n");
      const lang = (nl >= 0 ? body.slice(0, nl) : "").trim();
      const code = nl >= 0 ? body.slice(nl + 1) : body;
      html += `<div class="codeblock"><div class="codehead"><span>${esc(lang || "code")}</span>` +
        `<button class="copybtn" type="button">复制</button></div>` +
        `<pre><code>${esc(code.replace(/\n$/, ""))}</code></pre></div>`;
    } else {
      html += mdBlocks(parts[i]);
    }
  }
  return html;
}

/* ---------------- 基础 UI ---------------- */
function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
}
function hideHero() { const h = $("#hero"); if (h) h.remove(); }
function scrollDown(force) {
  const m = $("main");
  const near = m.scrollHeight - m.scrollTop - m.clientHeight < 140;
  if (near || force) m.scrollTop = m.scrollHeight;
}

function addUser(text) {
  hideHero();
  chat.appendChild(el("div", "msg user", esc(text)));
  scrollDown(true);
}

/* 助手流式消息：thinking 折叠块 + markdown 正文 */
let current = null;   // {el, body, think, raw, thinkRaw, timer}
function newAssistant() {
  const wrap = el("div", "msg assistant");
  const mdEl = el("div", "md");
  wrap.appendChild(mdEl);
  chat.appendChild(wrap);
  current = { el: wrap, md: mdEl, raw: "", think: null, thinkRaw: "", timer: null };
  current.timer = setInterval(() => {
    if (current && current.dirty) { current.dirty = false; current.md.innerHTML = md(current.raw); }
  }, 60);
  return current;
}
function closeCurrent() {
  if (!current) return;
  clearInterval(current.timer);
  if (current.raw) current.md.innerHTML = md(current.raw);
  if (!current.raw && !current.thinkRaw) current.el.remove();
  current = null;
}
function streamText(text) {
  hideHero();
  if (!current) newAssistant();
  current.raw += text;
  current.dirty = true;
  scrollDown();
}
function streamReason(text) {
  hideHero();
  if (!current) newAssistant();
  if (!current.think) {
    current.think = el("details", "thinking");
    current.think.open = true;
    current.think.innerHTML = `<summary>💭 思考过程</summary><div class="think-body"></div>`;
    current.el.insertBefore(current.think, current.md);
  }
  current.thinkRaw += text;
  current.think.querySelector(".think-body").textContent = current.thinkRaw;
  scrollDown();
}

/* 工具卡片 */
const TOOL_KIND = {
  read_file: "read", glob: "read", grep: "read", list_dir: "read",
  web_fetch: "read", web_search: "read", bash_output: "read", bash_kill: "read",
  skill: "read", dispatch_agent: "read", consult_panel: "read", todo_write: "meta",
  ask_user: "meta", exit_plan: "meta",
  write_file: "write", edit_file: "write", apply_patch: "write",
  notebook_edit: "write", brain_write: "write",
  bash: "bash",
};
function kindOf(name) {
  if (String(name).startsWith("mcp__")) return "mcp";
  return TOOL_KIND[name] || "meta";
}
let resultQueue = [];   // 等待结果的工具卡片（并行批次按顺序回填）
function oneLine(s) {
  const t = String(s == null ? "" : s).replace(/\s+/g, " ").trim();
  return t.length > 140 ? t.slice(0, 140) + "…" : t;
}
function addTool(name, summary) {
  closeCurrent();
  hideHero();
  const kind = kindOf(name);
  const card = el("div", `tool-card k-${kind}`);
  card.innerHTML = `<div class="tool-head"><span class="dot"></span><b>${esc(name)}</b>` +
    `<span class="summary">${esc(oneLine(summary))}</span></div>` +
    `<pre class="tool-result" hidden></pre>`;
  card.querySelector(".tool-head").addEventListener("click", () => {
    const r = card.querySelector(".tool-result");
    r.hidden = !r.hidden;
  });
  chat.appendChild(card);
  resultQueue.push(card);
  scrollDown();
}
function attachResult(text) {
  let t = resultQueue.shift();
  if (!t) {                       // 无排队卡片（如被拒绝的工具调用）→ 回退到最后一张
    const cards = chat.querySelectorAll(".tool-card");
    t = cards.length ? cards[cards.length - 1] : null;
  }
  if (!t) { sysLine("plain", text); return; }
  const r = t.querySelector(".tool-result");
  const first = !r.textContent;
  r.textContent += (first ? "" : "\n") + text;
  if (text) r.hidden = false;
  scrollDown();
}

/* 系统行 / todos */
function sysLine(kind, text) {
  if (!text) return;
  hideHero();
  chat.appendChild(el("div", `sysline ${kind}`, esc(text)));
  scrollDown();
}
function renderTodos(todos) {
  if (!todos || !todos.length) return;
  hideHero();
  const mark = { completed: "✓", in_progress: "→", pending: "○" };
  const rows = todos.map(t =>
    `<div class="t-${esc(t.status)}">${mark[t.status] || "○"} ${esc(t.content)}</div>`).join("");
  chat.appendChild(el("div", "todo-card", `<b>To-dos</b>${rows}`));
  scrollDown();
}

/* 确认卡片（权限 / 敏感路径 / 高危） */
function renderConfirm(id, title, preview) {
  closeCurrent();
  hideHero();
  const card = el("div", "confirm-card");
  const pv = preview ? preview.split("\n").map(ln => {
    const cls = /^\s*\+/.test(ln) ? "add" : (/^\s*-/.test(ln) ? "del" : "");
    return cls ? `<span class="${cls}">${esc(ln)}</span>` : esc(ln);
  }).join("\n") : "";
  card.innerHTML = `<div class="confirm-title">⚠ ${esc(title)}</div>` +
    (preview ? `<pre class="preview">${pv}</pre>` : "") +
    `<div class="actions">` +
    `<button data-v="y">允许</button><button data-v="a">本次总是</button>` +
    `<button data-v="n" class="danger">拒绝</button></div>`;
  card.addEventListener("click", e => {
    const b = e.target.closest("button");
    if (b) answerAsk(id, b.dataset.v, card);
  });
  chat.appendChild(card);
  scrollDown(true);
}

/* ask_user 选项卡片 */
function renderChoose(id, question, options, multi, allowOther) {
  closeCurrent();
  hideHero();
  const card = el("div", "choose-card");
  card.innerHTML = `<div class="q">❓ ${esc(question)}</div>`;
  const picked = new Set();
  const finish = (labels) => {
    card.innerHTML = `<div class="q">❓ ${esc(question)}</div>` +
      `<div class="done">✓ 已回答：${esc(labels.join("；") || "（跳过）")}</div>`;
    api("/api/answer", { id, value: labels });
  };
  options.forEach((o, i) => {
    const b = el("button", "opt",
      `${multi ? `<input type="checkbox" style="accent-color:var(--accent)"> ` : ""}` +
      `<b>${esc(o.label)}</b>` + (o.description ? `<div class="desc">${esc(o.description)}</div>` : ""));
    b.addEventListener("click", () => {
      if (multi) {
        const cb = b.querySelector("input");
        cb.checked = !cb.checked;
        if (cb.checked) picked.add(i); else picked.delete(i);
      } else {
        finish([o.label]);
      }
    });
    card.appendChild(b);
  });
  if (multi) {
    const submit = el("button", "opt", "<b>提交选择</b>");
    submit.addEventListener("click", () =>
      finish([...picked].map(i => options[i].label)));
    card.appendChild(submit);
  }
  if (allowOther) {
    const row = el("div", "other");
    const input = el("input");
    input.type = "text";
    input.placeholder = "其他（自由输入）…";
    const ok = el("button", "opt", "确定");
    ok.style.flex = "0 0 auto";
    ok.addEventListener("click", () => {
      const v = input.value.trim();
      if (v) finish([v]);
    });
    row.appendChild(input);
    row.appendChild(ok);
    card.appendChild(row);
  }
  chat.appendChild(card);
  scrollDown(true);
}
async function answerAsk(id, value, card) {
  const acts = card.querySelector(".actions");
  if (acts) acts.remove();
  await api("/api/answer", { id, value });
  if (card) {
    const div = el("div");
    div.innerHTML = value === "n" ? `<div class="declined">✗ 已拒绝</div>`
      : `<div class="approved">✓ 已${value === "a" ? "总是" : ""}允许</div>`;
    card.appendChild(div.firstChild);
  }
}

/* ---------------- API ---------------- */
async function api(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Minicode-Token": TOKEN },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    sysLine("error", err.error || `HTTP ${r.status}`);
    throw new Error(err.error || r.status);
  }
  return r.json();
}
async function getStatus() {
  const r = await fetch("/api/status", { headers: { "X-Minicode-Token": TOKEN } });
  return r.json();
}

/* ---------------- 状态栏 ---------------- */
function setBusy(b) {
  document.body.classList.toggle("busy", b);
  $("#btnSend").disabled = b;
  $("#btnSend").textContent = b ? "⏳" : "➤";
  if (!b) closeCurrent();
}
async function refreshStatus() {
  try {
    const s = await getStatus();
    $("#model").textContent = s.model;
    $("#ctxFill").style.width = Math.min(100, s.context_tokens / (s.context_limit || 1) * 100) + "%";
    $("#ctxFill").style.background =
      s.context_tokens / (s.context_limit || 1) > 0.8 ? "var(--red)"
        : s.context_tokens / (s.context_limit || 1) > 0.5 ? "var(--amber)" : "var(--green)";
    $("#ctxText").textContent =
      `${fmtTok(s.context_tokens)} / ${fmtTok(s.context_limit)} tok`;
    if (document.activeElement !== $("#modeSel")) $("#modeSel").value = s.mode;
    setBusy(s.busy);
  } catch (e) { /* 服务未就绪时静默 */ }
}
function fmtTok(n) {
  n = Number(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 10000) return Math.round(n / 1000) + "k";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

/* ---------------- SSE 事件流 ---------------- */
function connectEvents() {
  const es = new EventSource(`/api/events?token=${encodeURIComponent(TOKEN)}`);
  es.onopen = () => $("#conn").classList.add("on");
  es.onerror = () => $("#conn").classList.remove("on");
  es.onmessage = ev => {
    let e;
    try { e = JSON.parse(ev.data); } catch (err) { return; }
    switch (e.t) {
      case "hello": $("#conn").classList.add("on"); break;
      case "user": closeCurrent(); addUser(e.text); break;
      case "text": streamText(e.text); break;
      case "reason": streamReason(e.text); break;
      case "tool": addTool(e.name, e.summary); break;
      case "result": attachResult(e.text); break;
      case "info": sysLine("info", e.text); break;
      case "warn": sysLine("warn", e.text); break;
      case "error": sysLine("error", e.text); break;
      case "plain": sysLine("plain", e.text); break;
      case "tokens": break;                       // 状态轮询已覆盖
      case "todos": renderTodos(e.todos); break;
      case "confirm": renderConfirm(e.id, e.title, e.preview); break;
      case "choose": renderChoose(e.id, e.question, e.options, e.multi, e.allow_other); break;
      case "busy": setBusy(e.busy); break;
      case "cleared": location.reload(); break;
    }
  };
}

/* ---------------- 历史回放 ---------------- */
function argsSummary(argsStr) {
  try {
    const o = JSON.parse(argsStr || "{}");
    if (o.command != null) return oneLine(o.command);
    if (o.path != null) return oneLine(String(o.path) + (o.old_string ? " (edit)" : ""));
    if (o.pattern != null) return oneLine(String(o.pattern));
    return oneLine(JSON.stringify(o));
  } catch (e) { return oneLine(String(argsStr)); }
}
function renderHistory(data) {
  for (const m of data.messages || []) {
    if (m.role === "user") {
      addUser(typeof m.content === "string" ? m.content : "(多部分内容)");
    } else if (m.role === "assistant") {
      if (m.content) {
        const a = newAssistant();
        a.raw = m.content;
        a.dirty = true;
        a.md.innerHTML = md(m.content);
        clearInterval(a.timer);
        current = null;
      }
      for (const tc of m.tool_calls || []) addTool(tc.name, argsSummary(tc.args));
    } else if (m.role === "tool") {
      const body = typeof m.content === "string" ? m.content
        : (Array.isArray(m.content)
          ? m.content.filter(b => b.type === "text").map(b => b.text).join("\n")
          : "");
      attachResult(body || (m.is_error ? "(错误)" : ""));
    }
  }
  renderTodos(data.todos);
}

/* ---------------- 输入区 ---------------- */
const input = $("#input");
function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(180, input.scrollHeight) + "px";
}
input.addEventListener("input", autosize);
input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$("#btnSend").addEventListener("click", send);
async function send() {
  const text = input.value.trim();
  if (!text || document.body.classList.contains("busy")) return;
  input.value = "";
  autosize();
  try { await api("/api/turn", { prompt: text }); }
  catch (e) { /* 错误已由 api() 展示 */ }
}

/* 头部按钮 */
$("#modeSel").addEventListener("change", e => api("/api/mode", { mode: e.target.value }));
$("#btnCompact").addEventListener("click", () => api("/api/compact"));
$("#btnClear").addEventListener("click", () => {
  if (confirm("开始新会话？（当前上下文将被清空，历史文件改动不受影响）")) api("/api/clear");
});

/* 代码复制（事件委托） */
chat.addEventListener("click", e => {
  const btn = e.target.closest(".copybtn");
  if (!btn) return;
  const code = btn.closest(".codeblock").querySelector("code").textContent;
  navigator.clipboard.writeText(code).then(() => {
    btn.textContent = "已复制";
    setTimeout(() => { btn.textContent = "复制"; }, 1200);
  });
});

/* ---------------- 启动 ---------------- */
(async function boot() {
  try {
    const s = await getStatus();
    if (!s.busy) {
      const hist = await fetch("/api/messages",
        { headers: { "X-Minicode-Token": TOKEN } }).then(r => r.json());
      renderHistory(hist);
      if ((hist.messages || []).length) hideHero();
    }
  } catch (e) { /* 忽略 */ }
  connectEvents();
  refreshStatus();
  setInterval(refreshStatus, 2500);
  input.focus();
})();
