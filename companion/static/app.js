const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
}

const initData = tg?.initData || "";
const root = document.querySelector("#app");
const state = {
  page: "home",
  profile: null,
  categories: [],
  overview: { deliveries: [], events: [], stats: { delivered: 0, pending: 0, failed: 0 } },
  saving: false,
};

const pages = [["home", "首页"], ["rules", "接收"], ["records", "记录"], ["account", "我的"]];
const modeLabels = { all: "全部接收", subscription: "按订阅关键词", category: "按分类", either: "订阅或分类" };
const statusLabels = { waiting: "等待中", running: "派送中", retry: "重试中", delivered: "已完成", cancelled: "已取消", failed: "失败" };

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  "\"": "&quot;",
  "'": "&#39;",
}[char]));

const time = (stamp) => stamp ? new Date(stamp * 1000).toLocaleString("zh-CN", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
}) : "未记录";

async function api(path, method = "GET", body) {
  const response = await fetch(path, {
    method,
    headers: { Authorization: `tma ${initData}`, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

function toast(text) {
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = text;
  document.body.append(node);
  setTimeout(() => node.remove(), 2400);
}

async function load() {
  const [me, overview] = await Promise.all([
    api("/api/mini/me"),
    api("/api/mini/overview").catch(() => ({ deliveries: [], events: [], stats: {} })),
  ]);
  state.profile = me.user;
  state.categories = me.categories || [];
  state.overview = { deliveries: overview.deliveries || [], events: overview.events || [], stats: overview.stats || {} };
}

function setPage(page) {
  state.page = page;
  render();
}

function ruleValues() {
  return {
    enabled: document.querySelector("#enabled")?.checked ?? true,
    hierarchy: document.querySelector("#hierarchy")?.checked ?? false,
    mode: document.querySelector("input[name=mode]:checked")?.value || "all",
    categories: [...document.querySelectorAll("input[name=category]:checked")].map((item) => item.value),
    include_terms: document.querySelector("#include")?.value.trim() || "",
    exclude_terms: document.querySelector("#exclude")?.value.trim() || "",
  };
}

function accountValues() {
  return {
    target_cid: document.querySelector("#cid")?.value.trim() || "0",
    target_name: document.querySelector("#target")?.value.trim() || "根目录",
  };
}

async function savePreferences() {
  state.saving = true;
  render();
  try {
    const data = await api("/api/mini/preferences", "PUT", ruleValues());
    state.profile = data.user;
    toast("接收设置已保存");
  } catch (error) {
    toast(error.message);
  } finally {
    state.saving = false;
    render();
  }
}

async function bindAccount() {
  const cookie = document.querySelector("#cookie")?.value.trim();
  if (!cookie) {
    toast("请先填写 115 Cookie");
    return;
  }
  state.saving = true;
  render();
  try {
    const data = await api("/api/mini/account", "PUT", { ...accountValues(), cookie });
    state.profile = data.user;
    toast(`115 已绑定：${data.account?.name || data.account?.uid || "账号"}`);
  } catch (error) {
    toast(error.message);
  } finally {
    state.saving = false;
    render();
  }
}

function shell(content) {
  const user = state.profile || {};
  return `<main class="member-shell">
    <header class="brand-row">
      <a class="brand" href="#" data-page="home">蟑螂快跑<span>COCKROACH RUNNER</span></a>
      <span class="muted">${esc(user.name || user.username || user.tg_id || "")}</span>
    </header>
    ${content}
    <nav class="bottom-nav">
      ${pages.map(([id, label]) => `<button class="${state.page === id ? "active" : ""}" data-page="${id}">${label}</button>`).join("")}
    </nav>
  </main>`;
}

function home() {
  const user = state.profile;
  const stats = state.overview.stats || {};
  const recent = state.overview.deliveries.slice(0, 3);
  return shell(`<section class="panel hero-panel">
    <span class="badge green">Telegram 已连接</span>
    <h1>晚上好，${esc(user.name || user.username || "朋友")}</h1>
    <p>这里已经接入你的自制小程序风格，用来管理 115 账号、自动接收规则和派送记录。</p>
  </section>
  <section class="stat-grid">
    <div class="stat"><span>已完成派送</span><strong>${stats.delivered || 0}</strong></div>
    <div class="stat"><span>处理中</span><strong>${stats.pending || 0}</strong></div>
    <div class="stat"><span>失败/取消</span><strong>${stats.failed || 0}</strong></div>
    <div class="stat"><span>115 账号</span><strong>${user.account_bound ? "已绑定" : "未绑定"}</strong></div>
  </section>
  <section class="panel">
    <div class="section-head"><h2>快捷操作</h2></div>
    <div class="quick-grid">
      <button data-page="account">绑定 115</button>
      <button data-page="rules">接收规则</button>
      <button data-page="records">派送记录</button>
    </div>
  </section>
  <section class="panel">
    <div class="section-head"><h2>最近派送</h2><button class="text-button" data-refresh>刷新</button></div>
    ${recent.length ? recent.map(deliveryRow).join("") : empty("还没有派送任务", "监听目录发现新资源并匹配你的规则后，会显示在这里。")}
  </section>`);
}

function rules() {
  const user = state.profile;
  return shell(`<section class="panel">
    <div class="section-head"><h2>接收模式</h2><span class="badge">${modeLabels[user.mode] || "全部接收"}</span></div>
    <p class="muted">这些规则会用于判断监听目录里的新资源是否自动派送给你。</p>
    <div class="option-grid">
      ${Object.entries(modeLabels).map(([value, label]) => `<label class="option ${user.mode === value ? "selected" : ""}">
        <input type="radio" name="mode" value="${value}" ${user.mode === value ? "checked" : ""}>
        <span>${label}</span>
      </label>`).join("")}
    </div>
  </section>
  <section class="panel">
    <h2>分类与关键词</h2>
    <div class="chips">
      ${state.categories.map((item) => `<label class="chip ${(user.categories || []).includes(item) ? "selected" : ""}">
        <input type="checkbox" name="category" value="${esc(item)}" ${(user.categories || []).includes(item) ? "checked" : ""}>
        ${esc(item)}
      </label>`).join("")}
    </div>
    <label class="field"><span>订阅关键词，逗号分隔</span><input id="include" value="${esc(user.include_terms)}" placeholder="例如：三体, 沙丘"></label>
    <label class="field"><span>排除关键词，逗号分隔</span><input id="exclude" value="${esc(user.exclude_terms)}" placeholder="例如：预告, 花絮"></label>
    <label class="switch-row"><span><strong>启用自动派送</strong><small>关闭后会保留账号和规则，但不再接收新任务。</small></span><input id="enabled" type="checkbox" ${user.enabled ? "checked" : ""}></label>
    <label class="switch-row"><span><strong>按分类建立层级目录</strong></span><input id="hierarchy" type="checkbox" ${user.hierarchy ? "checked" : ""}></label>
    <button class="primary full" data-save ${state.saving ? "disabled" : ""}>${state.saving ? "正在保存..." : "保存接收设置"}</button>
  </section>`);
}

function records() {
  const deliveries = state.overview.deliveries;
  const events = state.overview.events;
  return shell(`<section class="panel">
    <div class="section-head"><h2>派送任务</h2><button class="text-button" data-refresh>刷新</button></div>
    ${deliveries.length ? deliveries.map(deliveryRow).join("") : empty("暂无派送记录", "你的自动派送、重试和完成状态会同步到这里。")}
  </section>
  <section class="panel">
    <h2>最近事件</h2>
    ${events.length ? events.map((item) => `<div class="event-row"><div><strong>${esc(item.kind)}</strong><small>${esc(item.message)}</small></div><span>${time(item.created)}</span></div>`).join("") : empty("暂无事件", "服务运行事件会在这里显示。")}
  </section>`);
}

function account() {
  const user = state.profile;
  return shell(`<section class="panel">
    <div class="identity">
      <div class="avatar">${esc(String(user.name || user.username || "蟑").slice(0, 1))}</div>
      <div>
        <h2>${esc(user.name || user.username || user.tg_id)}</h2>
        <p class="muted">${user.account_bound ? `115 UID ${esc(user.uid)}` : "115 尚未绑定"}</p>
      </div>
    </div>
  </section>
  <section class="panel">
    <div class="section-head"><h2>115 账号</h2><span class="badge ${user.account_bound ? "green" : "gray"}">${user.account_bound ? "已加密保存" : "未填写"}</span></div>
    <p class="muted">Cookie 只保存在服务器端，用于把匹配到的资源派送到你的 115 目录。</p>
    <label class="field"><span>115 Cookie</span><textarea id="cookie" rows="4" placeholder="UID=...; CID=...; SEID=...; KID=..."></textarea></label>
    <label class="field"><span>目标目录 CID</span><input id="cid" value="${esc(user.target_cid || "0")}"></label>
    <label class="field"><span>目标目录名称</span><input id="target" value="${esc(user.target_name || "根目录")}"></label>
    <button class="primary full" data-bind ${state.saving ? "disabled" : ""}>${state.saving ? "正在验证..." : "验证并绑定 115"}</button>
  </section>`);
}

function deliveryRow(item) {
  return `<div class="task-row">
    <div>
      <strong>${esc(item.name || "未命名资源")}</strong>
      <small>${esc(item.category || "其他")} · ${time(item.created)}${item.attempts ? ` · 尝试 ${item.attempts} 次` : ""}</small>
      ${item.error ? `<small class="danger-text">${esc(item.error)}</small>` : ""}
    </div>
    <span class="badge ${item.status === "delivered" ? "green" : item.status === "cancelled" || item.status === "failed" ? "red" : "blue"}">${statusLabels[item.status] || item.status}</span>
  </div>`;
}

function empty(title, description) {
  return `<div class="empty"><h3>${title}</h3><p>${description}</p></div>`;
}

function bind() {
  root.querySelectorAll("[data-page]").forEach((node) => node.addEventListener("click", (event) => {
    event.preventDefault();
    setPage(node.dataset.page);
  }));
  root.querySelector("[data-save]")?.addEventListener("click", savePreferences);
  root.querySelector("[data-bind]")?.addEventListener("click", bindAccount);
  root.querySelectorAll("[data-refresh]").forEach((node) => node.addEventListener("click", async () => {
    try {
      await load();
      render();
      toast("已刷新");
    } catch (error) {
      toast(error.message);
    }
  }));
  root.querySelectorAll(".option input").forEach((input) => input.addEventListener("change", () => {
    root.querySelectorAll(".option").forEach((item) => item.classList.toggle("selected", item.querySelector("input").checked));
  }));
  root.querySelectorAll(".chip input").forEach((input) => input.addEventListener("change", () => {
    input.closest(".chip").classList.toggle("selected", input.checked);
  }));
}

function render() {
  if (!state.profile) return;
  root.innerHTML = ({ home, rules, records, account }[state.page] || home)();
  bind();
}

async function start() {
  if (!initData) {
    root.innerHTML = `<main class="member-shell"><section class="panel error"><h2>请从 Telegram Bot 打开</h2><p>这个小程序需要 Telegram Mini App 登录信息。请在机器人菜单或按钮里打开。</p></section></main>`;
    return;
  }
  try {
    await load();
    render();
  } catch (error) {
    root.innerHTML = `<main class="member-shell"><section class="panel error"><h2>打开失败</h2><p>${esc(error.message)}</p></section></main>`;
  }
}

start();
