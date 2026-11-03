# 部署指南

本文说明如何把 RieszManager 部署到一台运行 Claude Code 的 Linux 主机上，并通过 HTTPS 对外提供服务。

## 1. 组件与要求

| 组件 | 运行位置 | 说明 |
| --- | --- | --- |
| runner | 宿主机（systemd 用户服务） | 读取 `~/.claude`、浏览工作区、启动和驱动 `claude` 进程 |
| backend | Docker | Django（ASGI/uvicorn），负责登录、两步验证、API 与 WebSocket 代理 |
| frontend | Docker | 构建好的静态前端（nginx） |
| gateway | Docker | nginx 网关，监听 `RIESZ_BIND:RIESZ_PORT`（默认 `127.0.0.1:18573`） |

要求：

- Linux，systemd（用于 runner 的用户服务）。
- Claude Code 已安装并登录：在要使用的用户下执行 `claude` 能正常对话。
- Python ≥ 3.10（runner 使用）。
- Docker Engine 与 Docker Compose v2；运行 `rieszctl` 的用户需要能执行 `docker`（加入 `docker` 组后重新登录）。
- 一个 TLS 终端（Cloudflare Tunnel、Caddy、带证书的 nginx 等）。**不要**把网关端口直接暴露到公网。

runner 和 backend 容器必须以**同一个用户**（同一 uid/gid）运行：容器通过挂载目录访问 runner 的 Unix socket，socket 只对该用户可读写。

## 2. 获取代码并初始化配置

```bash
git clone <repo-url> RieszManager
cd RieszManager
./rieszctl init
```

`init` 会从 `.env.example` 生成 `.env`（权限 600），填入：

- `RIESZ_SECRET_KEY`、`RUNNER_TOKEN`：随机生成；
- `RIESZ_PASSWORD`：随机临时密码（会打印在终端上）；
- `RIESZ_UID` / `RIESZ_GID`：当前用户；
- `RUNNER_SOCKET_DIR`：`~/.local/state/riesz-manager/run`；
- `WORKSPACE_ROOT`：默认是家目录，可以用 `WORKSPACE_ROOT=/path ./rieszctl init` 指定。

然后编辑 `.env`，至少确认：

| 变量 | 说明 |
| --- | --- |
| `WORKSPACE_ROOT` | 工作区根目录，其下每个一级文件夹是一个项目 |
| `RIESZ_PUBLIC_ORIGIN` | 对外访问地址，如 `https://claude.example.com`（用于 CSRF、WebSocket 来源校验、安全 Cookie） |
| `RIESZ_USERNAME` / `RIESZ_PASSWORD` | 登录账号；建议改用 `RIESZ_PASSWORD_HASH`（见第 6 节） |
| `RIESZ_PORT` / `RIESZ_BIND` | 网关监听地址，默认只监听本机 |
| `RIESZ_TRUST_CF_HEADER` | 使用 Cloudflare Tunnel 时设为 `true`，以便按真实客户端 IP 限流 |
| `RUNNER_DEFAULT_PERMISSION_MODE` | 新会话的权限模式 |
| `RUNNER_ALLOW_BYPASS` | 是否允许在网页中切换到“跳过权限”模式 |
| `RUNNER_CHROME` | 网页端会话是否连接 Claude in Chrome：`auto`（跟随你在终端里 `/chrome` 的默认设置）、`on`、`off` |
| `OPENAI_API_KEY` | 可选，启用服务端语音转写 |

所有变量的说明见 `.env.example`。含有 `$` 的值（例如密码哈希）必须用单引号包起来。

## 3. 安装 runner

```bash
./rieszctl runner install
```

该命令会：

1. 在 `runner/.venv` 创建虚拟环境并安装依赖；
2. 写入 `~/.config/systemd/user/riesz-runner.service` 并启动；
3. 尝试执行 `loginctl enable-linger $USER`，让服务在用户注销和重启后继续运行（若没有权限，会提示手动用 sudo 执行）。

runner 默认通过登录 shell（`$SHELL -lc`）启动 `claude`，因此会话里的 Bash 命令看到的环境与你在终端里的一致；若不需要，设置 `RUNNER_LOGIN_SHELL=false`。

常用操作：

```bash
./rieszctl runner status
./rieszctl runner logs
./rieszctl runner reload      # 应用新代码或修改后的 .env：等所有托管会话空闲后再重载
./rieszctl runner restart     # 立即重启：会中断正在工作的会话
```

`reload` 会等到 runner 托管的所有 Claude 会话都空闲（没有进行中的回合、待确认的权限或排队消息）时，才结束这些空闲进程并重新加载自己；会话记录都在磁盘上，下次发送消息时自动恢复。`restart` 则立即生效，正在工作的会话会被打断，一般只在 runner 卡住时使用。

## 4. 启动容器

```bash
./rieszctl up
./rieszctl status
```

`status` 会检查 runner 的 socket、容器状态，以及 `http://127.0.0.1:$RIESZ_PORT/api/healthz`。后端的 SQLite 数据库保存在 `data/backend/`（登录记录、两步验证密钥等）。

修改 `.env` 后执行 `./rieszctl restart`（只改容器相关配置）或 `./rieszctl up`（重新构建）。

## 5. 配置 HTTPS

网关只说 HTTP，并且默认只监听 `127.0.0.1`。在它前面放一个 TLS 终端：

### 方案 A：Cloudflare Tunnel（推荐）

1. 在 Cloudflare Zero Trust 后台 → Networks → Tunnels 创建（或选择已有的）隧道，并在主机上安装 `cloudflared` 连接器。
2. 在该隧道的 **Public Hostname** 中添加一条：
   - Subdomain / Domain：你的域名；
   - Service：`HTTP`，URL：`localhost:18573`（与 `RIESZ_PORT` 一致）。
3. `.env` 中设置 `RIESZ_PUBLIC_ORIGIN=https://你的域名` 和 `RIESZ_TRUST_CF_HEADER=true`，然后 `./rieszctl restart`。

WebSocket 无需额外配置。可选：在 Cloudflare Access 中再加一层身份验证。

### 方案 B：Caddy

```caddyfile
claude.example.com {
    reverse_proxy 127.0.0.1:18573
}
```

Caddy 会自动申请证书并设置 `X-Forwarded-Proto`。

### 方案 C：已有的 nginx

```nginx
server {
    listen 443 ssl http2;
    server_name claude.example.com;
    ssl_certificate     /path/fullchain.pem;
    ssl_certificate_key /path/privkey.pem;
    client_max_body_size 210m;
    location / {
        proxy_pass http://127.0.0.1:18573;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 1d;
    }
}
```

## 6. 账号与两步验证

- **修改密码**：`./rieszctl hash-password`，把输出的 `RIESZ_PASSWORD_HASH='...'` 写入 `.env`，删除或清空 `RIESZ_PASSWORD`，然后 `./rieszctl restart`。
- **启用两步验证**：登录后打开“设置 → 安全 → 开始设置”，用验证器扫码并输入验证码；保存显示的 8 个恢复码。
- **强制两步验证**：设置 `RIESZ_REQUIRE_TOTP=true`，未设置验证器之前只能进入设置页。
- **丢失验证器**：用恢复码登录；或在主机上执行 `./rieszctl reset-totp`。
- **登出所有设备**：设置 → 设备，或 `./rieszctl revoke-sessions`。

登录失败会被限流：同一 IP 在 `RIESZ_LOGIN_LOCKOUT_MINUTES` 分钟内失败 `RIESZ_LOGIN_MAX_FAILURES` 次后暂时锁定；全局失败次数过多时所有登录都会暂缓。

## 7. 升级

```bash
git pull
./rieszctl runner install     # 更新 runner 依赖；服务已在运行时会平滑重载（不打断工作中的会话）
./rieszctl up                 # 重新构建并启动容器
```

## 8. 备份

需要备份的只有 `.env` 和 `data/backend/`。会话本身由 Claude Code 保存在 `~/.claude/projects/`；上传的附件在 `~/.local/state/riesz-manager/uploads/`（30 天后自动清理）。

## 9. 安全说明

- 任何能登录 RieszManager 的人，都能以宿主机用户的身份让 Claude 执行命令。请使用强密码并启用两步验证。
- 网关只监听本机；不要把 `RIESZ_BIND` 改成 `0.0.0.0` 后直接暴露到公网。
- runner 只监听 Unix socket（目录权限 0750，socket 0660），并校验 `RUNNER_TOKEN`。
- 项目文件以只读方式提供，路径会解析符号链接并限制在项目目录内；原始文件以 `Content-Security-Policy: sandbox` 返回，不会在本站源下执行。
- HTML 文件的渲染预览通过 `/api/view/<令牌>/<路径>` 提供：令牌是签名的只读凭证，只对一个项目有效、一小时后过期；响应带 `Content-Security-Policy: sandbox`（不含 `allow-same-origin`），页面脚本运行在匿名来源中，无法使用本站的 Cookie、存储或接口。页面可以加载外部资源，因此它能读到并外发的最多是该项目内的文件。
- 看板娘玩偶显示的套餐用量来自 runner 定期执行的 `claude -p /usage`：这是 Claude Code 的本地命令，不调用模型、不消耗额度，也不会留下会话记录。
- `RIESZ_TRUST_CF_HEADER=true` 只应在经由 Cloudflare 访问且网关只监听本机时开启，否则客户端可以伪造 IP 头绕过按 IP 的限流。

## 10. 排错

| 现象 | 处理 |
| --- | --- |
| 页面提示“宿主机运行器未连接” | `./rieszctl runner status`；确认 `RUNNER_SOCKET_DIR` 是绝对路径且与 runner 一致；确认 `RIESZ_UID/GID` 与运行 runner 的用户相同 |
| 登录后立即掉线 / CSRF 失败 | `RIESZ_PUBLIC_ORIGIN` 必须与浏览器地址栏完全一致（协议 + 域名 + 端口） |
| WebSocket 连不上 | 反向代理需要转发 `Upgrade`/`Connection` 头 |
| 新会话启动失败 | `./rieszctl runner logs`；在同一用户的终端里运行 `claude -p "hi"` 确认 Claude Code 已登录 |
| 语音按钮不可用 | 浏览器只在 HTTPS（或 localhost）下允许麦克风；未配置 `OPENAI_API_KEY` 时依赖浏览器语音识别 |
