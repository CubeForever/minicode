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

/* ---------------- 流光占位（思考中 / 执行中） ---------------- */
let shimmerEl = null;
function showShimmer(label) {
  hideHero();
  if (shimmerEl) { shimmerEl.querySelector(".shimmer").textContent = label; return; }
  shimmerEl = el("div", "shimmer-row", `<span class="shimmer">${esc(label)}</span>`);
  chat.appendChild(shimmerEl);
  scrollDown();
}
function hideShimmer() {
  if (shimmerEl) { shimmerEl.remove(); shimmerEl = null; }
}

/* ---------------- 助手消息（思考面板 + markdown 正文） ---------------- */
let current = null;   // {el, md, think, thinkStart, thinkDone, raw, timer}
function newAssistant() {
  const wrap = el("div", "msg assistant");
  wrap.appendChild(el("div", "assistant-head",
    `<span class="assistant-logo"><svg viewBox="0 0 24 24" fill="currentColor"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg></span>`));
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

/* ---------------- 深度思考（DeepSeek 式 + 流光边框签名） ---------------- */
function streamReason(text) {
  hideShimmer();
  if (!current) newAssistant();
  if (!current.think) {
    current.thinkStart = Date.now();
    current.think = el("div", "thinking open active");
    current.think.innerHTML =
      `<div class="think-label"><span class="caret">▶</span>` +
      `<span class="shimmer">深度思考中…</span></div><div class="think-body"></div>`;
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
  current.think.classList.remove("active");    // 流光边框只在思考时点亮
  const secs = Math.max(0.1, (Date.now() - current.thinkStart) / 1000).toFixed(1);
  current.think.querySelector(".think-label").innerHTML =
    `<span class="caret">▶</span>已深度思考（用时 ${secs} 秒）`;
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
  skill: "read", dispatch_agent: "read", consult_panel: "read", todo_write: "meta",
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
  if (!b) { hideShimmer(); closeCurrent(); }
  else showShimmer("思考中…");
}
async function refreshStatus() {
  try {
    const s = await getStatus();
    $("#model").textContent = s.model;
    const ratio = s.context_tokens / (s.context_limit || 1);
    $("#ctxFill").style.width = Math.min(100, ratio * 100) + "%";
    $("#ctxFill").style.background =
      ratio > 0.8 ? "var(--red)" : ratio > 0.5 ? "var(--amber)" : "var(--green)";
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
function addHistoryThink(wrap, body, secs) {
  const think = el("div", "thinking",
    `<div class="think-label"><span class="caret">▶</span>已深度思考` +
    (secs ? `（用时 ${secs} 秒）` : "") + `</div><div class="think-body">${esc(body)}</div>`);
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
      wrap.appendChild(el("div", "assistant-head",
        `<span class="assistant-logo"><svg viewBox="0 0 24 24" fill="currentColor"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg></span>`));
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
  connectEvents();
  refreshStatus();
  setInterval(refreshStatus, 2500);
  input.focus();
})();
