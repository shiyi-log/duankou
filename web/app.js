import { classifyPort, filterPorts, localizeApiError, ownershipLabel, parsePortInput, portDetails, requireProjectPath, stateLabel, validationSuccessMessage } from './ports.js';

const state = { ports: [], projects: [], filter: 'all', query: '', removingProject: null };
const $ = (selector) => document.querySelector(selector);
const serviceStatus = $('#service-status');
const portRows = $('#port-rows');
const emptyState = $('#empty-state');
const errorState = $('#error-state');
const toast = $('#toast');
let toastTimer;

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(localizeApiError(body.detail || body.message || body.error || `请求失败（${response.status}）`));
    error.status = response.status;
    throw error;
  }
  return body;
}

function showToast(message, kind = 'success') {
  toast.textContent = message;
  toast.className = `toast is-visible ${kind === 'error' ? 'is-error' : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('is-visible'), 3200);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);
}

function setConnection(online, message) {
  serviceStatus.classList.toggle('is-offline', !online);
  serviceStatus.querySelector('span:last-child').textContent = message;
}

function renderSummary(summary = {}) {
  $('#metric-registered').textContent = summary.registered ?? state.projects.reduce((sum, project) => sum + project.ports.length, 0);
  $('#metric-listening').textContent = summary.listening ?? state.ports.filter((port) => port.status === 'listening' || port.status === 'conflict').length;
  $('#metric-conflicts').textContent = summary.conflicts ?? state.ports.filter((port) => classifyPort(port) === 'unregistered').length;
  $('#attention-count').textContent = state.ports.filter((port) => ['unregistered', 'mismatch', 'unverified'].includes(classifyPort(port))).length;
  $('#project-count').textContent = state.projects.length;
}

function renderPorts() {
  const visiblePorts = filterPorts(state.ports, state.filter, state.query);
  portRows.replaceChildren();
  $('#row-count').textContent = `${visiblePorts.length} 个端口`;
  $('#ports-caption').textContent = `${state.ports.length} 个监听或登记端口 · 本机实时采集`;
  emptyState.hidden = visiblePorts.length !== 0;

  for (const port of visiblePorts) {
    const kind = classifyPort(port);
    const project = state.projects.find((item) => item.name === port.project);
    const projectName = project?.system ? '端口台账服务' : port.project;
    const ownership = ownershipLabel(port.ownership);
    const row = document.createElement('tr');
    row.innerHTML = `
      <td data-label="端口 / 协议"><button class="port-number port-detail-link" type="button" data-port-detail="${escapeHtml(port.port)}" aria-label="查看端口 ${escapeHtml(port.port)} 的使用详情">${escapeHtml(port.port)}</button><span class="protocol">${escapeHtml((port.protocol || 'tcp').toUpperCase())}</span></td>
      <td data-label="项目归属"><span class="project-name ${port.project ? '' : 'muted'}">${escapeHtml(projectName || '未分配')}</span></td>
      <td data-label="进程"><span class="process-name">${escapeHtml(port.process || (port.status === 'registered' ? '等待项目启动' : '—'))}</span>${port.pid ? `<span class="process-pid">PID ${escapeHtml(port.pid)}</span>` : ''}</td>
      <td data-label="状态"><div class="state-stack"><span class="state-pill state-${kind}"><i></i>${stateLabel(port)}</span>${ownership && kind !== 'unregistered' && kind !== 'mismatch' ? `<span class="ownership-badge ownership-${escapeHtml(port.ownership)}">${ownership}</span>` : ''}</div></td>
      <td class="row-action"><button class="text-button port-detail-action" type="button" data-port-detail="${escapeHtml(port.port)}" aria-label="查看端口 ${escapeHtml(port.port)} 的使用详情">详情</button></td>`;
    portRows.append(row);
  }
}

function renderProjects() {
  const list = $('#project-list');
  list.replaceChildren();
  if (!state.projects.length) {
    const empty = document.createElement('p');
    empty.className = 'project-empty';
    empty.textContent = '尚无项目登记';
    list.append(empty);
  }
  for (const project of state.projects) {
    const item = document.createElement('article');
    item.className = 'project-row';
    const projectName = project.system ? '端口台账服务' : project.name;
    const ownership = ownershipLabel(project.ownership);
    const projectPath = project.path || '未绑定项目目录';
    item.innerHTML = `
      <div class="project-identity"><span class="project-led" aria-hidden="true"></span><div><strong>${escapeHtml(projectName)}${ownership ? `<small class="ownership-badge ownership-${escapeHtml(project.ownership)}">${ownership}</small>` : ''}</strong><span>${project.ports.map((port) => `<b>${escapeHtml(port)}</b>`).join('<span class="port-comma">, </span>')}</span><small class="project-path" title="${escapeHtml(projectPath)}">${escapeHtml(projectPath)}</small></div></div>
      <div class="project-actions"><button class="icon-button" type="button" data-validate="${escapeHtml(project.id)}" title="验证端口使用权" aria-label="验证 ${escapeHtml(projectName)} 的端口"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 5 6v5c0 4.7 3 8.3 7 10 4-1.7 7-5.3 7-10V6l-7-3Z"/><path d="m9 12 2 2 4-4"/></svg></button>${project.system ? '<span class="system-tag">系统</span>' : `<button class="icon-button danger-icon" type="button" data-remove="${escapeHtml(project.id)}" title="释放项目登记" aria-label="释放 ${escapeHtml(projectName)} 的登记"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M10 11v6m4-6v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg></button>`}</div>`;
    list.append(item);
  }
}

function render() {
  renderSummary(state.summary);
  renderPorts();
  renderProjects();
}

async function loadDashboard() {
  const refresh = $('#refresh');
  refresh.classList.add('is-loading');
  errorState.hidden = true;
  try {
    const [health, portData, projectData] = await Promise.all([
      request('/api/health'), request('/api/ports'), request('/api/projects'),
    ]);
    state.ports = portData.ports || [];
    state.summary = portData.summary || {};
    state.projects = (projectData.projects || []).map((project) => ({ ...project, ports: project.ports || [] }));
    setConnection(true, health.status === 'ok' || health.ok === true ? '本机服务在线' : '已连接本机服务');
    render();
    $('#last-updated').textContent = `更新于 ${new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
  } catch (error) {
    setConnection(false, '本机服务离线');
    $('#error-message').textContent = error.message || '请确认本机服务已启动。';
    errorState.hidden = false;
    emptyState.hidden = true;
    portRows.replaceChildren();
  } finally {
    refresh.classList.remove('is-loading');
  }
}

function openDialog(id) {
  const dialog = document.getElementById(id);
  if (!dialog.open) dialog.showModal();
}

function closeDialog(id) {
  document.getElementById(id).close();
}

function openValidation(projectId) {
  const project = state.projects.find((item) => String(item.id) === String(projectId));
  if (!project) return;
  $('#validate-project-id').value = project.id;
  $('#validate-project-name').textContent = project.name;
  $('#validate-port').innerHTML = project.ports.map((port) => `<option value="${escapeHtml(port)}">${escapeHtml(port)}</option>`).join('');
  $('#validate-result').hidden = true;
  $('#validate-error').hidden = true;
  openDialog('validate-dialog');
}

function openPortDetails(portNumber) {
  const port = state.ports.find((item) => Number(item.port) === Number(portNumber));
  if (!port) return;
  const project = state.projects.find((item) => item.name === port.project);
  const detail = portDetails(port, project);
  const owner = project?.system ? '端口台账服务' : detail.owner;
  const field = (label, value, mono = false) => `<div><dt>${label}</dt><dd${mono ? ' class="mono-value"' : ''}>${escapeHtml(value)}</dd></div>`;
  $('#detail-title').textContent = `端口 ${detail.port}`;
  $('#port-detail-content').innerHTML = `
    <dl class="detail-grid">
      ${field('协议', detail.protocol.toUpperCase())}
      ${field('登记项目', owner)}
      ${field('当前状态', stateLabel(port))}
      ${field('归属检查', detail.ownership || '未登记')}
      ${field('登记目录', detail.registeredPath, true)}
    </dl>
    <h3 class="listener-title">监听进程 <span>${detail.listeners.length}</span></h3>
    ${detail.listeners.length ? detail.listeners.map((listener) => `
      <dl class="listener-detail">
        ${field('监听地址', listener.address, true)}
        ${field('地址类型', listener.family)}
        ${field('进程名称', listener.process, true)}
        ${field('PID', listener.pid, true)}
        ${field('工作目录', listener.path, true)}
      </dl>`).join('') : '<p class="detail-empty">当前没有监听进程。登记端口仍为该项目保留。</p>'}
    <p class="field-help">这些信息来自最近一次本机监听扫描；工作目录不可读取时会显示“无法读取”。</p>`;
  openDialog('port-detail-dialog');
}

$('#open-register').addEventListener('click', () => openDialog('register-dialog'));
$('#side-register').addEventListener('click', () => openDialog('register-dialog'));
$('#refresh').addEventListener('click', loadDashboard);
$('#retry').addEventListener('click', loadDashboard);

document.querySelectorAll('[data-close]').forEach((button) => {
  button.addEventListener('click', () => closeDialog(button.dataset.close));
});

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    state.filter = tab.dataset.filter;
    document.querySelectorAll('.tab').forEach((candidate) => {
      candidate.classList.toggle('is-active', candidate === tab);
      candidate.setAttribute('aria-selected', candidate === tab ? 'true' : 'false');
    });
    renderPorts();
  });
});

$('#search').addEventListener('input', (event) => {
  state.query = event.target.value;
  renderPorts();
});

document.addEventListener('keydown', (event) => {
  if (event.key === '/' && !event.metaKey && !event.ctrlKey && !event.altKey && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
    event.preventDefault();
    $('#search').focus();
  }
  if (event.key === 'Escape' && event.target === document.body) {
    document.querySelectorAll('dialog[open]').forEach((dialog) => dialog.close());
  }
});

$('#register-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const submit = $('#submit-register');
  const errorNode = $('#register-error');
  errorNode.hidden = true;
  try {
    const path = requireProjectPath($('#project-path').value);
    const name = $('#project-name').value.trim();
    const ports = parsePortInput($('#project-ports').value);
    if (!name) throw new Error('请填写项目名称。');
    submit.disabled = true;
    await request('/api/projects', { method: 'POST', body: JSON.stringify({ name, ports, path }) });
    closeDialog('register-dialog');
    $('#register-form').reset();
    await loadDashboard();
    showToast(`${name} 已登记，分配 ${ports.length} 个端口`);
  } catch (error) {
    errorNode.textContent = error.message;
    errorNode.hidden = false;
  } finally {
    submit.disabled = false;
  }
});

$('#validate-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const projectId = $('#validate-project-id').value;
  const port = Number($('#validate-port').value);
  const result = $('#validate-result');
  const errorNode = $('#validate-error');
  result.hidden = true;
  errorNode.hidden = true;
  try {
    const response = await request(`/api/projects/${encodeURIComponent(projectId)}/validate`, {
      method: 'POST', body: JSON.stringify({ port }),
    });
    result.className = 'validate-result is-allowed';
    result.textContent = validationSuccessMessage($('#validate-project-name').textContent, response.port ?? port);
    result.hidden = false;
  } catch (error) {
    errorNode.textContent = error.message;
    errorNode.hidden = false;
    if (error.status === 403) {
      result.className = 'validate-result is-denied';
      result.textContent = '该项目无权使用此端口';
      result.hidden = false;
    }
  }
});

document.addEventListener('click', (event) => {
  const detailButton = event.target.closest('[data-port-detail]');
  if (detailButton) openPortDetails(detailButton.dataset.portDetail);
  const validateButton = event.target.closest('[data-validate]');
  if (validateButton) openValidation(validateButton.dataset.validate);
  const removeButton = event.target.closest('[data-remove]');
  if (removeButton) {
    const project = state.projects.find((item) => String(item.id) === String(removeButton.dataset.remove));
    if (!project) return;
    state.removingProject = project;
    $('#confirm-copy').textContent = `将释放 ${project.name} 的 ${project.ports.join('、')} 端口登记。`;
    openDialog('confirm-dialog');
  }
});

$('#confirm-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const project = state.removingProject;
  if (!project) return;
  const button = $('#confirm-remove');
  button.disabled = true;
  try {
    await request(`/api/projects/${encodeURIComponent(project.id)}`, { method: 'DELETE' });
    closeDialog('confirm-dialog');
    await loadDashboard();
    showToast(`${project.name} 的登记已释放`);
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    state.removingProject = null;
    button.disabled = false;
  }
});

loadDashboard();
