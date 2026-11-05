/**
 * 以串行方式定时读取扫描快照，避免请求尚未完成时再次访问本机服务。
 */
export function createSnapshotPoller(readSnapshot, options = {}) {
  if (typeof readSnapshot !== 'function') throw new TypeError('必须提供快照读取函数');
  const intervalMs = options.intervalMs ?? 5000;
  const setTimeoutFn = options.setTimeoutFn ?? setTimeout;
  const clearTimeoutFn = options.clearTimeoutFn ?? clearTimeout;
  let active = false;
  let timerId = null;
  let inFlight = null;
  let pendingAction = null;
  let pendingRefresh = null;

  function schedule() {
    if (!active || timerId !== null) return;
    timerId = setTimeoutFn(() => {
      timerId = null;
      // 定时触发没有调用方等待，避免失败 Promise 形成未处理拒绝。
      void execute().catch(() => {});
    }, intervalMs);
  }

  function execute(action = readSnapshot) {
    if (!active) return Promise.resolve(undefined);
    if (inFlight) return inFlight;
    let result;
    try {
      result = action();
    } catch (error) {
      result = Promise.reject(error);
    }
    inFlight = Promise.resolve(result).finally(() => {
      inFlight = null;
      if (pendingAction) {
        const pending = pendingAction;
        pendingAction = null;
        pendingRefresh = null;
        execute(pending.action).then(pending.resolve, pending.reject);
      } else {
        schedule();
      }
    });
    return inFlight;
  }

  return {
    start() {
      active = true;
      return execute();
    },
    refresh(action) {
      active = true;
      if (timerId !== null) {
        clearTimeoutFn(timerId);
        timerId = null;
      }
      if (inFlight) {
        if (!action) return inFlight;
        if (!pendingRefresh) {
          pendingRefresh = new Promise((resolve, reject) => {
            pendingAction = { action, resolve, reject };
          });
        }
        return pendingRefresh;
      }
      return execute(action ?? readSnapshot);
    },
    stop() {
      active = false;
      if (pendingAction) pendingAction.resolve(undefined);
      pendingAction = null;
      pendingRefresh = null;
      if (timerId !== null) clearTimeoutFn(timerId);
      timerId = null;
    },
    get isRunning() {
      return inFlight !== null;
    },
  };
}

export function scanStatusText(scan = {}) {
  if (scan.error) return '扫描失败 · 自动重试，保留上次结果';
  const seconds = Number(scan.interval_seconds) || 15;
  return scan.scanning ? `正在主动扫描 · 每 ${seconds} 秒自动扫描` : `每 ${seconds} 秒自动扫描`;
}

export function mergeScanSnapshot(previous = {}, report = {}) {
  const scan = report.scan || previous.scan || {};
  const preservePrevious = Boolean(scan.error && previous.loaded);
  return {
    loaded: true,
    ports: preservePrevious ? previous.ports : (report.ports || []),
    summary: preservePrevious ? previous.summary : (report.summary || {}),
    scan,
  };
}
