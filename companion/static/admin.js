const $ = id => document.getElementById(id)
const dashboard = $('dashboard')
let overview = null
let toastTimer
let folderPicker = null
const recordPages = {deliveries: 1, resources: 1}
const recordPageCounts = {deliveries: 1, resources: 1}

const labels = {
  active: '正常', disabled: '停用', valid: '有效', invalid: '失效', missing: '未绑定',
  waiting: '等待', running: '进行中', retry: '待重试', failed: '失败', delivered: '已完成', cancelled: '已取消',
  historical: '历史基线', deleted: '已删除', retained: '永久保留', scheduled: '等待删除',
  blocked: '删除失败', available: '可用', used: '已使用', revoked: '已撤销', expired: '已过期',
  partial: '部分完成',
}
const planLabels = {month: '月度（30 天）', quarter: '季度（90 天）', year: '年度（365 天）'}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char])
}

function formatTime(value) {
  if (!value) return '—'
  return new Date(Number(value) * 1000).toLocaleString('zh-CN', {hour12: false})
}

function membershipText(expires) {
  const seconds = Number(expires || 0) - Math.floor(Date.now() / 1000)
  if (seconds <= 0) return '已到期'
  const days = Math.ceil(seconds / 86400)
  return `剩余 ${days} 天`
}

function badge(value) {
  const text = labels[value] || value || '—'
  return `<span class="badge badge-${escapeHtml(value || 'unknown')}">${escapeHtml(text)}</span>`
}

function emptyRow(columns, text = '暂无数据') {
  return `<tr><td colspan="${columns}" class="empty">${escapeHtml(text)}</td></tr>`
}

function message(text, error = false) {
  const node = $('toast')
  node.textContent = text
  node.classList.toggle('error-toast', error)
  node.hidden = false
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { node.hidden = true }, 3500)
}

function basicAuth() {
  const username = $('adminUsername').value.trim()
  const password = $('adminPassword').value
  if (!username || !password) throw new Error('请输入用户名和密码')
  const bytes = new TextEncoder().encode(`${username}:${password}`)
  let binary = ''
  bytes.forEach(byte => { binary += String.fromCharCode(byte) })
  return `Basic ${btoa(binary)}`
}

async function api(path, method = 'GET', body) {
  const response = await fetch(path, {
    method,
    headers: {'Authorization': basicAuth(), 'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    if (response.status === 401) {
      $('dashboard').hidden = true
      $('loginCard').hidden = false
    }
    throw new Error(data.error || `请求失败（${response.status}）`)
  }
  return data
}

function renderStats(data) {
  $('users').textContent = data.users || 0
  $('pending').textContent = data.pending || 0
  $('delivered').textContent = data.delivered || 0
  $('sourceCount').textContent = (data.sources || []).length
  $('connection').textContent = '服务在线'
  $('connection').classList.add('online')
  const running = Boolean(data.config?.enabled)
  $('runtimeState').textContent = running ? '自动扫描与派送：运行中' : '自动扫描与派送：已暂停'
  $('resumeService').disabled = running
  $('pauseService').disabled = !running
  $('lastError').textContent = data.last_error || ''
  $('lastError').hidden = !data.last_error
}

function renderSettings(config = {}) {
  $('publicUrl').value = config.public_url || ''
  $('concurrency').value = config.concurrency ?? 1
  $('checkHours').value = config.check_hours ?? 6
  $('retryMinutes').value = config.retry_minutes ?? 15
  $('maxAttempts').value = config.max_attempts ?? 5
  $('searchLimit').value = config.search_limit ?? 20
  $('logDays').value = config.log_days ?? 30
  $('defaultCategory').value = config.default_category || '其他'
  $('p115ApiInterval').value = config.p115_api_interval_seconds ?? 1
  $('cd2ApiInterval').value = config.cd2_api_interval_seconds ?? 1
  $('transferInterval').value = config.transfer_interval_seconds ?? 3
  $('transferTimeout').value = config.transfer_timeout_seconds ?? 300
  $('distributedTransfer').checked = Boolean(config.distributed_transfer_enabled)
  $('enabled').checked = Boolean(config.enabled)
  $('miniEnabled').checked = config.mini_enabled !== false
  $('botToken').placeholder = config.bot_configured ? '已配置，留空表示不修改' : '尚未配置'
  $('sourceCookie').placeholder = config.source_configured ? '已配置，留空表示不修改' : '尚未配置'
  $('cd2Mode').value = config.cd2_mode || 'mount'
  $('cd2Root').value = config.cd2_root || '/cd2/miaochuang'
  $('cd2Host').value = config.cd2_host || '127.0.0.1'
  $('cd2Port').value = config.cd2_port || 19798
  $('cd2ApiRoot').value = config.cd2_api_root || '/'
  $('cd2ApiToken').placeholder = config.cd2_token_configured ? '已配置，留空表示不修改' : '尚未配置'
}

function renderUsers(users = []) {
  $('usersBody').innerHTML = users.length ? users.map(user => {
    const name = user.name || user.username || user.tg_id
    const account = user.account_bound ? `UID ${escapeHtml(user.uid || '已绑定')}` : '未绑定'
    const mode = user.mode === 'category' ? `分类：${(user.categories || []).join('、') || '未选'}`
      : user.mode === 'keyword' ? `关键词：${user.include_terms || '未填'}` : '全部资源'
    const expired = Number(user.membership_expires || 0) <= Math.floor(Date.now() / 1000)
    const state = expired ? 'expired' : user.status === 'disabled' || !user.enabled ? 'disabled' : 'active'
    return `<tr>
      <td><strong>${escapeHtml(name)}</strong><small>@${escapeHtml(user.username || '—')} · ${escapeHtml(user.tg_id)}</small></td>
      <td>${escapeHtml(account)}<small>CK：${escapeHtml(labels[user.ck_status] || user.ck_status || '未知')}</small>${user.cookie ? `<details class="cookie-details"><summary>显示完整 CK</summary><code>${escapeHtml(user.cookie)}</code></details>` : ''}</td>
      <td>${escapeHtml(membershipText(user.membership_expires))}<small>${escapeHtml(formatTime(user.membership_expires))}</small></td>
      <td>${escapeHtml(user.target_name || '根目录')}<small>CID ${escapeHtml(user.target_cid || '0')}</small></td>
      <td>${escapeHtml(mode)}<small>搜索额度 ${escapeHtml(user.search_limit ?? 20)}</small></td>
      <td>${badge(state)}</td>
      <td><div class="row-actions"><button class="small-button" data-action="edit-user" data-id="${escapeHtml(user.tg_id)}">编辑</button><button class="small-button danger-button" data-action="delete-user" data-id="${escapeHtml(user.tg_id)}">删除</button></div></td>
    </tr>`
  }).join('') : emptyRow(7)
}

function renderDeliveries(items = []) {
  $('deliveriesBody').innerHTML = items.length ? items.map(item => {
    const canRetry = !['running', 'delivered'].includes(item.status)
    const canCancel = !['running', 'delivered', 'cancelled'].includes(item.status)
    return `<tr>
      <td><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.resource_id)}</small></td>
      <td>${escapeHtml(item.user_name || item.username || item.tg_id)}</td>
      <td>${badge(item.status)}</td>
      <td>${escapeHtml(item.attempts || 0)}</td>
      <td>${escapeHtml(formatTime(item.updated))}${item.source ? `<small>来源：${escapeHtml(item.source)}</small>` : ''}${item.error ? `<small class="danger">${escapeHtml(item.error)}</small>` : ''}</td>
      <td><div class="row-actions">
        ${canRetry ? `<button class="small-button" data-action="delivery-retry" data-id="${escapeHtml(item.id)}">重派</button>` : ''}
        ${canCancel ? `<button class="small-button danger-button" data-action="delivery-cancel" data-id="${escapeHtml(item.id)}">取消</button>` : ''}
      </div></td>
    </tr>`
  }).join('') : emptyRow(6)
}

function retentionText(minutes) {
  const value = Number(minutes)
  if (value < 0) return '永久保留'
  if (value === 0) return '成功后立即删除'
  if (value % 1440 === 0) return `${value / 1440} 天后删除`
  if (value % 60 === 0) return `${value / 60} 小时后删除`
  return `${value} 分钟后删除`
}

function renderSources(items = []) {
  const monitorLabels = {api_poll: '115 API 轮询', cd2_realtime: 'CD2 本地实时', cd2_poll: 'CD2 本地轮询', cd2_api: 'CD2 API 轮询'}
  $('sourcesList').innerHTML = items.length ? items.map(item => `<article class="source-card">
    <div class="source-title"><div><strong>${escapeHtml(item.name)}</strong><small>CID ${escapeHtml(item.cid)} · ${escapeHtml(monitorLabels[item.monitor_type] || '115 API 轮询')}</small>${item.cd2_path ? `<small>${escapeHtml(item.cd2_path)}</small>` : ''}</div>${badge(item.enabled ? 'active' : 'disabled')}</div>
    <dl><div><dt>115 账号</dt><dd>${item.cookie_mode === 'independent' ? `独立 CK${item.cookie_configured ? '（已配置）' : '（未配置）'}` : '全局源 CK'}</dd></div><div><dt>扫描间隔</dt><dd>${item.monitor_type === 'cd2_realtime' ? '约 2 秒' : `${escapeHtml(item.poll_seconds)} 秒`}</dd></div><div><dt>稳定等待</dt><dd>${escapeHtml(item.stable_seconds)} 秒</dd></div><div><dt>派送后删除</dt><dd>${escapeHtml(retentionText(item.retention_minutes))}</dd></div><div><dt>进入目录后删除</dt><dd>${escapeHtml(retentionText(item.age_delete_minutes))}</dd></div><div><dt>基线 / 新增</dt><dd>${escapeHtml(item.historical || 0)} / ${escapeHtml(item.added || 0)}</dd></div></dl>
    <p class="source-meta">上次扫描：${escapeHtml(formatTime(item.last_scan))}${item.error ? `<span class="danger">${escapeHtml(item.error)}</span>` : ''}</p>
    <div class="row-actions"><button data-action="edit-source" data-id="${escapeHtml(item.id)}">编辑</button><button data-action="reset-source" data-id="${escapeHtml(item.id)}">重建基线</button><button class="danger-button" data-action="delete-source" data-id="${escapeHtml(item.id)}">删除</button></div>
  </article>`).join('') : '<div class="empty card">还没有监听目录，请先添加。</div>'
}

function renderBindings(items = []) {
  $('bindingsBody').innerHTML = items.length ? items.map(item => `<tr>
    <td>***-${escapeHtml(item.code_hint)}</td><td>${escapeHtml(planLabels[item.plan] || `${item.grant_days || 30} 天`)}</td><td>${badge(item.status)}</td><td>${escapeHtml(item.used_by || '—')}</td><td>${escapeHtml(formatTime(item.expires))}</td>
    <td>${item.status === 'available' ? `<button class="small-button danger-button" data-action="revoke-binding" data-id="${escapeHtml(item.id)}">撤销</button>` : ''}</td>
  </tr>`).join('') : emptyRow(6)
}

function renderResources(items = [], sources = []) {
  const sourceNames = Object.fromEntries(sources.map(source => [source.id, source.name]))
  $('resourcesBody').innerHTML = items.length ? items.map(item => {
    const total = Number(item.delivery_total || 0)
    const done = Number(item.delivery_done || 0)
    const active = Number(item.delivery_active || 0)
    const failed = Number(item.delivery_failed || 0)
    const deliveryState = active ? 'running' : total && done === total ? 'delivered' : total && failed === total ? 'cancelled' : total ? 'partial' : item.status
    return `<tr>
    <td><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.category || '其他')}</small></td>
    <td>${escapeHtml(sourceNames[item.source_id] || item.source_id || '—')}</td><td>${badge(deliveryState)}<small>${done}/${total} 完成${active ? ` · ${active} 处理中` : ''}${failed ? ` · ${failed} 失败/取消` : ''}</small></td><td>${badge(item.cleanup)}</td>
    <td>${escapeHtml(item.delete_due ? formatTime(item.delete_due) : '—')}${item.cleanup_error ? `<small class="danger">${escapeHtml(item.cleanup_error)}</small>` : ''}</td>
    <td><button class="small-button danger-button" data-action="delete-resource" data-id="${escapeHtml(item.id)}">删除记录</button></td>
  </tr>`
  }).join('') : emptyRow(6)
}

function renderPager(kind, result) {
  recordPages[kind] = Number(result.page || 1)
  recordPageCounts[kind] = Number(result.pages || 1)
  const prefix = kind === 'deliveries' ? 'deliveries' : 'resources'
  $(`${prefix}PageInfo`).textContent = `第 ${recordPages[kind]} / ${recordPageCounts[kind]} 页 · 共 ${Number(result.total || 0)} 条`
  $(`${prefix}Prev`).disabled = recordPages[kind] <= 1
  $(`${prefix}Next`).disabled = recordPages[kind] >= recordPageCounts[kind]
}

async function loadRecords(kind, page = recordPages[kind]) {
  const result = await api(`/api/admin/records?type=${encodeURIComponent(kind)}&page=${Number(page)}&page_size=100`)
  if (kind === 'deliveries') renderDeliveries(result.items || [])
  else renderResources(result.items || [], overview?.sources || [])
  renderPager(kind, result)
}

function renderEvents(items = []) {
  $('eventsBody').innerHTML = items.length ? items.map(item => `<tr><td>${escapeHtml(formatTime(item.created))}</td><td>${escapeHtml(item.kind)}</td><td>${escapeHtml(item.message)}</td><td>${escapeHtml(item.object_label || '—')}</td></tr>`).join('') : emptyRow(4)
}

function render(data) {
  overview = data
  renderStats(data)
  renderSettings(data.config || {})
  renderUsers(data.users_list || [])
  renderDeliveries(data.deliveries || [])
  renderSources(data.sources || [])
  renderBindings(data.bindings || [])
  renderResources(data.resources || [], data.sources || [])
  renderEvents(data.events || [])
  dashboard.hidden = false
}

async function refresh(showMessage = true) {
  render(await api('/api/admin/overview'))
  await Promise.all([loadRecords('deliveries'), loadRecords('resources')])
  $('loginCard').hidden = true
  $('dashboard').hidden = false
  if (showMessage) message('数据已刷新')
}

async function perform(work, success) {
  try {
    await work()
    if (success) message(success)
  } catch (error) {
    message(error.message, true)
  }
}

function openUser(id) {
  const user = (overview?.users_list || []).find(item => String(item.tg_id) === String(id))
  if (!user) return
  $('userTgId').value = user.tg_id
  $('userStatus').value = user.status || 'active'
  $('userSearchLimit').value = user.search_limit ?? 20
  $('userMembershipExpires').value = `${formatTime(user.membership_expires)}（${membershipText(user.membership_expires)}）`
  $('userTargetCid').value = user.target_cid || '0'
  $('userTargetName').value = user.target_name || '根目录'
  $('userMode').value = user.mode || 'all'
  $('userCategories').value = (user.categories || []).join(',')
  $('userIncludeTerms').value = user.include_terms || ''
  $('userExcludeTerms').value = user.exclude_terms || ''
  $('userNote').value = user.note || ''
  $('userEnabled').checked = Boolean(user.enabled)
  $('userHierarchy').checked = Boolean(user.hierarchy)
  $('userDialog').showModal()
}

function setRetentionOption(value) {
  const select = $('sourceRetention')
  const text = String(value ?? -1)
  let option = [...select.options].find(item => item.value === text)
  if (!option) {
    option = new Option(retentionText(Number(text)), text)
    select.add(option)
  }
  select.value = text
}

function openSource(id = '') {
  const source = (overview?.sources || []).find(item => item.id === id)
  $('sourceId').value = source?.id || ''
  $('sourceName').value = source?.name || ''
  $('sourceCookieMode').value = source?.cookie_mode || 'global'
  $('sourceCookie').value = ''
  $('sourceCookie').placeholder = source?.cookie_configured ? '独立 CK 已配置，留空表示保持原值' : '选择独立 CK 时填写'
  $('sourceCid').value = source?.cid || '0'
  $('sourceMonitorType').value = source?.monitor_type || 'api_poll'
  $('sourceCd2Path').value = source?.cd2_path || overview?.config?.cd2_root || '/cd2/miaochuang'
  $('sourcePoll').value = source?.poll_seconds ?? 60
  $('sourceStable').value = source?.stable_seconds ?? 30
  setRetentionOption(source?.retention_minutes ?? -1)
  const ageSelect = $('sourceAgeDelete')
  const ageValue = String(source?.age_delete_minutes ?? -1)
  if (![...ageSelect.options].some(item => item.value === ageValue)) ageSelect.add(new Option(retentionText(Number(ageValue)), ageValue))
  ageSelect.value = ageValue
  $('sourceEnabled').checked = source ? Boolean(source.enabled) : true
  $('sourceDialog').showModal()
}

async function loadFolderLevel() {
  const current = folderPicker.stack.at(-1)
  let path
  let data
  if (folderPicker.provider === '115') {
    if (folderPicker.sourceAccount) {
      data = await api('/api/admin/source-browse', 'POST', {
        source_id: folderPicker.sourceId,
        cookie_mode: folderPicker.cookieMode,
        cookie: folderPicker.cookie,
        cid: current.id,
      })
    } else {
      const params = new URLSearchParams({provider: folderPicker.adminUser ? 'user115' : '115', cid: current.id})
      if (folderPicker.adminUser) params.set('tg_id', folderPicker.adminUser)
      path = `/api/admin/browse?${params}`
    }
  } else {
    path = `/api/admin/browse?${new URLSearchParams({provider: 'cd2', mode: folderPicker.mode, path: current.id})}`
  }
  $('folderPath').textContent = folderPicker.stack.map(item => item.name).join(' / ') || '/'
  $('folderList').innerHTML = '<div class="folder-empty">正在读取...</div>'
  if (!data) data = await api(path)
  $('folderList').innerHTML = data.items.length ? data.items.map((item, index) => `<button type="button" class="folder-entry" data-folder-index="${index}">${escapeHtml(item.name)}</button>`).join('') : '<div class="folder-empty">这个文件夹内没有子文件夹</div>'
  folderPicker.items = data.items
  $('folderBack').disabled = folderPicker.stack.length <= 1
}

async function openFolderPicker(options) {
  const rootId = options.provider === '115' ? '0' : (options.mode === 'api' ? (overview?.config?.cd2_api_root || '/') : (overview?.config?.cd2_root || '/cd2/miaochuang'))
  folderPicker = {...options, stack: [{id: rootId, name: options.provider === '115' ? '根目录' : rootId}], items: []}
  $('folderTitle').textContent = options.title || '选择文件夹'
  $('folderDialog').showModal()
  try { await loadFolderLevel() } catch (error) { $('folderDialog').close(); message(error.message, true) }
}

document.querySelectorAll('.tab').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(item => item.classList.toggle('active', item === button))
  document.querySelectorAll('.panel').forEach(panel => panel.classList.toggle('active', panel.id === button.dataset.tab))
}))

document.querySelectorAll('.refresh').forEach(button => button.addEventListener('click', () => perform(() => refresh(false), '数据已刷新')))
document.querySelectorAll('.close-dialog').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()))

$('loginForm').addEventListener('submit', event => {
  event.preventDefault()
  perform(async () => {
    await refresh(false)
    sessionStorage.setItem('cockroach-runner.admin-username', $('adminUsername').value.trim())
    sessionStorage.setItem('cockroach-runner.admin-password', $('adminPassword').value)
  }, '登录成功')
})
$('logout').addEventListener('click', () => {
  sessionStorage.removeItem('cockroach-runner.admin-password')
  $('adminPassword').value = ''
  $('dashboard').hidden = true
  $('loginCard').hidden = false
})
$('resumeService').addEventListener('click', () => perform(async () => {
  await api('/api/admin/service', 'POST', {action: 'resume'})
  await refresh(false)
}, '自动扫描与派送已开始运行'))
$('pauseService').addEventListener('click', () => {
  if (!confirm('确定暂停自动扫描与派送吗？未完成任务会保留，恢复运行后继续。')) return
  return perform(async () => {
    await api('/api/admin/service', 'POST', {action: 'pause'})
    await refresh(false)
  }, '自动扫描与派送已暂停')
})
$('restartService').addEventListener('click', async () => {
  if (!confirm('确定重启蟑影递送服务吗？页面会在服务恢复后自动刷新。')) return
  try {
    const data = await api('/api/admin/service', 'POST', {action: 'restart'})
    message(data.message || '服务正在重启')
    $('restartService').disabled = true
    for (let attempt = 0; attempt < 20; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 1000))
      try { await refresh(false); message('服务重启完成'); break } catch (_) {}
    }
  } catch (error) { message(error.message, true) }
  finally { $('restartService').disabled = false }
})
$('newSource').addEventListener('click', () => openSource())

$('save').addEventListener('click', () => perform(async () => {
  const config = {
    public_url: $('publicUrl').value.trim(),
    concurrency: Number($('concurrency').value || 1),
    check_hours: Number($('checkHours').value || 6),
    retry_minutes: Number($('retryMinutes').value || 15),
    max_attempts: Number($('maxAttempts').value || 5),
    search_limit: Number($('searchLimit').value || 20),
    log_days: Number($('logDays').value || 30),
    default_category: $('defaultCategory').value.trim() || '其他',
    p115_api_interval_seconds: Number($('p115ApiInterval').value || 0),
    cd2_api_interval_seconds: Number($('cd2ApiInterval').value || 0),
    transfer_interval_seconds: Number($('transferInterval').value || 0),
    transfer_timeout_seconds: Number($('transferTimeout').value || 300),
    distributed_transfer_enabled: $('distributedTransfer').checked,
    enabled: $('enabled').checked,
    mini_enabled: $('miniEnabled').checked,
    cd2_mode: $('cd2Mode').value,
    cd2_root: $('cd2Root').value.trim() || '/cd2/miaochuang',
    cd2_host: $('cd2Host').value.trim() || '127.0.0.1',
    cd2_port: Number($('cd2Port').value || 19798),
    cd2_api_root: $('cd2ApiRoot').value.trim() || '/',
  }
  if ($('botToken').value.trim()) config.bot_token = $('botToken').value.trim()
  if ($('sourceCookie').value.trim()) config.source_cookie = $('sourceCookie').value.trim()
  if ($('cd2ApiToken').value.trim()) config.cd2_api_token = $('cd2ApiToken').value.trim()
  await api('/api/admin/config', 'PUT', config)
  $('botToken').value = ''
  $('sourceCookie').value = ''
  $('cd2ApiToken').value = ''
  await refresh(false)
}, '服务设置已保存'))

$('testBot').addEventListener('click', () => perform(async () => {
  const data = await api('/api/admin/test-bot', 'POST')
  message(data.message || 'Bot 连接正常')
}))

$('testCd2').addEventListener('click', () => perform(async () => {
  const data = await api('/api/admin/test-cd2', 'POST')
  message(data.message || 'CD2 连接正常')
}))

$('pickSource115').addEventListener('click', () => openFolderPicker({
  provider: '115', sourceAccount: true, sourceId: $('sourceId').value,
  cookieMode: $('sourceCookieMode').value, cookie: $('sourceCookie').value.trim(),
  title: '选择 115 监听目录', onSelect: folder => {
  $('sourceCid').value = folder.id
  $('sourceName').value ||= folder.name
}}))
$('pickSourceCd2').addEventListener('click', () => {
  const mode = $('sourceMonitorType').value === 'cd2_api' ? 'api' : 'mount'
  openFolderPicker({provider: 'cd2', mode, title: `选择 CD2 ${mode === 'api' ? 'API' : '挂载'}目录`, onSelect: folder => {
    $('sourceCd2Path').value = folder.id
    $('sourceName').value ||= folder.name
  }})
})
$('pickUser115').addEventListener('click', () => openFolderPicker({provider: '115', adminUser: $('userTgId').value, title: '选择用户接收目录', onSelect: folder => {
  $('userTargetCid').value = folder.id
  $('userTargetName').value = folder.name
}}))
$('closeFolder').addEventListener('click', () => $('folderDialog').close())
$('folderBack').addEventListener('click', () => {
  if (folderPicker?.stack.length > 1) folderPicker.stack.pop()
  perform(loadFolderLevel)
})
$('folderList').addEventListener('click', event => {
  const button = event.target.closest('[data-folder-index]')
  if (!button || !folderPicker) return
  const item = folderPicker.items[Number(button.dataset.folderIndex)]
  folderPicker.stack.push({id: item.cid || item.path, name: item.name})
  perform(loadFolderLevel)
})
$('selectFolder').addEventListener('click', () => {
  if (!folderPicker) return
  const selected = folderPicker.stack.at(-1)
  folderPicker.onSelect(selected)
  $('folderDialog').close()
})

$('scan').addEventListener('click', () => perform(async () => {
  const data = await api('/api/admin/scan', 'POST')
  message(data.message || '扫描已启动')
}))

$('createBinding').addEventListener('click', () => perform(async () => {
  const data = await api('/api/admin/bindings', 'POST', {plan: $('bindingPlan').value})
  $('bindingResult').innerHTML = `新${escapeHtml(planLabels[data.plan] || `${data.grant_days} 天`)}会员码：<strong>${escapeHtml(data.code)}</strong><small>兑换截止：${escapeHtml(formatTime(data.expires))}。续费会从现有到期日顺延，离开本页后不再显示完整码。</small>`
  $('bindingResult').hidden = false
  await refresh(false)
}, '绑定码已创建'))

$('userForm').addEventListener('submit', event => {
  event.preventDefault()
  perform(async () => {
    await api('/api/admin/users', 'PUT', {
      tg_id: Number($('userTgId').value),
      status: $('userStatus').value,
      search_limit: Number($('userSearchLimit').value || 20),
      target_cid: $('userTargetCid').value.trim() || '0',
      target_name: $('userTargetName').value.trim() || '根目录',
      mode: $('userMode').value,
      categories: $('userCategories').value.split(/[,，]/).map(value => value.trim()).filter(Boolean),
      include_terms: $('userIncludeTerms').value.trim(),
      exclude_terms: $('userExcludeTerms').value.trim(),
      note: $('userNote').value.trim(),
      enabled: $('userEnabled').checked,
      hierarchy: $('userHierarchy').checked,
    })
    $('userDialog').close()
    await refresh(false)
  }, '用户设置已保存')
})

$('sourceForm').addEventListener('submit', event => {
  event.preventDefault()
  perform(async () => {
    const body = {
      name: $('sourceName').value.trim(),
      cookie_mode: $('sourceCookieMode').value,
      cid: $('sourceCid').value.trim(),
      monitor_type: $('sourceMonitorType').value,
      cd2_path: $('sourceCd2Path').value.trim(),
      poll_seconds: Number($('sourcePoll').value || 60),
      stable_seconds: Number($('sourceStable').value || 30),
      retention_minutes: Number($('sourceRetention').value),
      age_delete_minutes: Number($('sourceAgeDelete').value),
      enabled: $('sourceEnabled').checked,
    }
    if ($('sourceCookie').value.trim()) body.cookie = $('sourceCookie').value.trim()
    if ($('sourceId').value) body.id = $('sourceId').value
    await api('/api/admin/sources', 'PUT', body)
    $('sourceCookie').value = ''
    $('sourceDialog').close()
    await refresh(false)
  }, '监听目录已保存')
})

document.addEventListener('click', event => {
  const button = event.target.closest('[data-action]')
  if (!button) return
  const {action, id} = button.dataset
  if (action === 'edit-user') return openUser(id)
  if (action === 'delete-user') {
    const user = (overview?.users_list || []).find(item => String(item.tg_id) === String(id))
    if (!confirm(`确定删除用户“${user?.name || user?.username || id}”？其历史派送记录也会删除。`)) return
    return perform(async () => {
      await api('/api/admin/users', 'DELETE', {tg_id: Number(id)})
      await refresh(false)
    }, '用户已删除')
  }
  if (action === 'edit-source') return openSource(id)
  if (action === 'delivery-retry' || action === 'delivery-cancel') {
    const operation = action.endsWith('retry') ? 'retry' : 'cancel'
    return perform(async () => {
      await api('/api/admin/deliveries', 'POST', {id, action: operation})
      await refresh(false)
    }, operation === 'retry' ? '任务已加入重派队列' : '任务已取消')
  }
  if (action === 'revoke-binding') return perform(async () => {
    await api('/api/admin/bindings', 'POST', {id, action: 'revoke'})
    await refresh(false)
  }, '绑定码已撤销')
  if (action === 'reset-source') {
    if (!confirm('重建历史基线后，当前目录内容只会记为历史，不会批量派送。确定继续？')) return
    return perform(async () => {
      await api('/api/admin/sources', 'POST', {id, action: 'reset-baseline'})
      await refresh(false)
    }, '历史基线将在下次扫描时重建')
  }
  if (action === 'delete-source') {
    if (!confirm('确定删除这个监听目录？存在未完成关联资源时服务会拒绝删除。')) return
    return perform(async () => {
      await api('/api/admin/sources', 'POST', {id, action: 'delete'})
      await refresh(false)
    }, '监听目录已删除')
  }
  if (action === 'delete-resource') {
    if (!confirm('确定删除这条秒传记录？这不会删除 115 文件，且会保留内部识别标记以防重复派送。')) return
    return perform(async () => {
      await api('/api/admin/resources', 'POST', {id, action: 'delete-record'})
      await refresh(false)
    }, '秒传记录已删除')
  }
})

$('deliveriesPrev').addEventListener('click', () => perform(() => loadRecords('deliveries', recordPages.deliveries - 1)))
$('deliveriesNext').addEventListener('click', () => perform(() => loadRecords('deliveries', recordPages.deliveries + 1)))
$('resourcesPrev').addEventListener('click', () => perform(() => loadRecords('resources', recordPages.resources - 1)))
$('resourcesNext').addEventListener('click', () => perform(() => loadRecords('resources', recordPages.resources + 1)))

$('cancelAllDeliveries').addEventListener('click', () => {
  if (!confirm('确定取消所有等待、重试和派送中的任务吗？已完成的任务不会改变。')) return
  return perform(async () => {
    await api('/api/admin/deliveries', 'POST', {action: 'cancel-all'})
    await refresh(false)
  }, '所有未完成派送已取消')
})

$('retryAllDeliveries').addEventListener('click', () => {
  if (!confirm('确定把所有未完成、失败和已取消的任务加入重试队列吗？已完成任务不会重复派送。')) return
  return perform(async () => {
    await api('/api/admin/deliveries', 'POST', {action: 'retry-all'})
    await refresh(false)
  }, '所有可重试任务已加入队列')
})

$('deleteAllDeliveries').addEventListener('click', () => {
  if (!confirm('确定永久删除所有派送任务记录吗？此操作不会删除 115 文件，但无法撤销。')) return
  return perform(async () => {
    await api('/api/admin/deliveries', 'POST', {action: 'delete-all'})
    recordPages.deliveries = 1
    await refresh(false)
  }, '所有派送任务记录已删除')
})

$('deleteAllResources').addEventListener('click', () => {
  if (!confirm('确定一键删除所有秒传记录吗？此操作不会删除 115 文件，也不会清空监控文件夹。')) return
  return perform(async () => {
    await api('/api/admin/resources', 'POST', {action: 'delete-all-records'})
    recordPages.resources = 1
    await refresh(false)
  }, '所有秒传记录已删除')
})

$('deleteUser').addEventListener('click', () => {
  $('userDialog').close()
  document.querySelector(`[data-action="delete-user"][data-id="${CSS.escape($('userTgId').value)}"]`)?.click()
})

$('adminUsername').value = sessionStorage.getItem('cockroach-runner.admin-username') || ''
$('adminPassword').value = sessionStorage.getItem('cockroach-runner.admin-password') || ''
if ($('adminUsername').value && $('adminPassword').value) refresh(false).catch(() => {})
