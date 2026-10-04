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
  qrApps: {},
  overview: { deliveries: [], events: [], stats: { delivered: 0, pending: 0, failed: 0 } },
  saving: false,
  qr: null,
  folder: null,
  accountDraft: null,
};
let qrTimer = null;

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

function membership(expires) {
  const seconds = Number(expires || 0) - Math.floor(Date.now() / 1000);
  if (seconds <= 0) return {active: false, text: "会员已到期", days: 0};
  const days = Math.ceil(seconds / 86400);
  return {active: true, text: `会员剩余 ${days} 天`, days};
}

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
  state.qrApps = me.qr_apps || { alipaymini: "115生活（支付宝小程序）" };
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
    toast(`CK 已验证并保存：${data.account?.name || data.account?.uid || "115账号"}`);
  } catch (error) {
    toast(error.message);
  } finally {
    state.saving = false;
    render();
  }
}

async function loadMiniFolders() {
  const current = state.folder.stack.at(-1);
  state.folder.loading = true;
  render();
  try {
    const data = await api(`/api/mini/browse?cid=${encodeURIComponent(current.id)}`);
    state.folder.items = data.items || [];
  } catch (error) {
    toast(error.message);
    state.folder = null;
  } finally {
    if (state.folder) state.folder.loading = false;
    render();
  }
}

async function openMiniFolderPicker() {
  state.accountDraft = accountValues();
  const cookie = document.querySelector("#cookie")?.value.trim() || "";
  if (!state.profile.account_bound && !cookie) {
    toast("请先填写 115 CK，再选择接收文件夹");
    return;
  }
  if (cookie) {
    state.saving = true;
    try {
      const data = await api("/api/mini/account", "PUT", {...state.accountDraft, cookie});
      state.profile = data.user;
      toast("CK 已验证并安全保存，正在读取网盘目录");
    } catch (error) {
      toast(error.message);
      state.saving = false;
      render();
      return;
    }
    state.saving = false;
  }
  state.folder = {stack: [{id: "0", name: "根目录"}], items: [], loading: false};
  loadMiniFolders();
}

function enterMiniFolder(index) {
  const item = state.folder?.items?.[index];
  if (!item) return;
  state.folder.stack.push({id: item.cid, name: item.name});
  loadMiniFolders();
}

function chooseMiniFolder() {
  const selected = state.folder.stack.at(-1);
  state.accountDraft = {target_cid: selected.id, target_name: selected.name};
  state.folder = null;
  render();
  toast("接收文件夹已选择，保存或扫码绑定后生效");
}

async function startQrLogin() {
  clearTimeout(qrTimer);
  state.saving = true;
  try {
    const target = accountValues();
    const app = document.querySelector("#qrApp")?.value || "alipaymini";
    const data = await api("/api/mini/qr/start", "POST", { app });
    state.qr = { ...data, ...target, status: "waiting" };
    render();
    pollQrLogin();
  } catch (error) {
    toast(error.message);
  } finally {
    state.saving = false;
    render();
  }
}

async function pollQrLogin() {
  if (!state.qr?.session) return;
  try {
    const data = await api("/api/mini/qr/status", "POST", {
      session: state.qr.session,
      target_cid: state.qr.target_cid,
      target_name: state.qr.target_name,
    });
    if (data.status === "confirmed") {
      state.profile = data.user;
      state.qr = null;
      render();
      toast(`扫码绑定成功：${data.account?.name || data.account?.uid || "115账号"}`);
      return;
    }
    state.qr.status = data.status;
    render();
    if (data.status !== "expired") qrTimer = setTimeout(pollQrLogin, 2000);
  } catch (error) {
    state.qr.status = "error";
    state.qr.error = error.message;
    render();
  }
}

function shell(content) {
  const user = state.profile || {};
  return `<main class="member-shell">
    <header class="brand-row">
      <a class="brand brand-with-icon" href="#" data-page="home"><video src="/brand-icon.mp4" autoplay muted loop playsinline aria-label="蟑影递送图标"></video><span class="brand-copy">蟑影递送<small>SHADOW DELIVERY</small></span></a>
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
  const member = membership(user.membership_expires);
  const stats = state.overview.stats || {};
  const recent = state.overview.deliveries.slice(0, 3);
  return shell(`<section class="panel hero-panel">
    <span class="badge ${member.active ? "green" : "red"}">${esc(member.text)}</span>
    <h1>晚上好，${esc(user.name || user.username || "朋友")}</h1>
    <p>欢迎来到蟑影，祝你有个美好观影体验。</p>
  </section>
  <section class="panel membership-panel ${member.active ? "" : "expired-membership"}">
    <div><small>会员有效期</small><strong>${esc(member.text)}</strong></div>
    <span>${esc(time(user.membership_expires))}</span>
    ${member.active ? "" : "<p>会员到期后自动派送已暂停，请使用新的绑定码续费。</p>"}
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
  const target = state.accountDraft || {target_cid: user.target_cid || "0", target_name: user.target_name || "根目录"};
  const qrLabels = { waiting: "等待扫码", scanned: "已扫码，请在 115 确认", expired: "二维码已过期", error: "查询失败" };
  const selectedQrApp = state.qr?.app || "alipaymini";
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
    <label class="field"><span>目标目录 CID</span><input id="cid" value="${esc(target.target_cid)}"></label>
    <label class="field"><span>目标目录名称</span><input id="target" value="${esc(target.target_name)}"></label>
    <p class="folder-tip">首次选择接收文件夹前，请先填写 115 CK。点击“从 115 网盘选择”时，系统会先验证并安全保存 CK，再为你打开网盘目录。</p>
    <button class="secondary full compact" data-folder-open>从 115 网盘选择接收文件夹</button>
    ${state.folder ? `<div class="mini-folder-picker">
      <div class="mini-folder-head"><button class="text-button" data-folder-back ${state.folder.stack.length <= 1 ? "disabled" : ""}>返回</button><strong>${esc(state.folder.stack.map(item => item.name).join(" / "))}</strong><button class="text-button" data-folder-close>关闭</button></div>
      ${state.folder.loading ? '<div class="folder-loading">正在读取...</div>' : state.folder.items.length ? state.folder.items.map((item, index) => `<button class="mini-folder-entry" data-folder-enter="${index}">📁 ${esc(item.name)}</button>`).join("") : '<div class="folder-loading">没有子文件夹</div>'}
      <button class="primary full" data-folder-select>选择当前文件夹</button>
    </div>` : ""}
    <button class="secondary full compact" data-bind ${state.saving ? "disabled" : ""}>${state.saving ? "正在验证..." : "验证并保存 CK"}</button>
    <div class="account-divider"><span>或使用扫码获取 CK</span></div>
    <label class="field"><span>扫码登录端</span><select id="qrApp" ${state.qr && !["expired","error"].includes(state.qr.status) ? "disabled" : ""}>${Object.entries(state.qrApps).map(([value, label]) => `<option value="${esc(value)}" ${selectedQrApp === value ? "selected" : ""}>${esc(label)}</option>`).join("")}</select></label>
    <button class="primary full" data-qr-start ${state.saving ? "disabled" : ""}>${state.saving ? "正在生成..." : "扫码获取 CK"}</button>
    ${state.qr ? `<div class="qr-card">
      <img data-qr-image src="${esc(state.qr.image_url || state.qr.image)}" data-fallback="${esc(state.qr.image || "")}" alt="115 登录二维码" decoding="sync">
      <strong>${esc(qrLabels[state.qr.status] || state.qr.status)}</strong><span class="qr-app-name">${esc(state.qr.app_name || state.qrApps[state.qr.app] || state.qr.app)}</span>
      <small>${state.qr.error ? esc(state.qr.error) : "请使用 115 App 扫描，并在手机上确认登录。二维码约 5 分钟有效。"}</small>
      ${["expired","error"].includes(state.qr.status) ? '<button class="text-button" data-qr-start>重新生成</button>' : ''}
    </div>` : ''}
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
  root.querySelectorAll("[data-qr-start]").forEach((node) => node.addEventListener("click", startQrLogin));
  const qrImage = root.querySelector("[data-qr-image]");
  qrImage?.addEventListener("error", () => {
    const fallback = qrImage.dataset.fallback;
    if (fallback && !qrImage.dataset.fallbackUsed) {
      qrImage.dataset.fallbackUsed = "1";
      qrImage.src = fallback;
      return;
    }
    qrImage.hidden = true;
    const hint = qrImage.closest(".qr-card")?.querySelector("small");
    if (hint) hint.textContent = "二维码图片加载失败，请点击重新生成。";
  });
  root.querySelector("[data-folder-open]")?.addEventListener("click", openMiniFolderPicker);
  root.querySelector("[data-folder-close]")?.addEventListener("click", () => { state.folder = null; render(); });
  root.querySelector("[data-folder-back]")?.addEventListener("click", () => { if (state.folder.stack.length > 1) state.folder.stack.pop(); loadMiniFolders(); });
  root.querySelector("[data-folder-select]")?.addEventListener("click", chooseMiniFolder);
  root.querySelectorAll("[data-folder-enter]").forEach((node) => node.addEventListener("click", () => enterMiniFolder(Number(node.dataset.folderEnter))));
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
