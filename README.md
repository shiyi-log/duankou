# 本地端口管理器

本机使用的端口登记与监听状态面板。项目端口归属保存在用户目录中，服务停止后仍保留；未登记但正在监听的端口会标为冲突，不能分配给其他项目。

当前正式版本：`v1.0.1`。发布包和校验值见 [GitHub Releases](https://github.com/shiyi-log/duankou/releases)。

[![CI](https://github.com/shiyi-log/duankou/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/shiyi-log/duankou/actions/workflows/ci.yml)

## 功能

- 查看本机 TCP 监听端口、进程、PID、项目归属和登记状态。
- 管理器在后台每 15 秒扫描一次；面板每 5 秒读取最新快照，也可立即触发扫描。
- 只读识别 Docker 发布端口对应的 Compose 项目、服务和容器名；监听工作目录恰好是 Git 仓库根目录时，也显示项目目录名。
- 为项目登记一个或多个专属端口；端口已被占用或已分配时拒绝登记。
- 登记并绑定项目目录；检查监听进程是否归属对应项目。
- 受管启动前检查端口登记和占用情况，并监测本次启动的进程组是否监听未登记端口；可按有限次数自动保活。
- 不需要登录；服务仅绑定 `127.0.0.1`。
- 用户级注册表位于 `~/Library/Application Support/LocalPortManager/registry.json`，不会写入本仓库。

## 运行

要求 macOS、Python 3.9 或更高版本，以及系统自带的 `lsof`。

```sh
python3 server.py
```

启动时会输出本机访问地址。打开该地址即可使用面板。停止服务不会释放登记端口；从面板显式删除项目登记后才会释放。

## 端口使用约定

1. 使用前先登记端口，并把端口配置写入对应项目。
2. 项目登记必须绑定本地项目目录；受管启动会固定从该目录启动。
3. 项目只能使用自己登记的端口，不能临时改用其他项目的端口或未登记端口。
4. 对未登记的监听端口，先确认其项目归属；不要仅凭端口号结束进程。
5. 如果首选端口被占用，为项目登记另一个空闲端口后再更新配置。

面板用于本机登记、校验和审计。端口登记会在服务停止后继续保留。面板不会接管其他进程，也不会自动终止占用端口的服务。管理器的使用权校验只确认端口登记给哪个项目，不代表端口当前空闲；启动前应使用 `portctl.py check` 或受管 `run` 检查占用情况。

Docker 名称和 Git 根目录名是监听扫描得到的发现线索，面板会标为“探测发现 · 待登记”，不会自动写入项目登记或改变端口所有权。Docker 仅按运行中容器的 TCP 发布端口匹配已知代理监听；原生进程仅当工作目录恰好等于 Git 仓库根目录时才显示目录名。容器元数据不可读、目录不匹配或扫描失败时，面板保留未分配/上次成功快照并显示扫描状态，不会猜测归属或停止进程。

受管启动会将项目工作目录固定为登记目录，并监测本次启动的进程组。它不是操作系统级端口隔离：绕过管理器启动、脱离该进程组运行或在扫描间隔内短暂监听的程序，不能保证被受管启动拦截；面板会报告扫描时观察到的未登记监听。已有监听只有在其工作目录与登记目录相同，或位于登记目录下且 Git 仓库根目录恰好等于登记目录时，才可能被认作该项目。文件系统根目录和用户主目录不能用于认领监听。

## 受管启动与保活

```sh
# 查看项目编号和登记端口
python3 portctl.py status

# 登记时绑定项目目录
python3 portctl.py register --name "订单 API" --ports 8300,8301 --path "/Users/用户名/Projects/order-api"

# 检查端口属于该项目且当前没有被占用
python3 portctl.py check --project <项目编号> --ports 8300

# 受管启动，并在退出后最多重启五次
python3 portctl.py run --project <项目编号> --ports 8300 --keepalive -- python3 app.py
```

`POST /api/projects/{id}/validate` 只确认登记归属，不检查端口是否空闲。`check` 会同时检查登记归属和当前监听；`run` 启动前也会执行这两项检查。`run` 只会监测和停止本次创建的进程组；发现该组监听未登记端口时，会停止本次启动并退出，不会结束系统中的其他进程。`--keepalive` 的默认上限为五次重启。

旧登记如果没有绑定目录，可用 `python3 portctl.py bind --project <项目编号> --path "/项目目录"` 绑定后再受管启动。目录变更需先释放原登记并重新登记。

## 验证

```sh
python3 -m unittest discover -v
node --test web/tests/*.test.mjs
```

## 发布

提交到 `main` 和 Pull Request 会自动运行 Python 3.9 与最新稳定版 Python 测试，以及前端测试。正式版本使用 `v主版本.次版本.修订版本` 标签；推送标签后，发布工作流会再次运行全量测试，核对标签与 `VERSION` 一致，再创建 GitHub Release，附上源码 ZIP 和 SHA-256 校验文件。版本记录见 [CHANGELOG.md](CHANGELOG.md)。

## 本地端口管理技能

Codex 技能源文件位于 `skills/local-port-management/SKILL.md`。克隆本仓库后，可将它安装到个人技能目录：

```sh
mkdir -p "$HOME/.codex/skills/local-port-management"
cp skills/local-port-management/SKILL.md "$HOME/.codex/skills/local-port-management/SKILL.md"
```

该技能指导 Codex 在选择或更改项目端口前检查共享登记表，并遵守项目端口归属。安装技能不会自动启动端口管理器。

## API

- `GET /api/health`
- `GET /api/ports`，返回最近监听快照及 `scan.interval_seconds`、`scan.scanned_at`、`scan.next_scan_at`、`scan.scanning`、`scan.error`
- `POST /api/scan`，立即触发扫描并返回最新快照
- `GET /api/projects`
- `POST /api/projects`，JSON：`{"name":"web","ports":[5173],"path":"/项目目录"}`
- `POST /api/projects/{id}/validate`，JSON：`{"port":5173}`
- `DELETE /api/projects/{id}`

## 许可证

MIT 许可证，详见 [LICENSE](LICENSE)。
