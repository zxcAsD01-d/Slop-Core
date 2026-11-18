'use strict';

const icons = {
  grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  database:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  messages:'<path d="M20 11.5V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h3l4 4v-4h6a2 2 0 0 0 2-2z"/><path d="M7 7h9M7 11h6"/>',
  memory:'<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 2v4m6-4v4M9 10h6m-6 4h6m-6 4h3"/>',
  spark:'<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3zM20 2v4m-2-2h4"/>',
  sliders:'<path d="M4 7h4m4 0h8M4 17h9m4 0h3"/><circle cx="10" cy="7" r="2"/><circle cx="15" cy="17" r="2"/>',
  arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',
  'arrow-left':'<path d="M20 12H4m6-6-6 6 6 6"/>',
  feather:'<path d="M20 4C9-2 2 10 8 15c5 6 17-1 12-11ZM4 21 17 8M9 16l-1-5m5 1 4 1"/>',
  bolt:'<path d="m13 2-9 12h7l-1 8 10-13h-7l1-7"/>',
  flame:'<path d="M12 2c1 7-5 7-5 12a5 5 0 0 0 10 0c0-2-1-3-1-3 0 3-3 3-3 1 0-3 3-4-1-10z"/>',
  copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
  terminal:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3m6 0h4"/>',
  download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
  shield:'<path d="m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6l8-4zM8 12l3 3 5-6"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v.1"/>',
  search:'<circle cx="10" cy="10" r="7"/><path d="m15 15 6 6"/>',
};

const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="workbench-token"]').content;
const state = {config:null, users:[], userId:null, profile:null, intensity:2, prompt:'', busy:false, revision:0, schemas:[]};
const labels = {preference:'交流偏好',habit:'表达习惯',tension:'观点张力',callback:'自嘲回调'};
let toastTimer;

function icon(name) {
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.info}</svg>`;
}

function mountIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach(element => {
    element.outerHTML = icon(element.dataset.icon);
  });
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
}

async function api(path, data) {
  const options = {headers:{'X-Workbench-Token':token},cache:'no-store'};
  if (data !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(data);
  }
  const response = await fetch('/api/' + path, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || '请求失败：' + response.status);
  return payload;
}

function toast(message, error = false) {
  clearTimeout(toastTimer);
  $('toast').textContent = message;
  $('toast').classList.toggle('error', error);
  $('toast').hidden = false;
  toastTimer = setTimeout(() => {$('toast').hidden = true;}, error ? 9000 : 5000);
}

async function action(task) {
  if (state.busy) return;
  state.busy = true;
  updateControls();
  try {await task();} catch (error) {toast(error.message || '操作失败', true);}
  finally {state.busy = false; updateControls();}
}

function updateControls() {
  ['analyze-button','generate-button'].forEach(id => {
    $(id).disabled = state.busy || !state.profile?.consent;
  });
  $('consent-switch').disabled = state.busy || !state.userId;
  $('analyze-button').classList.toggle('busy', state.busy);
  $('copy-button').disabled = !state.prompt || state.busy;
  $('export-button').disabled = !state.prompt || state.busy;
  ['index-person','index-all','search-materials'].forEach(id => {
    $(id).disabled = state.busy || !state.profile?.consent;
  });
  document.querySelectorAll('.user-button, .card-delete, #settings-form button').forEach(element => {
    element.disabled = state.busy;
  });
}

function switchView(view) {
  const settings = view === 'settings';
  $('settings-view').hidden = !settings;
  $('workbench-view').hidden = settings;
  $('page-label').textContent = settings ? '数据与模型' : '提示词工作台';
  document.querySelectorAll('.nav-item').forEach(element => {
    element.classList.toggle('active', element.dataset.view === view);
  });
}

function resetPrompt() {
  state.prompt = '';
  $('prompt-output').hidden = true;
  $('prompt-output').textContent = '';
  $('prompt-placeholder').hidden = false;
  $('prompt-meta').textContent = '等待生成';
  updateControls();
}

function renderUsers() {
  $('user-count').textContent = String(state.users.length).padStart(2,'0');
  $('user-list').innerHTML = state.users.map(user => `
    <button class="user-button ${user.id === state.userId ? 'active' : ''}" data-user="${escapeHtml(user.id)}" aria-pressed="${user.id === state.userId}">
      <span class="avatar">${escapeHtml([...user.name][0] || '?')}</span>
      <span><strong>${escapeHtml(user.name)}</strong><small>${user.count} 条聊天记录</small></span>
      ${user.id === state.userId ? '<span class="user-indicator"></span>' : ''}
    </button>`).join('');
}

function clearProfile(message = '正在读取人物档案…') {
  state.profile = null;
  $('material-results').innerHTML = '';
  $('material-note').textContent = '查询和预览不计数。实际采用后再确认使用；过去 24 小时用过的素材后置。';
  $('person-name').textContent = state.userId ? '正在读取…' : '尚无可读取的用户';
  $('person-avatar').textContent = '—';
  $('person-id').textContent = 'USER / ' + (state.userId || '—');
  $('person-status').textContent = '等待分析';
  $('consent-switch').checked = false;
  ['message-count','message-tab-count','memory-count','memory-tab-count'].forEach(id => {$(id).textContent = '0';});
  $('memories-content').innerHTML = `<div class="empty-state">${escapeHtml(message)}</div>`;
  $('messages-content').innerHTML = '';
  resetPrompt();
}

async function selectUser(userId) {
  if (state.busy) return;
  state.userId = userId;
  const revision = ++state.revision;
  renderUsers();
  clearProfile();
  try {
    const profile = await api('profile?user_id=' + encodeURIComponent(userId));
    if (revision !== state.revision) return;
    state.profile = profile;
    renderProfile();
  } catch (error) {
    if (revision !== state.revision) return;
    $('memories-content').innerHTML = `<div class="empty-state error-state">${escapeHtml(error.message)}</div>`;
    toast(error.message, true);
  }
  updateControls();
}

async function refreshProfile() {
  state.profile = await api('profile?user_id=' + encodeURIComponent(state.userId));
  renderProfile();
}

function renderProfile() {
  const profile = state.profile;
  $('person-name').textContent = profile.name;
  $('person-avatar').textContent = [...profile.name][0] || '?';
  $('person-id').textContent = 'USER / ' + profile.user_id;
  $('person-status').textContent = profile.consent ? '记忆已开启' : '记忆未开启';
  $('consent-switch').checked = profile.consent;
  $('message-count').textContent = profile.messages.length;
  $('message-tab-count').textContent = profile.messages.length;
  $('memory-count').textContent = profile.memories.length;
  $('memory-tab-count').textContent = profile.memories.length;
  const demo = state.config.source.path.replaceAll('\\','/').split('/').pop() === 'demo.db';
  $('person-mode').textContent = demo ? '虚构测试' : '自定义数据';
  renderMemories(profile.memories);
  renderMessages(profile.messages);
  updateControls();
}

function renderMemories(memories) {
  if (!memories.length) {
    $('memories-content').innerHTML = `<div class="empty-state">${icon('memory')}<strong>原话在这里，记忆还没开始。</strong><p>确认用户已同意记录，开启长期记忆，然后提取第一组证据。</p></div>`;
    return;
  }
  $('memories-content').innerHTML = memories.map(memory => `
    <article class="evidence-card">
      <div class="evidence-top"><span class="kind-tag kind-${escapeHtml(memory.kind)}">${escapeHtml(labels[memory.kind] || memory.kind)}</span><span class="verified-label ${memory.fresh ? '' : 'stale'}">${icon(memory.fresh ? 'check' : 'info')}${memory.fresh ? '引文已核对' : '语境已变 · 待重提取'}</span><button class="card-delete" data-delete="${memory.id}" aria-label="删除记忆：${escapeHtml(memory.title)}" title="删除此记忆">${icon('trash')}</button></div>
      <h4>${escapeHtml(memory.title)}</h4><p class="observation">${escapeHtml(memory.observation)}</p>
      ${memory.evidence.map(evidence => `<div class="quote">“${escapeHtml(evidence.quote)}”<cite>${escapeHtml(evidence.message_id)} · ${escapeHtml(formatTime(evidence.timestamp))}</cite></div>`).join('')}
      <p class="card-angle">${icon('bolt')}<span>${escapeHtml(memory.severity >= 3 || memory.risk_level >= 3 ? (memory.safe_rewrite || '待重新分析后使用') : memory.angle)}</span></p>
      <p class="card-angle"><span>表达 ${escapeHtml(['','轻度玩笑','中度严肃','高度伤害','重度过分'][memory.severity] || '待复核')} · 风险 ${memory.risk_level}/4${memory.risk_reason ? ' · ' + escapeHtml(memory.risk_reason) : ''}</span></p>
    </article>`).join('');
}

function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
}

function renderMessages(messages) {
  $('messages-content').innerHTML = messages.map(message => `<article class="message"><div class="message-top"><span>${escapeHtml(message.id)}</span><time>${escapeHtml(formatTime(message.timestamp))}</time></div><p class="message-body">${escapeHtml(message.content)}</p></article>`).join('') || '<div class="empty-state">没有可读取的本人消息。</div>';
}

function selectTab(messages) {
  $('messages-content').hidden = !messages;
  $('memories-content').hidden = messages;
  $('messages-tab').classList.toggle('active', messages);
  $('memories-tab').classList.toggle('active', !messages);
  $('messages-tab').setAttribute('aria-selected', String(messages));
  $('memories-tab').setAttribute('aria-selected', String(!messages));
}

async function changeConsent() {
  const enabled = $('consent-switch').checked;
  if (!enabled && state.profile.memories.length && !confirm('关闭将清除该用户的全部记忆。继续吗？')) {
    $('consent-switch').checked = true;
    return;
  }
  await action(async () => {
    try {
      await api('consent',{user_id:state.userId,enabled});
      await refreshProfile();
      resetPrompt();
      toast(enabled ? '已开启该用户的记忆，可以提取证据。' : '已撤回并清除该用户的全部记忆。');
    } finally {$('consent-switch').checked = Boolean(state.profile?.consent);}
  });
}

async function analyzeProfile() {
  await action(async () => {
    $('analysis-note').textContent = state.config.llm.mode === 'demo' ? '正在运行本地示例规则…' : '正在分析原文，可能需要一分钟…';
    try {
      const result = await api('analyze',{user_id:state.userId});
      await refreshProfile();
      resetPrompt();
      selectTab(false);
      toast(`通过 ${result.accepted} 条证据记忆，新增 ${result.saved} 条。${result.mode === 'demo' ? '当前为示例规则分析。' : '语义判断请结合原文复核。'}`);
    } finally {$('analysis-note').innerHTML = '<i class="tiny-dot"></i>原句可追溯，判断有边界';}
  });
}

async function generatePrompt() {
  await action(async () => {
    const result = await api('generate',{user_id:state.userId,intensity:state.intensity,budget:Number($('prompt-budget').value)});
    state.prompt = result.text;
    $('prompt-placeholder').hidden = true;
    $('prompt-output').hidden = false;
    $('prompt-output').textContent = result.text;
    $('prompt-meta').textContent = `${result.characters.toLocaleString()} 字符 · ${result.included} 条记忆${result.omitted ? ' · 预算省略 ' + result.omitted + ' 条' : ''}`;
    if (result.stale) toast(`${result.stale} 条记忆因原文或读取范围变化未注入，请重新提取。`);
    else if (!result.included) toast('已生成通用风格块，尚无可注入的证据记忆。');
    else toast('提示词已生成，可以复制或导出。');
  });
}

async function copyPrompt() {
  if (!state.prompt) return;
  try {await navigator.clipboard.writeText(state.prompt); toast('提示词已复制。');}
  catch (error) {toast('剪贴板不可用，请使用导出按钮或选中文本复制。', true);}
}

function exportPrompt() {
  if (!state.prompt) return;
  const url = URL.createObjectURL(new Blob([state.prompt],{type:'text/markdown;charset=utf-8'}));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = 'prompt-' + state.userId.replace(/[^\p{L}\p{N}_-]/gu,'_').slice(0,80) + '.md';
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url),1000);
  toast('已导出 Markdown 提示词。');
}

const mapping = [
  ['id_column','消息 ID','必填 · 同用户唯一'],['user_column','用户 ID','必填'],
  ['content_column','原文内容','必填'],['time_column','消息时间','必填 · 用于排序'],
  ['name_column','显示名称','可留空'],['conversation_column','会话 / 群 ID','可留空 · 空时不比较矛盾'],
  ['role_column','角色字段','可留空 · 仅含用户消息时'],['user_role','用户角色值','通常为 user'],
];

function fillSettings() {
  const config = state.config;
  $('source-path').value = config.source.path;
  $('source-table').value = config.source.table;
  $('source-limit').value = config.limit;
  $('mapping-fields').innerHTML = mapping.map(([field,label,hint]) => `<label class="field">${label}<input id="source-${field}" list="column-options" value="${escapeHtml(config.source[field])}" placeholder="${hint}" ${['id_column','user_column','content_column','time_column','user_role'].includes(field) ? 'required' : ''}><small>${hint}</small></label>`).join('');
  $('llm-mode').value = config.llm.mode;
  $('llm-url').value = config.llm.base_url;
  $('llm-model').value = config.llm.model;
  $('llm-key').value = '';
  $('key-note').textContent = config.key_configured ? 'Key 已在服务内存中；留空保留，重启后需重新填写或设环境变量。' : '不会写入配置文件，不会返回给浏览器。';
  $('remote-confirm').checked = config.llm.mode === 'llm';
  updateModelFields();
}

function updateModelFields() {
  const real = $('llm-mode').value === 'llm';
  $('llm-fields').hidden = !real;
  $('demo-note').hidden = real;
  $('llm-url').required = real;
  $('llm-model').required = real;
  $('remote-confirm').required = real;
}

async function inspectSchema() {
  await action(async () => {
    const result = await api('schema',{path:$('source-path').value});
    state.schemas = result.tables;
    $('schema-result').hidden = false;
    $('schema-result').textContent = result.tables.map(table => table.name + '\n  ' + table.columns.map(column => column.name + ' : ' + (column.type || 'ANY')).join('\n  ')).join('\n\n') || '数据库中没有数据表。';
    $('table-options').innerHTML = result.tables.map(table => `<option value="${escapeHtml(table.name)}"></option>`).join('');
    fillColumnOptions();
    toast('已读取数据库结构，请按实际字段配置映射。');
  });
}

function fillColumnOptions() {
  const table = state.schemas.find(item => item.name === $('source-table').value);
  $('column-options').innerHTML = (table?.columns || []).map(column => `<option value="${escapeHtml(column.name)}"></option>`).join('');
}

async function saveSettings(event) {
  event.preventDefault();
  await action(async () => {
    const source = {path:$('source-path').value.trim(),table:$('source-table').value.trim()};
    mapping.forEach(([field]) => {source[field] = $('source-' + field).value.trim();});
    const data = {source,limit:Number($('source-limit').value),llm:{mode:$('llm-mode').value,base_url:$('llm-url').value.trim(),model:$('llm-model').value.trim()}};
    if ($('llm-key').value) data.api_key = $('llm-key').value;
    state.config = await api('config',data);
    updateOverview();
    fillSettings();
    const result = await api('users');
    state.users = result.users;
    state.userId = null;
    state.revision++;
    clearProfile('当前映射中没有用户消息，请检查数据与角色字段。');
    renderUsers();
    toast('配置已验证并保存。');
  });
  if (state.users.length && !state.userId) await selectUser(state.users[0].id);
}

function updateOverview() {
  $('database-name').textContent = state.config.source.path.replaceAll('\\','/').split('/').pop();
  $('database-name').title = state.config.source.path;
  const demo = state.config.llm.mode === 'demo';
  $('engine-name').textContent = demo ? '本地演示' : state.config.llm.model;
  $('engine-badge').textContent = demo ? 'DEMO' : 'LLM';
}

function bindEvents() {
  bindCorpusEvents();
  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click',() => switchView(button.dataset.view)));
  $('open-settings').addEventListener('click',() => switchView('settings'));
  $('back-workbench').addEventListener('click',() => switchView('workbench'));
  $('user-list').addEventListener('click',event => {
    const target = event.target.closest('[data-user]');
    if (target) selectUser(target.dataset.user);
  });
  $('consent-switch').addEventListener('change',changeConsent);
  $('memories-tab').addEventListener('click',() => selectTab(false));
  $('messages-tab').addEventListener('click',() => selectTab(true));
  $('analyze-button').addEventListener('click',analyzeProfile);
  $('generate-button').addEventListener('click',generatePrompt);
  $('copy-button').addEventListener('click',copyPrompt);
  $('export-button').addEventListener('click',exportPrompt);
  $('memories-content').addEventListener('click',event => {
    const target = event.target.closest('[data-delete]');
    if (!target) return;
    action(async () => {
      await api('delete',{user_id:state.userId,memory_id:Number(target.dataset.delete)});
      await refreshProfile();
      resetPrompt();
      toast('已删除此条记忆。');
    });
  });
  document.querySelectorAll('[data-intensity]').forEach(button => button.addEventListener('click',() => {
    state.intensity = Number(button.dataset.intensity);
    document.querySelectorAll('[data-intensity]').forEach(element => element.classList.toggle('selected',element === button));
    resetPrompt();
  }));
  $('prompt-budget').addEventListener('change',resetPrompt);
  $('llm-mode').addEventListener('change',updateModelFields);
  $('source-table').addEventListener('input',fillColumnOptions);
  $('inspect-schema').addEventListener('click',inspectSchema);
  $('settings-form').addEventListener('submit',saveSettings);
}

async function initialize() {
  mountIcons();
  bindEvents();
  updateControls();
  try {
    state.config = await api('config');
    fillSettings();
    updateOverview();
    state.users = (await api('users')).users;
    renderUsers();
    if (state.users.length) await selectUser(state.users[0].id);
    else clearProfile('当前映射中没有用户消息，请检查数据与角色字段。');
  } catch (error) {
    toast(error.message, true);
    switchView('settings');
  }
}

initialize();

let corpusJob = '';
let corpusTimer = null;

function bindCorpusEvents() {
  $('index-person').addEventListener('click',() => startCorpusIndex(false));
  $('index-all').addEventListener('click',() => startCorpusIndex(true));
  $('material-search').addEventListener('submit',event => {
    event.preventDefault();
    action(queryMaterials);
  });
  $('material-results').addEventListener('click',event => {
    const target = event.target.closest('[data-confirm-material]');
    if (target) confirmMaterial(target);
  });
  api('index-status').then(showIndexStatus).catch(error => {
    $('index-status').textContent = error.message.includes('没有找到') ? '尚未建立全文索引。' : error.message;
  });
}

async function startCorpusIndex(all) {
  await action(async () => {
    const result = await api('index', all ? {} : {user_id:state.userId});
    showIndexStatus(result);
    toast('后台索引已开始，完成后可查询素材。');
  });
}

function showIndexStatus(result) {
  corpusJob = result.id;
  if (corpusTimer) clearTimeout(corpusTimer);
  const labels = {queued:'等待中',running:'处理中',completed:'完成',completed_with_errors:'部分失败'};
  $('index-status').textContent = '索引' + (labels[result.status] || result.status) + ' · ' +
    result.finished_users + '/' + result.total_users + ' 人物 · 已扫描 ' +
    result.messages_scanned + ' 条' + (result.errors.length ? ' · ' +
    result.errors.map(item => item.user_id + '：' + item.error).join('；') : '');
  if (['queued','running'].includes(result.status)) {
    corpusTimer = setTimeout(pollCorpusIndex, 1500);
  }
}

async function pollCorpusIndex() {
  try {showIndexStatus(await api('index-status?job_id=' + encodeURIComponent(corpusJob)));}
  catch (error) {$('index-status').textContent = error.message;}
}

async function queryMaterials() {
  const userId = state.userId;
  const params = new URLSearchParams({user_id:userId,query:$('material-query').value.trim(),
    topic:$('material-topic').value.trim()});
  const result = await api('materials?' + params);
  if (userId !== state.userId) return;
  renderMaterials(result.materials);
  $('material-note').textContent = result.usage_note + (result.pending_confirmations.length ?
    ' 之前有 ' + result.pending_confirmations.length + ' 条预览尚未确认；未采用无需确认。' : '');
  state.prompt = result.prompt_block;
  $('prompt-placeholder').hidden = true;
  $('prompt-output').hidden = false;
  $('prompt-output').textContent = state.prompt;
  $('prompt-meta').textContent = state.prompt.length.toLocaleString() + ' 字符 · 人物检索提示词块';
}

function renderMaterials(materials) {
  if (!materials.length) {
    $('material-results').innerHTML = '<div class="empty-state">没有匹配的素材，可尝试其他关键词。</div>';
    return;
  }
  const levels = ['','轻度玩笑','中度严肃','高度伤害','重度过分'];
  $('material-results').innerHTML = materials.map(item =>
    '<article class="evidence-card"><h4>' + escapeHtml(item.title) + '</h4>' +
    '<p class="observation">' + escapeHtml(item.observation) + '</p>' +
    '<p>' + escapeHtml(item.topic) + ' · ' + escapeHtml(levels[item.severity]) +
    ' · 风险 ' + item.risk_level + '/4</p><p>' +
    escapeHtml(item.risk_only ? item.safe_rewrite : item.angle) + '</p>' +
    (item.risk_reason ? '<p class="material-risk">' + escapeHtml(item.risk_reason) + '</p>' : '') +
    item.evidence.map(evidence => '<div class="quote">“' + escapeHtml(evidence.quote) +
      '”<cite>' + escapeHtml(evidence.message_id) + '</cite></div>').join('') +
    '<p>' + escapeHtml(item.scope_note) + '</p><div class="material-usage"><span>已使用 ' +
    item.use_count + ' 次 · 最近 ' + escapeHtml(item.last_used_at ? formatTime(item.last_used_at) : '未使用') +
    '</span>' + (item.risk_only ? '<span>仅风险说明</span>' :
      '<button class="button secondary" data-confirm-material="' + item.id +
      '" data-receipt="' + escapeHtml(item.use_receipt) + '">实际采用后确认</button>') +
    '</div></article>').join('');
}

async function confirmMaterial(button) {
  if (button.disabled) return;
  button.disabled = true;
  await action(async () => {
    try {
      const result = await api('confirm-use',{user_id:state.userId,
        material_id:Number(button.dataset.confirmMaterial),idempotency_key:button.dataset.receipt});
      button.textContent = '已确认 · 共 ' + result.use_count + ' 次';
      toast(result.counted ? '已记录实际使用。下次查询会调整排序。' : '此前已确认，本次未重复计数。');
    } catch (error) {button.disabled = false; throw error;}
  });
}
