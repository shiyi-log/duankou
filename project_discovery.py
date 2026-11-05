"""根据监听进程的工作目录，安全推断本机 Git 项目展示名称。"""

import subprocess
from pathlib import Path


def _canonical_path(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError, TypeError):
        return None


def _git_root(cwd):
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return _canonical_path(result.stdout.strip())


def enrich_listeners_with_projects(listeners):
    """只提示 cwd 恰为 Git 根目录的项目，不修改监听的登记归属。"""
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    filesystem_root = Path("/").resolve()
    root_cache = {}
    enriched = []

    for listener in listeners:
        result = dict(listener)
        containers = listener.get("containers")
        if listener.get("container") or (isinstance(containers, list) and containers):
            enriched.append(result)
            continue

        cwd = _canonical_path(listener.get("path"))
        if not cwd or cwd in (filesystem_root, home):
            enriched.append(result)
            continue

        cache_key = str(cwd)
        if cache_key not in root_cache:
            root_cache[cache_key] = _git_root(cwd)
        git_root = root_cache[cache_key]
        if git_root == cwd and git_root not in (filesystem_root, home):
            result["detected_project"] = {
                "name": git_root.name,
                "path": str(git_root),
                "source": "git",
            }
        enriched.append(result)

    return enriched
