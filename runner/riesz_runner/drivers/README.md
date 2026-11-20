# 接入一个新的 harness

Riesz Manager 用同一个网页界面驱动多种编程 agent（Claude Code、Codex、DeepSeek Harness、Gemini CLI、Antigravity……）。
每个 agent 只通过它**官方提供的程序化接口**接入，不改它的内部流程。本文是新增一个 harness 时必须走完的流程；
其中大部分步骤由代码强制：漏了哪一步，导入时会抛 `TypeError`，或者 `tests/test_conformance.py` 会失败并指出是哪一步。

## 整体结构

```
网页 ──HTTP/WebSocket──▶ runner/server.py ──▶ hub.py（Claude Code：process.py）
                                         └──▶ harnesses.py ──▶ drivers/<harness>.py ──stdio JSON-RPC──▶ agent 进程
```

- **驱动（driver）**：`drivers/base.py` 里的 `Driver` 子类，一个实例对应一个会话。它把 agent 的协议翻译成网页统一的
  条目（items）、状态快照（snapshot）和事件（items、reset、state、permission、result、notice、exit）。
- **登记项（`harnesses.py` 里的 `HarnessSpec`）**：关于这个 harness 的全部事实。所有字段都是必填的，构造时会检查。
- **前端不用改**：工具名、标签、模式、命令、恢复命令、能否分叉等都由 `/v1/harnesses` 从登记项读出来。
  （可选：在 `frontend/src/theme/components.css` 给 `.rz-tag.harness-<id>` 配一个颜色。）

## 流程（逐项）

### 1. 选接口
优先用 agent 官方的程序化接口：JSON-RPC app-server（Codex）、SDK 协议（dsh）、ACP（Gemini CLI、Antigravity，
可直接继承 `drivers/acp.py` 的 `AcpDriver`）。不要解析它的终端输出，也不要改它的代码或私有文件
（交接历史时写入它**自己会读**的记录格式除外，见第 6 步）。

### 2. 写驱动 `drivers/<id>.py`
- 设 `harness = "<id>"`（必须等于登记项的 id），声明 `capabilities = Capabilities(...)`：它不能做的事情
  （排队还是并入当前轮、中断是否重启进程、能否「本会话都允许」、能否问问题、换模型何时生效、能否换 API……）
  都在这里说清楚，前端据此显示或隐藏功能。
- 必须实现：`start()`（新建或恢复会话，恢复时把历史读成 items 并 `reset()`）、`send()`、`interrupt()`、`stop()`、
  `modes()`；按需实现 `models()`、`set_model/set_effort/set_mode`、`native_commands()`。
- 构造函数接受一个关键字参数传可执行文件路径（登记项的 `arg`），并设好默认模式 `self.mode`。
- 权限请求统一走 `self.ask(entry)`：`toolName` 尽量用网页认识的名字（Bash、Read、Edit、Write、ApplyPatch、
  WebSearch、WebFetch、AskUserQuestion……），`input` 用对应的形状，回答由 `answer_permission()` 交回驱动。
- 工具调用映射成 `tool_block(id, name, input, result)`；认不出的工具用它自己的名字，按通用格式显示。

### 3. 权限模式和档位
- `modes()` 里每个 `Mode` 都必须带档位 `tier`（`modes.py`：readonly / ask / edits / auto / full），否则构造时报错。
- 登记项的 `modes` 是一张表：**五个档位每个都要写上这个 harness 用哪个模式**（没有完全对应的，填最接近、
  偏保守的那个，并在旁边注释原因）。换工具时按「原来模式的档位 → 目标的这张表」选模式。
- 一致性测试会检查：表里的模式都存在；每个模式自己的档位，在表里映射回同一档位的模式。
- 每个 harness 都必须有一个「全部自动通过」（full 档）的模式：它自己有就用它的；没有就在模式列表里加上
  `drivers.base.BYPASS`（`rz-bypass`），驱动让它跑在自己最宽松的模式上，剩下的权限请求由 `Driver.ask` 直接批准（提问不算）。
  ACP 驱动已经自动这样做。full 档的模式下，驱动收到的权限请求一律自动批准。

### 4. 斜杠命令
- 通用命令表在 `drivers/base.py` 的 `UNIVERSAL_COMMANDS`（目前有 `/compact`）。登记项的 `commands`
  **必须给表里每一个语义填写这个 harness 的做法**，写法只有四种：
  - `"/<命令>"`：它自己的斜杠命令，原样发过去；
  - `"offered:/<命令>"`：同上，但只在它报告的命令列表里有这条时才发（ACP 这类会报命令列表的 agent）；
  - `"method:<方法名>"`：调用驱动的这个方法（必须存在）；
  - `"none:<原因>"`：它没有对应功能，原因会显示给用户。
- **填之前必须实测或查官方文档确认行为**（比如 Gemini CLI 的 `/compress` 只在终端界面里有，ACP 模式下没有，
  所以它填的是 `none:`）。在登记项旁边用注释写明依据。
- 它自己的其他命令由 `native_commands()` 列出，会和通用命令一起出现在输入框的命令提示里。
- 以后新增一个通用命令：在 `UNIVERSAL_COMMANDS` 加一项，然后每个登记项都必须补上它，不补就无法导入。

### 5. API 线路（`routes.py`、`credentials.py`）
- 在 `routes.ROUTES` 里列出它能用的线路，订阅放最前面（有订阅就优先用订阅）。
  只通过官方配置入口接入：环境变量、官方配置项、官方 provider 配置；订阅只在它自己的程序里使用。
- 在 `harnesses._env()` / `_make()` 里按线路给驱动传 key 和参数；需要的话在 `routes.py` 加该 harness 的设置函数。
- key 不能出现在它运行的命令能看到的环境里：实测确认（跑一条 `env | grep -c KEY`），不行就用它的设置关掉。
- 登记项的 `native_provider` 必须是它某条线路的服务商。

### 6. 交接（换工具继续）和分叉
- `handoff`：别的会话转到它这里时，历史怎么交给它，取 `handoff.STRATEGIES` 中的一种：
  `inject-responses`（Codex 式注入）、`first-message`（写不进去时，附在第一条消息前）。
  写入它的记录文件之前，要确认写进去的记录在它**之后继续追加**时仍然有效（dsh 就是反例：手写的日志第一次能读，
  它追加系统消息后就读不了了，所以 dsh 用 `first-message`）。
  新的写法：在 `handoff.py` 加写入函数、在 `STRATEGIES` 加一项、在 `hub.handoff()` 加一个分支。
- `fork`：`native`（它自己能分叉，在 `Harnesses.fork()` 里实现）或 `copy`（复制历史开新会话，走交接的流程）。
- `resume_command`：终端里按会话 ID 接着用的命令（`{id}` 代表会话 ID），没有就填 `None`。

### 7. Riesz Manager 的工具
Riesz Manager 自己的工具（定时唤醒、给别的会话发消息、开新会话……）写在 `tools.py`，每个 harness 都要能用。
runner 把它们做成一个 stdio MCP 服务器（`tool_server.py`，只做转发），由 harness **自己**按它官方的 MCP 配置启动。
工具在 runner 里执行；是哪个会话在调用，runner 从调用方的进程往上找到它启动的 harness 进程来确定，不用给 harness 传会话 ID 或密钥。
- 登记项的 `tools` 填 `"mcp"`，驱动实现 `mount_tools(server)`：返回它启动时要加的东西（参数、配置条目、会话参数），
  并在 `start()` 里用上 `self.tools`。实在挂不上才填 `"none:<原因>"`。
- 实测确认：它能列出 `mcp__riesz__*` 工具、调用后定时记在**它自己的**会话上（`tests/test_tools.py` 有各家的挂法检查）。
  注意它自己的同类工具（比如 dsh 自带定时）不会经过 Riesz Manager。
- 以后加工具：在 `tools.TOOLS` 加一个 `Tool(...)`，所有 harness 自动都有。

### 8. 登记
在 `harnesses.py` 的 `HARNESSES` 里加一个 `HarnessSpec`，在 `handoff.TARGETS` 里加上 id。
`HarnessSpec` 构造时会检查两张表（commands、modes）、方法是否存在、交接方式是否已知、工具是否挂上。

### 9. 测试
- 驱动单元测试：用录下来的真实协议样本（`tests/tools/record.py` 录、`scrub.py` 去掉隐私）或脚本化的假 agent
  （参考 `tests/fake_rpc_server.py`、`tests/fake_acp_agent.py`），覆盖：一轮对话、工具调用、权限（允许、拒绝、本会话）、
  中断、恢复历史、换模式。测试模块里要出现驱动类名（一致性测试据此确认有测试）。
- 需要真账号的端到端测试放进 `tests/test_live.py`，默认跳过。
- 运行全部测试：`cd runner && .venv/bin/python -m unittest discover -s tests -t .`，`test_conformance` 必须通过。

### 10. 文档
- 使用指南 `docs/_src/{zh,en}/guide.html`：在「其他工具」一节写上它的名字、权限模式、和 Claude Code 的不同之处。
- 部署文档 `docs/_src/{zh,en}/deploy.html`：怎么安装（出现它的可执行文件名）、怎么登录、要注意的条款或系统设置。
- 改完运行 `python3 docs/_src/build.py` 生成网页。一致性测试会检查这两个文档里有没有提到它。

## 现有 harness 速查

| id | 接口 | 交接方式 | 分叉 | /compact | Riesz 工具怎么挂 |
|---|---|---|---|---|---|
| claude | stream-json（process.py） | 写 Claude Code 会话记录 | 原生 | 原生 /compact | `--mcp-config` |
| codex | `codex app-server`（JSON-RPC） | `thread/inject_items` | 原生 `thread/fork`（在临时进程里分叉） | `thread/compact/start` | `-c mcp_servers.riesz.*` |
| dsh | `dsh --profile sdk`（SDK 协议） | 第一条消息前 | 复制 | SDK 原生 /compact | `--patch`（`insert` 一条 dsh-mcp-client） |
| gemini | `gemini --acp`（ACP） | 第一条消息前 | 复制 | 不支持（ACP 模式没有） | `session/new` 的 `mcpServers` |
| antigravity | `agy acp`（ACP） | 第一条消息前 | 复制 | `/compact`（它报告了才用） | `session/new` 的 `mcpServers` |
