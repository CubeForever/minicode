/* minicode web — 原生 JS，无构建、无依赖。DeepSeek 风格渲染层。 */
"use strict";

const TOKEN = "__TOKEN__";
const $ = (s, el) => (el || document).querySelector(s);
const chat = $("#chat");

/* ---------------- 主题 ---------------- */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("minicode-theme", t); } catch (e) { /* 隐私模式忽略 */ }
}
applyTheme((() => {
  try { return localStorage.getItem("minicode-theme") || "dark"; }
  catch (e) { return "dark"; }
})());
$("#btnTheme").addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
});

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
function splitRow(l) {
  l = l.trim();
  if (l.startsWith("|")) l = l.slice(1);
  if (l.endsWith("|")) l = l.slice(0, -1);
  return l.split("|").map(c => c.trim());
}
function buildTable(header, rows) {
  if (header.length < 2) return null;
  const th = header.map(h => `<th>${inlineMd(h)}</th>`).join("");
  const trs = rows.map(r =>
    `<tr>${r.map(c => `<td>${inlineMd(c)}</td>`).join("")}</tr>`).join("");
  return `<table><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table>`;
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
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
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
    } else if (line.includes("|") && i + 1 < lines.length
               && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])) {
      const t = buildTable(splitRow(line), []);
      if (t) {                                   // GFM 表格
        flushP(); flushL();
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        i--;
        out.push(buildTable(splitRow(line), rows));
      } else {
        para.push(line.trim());
      }
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
  const near = m.scrollHeight - m.scrollTop - m.clientHeight < 160;
  if (near || force) m.scrollTop = m.scrollHeight;
}

function addUser(text) {
  hideHero();
  chat.appendChild(el("div", "msg user", esc(text)));
  scrollDown(true);
}

/* ---------------- 工作中占位（三点呼吸） ---------------- */
let shimmerEl = null;
function showShimmer(label) {
  hideHero();
  if (shimmerEl) return;
  shimmerEl = el("div", "working-row",
    `<span class="lbl">${esc(label)}</span><span class="dots"><i></i><i></i><i></i></span>`);
  chat.appendChild(shimmerEl);
  scrollDown();
}
function hideShimmer() {
  if (shimmerEl) { shimmerEl.remove(); shimmerEl = null; }
}

/* ---------------- 助手消息（等宽元数据 + markdown 正文） ---------------- */
let current = null;   // {el, md, think, thinkStart, thinkDone, raw, timer}
const nowHM = () => new Date().toTimeString().slice(0, 5);
function newAssistant() {
  const wrap = el("div", "msg assistant");
  wrap.appendChild(el("div", "msg-meta",
    `<span class="who-mark">❯</span><span class="who">minicode</span>` +
    `<span class="ts">${nowHM()}</span>`));
  const mdEl = el("div", "md");
  wrap.appendChild(mdEl);
  chat.appendChild(wrap);
  current = { el: wrap, md: mdEl, think: null, thinkStart: 0, thinkDone: false,
              raw: "", timer: null };
  current.timer = setInterval(() => {
    if (current && current.dirty) {
      current.dirty = false;
      current.md.innerHTML = md(current.raw);
      placeCaret(current.md);          // 流式打字光标
      scrollDown();
    }
  }, 60);
  return current;
}
function placeCaret(mdEl) {
  mdEl.querySelector(".stream-caret") && mdEl.querySelector(".stream-caret").remove();
  const walker = document.createTreeWalker(mdEl, NodeFilter.SHOW_TEXT);
  let last = null;
  while (walker.nextNode()) {
    if (walker.currentNode.textContent) last = walker.currentNode;
  }
  if (last && last.parentNode) {
    last.parentNode.insertBefore(el("span", "stream-caret"), last.nextSibling);
  }
}
function closeCurrent() {
  if (!current) return;
  clearInterval(current.timer);
  finalizeThink();
  if (current.raw) {
    const text = current.raw;                  // 捕获快照：current 即将置空
    current.md.innerHTML = md(text);
    addCopyAction(current.el, () => text);
  }
  if (!current.raw && !current.think) current.el.remove();
  current = null;
}
function addCopyAction(wrap, getText) {
  const row = el("div", "msg-actions", `<button type="button">复制</button>`);
  const btn = row.querySelector("button");
  btn.addEventListener("click", () => {
    navigator.clipboard.writeText(getText()).then(() => {
      btn.textContent = "已复制";
      setTimeout(() => { btn.textContent = "复制"; }, 1200);
    });
  });
  wrap.appendChild(row);
}

/* ---------------- 思考面板（三点呼吸 + 等宽计时） ---------------- */
function streamReason(text) {
  hideShimmer();
  if (!current) newAssistant();
  if (!current.think) {
    current.thinkStart = Date.now();
    current.think = el("div", "thinking open active");
    current.think.innerHTML =
      `<div class="think-label"><span class="caret">▶</span>思考中` +
      `<span class="dots"><i></i><i></i><i></i></span></div><div class="think-body"></div>`;
    current.think.querySelector(".think-label").addEventListener("click", () => {
      if (current && current.thinkDone)
        current.think.classList.toggle("open");
    });
    current.el.insertBefore(current.think, current.md);
  }
  current.think.querySelector(".think-body").textContent += text;
  scrollDown();
}
function finalizeThink() {
  if (!current || !current.think || current.thinkDone) return;
  current.thinkDone = true;
  current.think.classList.remove("active");
  const secs = Math.max(0.1, (Date.now() - current.thinkStart) / 1000).toFixed(1);
  current.think.querySelector(".think-label").innerHTML =
    `<span class="caret">▶</span>已深度思考 · ${secs}s`;
  current.think.classList.remove("open");     // 思考完成自动折叠
}

/* ---------------- 文本流 ---------------- */
function streamText(text) {
  hideShimmer();
  if (!current) newAssistant();
  finalizeThink();                             // 开始正文后思考面板折叠
  current.raw += text;
  current.dirty = true;
  scrollDown();
}

/* ---------------- 工具卡片 ---------------- */
const TOOL_KIND = {
  read_file: "read", glob: "read", grep: "read", list_dir: "read",
  web_fetch: "read", web_search: "read", bash_output: "read", bash_kill: "read",
  skill: "read", dispatch_agent: "read", dispatch_agents: "read",
  consult_panel: "read", todo_write: "meta",
  ask_user: "meta", exit_plan: "meta",
  write_file: "write", edit_file: "write", apply_patch: "write",
  notebook_edit: "write", brain_write: "write",
  bash: "bash",
};
const TOOL_ICON = { read: "读", write: "写", bash: "⌘", meta: "✦", mcp: "插" };
function kindOf(name) {
  if (String(name).startsWith("mcp__")) return "mcp";
  return TOOL_KIND[name] || "meta";
}
let resultQueue = [];
function oneLine(s) {
  const t = String(s == null ? "" : s).replace(/\s+/g, " ").trim();
  return t.length > 140 ? t.slice(0, 140) + "…" : t;
}
function addTool(name, summary) {
  closeCurrent();
  hideShimmer();
  hideHero();
  const kind = kindOf(name);
  const card = el("div", "tool-card running");
  card.innerHTML = `<div class="tool-head">` +
    `<span class="tool-icon k-${kind}">${TOOL_ICON[kind]}</span>` +
    `<b>${esc(name)}</b>` +
    `<span class="summary running-sum">${esc(oneLine(summary))}</span>` +
    `<span class="chev">▶</span></div>` +
    `<pre class="tool-result" hidden></pre>`;
  card.querySelector(".tool-head").addEventListener("click", () => {
    card.classList.toggle("open");
    card.querySelector(".tool-result").hidden = !card.classList.contains("open");
  });
  chat.appendChild(card);
  resultQueue.push(card);
  scrollDown();
}
function attachResult(text) {
  let t = resultQueue.shift();
  if (!t) {
    const cards = chat.querySelectorAll(".tool-card");
    t = cards.length ? cards[cards.length - 1] : null;
  }
  if (!t) { sysLine("plain", text); return; }
  t.classList.remove("running");
  const sum = t.querySelector(".running-sum");
  if (sum) sum.classList.remove("shimmer");
  const r = t.querySelector(".tool-result");
  const first = !r.textContent;
  r.textContent += (first ? "" : "\n") + text;
  if (text) {
    r.hidden = false;
    if (r.textContent.split("\n").length <= 8) t.classList.add("open"); // 短结果自动展开
  }
  scrollDown();
}

/* ---------------- 系统行 / todos ---------------- */
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

/* ---------------- 确认卡片 ---------------- */
function renderConfirm(id, title, preview) {
  closeCurrent();
  hideShimmer();
  hideHero();
  const card = el("div", "confirm-card");
  const pv = preview ? preview.split("\n").map(ln => {
    const cls = /^\s*\+/.test(ln) ? "add" : (/^\s*-/.test(ln) ? "del" : "");
    return cls ? `<span class="${cls}">${esc(ln)}</span>` : esc(ln);
  }).join("\n") : "";
  card.innerHTML = `<div class="confirm-title">` +
    `<span class="warn-ico">⚠</span>${esc(title)}</div>` +
    (preview ? `<pre class="preview">${pv}</pre>` : "") +
    `<div class="actions">` +
    `<button class="btn primary" data-v="y">允许</button>` +
    `<button class="btn ghost" data-v="a">本次总是</button>` +
    `<button class="btn danger" data-v="n">拒绝</button></div>`;
  card.addEventListener("click", e => {
    const b = e.target.closest("button");
    if (b) answerAsk(id, b.dataset.v, card);
  });
  chat.appendChild(card);
  scrollDown(true);
}

/* ---------------- ask_user 选项卡 ---------------- */
function renderChoose(id, question, options, multi, allowOther) {
  closeCurrent();
  hideShimmer();
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
    const ok = el("button", "btn ghost", "确定");
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
  if (!b) { hideShimmer(); closeCurrent(); refreshSessions(); }
  else showShimmer("minicode 正在工作");
}
async function refreshStatus() {
  try {
    const s = await getStatus();
    $("#model").textContent = s.model;
    const ratio = s.context_tokens / (s.context_limit || 1);
    $("#ctxFill").style.width = Math.min(100, ratio * 100) + "%";
    $("#ctxFill").style.background =
      ratio > 0.8 ? "var(--err)" : ratio > 0.5 ? "var(--warn)" : "var(--dim)";
    $("#ctxText").textContent =
      `${fmtTok(s.context_tokens)} / ${fmtTok(s.context_limit)} tok`;
    $("#usage").textContent =
      `ctx ${fmtTok(s.context_tokens)} · in ${fmtTok(s.usage.input)} · out ${fmtTok(s.usage.output)}`;
    if (document.activeElement !== $("#modeSel")) $("#modeSel").value = s.mode;
    $("#planBar").hidden = !(s.mode === "plan" && !s.busy);
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
  const connText = $("#connText");
  es.onopen = () => { $("#conn").classList.add("on"); connText.textContent = "已连接"; };
  es.onerror = () => { $("#conn").classList.remove("on"); connText.textContent = "重连中…"; };
  es.onmessage = ev => {
    let e;
    try { e = JSON.parse(ev.data); } catch (err) { return; }
    switch (e.t) {
      case "hello": $("#conn").classList.add("on"); connText.textContent = "已连接"; break;
      case "user": closeCurrent(); hideShimmer(); addUser(e.text); break;
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
      case "workspace": chat.innerHTML = ""; sysLine("info", "工作区已切换：" + e.path);
        refreshWorkspaces(); refreshSessions(); break;
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
function addHistoryThink(wrap, body) {
  const think = el("div", "thinking",
    `<div class="think-label"><span class="caret">▶</span>已深度思考</div>` +
    `<div class="think-body">${esc(body)}</div>`);
  think.querySelector(".think-label").addEventListener("click", () =>
    think.classList.toggle("open"));
  wrap.appendChild(think);
}
function renderHistory(data) {
  for (const m of data.messages || []) {
    if (m.role === "user") {
      addUser(typeof m.content === "string" ? m.content : "(多部分内容)");
    } else if (m.role === "assistant") {
      const wrap = el("div", "msg assistant");
      wrap.appendChild(el("div", "msg-meta",
        `<span class="who-mark">❯</span><span class="who">minicode</span>`));
      if (m.reasoning) addHistoryThink(wrap, m.reasoning);
      if (m.content) {
        const mdEl = el("div", "md", md(m.content));
        wrap.appendChild(mdEl);
        addCopyAction(wrap, () => m.content);
      }
      chat.appendChild(wrap);
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
  input.style.height = Math.min(190, input.scrollHeight) + "px";
}
input.addEventListener("input", () => { autosize(); showCmdHint(); });
input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
$("#btnSend").addEventListener("click", send);
async function send() {
  const text = input.value.trim();
  if (!text || document.body.classList.contains("busy")) return;
  input.value = "";
  autosize();
  hideCmdHint();
  try {
    if (text.startsWith("/")) {                      // 斜杠命令 → 分发器
      const r = await api("/api/command", { line: text });
      if (r.turn) { await api("/api/turn", { prompt: r.turn }); return; }
      if (r.list) { showPickList("选择回退点", r.list, r.hint, "/rewind"); return; }
      sysLine("info", r.output || "（完成）");
      refreshStatus();
    } else if (text.startsWith("!")) {               // ! 直通本地执行
      await api("/api/shell", { command: text.slice(1) });
    } else {
      await api("/api/turn", { prompt: text });
    }
  } catch (e) { /* 错误已由 api() 展示 */ }
}

/* 斜杠命令提示 */
const COMMANDS = ["/mode", "/undo", "/rewind", "/diff", "/limit", "/reasoning",
  "/cost", "/context", "/tools", "/todos", "/brain", "/memory", "/export",
  "/transcript", "/plans", "/agents", "/skills", "/mcp", "/model", "/models",
  "/add-dir", "/verify", "/init", "/commit", "/pr", "/review", "/stats",
  "/doctor", "/output-style", "/extensions", "/help"];
function showCmdHint() {
  const hint = $("#cmdHint");
  const v = input.value.trim();
  if (!v.startsWith("/") || v.includes(" ")) { hint.hidden = true; return; }
  const hits = COMMANDS.filter(c => c.startsWith(v.toLowerCase()));
  hint.hidden = !hits.length;
  hint.innerHTML = hits.map(c => `<b>${c}</b>`).join(" · ");
}
function hideCmdHint() { $("#cmdHint").hidden = true; }

/* ---------------- 侧边栏：会话管理 ---------------- */
async function refreshSessions() {
  try {
    const d = await fetch("/api/sessions",
      { headers: { "X-Minicode-Token": TOKEN } }).then(r => r.json());
    const box = $("#sessions");
    box.innerHTML = "";
    const mkRow = (s, archived) => {
      const row = el("div", "sess-row" + (s.name === activeSession ? " active" : ""));
      const b = el("button", "sess",
        `<span class="sess-title">${esc(s.title)}</span>` +
        `<span class="sess-time mono">${esc(s.time)}</span>`);
      b.title = `${s.title}（${s.time}）`;
      b.addEventListener("click", () => openSession(s.name, archived));
      const acts = el("span", "sess-acts");
      if (!archived) {
        acts.appendChild(actBtn("✎", "重命名", () => renameSession(s, row)));
        acts.appendChild(actBtn("▣", "归档", () => sessionOp("/api/session/archive", { name: s.name })));
      } else {
        acts.appendChild(actBtn("↑", "恢复", () => sessionOp("/api/session/unarchive", { name: s.name })));
      }
      acts.appendChild(actBtn("✕", "删除", () => {
        if (confirm(`永久删除会话「${s.title}」？此操作不可恢复。`))
          sessionOp("/api/session/delete", { name: s.name, archived });
      }, true));
      row.appendChild(b);
      row.appendChild(acts);
      return row;
    };
    if (!(d.sessions || []).length && !(d.archived || []).length) {
      box.innerHTML = `<div class="side-empty">暂无历史会话</div>`;
      return;
    }
    if (d.archived && d.archived.length) {
      box.appendChild(el("div", "sess-group", "已归档"));
      for (const s of d.archived) box.appendChild(mkRow(s, true));
      box.appendChild(el("div", "sess-group", "未归档"));
    }
    // 按日期分组：今天 / 近 7 天 / 更早
    const groups = [[], [], []];
    const dayStart = new Date(); dayStart.setHours(0, 0, 0, 0);
    const weekStart = dayStart.getTime() - 6 * 86400000;
    for (const s of d.sessions || []) {
      const ts = (s.ts || 0) * 1000;
      if (ts >= dayStart.getTime()) groups[0].push(s);
      else if (ts >= weekStart) groups[1].push(s);
      else groups[2].push(s);
    }
    const names = ["今天", "近 7 天", "更早"];
    groups.forEach((items, i) => {
      if (!items.length) return;
      box.appendChild(el("div", "sess-group", names[i]));
      for (const s of items) box.appendChild(mkRow(s, false));
    });
  } catch (e) { /* 服务未就绪时静默 */ }
}
function actBtn(glyph, title, fn, danger) {
  const b = el("button", danger ? "danger" : "", glyph);
  b.title = title;
  b.addEventListener("click", ev => { ev.stopPropagation(); fn(); });
  return b;
}
async function sessionOp(path, body) {
  try {
    await api(path, body);
    refreshSessions();
  } catch (e) { /* 错误已展示 */ }
}
async function renameSession(s) {
  const t = prompt("新的会话标题：", s.title);
  if (!t || t === s.title) return;
  await sessionOp("/api/session/rename", { name: s.name, title: t });
}
async function openSession(name, archived) {
  if (document.body.classList.contains("busy")) return;
  try {
    await api("/api/session/open", { name });
    activeSession = name;
    activeArchived = !!archived;
    document.body.classList.remove("side-open");
    const hist = await fetch("/api/messages",
      { headers: { "X-Minicode-Token": TOKEN } }).then(r => r.json());
    chat.innerHTML = "";
    renderHistory(hist);
    hideHero();
    refreshSessions();
    scrollDown(true);
  } catch (e) { /* 错误已由 api() 展示 */ }
}
let activeSession = null, activeArchived = false;

/* ---------------- 侧边栏：工作区 ---------------- */
function shortenPath(p) {
  const parts = String(p).replace(/\\/g, "/").split("/").filter(Boolean);
  return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : p;
}
async function refreshWorkspaces() {
  try {
    const d = await fetch("/api/workspaces",
      { headers: { "X-Minicode-Token": TOKEN } }).then(r => r.json());
    $("#wsPath").textContent = shortenPath(d.current || "");
    $("#wsPath").title = d.current || "";
    const list = $("#wsList");
    list.innerHTML = "";
    for (const w of d.list || []) {
      const item = el("div", "ws-item" + (w === d.current ? " current" : ""));
      item.innerHTML = `<span class="p" title="${esc(w)}">${esc(w)}</span>` +
        (w === d.current ? `<span class="tag">当前</span>` : "") +
        (w === d.current ? "" : `<button type="button">切换</button>` +
          `<button type="button" class="danger">移除</button>`);
      if (w !== d.current) {
        const [btnSw, btnRm] = item.querySelectorAll("button");
        btnSw.addEventListener("click", () =>
          api("/api/workspace/switch", { path: w }).then(refreshWorkspaces));
        btnRm.addEventListener("click", () =>
          api("/api/workspace/remove", { path: w }).then(refreshWorkspaces));
      }
      list.appendChild(item);
    }
  } catch (e) { /* 静默 */ }
}

/* ---------------- 设置弹窗（模型 API） ---------------- */
async function openSettings() {
  try {
    const c = await fetch("/api/config",
      { headers: { "X-Minicode-Token": TOKEN } }).then(r => r.json());
    $("#cfgProvider").value = c.provider;
    $("#cfgBase").value = c.base_url || "";
    $("#cfgKey").value = "";
    $("#cfgKey").placeholder = c.api_key_set ? `已配置（****${c.api_key_tail}）— 留空不修改` : "未配置";
    $("#cfgModel").value = c.model || "";
    $("#cfgMax").value = c.max_tokens || "";
    $("#cfgCtx").value = c.context_limit || "";
    $("#cfgEffort").value = c.reasoning_effort || "";
    $("#probeOut").textContent = "";
    $("#settingsModal").hidden = false;
  } catch (e) { /* 静默 */ }
}
$("#btnSettings").addEventListener("click", openSettings);
$("#model").addEventListener("click", openSettings);

/* ---------------- 扩展面板：技能 / 插件 / 子智能体 / 自定义命令 / MCP ---------------- */
$("#btnExt").addEventListener("click", openExtensions);

async function openExtensions() {
  $("#extModal").hidden = false;
  $("#extBody").innerHTML = `<div class="side-empty">加载中…</div>`;
  try {
    const d = await fetch("/api/extensions",
      { headers: { "X-Minicode-Token": TOKEN } }).then(r => r.json());
    const box = $("#extBody");
    box.innerHTML = "";
    const section = (title, rows, empty, opts = {}) => {
      const sec = el("div", "ext-sec");
      const head = el("div", "ext-sec-head");
      head.appendChild(el("div", "side-label mono", title));
      if (opts.addType) {
        const add = el("button", "btn ghost small", "+ 新建");
        add.addEventListener("click", () => openExtForm(opts.addType));
        head.appendChild(add);
      }
      if (opts.reload) {
        const rl = el("button", "btn ghost small", "重载");
        rl.addEventListener("click", async () => {
          await api("/api/ext/reload");
          openExtensions();
        });
        head.appendChild(rl);
      }
      sec.appendChild(head);
      if (!rows.length) { sec.appendChild(el("div", "side-empty", empty)); box.appendChild(sec); return; }
      for (const r of rows) {
        const line = el("div", "ext-item");
        line.innerHTML = `<span class="ext-name mono">${esc(r.name)}</span>` +
          (r.source ? `<span class="ext-src">${esc(r.source)}</span>` : "") +
          `<span class="ext-desc">${esc(r.desc || r.status || "")}</span>` +
          (opts.use ? `<button class="btn ghost small" data-use="${esc(r.name)}">使用</button>` : "") +
          (opts.run ? `<button class="btn ghost small" data-run="${esc(r.name)}">运行</button>` : "") +
          (r.deletable ? `<button class="btn ghost small danger" data-del="${esc(r.name)}" title="删除">✕</button>` : "");
        if (opts.use) {
          line.querySelector("[data-use]").addEventListener("click", () => {
            $("#extModal").hidden = true;
            api("/api/turn", { prompt: `请加载 ${r.name} 技能并按其流程协助我处理任务。` });
          });
        }
        if (opts.run) {
          line.querySelector("[data-run]").addEventListener("click", () => {
            $("#extModal").hidden = true;
            input.value = "/" + r.name;
            autosize();
            send();
          });
        }
        if (r.deletable) {
          line.querySelector("[data-del]").addEventListener("click", () => {
            if (!confirm(`删除「${r.name}」？此操作不可恢复。`)) return;
            api("/api/ext/" + opts.delKind + "/delete",
                { name: r.name, source: r.source })
              .then(() => openExtensions())
              .catch(() => {});
          });
        }
        sec.appendChild(line);
      }
      box.appendChild(sec);
    };
    section("技能（skill 工具按需加载）",
      d.skills.map(s => ({ name: s.name, desc: s.desc, source: s.source,
                           deletable: s.source !== "内置" })),
      "没有可用技能——点击右上角 + 新建，或放置 .minicode/skills/<名字>/SKILL.md",
      { use: true, addType: "skill", delKind: "skill" });
    section("插件工具（.minicode/tools/*.py）",
      d.plugins.map(n => ({ name: n, desc: "本地 Python 插件工具" })),
      "没有插件——点击右上角 + 新建",
      { addType: "plugin", delKind: "plugin", reload: true });
    section("自定义子智能体（.minicode/agents/*.md）",
      d.agents.map(a => ({ name: a.name, desc: a.desc + (a.tools ? ` · tools: ${a.tools}` : "") + (a.model ? ` · model: ${a.model}` : "") })),
      "没有自定义子智能体",
      { addType: "agent", delKind: "agent" });
    section("自定义命令（.minicode/commands/*.md）",
      d.commands.map(c => ({ name: c.name, desc: c.desc })),
      "没有自定义命令",
      { run: true, addType: "command", delKind: "command" });
    section("MCP 服务器（.minicode.json 的 mcpServers）",
      d.mcp.map(m => ({ name: m.server, desc: `${m.status} · ${m.tools} tools${m.error ? " · " + m.error : ""}` })),
      "未配置 MCP 服务器");
  } catch (e) {
    $("#extBody").innerHTML = `<div class="side-empty">加载失败</div>`;
  }
}
/* 弹窗：点击遮罩 / ✕ / Esc 关闭，并返还焦点到输入框 */
document.querySelectorAll(".modal").forEach(m => {
  m.addEventListener("click", e => {
    if (e.target === m || e.target.closest("[data-close]")) {
      m.hidden = true;
      input.focus();
    }
  });
});
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  let closed = false;
  document.querySelectorAll(".modal:not([hidden])").forEach(m => {
    m.hidden = true;
    closed = true;
  });
  if (document.body.classList.contains("side-open")) {
    document.body.classList.remove("side-open");
    closed = true;
  }
  hideCmdHint();
  if (closed) input.focus();
});
/* ---------------- 服务端目录浏览器 ---------------- */
let dirSelectCb = null;
async function browseDir(startPath, onPick) {
  dirSelectCb = onPick;
  $("#dirModal").hidden = false;
  await loadDir(startPath || "");
}
async function loadDir(path) {
  const list = $("#dirList");
  list.innerHTML = `<div class="side-empty">加载中…</div>`;
  try {
    const d = await fetch("/api/fs/list?path=" + encodeURIComponent(path),
      { headers: { "X-Minicode-Token": TOKEN } }).then(r => r.json());
    if (d.error) { list.innerHTML = `<div class="side-empty">${esc(d.error)}</div>`; return; }
    $("#dirPath").value = d.path || $("#dirPath").value;
    $("#dirUp").disabled = !d.parent;
    $("#dirUp").onclick = () => d.parent && loadDir(d.parent);
    list.innerHTML = "";
    if (!d.dirs.length) list.innerHTML = `<div class="side-empty">此目录下没有子目录</div>`;
    for (const name of d.dirs) {
      const row = el("button", "dir-row",
        `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>` +
        `<span>${esc(name)}</span>`);
      const target = (d.path && !d.isDrives ? d.path.replace(/[\/]+$/, "") + "/" : "") + name;
      row.addEventListener("click", () => loadDir(target));
      list.appendChild(row);
    }
  } catch (e) {
    list.innerHTML = `<div class="side-empty">加载失败</div>`;
  }
}
$("#dirGo").addEventListener("click", () => loadDir($("#dirPath").value.trim()));
$("#dirPath").addEventListener("keydown", e => {
  if (e.key === "Enter") loadDir($("#dirPath").value.trim());
});
$("#dirSelect").addEventListener("click", () => {
  const p = $("#dirPath").value.trim();
  if (dirSelectCb && p) dirSelectCb(p);
  $("#dirModal").hidden = true;
});

/* ---------------- 扩展新建表单 ---------------- */
const EXT_FORMS = {
  skill: {
    title: "新建技能",
    fields: [
      { k: "name", label: "名称（字母/数字/连字符）", type: "text" },
      { k: "desc", label: "一句话描述（模型据此决定何时使用）", type: "text" },
      { k: "content", label: "技能内容（Markdown 工作流步骤）", type: "textarea" },
    ],
    endpoint: "/api/ext/skill/create",
  },
  command: {
    title: "新建自定义命令",
    fields: [
      { k: "name", label: "命令名（用 /名称 触发）", type: "text" },
      { k: "desc", label: "描述", type: "text" },
      { k: "content", label: "命令内容（$ARGUMENTS 为参数占位）", type: "textarea" },
    ],
    endpoint: "/api/ext/command/create",
  },
  agent: {
    title: "新建自定义子智能体",
    fields: [
      { k: "name", label: "名称（dispatch_agent 的 subagent_type）", type: "text" },
      { k: "desc", label: "描述", type: "text" },
      { k: "tools", label: "可用工具（逗号分隔，如 read_file,grep；留空=全部只读）", type: "text" },
      { k: "prompt", label: "系统提示词", type: "textarea" },
    ],
    endpoint: "/api/ext/agent/create",
  },
  plugin: {
    title: "新建插件工具",
    fields: [
      { k: "name", label: "工具名（字母/数字/连字符）", type: "text" },
      { k: "desc", label: "描述", type: "text" },
    ],
    endpoint: "/api/ext/plugin/create",
  },
};
function openExtForm(type) {
  const spec = EXT_FORMS[type];
  if (!spec) return;
  $("#formTitle").textContent = spec.title;
  const box = $("#formFields");
  box.innerHTML = "";
  for (const f of spec.fields) {
    const label = el("label", "", esc(f.label));
    let field;
    if (f.type === "textarea") {
      field = document.createElement("textarea");
      field.rows = 6;
    } else {
      field = document.createElement("input");
      field.type = "text";
      field.className = "mono";
    }
    field.dataset.k = f.k;
    label.appendChild(field);
    box.appendChild(label);
  }
  $("#formSubmit").onclick = async () => {
    const body = {};
    for (const f of spec.fields) {
      const node = box.querySelector(`[data-k="${f.k}"]`);
      body[f.k] = node.value.trim();
    }
    try {
      await api(spec.endpoint, body);
      $("#formModal").hidden = true;
      openExtensions();
    } catch (e) { /* 错误已展示 */ }
  };
  $("#formModal").hidden = false;
}

$("#btnBrowse").addEventListener("click", () => {
  browseDir($("#wsPath").title || "", async (picked) => {
    try {
      await api("/api/workspace/add", { path: picked });
      await api("/api/workspace/switch", { path: picked });
      $("#wsModal").hidden = true;
      refreshWorkspaces();
    } catch (e) { /* 错误已展示 */ }
  });
});

/* API Key 显示 / 隐藏 */
$("#cfgKeyToggle").addEventListener("click", () => {
  const key = $("#cfgKey");
  const show = key.type === "password";
  key.type = show ? "text" : "password";
  $("#eyeOpen").hidden = show;
  $("#eyeClosed").hidden = !show;
});
$("#btnSaveCfg").addEventListener("click", async () => {
  const body = {
    provider: $("#cfgProvider").value,
    base_url: $("#cfgBase").value.trim(),
    model: $("#cfgModel").value.trim(),
    max_tokens: Number($("#cfgMax").value) || undefined,
    context_limit: Number($("#cfgCtx").value) || undefined,
    reasoning_effort: $("#cfgEffort").value,
    save: $("#cfgSave").checked,
  };
  const key = $("#cfgKey").value.trim();
  if (key) body.api_key = key;
  try {
    await api("/api/config", body);
    $("#settingsModal").hidden = true;
    refreshStatus();
  } catch (e) { /* 错误已展示 */ }
});
$("#btnProbe").addEventListener("click", async () => {
  $("#probeOut").textContent = "探测中…";
  try {
    const d = await fetch("/api/probe",
      { headers: { "X-Minicode-Token": TOKEN } }).then(r => r.json());
    if (d.error) { $("#probeOut").textContent = d.error; return; }
    $("#probeOut").textContent = d.rows.map(r => `${r[1] ? "✓" : "✗"} ${r[0]}`).join(" · ");
  } catch (e) {
    $("#probeOut").textContent = "探测失败";
  }
});

/* ---------------- 工作区弹窗 ---------------- */
$("#wsCurrent").addEventListener("click", () => {
  $("#wsModal").hidden = false;
  refreshWorkspaces();
});
/* ---------------- 通用列表弹窗（回退等） ---------------- */
function showPickList(title, items, hint, prefix) {
  $("#listTitle").textContent = title;
  $("#listHint").textContent = hint || "";
  const box = $("#pickList");
  box.innerHTML = "";
  items.forEach((it, i) => {
    const b = el("button", "", esc(it));
    b.addEventListener("click", async () => {
      $("#listModal").hidden = true;
      try {
        const r = await api("/api/command", { line: `${prefix} ${i + 1}` });
        sysLine("info", r.output || "（完成）");
      } catch (e) { /* 已展示 */ }
    });
    box.appendChild(b);
  });
  $("#listModal").hidden = false;
}

/* ---------------- 计划批准条 ---------------- */
$("#btnApprovePlan").addEventListener("click", async () => {
  try {
    await api("/api/command", { line: "/mode accept-edits" });
    await api("/api/turn",
      { prompt: "Plan approved. Start implementing it now, following the plan exactly." });
  } catch (e) { /* 已展示 */ }
});

/* 头部 / 侧边栏按钮 */
$("#modeSel").addEventListener("change", e => api("/api/mode", { mode: e.target.value }));
$("#btnCompact").addEventListener("click", () => api("/api/compact"));
$("#btnNew").addEventListener("click", () => {
  if (confirm("开始新会话？（当前上下文将被清空，历史文件改动不受影响）")) api("/api/clear");
});
$("#btnMenu").addEventListener("click", () => document.body.classList.toggle("side-open"));
$("#sideMask").addEventListener("click", () => document.body.classList.remove("side-open"));

/* 欢迎页建议 chips */
chat.addEventListener("click", e => {
  const chip = e.target.closest(".chip");
  if (chip && chip.dataset.prompt) {
    input.value = chip.dataset.prompt;
    autosize();
    send();
  }
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
  refreshSessions();
  refreshWorkspaces();
  connectEvents();
  refreshStatus();
  setInterval(refreshStatus, 2500);
  input.focus();
})();
