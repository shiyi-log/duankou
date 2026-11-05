import test from 'node:test';
import assert from 'node:assert/strict';

function element() {
  const classes = new Set();
  return {
    children: [], listeners: {}, hidden: false, textContent: '',
    classList: {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      toggle(name, force) {
        const include = force ?? !classes.has(name);
        if (include) classes.add(name);
        else classes.delete(name);
      },
      contains(name) { return classes.has(name); },
    },
    append(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; },
    setAttribute() {},
    querySelector() { return this.label || (this.label = element()); },
    addEventListener(name, callback) { this.listeners[name] = callback; },
  };
}

test('面板定时读取快照，按钮主动扫描，连接失败后保留表格', async () => {
  const original = { document: globalThis.document, fetch: globalThis.fetch, setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  const nodes = new Map();
  const node = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, element());
    return nodes.get(selector);
  };
  const timers = new Map();
  const calls = [];
  let nextTimer = 0;
  let offline = false;
  let projectFailures = 2;
  let projects = [{ id: 'p1', name: '测试项目', ports: [8300] }];
  let currentPort = 8300;
  const report = (port = 8300) => ({
    ports: [{ port, protocol: 'tcp', project: '测试项目', status: 'listening', ownership: 'verified' }],
    summary: { registered: 1, listening: 1, conflicts: 0 },
    scan: { interval_seconds: 15, scanned_at: '2026-11-06T10:20:30+08:00', scanning: false, error: null },
  });
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  globalThis.document = { querySelector: node, querySelectorAll: () => [], createElement: element, addEventListener() {} };
  globalThis.setTimeout = (callback, delay) => { timers.set(++nextTimer, { callback, delay }); return nextTimer; };
  globalThis.clearTimeout = (id) => timers.delete(id);
  globalThis.fetch = async (path, options = {}) => {
    calls.push({ path, method: options.method || 'GET' });
    if (offline) throw new Error('本机服务不可用');
    let payload;
    if (path === '/api/health') payload = { ok: true };
    if (path === '/api/projects') {
      if (projectFailures > 0) {
        projectFailures -= 1;
        throw new Error('项目列表暂不可用');
      }
      payload = { projects };
    }
    if (path === '/api/ports') payload = report(currentPort);
    if (path === '/api/scan') payload = report(8301);
    return { ok: true, status: 200, json: async () => payload };
  };

  try {
    await import('../app.js');
    await settle();
    assert.equal(node('#projects-status').textContent, '项目列表读取失败 · 自动重试');
    assert.equal(node('#port-rows').children.length, 1);
    assert.equal(node('#service-status').classList.contains('is-offline'), false);

    projects = [{ id: 'p2', name: '恢复项目', ports: [8302] }];
    const [startupTimerId, startupTimer] = [...timers].find(([, item]) => item.delay === 5000);
    timers.delete(startupTimerId);
    startupTimer.callback();
    await settle();
    assert.equal(node('#projects-status').textContent, '');
    assert.match(node('#project-list').children[0].innerHTML, /恢复项目/);
    assert.doesNotMatch(node('#project-list').children[0].innerHTML, /测试项目/);
    assert.equal(node('#scan-status').textContent, '每 15 秒自动扫描');
    assert.match(node('#last-updated').textContent, /^最近扫描 /);

    projects = [{ id: 'p3', name: '外部新增项目', ports: [8303] }];
    currentPort = 8303;
    const beforePoll = calls.length;
    const [timerId, timer] = [...timers].find(([, item]) => item.delay === 5000);
    timers.delete(timerId);
    timer.callback();
    await settle();
    assert.deepEqual(calls.slice(beforePoll).sort((a, b) => a.path.localeCompare(b.path)), [
      { path: '/api/ports', method: 'GET' },
      { path: '/api/projects', method: 'GET' },
    ]);
    assert.match(node('#project-list').children[0].innerHTML, /外部新增项目/);
    assert.doesNotMatch(node('#project-list').children[0].innerHTML, /恢复项目/);

    projectFailures = 1;
    currentPort = 8304;
    const [failureTimerId, failureTimer] = [...timers].find(([, item]) => item.delay === 5000);
    timers.delete(failureTimerId);
    failureTimer.callback();
    await settle();
    assert.match(node('#port-rows').children[0].innerHTML, /8304/);
    assert.equal(node('#projects-status').textContent, '项目列表读取失败 · 自动重试');
    assert.match(node('#project-list').children[0].innerHTML, /外部新增项目/);
    assert.equal(node('#service-status').classList.contains('is-offline'), false);

    projects = [{ id: 'p4', name: '最新项目列表', ports: [8305] }];
    currentPort = 8305;
    const [recoveryTimerId, recoveryTimer] = [...timers].find(([, item]) => item.delay === 5000);
    timers.delete(recoveryTimerId);
    recoveryTimer.callback();
    await settle();
    assert.equal(node('#projects-status').textContent, '');
    assert.match(node('#project-list').children[0].innerHTML, /最新项目列表/);
    assert.doesNotMatch(node('#project-list').children[0].innerHTML, /外部新增项目/);

    offline = false;
    await node('#refresh').listeners.click();
    assert.ok(calls.some((call) => call.path === '/api/scan' && call.method === 'POST'));
    assert.equal(node('#port-rows').children.length, 1);
    assert.match(node('#port-rows').children[0].innerHTML, /8301/);

    offline = true;
    const [retryId, retry] = [...timers].find(([, item]) => item.delay === 5000);
    timers.delete(retryId);
    retry.callback();
    await settle();
    assert.equal(node('#scan-status').textContent, '快照读取失败 · 自动重试');
    assert.match(node('#port-rows').children[0].innerHTML, /8301/);
  } finally {
    Object.assign(globalThis, original);
  }
});
