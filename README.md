# RieszManager

在浏览器里查看和操控一台机器上的所有 Claude Code 会话。界面风格类似 Claude 客户端，支持桌面和手机（可安装为 PWA）。

<p align="center"><img src="frontend/public/icons/icon-512.png" width="120" alt=""></p>

## 功能

- **会话总览**：读取本机 `~/.claude` 中的全部会话，按项目（工作区下的一级文件夹）归类；实时显示哪些会话正在运行、在等待确认。
- **完整的对话视图**：Markdown、代码高亮、数学公式、工具调用卡片（命令输出、文件 diff、待办清单、子代理、网页抓取、MCP 工具等）、思考块、上下文压缩记录、图片。
- **在网页里继续对话**：随时发送消息（运行中发送会排队，在合适时机送达）；`Esc` 中断当前任务并立即发送排队消息；`/` 打开斜杠命令列表；`@` 引用项目文件；`↑`/`↓` 调出历史输入；`Shift+Tab` 切换权限模式；切换模型和推理强度（默认沿用 Claude Code 自己的设置）。
- **交互式确认**：工具权限请求、`AskUserQuestion` 提问、计划审批都在网页中直接处理。
- **附件与语音**：粘贴/拖放图片和文件；语音输入（配置 OpenAI 兼容的转写接口，或使用浏览器自带的语音识别）。
- **终端里的会话**：实时旁观，也可以直接发消息——会话空闲时会自动转到这里继续（终端里的那个 Claude 进程会退出）；正在工作时消息先排队，等它空闲后自动送达。也可以分叉一份副本继续。
- **项目面板**：查看项目下的会话和文件树（只读，带 git 状态标记），在线预览代码、Markdown、图片、PDF，HTML 文件会直接渲染成网页（在隔离的沙箱中运行）。
- **安全**：没有注册；账号密码写在 `.env`；支持 TOTP 两步验证与恢复码、登录限流、设备管理与登录记录。
- **手机**：从屏幕左缘右滑打开会话列表，右缘左滑打开项目面板；支持安装到主屏幕。

## 架构

```
浏览器 ──https──▶ (TLS 终端：Cloudflare Tunnel / Caddy / …)
                         │
                         ▼  127.0.0.1:RIESZ_PORT
               ┌──────── gateway (nginx) ────────┐      Docker
               │   /            /api, /ws        │
               ▼                ▼                │
          frontend          backend (Django ASGI)│
         (静态 SPA)          登录 / TOTP / 代理  │
                                 │ Unix socket   │
               └─────────────────┼───────────────┘
                                 ▼
                    runner（宿主机，systemd 用户服务）
                    读取 ~/.claude，浏览工作区文件，
                    以 stream-json 协议驱动 claude 进程
```

Claude Code 必须运行在宿主机的真实环境中（你的工具链、凭据、文件权限），所以与会话打交道的部分是一个很小的宿主机守护进程 **runner**；它只监听一个仅当前用户可访问的 Unix socket，并要求令牌认证。前端、后端和网关都在 Docker 中运行，后端是唯一能访问 runner 的组件。

| 目录 | 内容 |
| --- | --- |
| `runner/` | 宿主机守护进程（Python + aiohttp） |
| `backend/` | Django 后端：认证、两步验证、REST/WebSocket 代理、语音转写 |
| `frontend/` | Vite + React + MUI 前端，PWA |
| `nginx/` | 网关配置 |
| `rieszctl` | 安装与运维脚本 |

## 快速开始

需要：Linux、已登录的 Claude Code（`claude` 可用）、Python ≥ 3.10、Docker 与 Docker Compose v2。

```bash
git clone <repo-url> RieszManager && cd RieszManager
./rieszctl init              # 生成 .env（随机密钥和临时密码）
$EDITOR .env                 # 至少设置 WORKSPACE_ROOT 和 RIESZ_PUBLIC_ORIGIN
./rieszctl runner install    # 安装并启动宿主机 runner
./rieszctl up                # 构建并启动容器
./rieszctl status            # 检查各组件
```

然后在 TLS 反向代理（推荐 Cloudflare Tunnel）里把你的域名指向 `http://127.0.0.1:18573`。完整步骤、HTTPS 方案、升级与排错见 [docs/deployment.md](docs/deployment.md)。

## 常用命令

```bash
./rieszctl hash-password     # 生成密码哈希，写入 RIESZ_PASSWORD_HASH
./rieszctl reset-totp        # 丢失验证器时关闭两步验证
./rieszctl revoke-sessions   # 让所有设备重新登录
./rieszctl runner logs       # 查看 runner 日志
./rieszctl logs backend      # 查看容器日志
```

## 开发

```bash
# runner
cd runner && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m riesz_runner --env-file ../.env
# backend（RIESZ_ENV_FILE 指向一个开发用 .env，其中 RUNNER_CONTAINER_SOCKET 指向 runner 的 socket）
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
RIESZ_ENV_FILE=../.env.dev .venv/bin/python manage.py migrate
RIESZ_ENV_FILE=../.env.dev .venv/bin/uvicorn riesz.asgi:application --port 18601
# frontend（/api 与 /ws 代理到 RIESZ_DEV_BACKEND，默认 http://127.0.0.1:18601）
cd frontend && npm install && npm run dev
```

## 许可

私有项目，暂未指定许可协议。
