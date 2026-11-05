import test from 'node:test';
import assert from 'node:assert/strict';
import { createSnapshotPoller, mergeScanSnapshot, scanStatusText } from '../scan-poller.js';

function createFakeTimers() {
  let nextId = 0;
  const timers = new Map();
  return {
    setTimeout(callback, delay) {
      const id = ++nextId;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    runNext() {
      const [id, timer] = timers.entries().next().value || [];
      if (!timer) throw new Error('没有待运行的定时器');
      timers.delete(id);
      timer.callback();
      return timer.delay;
    },
    get size() {
      return timers.size;
    },
  };
}

test('快照请求串行轮询，失败后仍按周期重试', async () => {
  const timers = createFakeTimers();
  const pending = [];
  let calls = 0;
  const poller = createSnapshotPoller(() => {
    calls += 1;
    return new Promise((resolve, reject) => pending.push({ resolve, reject }));
  }, {
    intervalMs: 5000,
    setTimeoutFn: timers.setTimeout,
    clearTimeoutFn: timers.clearTimeout,
  });

  const first = poller.start();
  assert.equal(calls, 1);
  assert.equal(timers.size, 0);
  assert.equal(poller.refresh(), first);
  assert.equal(calls, 1);
  pending[0].reject(new Error('快照暂不可用'));
  await assert.rejects(first, /快照暂不可用/);
  assert.equal(timers.size, 1);
  assert.equal(timers.runNext(), 5000);
  assert.equal(calls, 2);
  pending[1].resolve({ ports: [] });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(timers.size, 1);

  poller.stop();
  assert.equal(timers.size, 0);
});

test('手动扫描等待正在读取的快照，扫描期间不会再轮询', async () => {
  const timers = createFakeTimers();
  let resolveSnapshot;
  let resolveScan;
  const calls = [];
  const poller = createSnapshotPoller(() => {
    calls.push('快照');
    return new Promise((resolve) => { resolveSnapshot = resolve; });
  }, { setTimeoutFn: timers.setTimeout, clearTimeoutFn: timers.clearTimeout });
  const first = poller.start();
  const manual = poller.refresh(() => {
    calls.push('主动扫描');
    return new Promise((resolve) => { resolveScan = resolve; });
  });
  assert.deepEqual(calls, ['快照']);
  resolveSnapshot();
  await first;
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, ['快照', '主动扫描']);
  assert.equal(timers.size, 0);
  resolveScan();
  await manual;
  assert.equal(timers.size, 1);
  poller.stop();
});

test('停止轮询时未完成请求不会重新安排定时器', async () => {
  const timers = createFakeTimers();
  let complete;
  const poller = createSnapshotPoller(() => new Promise((resolve) => { complete = resolve; }), {
    setTimeoutFn: timers.setTimeout,
    clearTimeoutFn: timers.clearTimeout,
  });
  const request = poller.start();
  poller.stop();
  complete();
  await request;
  assert.equal(timers.size, 0);
});

test('空闲时手动刷新执行主动扫描并返回读取结果', async () => {
  const timers = createFakeTimers();
  const calls = [];
  const poller = createSnapshotPoller(async () => {
    calls.push('快照');
  }, { setTimeoutFn: timers.setTimeout, clearTimeoutFn: timers.clearTimeout });
  await poller.start();
  const result = await poller.refresh(async () => {
    calls.push('主动扫描');
    return { scanned: true };
  });
  assert.deepEqual(calls, ['快照', '主动扫描']);
  assert.deepEqual(result, { scanned: true });
  poller.stop();
});

test('排队的主动扫描立即完成时仍返回扫描结果', async () => {
  const timers = createFakeTimers();
  let resolveSnapshot;
  const poller = createSnapshotPoller(() => new Promise((resolve) => { resolveSnapshot = resolve; }), {
    setTimeoutFn: timers.setTimeout,
    clearTimeoutFn: timers.clearTimeout,
  });
  const first = poller.start();
  const manual = poller.refresh(async () => ({ scanned: true }));
  resolveSnapshot();
  await first;
  assert.deepEqual(await manual, { scanned: true });
  poller.stop();
});

test('扫描文案区分周期、进行中和保留旧结果的错误状态', () => {
  assert.equal(scanStatusText({ interval_seconds: 15 }), '每 15 秒自动扫描');
  assert.equal(scanStatusText({ interval_seconds: 20, scanning: true }), '正在主动扫描 · 每 20 秒自动扫描');
  assert.equal(scanStatusText({ interval_seconds: 15, error: '扫描不可用' }), '扫描失败 · 自动重试，保留上次结果');
});

test('扫描报错时保留最后成功快照，成功扫描后替换数据', () => {
  const previous = { loaded: true, ports: [{ port: 8300 }], summary: { listening: 1 }, scan: {} };
  const failed = mergeScanSnapshot(previous, { ports: [], summary: {}, scan: { error: 'lsof 不可用' } });
  assert.deepEqual(failed.ports, previous.ports);
  assert.deepEqual(failed.summary, previous.summary);
  assert.equal(failed.scan.error, 'lsof 不可用');

  const succeeded = mergeScanSnapshot(failed, { ports: [{ port: 8301 }], summary: { listening: 1 }, scan: {} });
  assert.deepEqual(succeeded.ports, [{ port: 8301 }]);
  assert.equal(succeeded.scan.error, undefined);
});
