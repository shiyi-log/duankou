export function classifyPort(port) {
  if (port.ownership === 'mismatch') return 'mismatch';
  if (port.project && port.ownership === 'unverified' && (port.status === 'conflict' || port.status === 'listening')) return 'unverified';
  if (port.status === 'conflict' || (port.status === 'listening' && !port.project)) {
    return 'unregistered';
  }
  if (port.status === 'registered' || (port.project && port.status !== 'listening')) {
    return 'reserved';
  }
  if (port.status === 'listening' && port.project) return 'active';
  return 'available';
}

export function detectedContainers(port) {
  let containers = Array.isArray(port.containers) ? port.containers : [];
  if (containers.length === 0 && port.container) containers = [port.container];
  if (containers.length === 0) {
    containers = (port.listeners || []).map((listener) => listener.container).filter(Boolean);
  }

  const unique = new Map();
  for (const container of containers) {
    if (!container || typeof container !== 'object') continue;
    const key = [container.name, container.project, container.service, container.working_dir].join('|');
    unique.set(key, container);
  }
  return [...unique.values()];
}

export function detectedProjects(port) {
  let projects = Array.isArray(port.detected_projects) ? port.detected_projects : [];
  if (projects.length === 0 && port.detected_project) projects = [port.detected_project];
  if (projects.length === 0) {
    projects = (port.listeners || []).map((listener) => listener.detected_project).filter(Boolean);
  }
  const hints = projects
    .filter((project) => project && typeof project === 'object' && project.name)
    .map((project) => ({
      name: project.name, path: project.path || '', source: project.source || '',
      service: '', containerName: '',
    }));
  for (const container of detectedContainers(port)) {
    const name = container.project || container.name || container.service;
    if (name) hints.push({
      name, path: container.working_dir || '', source: 'docker',
      service: container.service || '', containerName: container.name || '',
    });
  }
  const unique = new Map();
  for (const hint of hints) {
    const key = [hint.name, hint.path, hint.source, hint.service, hint.containerName].join('|');
    unique.set(key, hint);
  }
  return [...unique.values()];
}

export function detectedProjectLabel(port) {
  const labels = [...new Set(detectedProjects(port).map((project) => project.name))];
  return labels.join('、');
}

export function filterPorts(ports, filter = 'all', query = '') {
  const normalizedQuery = query.trim().toLowerCase();
  return ports
    .filter((port) => {
      const state = classifyPort(port);
      if (filter === 'listening' && !(port.listeners?.length > 0) && state !== 'active' && state !== 'mismatch') return false;
      if (filter === 'registered' && state !== 'reserved' && state !== 'active' && state !== 'mismatch' && state !== 'unverified') return false;
      if (filter === 'attention' && state !== 'unregistered' && state !== 'mismatch' && state !== 'unverified') return false;
      if (!normalizedQuery) return true;
      const projectValues = detectedProjects(port).flatMap((project) => [
        project.name, project.path, project.service, project.containerName,
      ]);
      return [port.port, port.protocol, port.project, port.process, port.pid, ...projectValues]
        .some((value) => String(value ?? '').toLowerCase().includes(normalizedQuery));
    })
    .sort((a, b) => Number(a.port) - Number(b.port));
}

export function stateLabel(port) {
  const state = classifyPort(port);
  return {
    active: '登记中 · 正在监听',
    reserved: '已登记 · 未启动',
    unregistered: '未登记监听',
    mismatch: '监听目录不匹配',
    unverified: '登记待核验 · 正在监听',
    available: '空闲',
  }[state];
}

export function validationSuccessMessage(projectName, port) {
  return `${projectName} 已登记端口 ${port}；启动前仍需确认端口当前空闲。`;
}

export function ownershipLabel(ownership) {
  return {
    verified: '归属已验证',
    unverified: '归属未验证',
    mismatch: '目录不匹配',
    'path-bound': '目录已绑定',
    expected: '等待进程验证',
    unregistered: '未登记',
  }[ownership] || '';
}

export function requireProjectPath(path) {
  const normalizedPath = String(path ?? '').trim();
  if (!normalizedPath) throw new Error('请填写项目目录。');
  return normalizedPath;
}

export function portDetails(port, project = null) {
  return {
    port: port.port,
    protocol: port.protocol || 'tcp',
    owner: port.project || '未分配',
    ownership: ownershipLabel(port.ownership),
    registeredPath: project?.path || (port.project ? port.path : null) || '未绑定',
    detectedProjects: detectedProjects(port).filter((hint) => hint.source !== 'docker').map((hint) => ({
      ...hint,
      path: hint.path || '无法读取',
      source: {
        git: 'Git 仓库', manifest: '项目清单', 'registered-path': '登记目录匹配',
        'package.json': '项目清单', 'pyproject.toml': '项目清单',
      }[hint.source] || hint.source || '本机探测',
    })),
    containers: detectedContainers(port).map((container) => ({
      name: container.name || '未知',
      project: container.project || '未标记',
      service: container.service || '未标记',
      workingDir: container.working_dir || '无法读取',
    })),
    listeners: (port.listeners || []).map((listener) => ({
      address: listener.address || '未知',
      family: listener.family || '未知',
      pid: listener.pid ?? '未知',
      process: listener.process || '未知',
      path: listener.path || '无法读取',
    })),
  };
}

export function parsePortInput(input) {
  const tokens = input.split(/[,\s]+/).filter(Boolean);
  if (!tokens.length || tokens.some((token) => !/^\d+$/.test(token))) {
    throw new Error('只能填写端口号，并以逗号分隔。');
  }
  const ports = [...new Set(tokens.map(Number))];
  if (ports.some((port) => port < 1 || port > 65535)) {
    throw new Error('端口号范围为 1 到 65535。');
  }
  return ports;
}

export function localizeApiError(message) {
  const text = String(message ?? '');
  let match = text.match(/^Port (\d+) is currently listening$/);
  if (match) return `端口 ${match[1]} 当前正在被监听，无法分配。`;
  match = text.match(/^Port (\d+) is already assigned to (.+)$/);
  if (match) return `端口 ${match[1]} 已分配给项目“${match[2]}”。`;
  if (text === 'A project with this name is already registered') return '项目名称已登记，请使用其他名称。';
  if (text === 'Port is not assigned to this project') return '此端口未分配给该项目。';
  if (text === 'Project name is required') return '请填写项目名称。';
  if (text === 'At least one port is required') return '请至少填写一个端口号。';
  if (text === 'A project cannot list the same port twice') return '同一项目中不能重复填写端口号。';
  if (text === 'Ports must be integers between 1 and 65535') return '端口号必须为 1 到 65535 之间的整数。';
  if (text === 'Project not found') return '找不到该项目，请刷新后重试。';
  if (text === 'Project path must be a directory') return '项目目录必须是本机已有文件夹。';
  if (text === 'Project path must exist') return '找不到该项目目录，请检查路径。';
  if (text === 'Project path is required') return '请填写项目目录。';
  if (text === 'Listener working directory does not match project path') return '监听进程的工作目录与登记的项目目录不一致。';
  if (text === 'Listener working directory is unavailable') return '无法读取监听进程的工作目录，请确认进程仍在运行。';
  if (text === 'The manager\'s own port assignment cannot be removed') return '端口台账服务的端口登记不能释放。';
  if (text === 'Could not inspect local TCP listeners') return '无法读取当前 TCP 监听信息。';
  if (text === 'Port registry is unreadable; refusing to overwrite it') return '端口登记文件无法读取，为保护现有登记，已停止写入。';
  if (text === 'Port registry has an invalid format; refusing to overwrite it') return '端口登记文件格式无效，为保护现有登记，已停止写入。';
  return text;
}
