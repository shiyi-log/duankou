import test from 'node:test';
import assert from 'node:assert/strict';
import { classifyPort, filterPorts, localizeApiError, ownershipLabel, parsePortInput, portDetails, requireProjectPath, validationSuccessMessage } from '../ports.js';

test('classifies listening ports without a project as unregistered', () => {
  assert.equal(classifyPort({ port: 5173, status: 'listening', project: null }), 'unregistered');
});

test('classifies registered ports without a listener as reserved', () => {
  assert.equal(classifyPort({ port: 8010, status: 'registered', project: 'api-service' }), 'reserved');
});

test('未绑定目录的登记端口监听时标记为待核验', () => {
  assert.equal(classifyPort({ port: 8011, status: 'conflict', project: 'legacy', ownership: 'unverified', listeners: [{ pid: 22 }] }), 'unverified');
});

test('待核验监听端口仍属于已登记项目筛选', () => {
  const ports = [
    { port: 8011, status: 'conflict', project: 'legacy', ownership: 'unverified', listeners: [{ pid: 22 }] },
  ];
  assert.deepEqual(filterPorts(ports, 'registered').map(({ port }) => port), [8011]);
});

test('目录不匹配的监听端口仍属于已登记项目筛选', () => {
  const ports = [
    { port: 8012, status: 'conflict', project: 'orders', ownership: 'mismatch', listeners: [{ pid: 23 }] },
  ];
  assert.deepEqual(filterPorts(ports, 'registered').map(({ port }) => port), [8012]);
});

test('the attention filter returns only unregistered listeners', () => {
  const ports = [
    { port: 3000, status: 'listening', project: 'web' },
    { port: 5173, status: 'listening', project: null },
    { port: 8010, status: 'registered', project: 'api-service' },
  ];

  assert.deepEqual(filterPorts(ports, 'attention').map(({ port }) => port), [5173]);
});

test('port rows sort numerically, not lexically', () => {
  const ports = [
    { port: 9000, status: 'listening', project: 'one' },
    { port: 8080, status: 'listening', project: 'two' },
  ];

  assert.deepEqual(filterPorts(ports, 'all').map(({ port }) => port), [8080, 9000]);
});

test('port input accepts a comma-separated list and removes duplicates', () => {
  assert.deepEqual(parsePortInput('8300, 8301,8300'), [8300, 8301]);
});

test('port input rejects values outside the TCP port range', () => {
  assert.throws(() => parsePortInput('80, 65536'), /1 到 65535/);
});

test('port input rejects non-numeric values', () => {
  assert.throws(() => parsePortInput('http,8300'), /只能填写端口号/);
});

test('translates port-conflict API errors into Chinese', () => {
  assert.equal(localizeApiError('Port 8300 is currently listening'), '端口 8300 当前正在被监听，无法分配。');
});

test('translates ownership validation errors into Chinese', () => {
  assert.equal(localizeApiError('Port is not assigned to this project'), '此端口未分配给该项目。');
});

test('marks an unverified project directory ownership in Chinese', () => {
  assert.equal(ownershipLabel('unverified'), '归属未验证');
  assert.equal(ownershipLabel('expected'), '等待进程验证');
});

test('identifies a listener running outside its assigned project directory', () => {
  assert.equal(classifyPort({ port: 8300, status: 'conflict', project: 'api', ownership: 'mismatch' }), 'mismatch');
});

test('the attention filter includes ownership mismatches', () => {
  const ports = [
    { port: 8300, status: 'conflict', project: 'api', ownership: 'mismatch' },
    { port: 8301, status: 'conflict', project: null, ownership: 'unregistered' },
    { port: 8302, status: 'listening', project: 'web', ownership: 'verified' },
    { port: 8303, status: 'conflict', project: 'legacy', ownership: 'unverified', listeners: [{ pid: 3 }] },
  ];
  assert.deepEqual(filterPorts(ports, 'attention').map(({ port }) => port), [8300, 8301, 8303]);
});

test('the listening filter includes unregistered and mismatched listeners', () => {
  const ports = [
    { port: 8300, status: 'conflict', project: null, listeners: [{ pid: 1 }] },
    { port: 8301, status: 'conflict', project: 'api', ownership: 'mismatch', listeners: [{ pid: 2 }] },
    { port: 8302, status: 'listening', project: 'web', ownership: 'verified', listeners: [{ pid: 3 }] },
    { port: 8303, status: 'registered', project: 'worker', listeners: [] },
  ];
  assert.deepEqual(filterPorts(ports, 'listening').map(({ port }) => port), [8300, 8301, 8302]);
});

test('project path is required before registration', () => {
  assert.throws(() => requireProjectPath('  '), /请填写项目目录/);
  assert.equal(requireProjectPath(' /Users/me/work '), '/Users/me/work');
});

test('使用权校验提示不把登记状态误报为端口空闲', () => {
  assert.equal(validationSuccessMessage('orders', 8300), 'orders 已登记端口 8300；启动前仍需确认端口当前空闲。');
});

test('port details include listener address, family, process path, and assigned directory', () => {
  const details = portDetails({
    port: 8300,
    protocol: 'tcp',
    status: 'listening',
    ownership: 'verified',
    project: 'orders',
    path: '/work/orders',
    listeners: [{ address: '127.0.0.1:8300', family: 'IPv4', pid: 412, process: 'python', path: '/work/orders' }],
  }, { name: 'orders', path: '/work/orders' });

  assert.equal(details.owner, 'orders');
  assert.equal(details.registeredPath, '/work/orders');
  assert.deepEqual(details.listeners[0], {
    address: '127.0.0.1:8300', family: 'IPv4', pid: 412, process: 'python', path: '/work/orders',
  });
});
