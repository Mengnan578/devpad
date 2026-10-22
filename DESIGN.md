# devpad — 设计文档

> 把「打开终端 + 启动项目」两件事变成一条命令，日志留在你自己的终端窗口里。
> 面向 macOS 开发者，npm 分发，同时服务人类用户和 AI agent。

---

## 1. 要解决的问题

不是"启动项目"——那有很多办法。真正的痛点有三个层次：

1. **命令长**。每天几十次重启前后端，每次都要复述一条长命令，或者去翻历史。
2. **AI 启动的项目看不见日志**。agent 跑起来的服务日志进了它的内部缓冲区，我需要的是
   **在我自己的终端窗口里能滚动、能复制、能 Cmd+F 的日志**。这是核心诉求，不是附赠功能。
3. **窗口要自己开**。我在 VS Code 里写代码，服务跑在独立的 iTerm 窗口里。

所以 devpad 的验收标准只有一条：

> `devpad up` 之后，屏幕上出现按依赖顺序排好的终端窗口，每个窗口里跑着一个服务，
> 服务崩了窗口**不关**，日志还在，能选中、能复制。

其余一切都是实现细节。

---

## 2. 选型：为什么是「CLI + 薄适配层」

三个候选，逐个排除：

### 插件 —— 否

- **绑定单一宿主**。要分享给别人，而别人用 Claude Code / Cursor / 终端 / Zed，插件只覆盖一小撮。
- **把开窗塞进宿主沙箱**，正好撞上权限墙。实测：在这个 agent 会话的沙箱里
  `tell application "iTerm" ... create window` 直接 `-10004 权限违例`。

### 纯 skills —— 否

skill 是 prompt 级指令；它能可靠约束的只有 **AI 的行为**，无法保证 **一个窗口被打开**。
而"窗口被打开"是唯一的验收标准。用 skill 去保证 OS 行为，是在错误的层解决问题。

### CLI + 薄适配层 —— 是

| 能力 | 归属 | 理由 |
|---|---|---|
| 短命令、依赖编排、就绪探测、日志留存 | **CLI 核心** | 纯逻辑，跨环境复用 |
| **在你的屏幕上开窗** | **OS / 终端模拟器** | 需要 Apple Event，必须由 CLI 直接对 OS 做 |
| AI 何时调用、如何汇报 | **适配层（skill）** | 编排与翻译，不含 OS 操作 |

关键收益：**一份核心，多宿主适配**。skill 和未来的 VS Code 扩展都调用同一个 CLI，
不重复实现逻辑；新增一种终端只需新增一个 driver。

---

## 3. 架构

```
devpad/
├── package.json               # npm 包，零运行时依赖，ESM
├── bin/devpad.mjs             # CLI 入口
├── src/
│   ├── cli.mjs                # 子命令路由、参数解析、退出码
│   ├── config.mjs             # 读 devpad.yaml（兼容 devctl.yaml），校验，依赖拓扑
│   ├── services.mjs           # 服务生命周期：起/停/重启/就绪/进程组所有权
│   ├── state.mjs              # .devpad/run/*.json、.devpad/logs/*.log
│   ├── ports.mjs              # lsof 端口占用、清理
│   ├── detect.mjs             # 项目探测：生成 devpad.yaml 草案
│   └── drivers/               # ★ 新增的核心：终端驱动
│       ├── index.mjs          # 驱动注册表 + 自动选择 + 统一接口
│       ├── iterm.mjs          # AppleScript，macOS 主力
│       ├── apple-terminal.mjs # Terminal.app，零安装兜底
│       ├── tmux.mjs           # session 持久，关窗不断
│       ├── vscode.mjs         # VS Code 集成终端
│       └── direct.mjs         # 前台运行，无窗口（CI / agent 自用）
├── skills/devpad/SKILL.md             # agent 适配层
├── reference/                 # 字段全表、驱动矩阵、排错
└── test/                      # 单元 + 端到端（含驱动 dry-run）
```

### 驱动接口

```js
// 每个 driver 导出同一组契约
export default {
  name: "iterm",
  // 不弹授权框的可用性判断：只做只读探测
  async available() { /* 返回 {ok, reason, needsPermission} */ },
  // 开一个可见终端，在里面跑 cmd，返回可寻址的句柄
  async spawn({ title, cwd, cmd, hold, env }) { /* → { driver, windowId, sessionId, pid } */ },
  // 关闭该句柄对应的窗口（并确保进程组被回收）
  async close(handle) { /* → boolean */ },
  // 只打印将要执行的动作，供测试与 `--dry-run`
  plan({ title, cwd, cmd, hold }) { /* → string[] */ },
};
```

`plan()` 是一等公民：**驱动可以无副作用地被测试**。没有它，终端驱动只能靠手动点窗户验证，
这在要分享给别人的项目里不可接受。

### 命令面

```bash
devpad up                  # 启动全部，日志进 .devpad/logs（无人值守）
devpad up --terminal       # ★ 每个服务开一个可见终端窗口
devpad up api --terminal   # 只给后端开窗口
devpad restart api         # 重启一个，其余不动
devpad down                # 停服务并关闭它开的窗口
devpad status              # 谁在跑、哪个窗口、哪个端口、起了多久
devpad logs -f api         # 在当���终端跟随（无窗口场景）
devpad attach api          # 在当前终端前台运行一个服务
devpad detect              # 探测项目，生成 devpad.yaml 草案
devpad doctor              # 环境/路径/端口/docker/驱动权限 自检
devpad kill 8081           # 释放端口
```

---

## 4. 窗口生命周期（最易翻车处）

### 4.1 服务崩了窗口必须留下

直接 `iTerm write text "npm run dev"` 的问题是：进程退出，会话就回到提示符或直接关闭，
你正要看的报错没了。所以窗口里真正执行的是一个包装脚本：

```sh
#!/bin/sh
# devpad runner —— 不要直接编辑，由 devpad 生成
cd "$DEVPAD_CWD" || exit 1
printf '\n\033[1m── devpad: %s ──\033[0m\n' "$DEVPAD_TITLE"
"$DEVPAD_SHELL" -lc "$DEVPAD_CMD"
code=$?
printf '\n\033[1m── devpad: %s exited with code %s ──\033[0m\n' "$DEVPAD_TITLE" "$code"
if [ "$DEVPAD_HOLD" = "1" ]; then
  printf 'Press Enter to close this window... '
  read -r _
fi
```

- 退出码**原样打印**，一眼看出是 0 还是崩在 3。
- `hold` 默认 1，窗口留住；`--no-hold` 或 `terminal.hold: false` 可关掉。
- 用 `$DEVPAD_SHELL -lc` 而非直接执行，保证拿到你完整的 shell 环境（nvm / pyenv / conda）——
  **这是"在 App 图标启动的终端里命令找不到"这类幽灵问题的根因**，必须一开始就处理对。

### 4.2 `down` 要能关掉它开的窗

`spawn` 返回的句柄落进 `.devpad/run/<svc>.json`：

```json
{ "driver": "iterm", "tty": "/dev/ttys004", "pid": 51234, "pgid": 51234 }
```

- **iTerm**：句柄是 `tty`（`id` 在 3.x 上不可靠，见第 5 节）。关窗时遍历窗口按 `tty`/`id` 匹配。
- **Terminal.app**：句柄是 `custom title`，因为 `do script` 的 tab 引用跨调用失效。
- **tmux**：句柄是 session 名，天生可寻址，最干净。
- **command-file**：**没有窗口句柄**。只能按 PID 反查并结束进程，窗口本身请你手动关——
  这个能力缺口必须在 `status` 里显式显示（例如 `window: -`），不能假装支持。

`down` 的顺序是**先信号、后关窗**：对进程组发 SIGTERM → 等 `stop_timeout` → SIGKILL →
再让 driver 关闭窗口。反过来的话，窗口没了但进程还活着，你会得到一个占着 8081 的孤儿，
下次启动报"端口被占用"却找不到凶手。

### 4.3 不用 AppleScript 的备选通道

AppleScript 有权限依赖，所以备选通道必须在设计里就留好：

| 通道 | 机制 | 优点 | 代价 |
|---|---|---|---|
| AppleScript | `tell application "iTerm"` | 能拿到 `tty` 句柄，能精确关闭；能设标题和配色 | 需要自动化授权（首次弹一次） |
| `.command` + `open` | 生成临时 `.command`，`open -a Terminal` 启动 | **不走授权框**，最稳，几乎不会失败 | 拿不到窗口句柄；关闭只能按 PID 反查 |
| tmux | `tmux new-session -d` + 开窗 attach | 关窗不断进程；session 名即服务名，天然好关；支持 `capture-pane` 取日志 | 依赖 tmux（本机未装）；多一层 |

**devpad 同时实现这三条**，`doctor` 报告哪条可用，`driver: auto` 按
「用户偏好 → 授权状态 → 最快可用」选择。这是"能分享给别人"的关键：别人的机器上授权状态
不可预测，一条道走到黑的工具在别人机器上会直接不可用。

---

## 5. 权限模型（实测修正版）

本机实测（iTerm 3.7.4，macOS），结果和直觉相反，必须写清楚：

| 操作 | 结果 | 说明 |
|---|---|---|
| `tell application "iTerm" to get version` | ✅ 返回 3.7.4 | **假信号** |
| `tell application "iTerm" to get count of windows` | ❌ `-10004 权限违例` | 同上一次运行 |
| `tell application "iTerm"` → `create window` / `write text` | ❌ `-10004 权限违例` | 需要自动化授权 |

**为什么 `get version` 能过而 `count of windows` 不能**：`version` 由 macOS 的通用
AppleScript 幕布应答，**根本不进 iTerm 的脚本层**；任何涉及窗口/会话的语句才真正进入 App。
所以：

> **`available()` 绝不能用 `get version` 探测**，它永远 PASS。
> 诚实的只读探测是窗口级读取（`count of windows`），它同样需要授权，但**不产生副作用**。

由此得到几条硬性设计约束：

1. 探测**只做只读**（窗口计数），绝不为了探测而开窗。
2. 首次真正开窗时弹一次授权框——这是**不可避免且符合预期**的，README 要提前说明。
3. `doctor` 必须把 `-10004` 翻译成人话：
   「系统设置 → 隐私与安全性 → 自动化 → 允许 <你的终端> 控制 iTerm2」，而不是抛原始错误。
4. `driver: auto` 在 iTerm 不可用时**退化到 `command-file` 通道**：
   `open -a Terminal foo.command` 走的是 LaunchServices，**不发 Apple Event、不需要自动化授权**，
   功能仍在，代价是拿不到窗口句柄（关闭只能按 PID 反查）。
5. 授权被拒后**不能报"机器坏了"**，要明确说出"是权限问题、去哪修、或者用哪条退化路径"。

### 对 agent 场景的直接含义（已实测修正）

**关键更正**：早期在 agent 会话里观察到 `-10004 权限违例`，我一度判断这是 macOS TCC 自动化授权。
**这个判断是错的。** 在放开宿主沙箱后，同一个 `osascript` 语句立刻成功，三个通道全部可用：

| 通道 | 放开沙箱前 | 放开沙箱后 |
|---|---|---|
| `tell application "iTerm"` → 读窗口、建窗、写文本 | ❌ -10004 | ✅ 成功 |
| `tell application "Terminal"` → `do script` | ❌ -10004 | ✅ 成功 |
| `open -a Terminal foo.command` | ❌ 静默无效 | ✅ 成功 |

所以拦截者是 **agent 宿主自己的沙箱**（它禁止了发往其他 App 的 Apple Event），
不是系统隐私授权。含义：

- **终端用户在普通 shell 里运行 CLI** → 不受影响，正常授权/正常弹框。
- **agent 替用户开窗** → 取决于宿主是否允许 Apple Event。DSH 需要 `danger-full-access`；
  其他客户端各有自己的策略。这必须写进 README，且 `devpad doctor` 要能检测并给出
  「你的 agent 宿主禁止跨 App 自动化，请改为自己运行 `devpad up --terminal`」这类明确指引。
- 设计上不能假设 Apple Event 一定可用：`command-file` 通道必须始终作为兜底存在。

### 已实测通过的完整链路

在本机（iTerm 3.7.4 / Terminal 2.15）真实执行，非模拟：

| 步骤 | 结果 | 证据 |
|---|---|---|
| iTerm 建窗并拿到句柄 | ✅ | `tty=/dev/ttys005` |
| 会话就绪轮询 | ✅ | 等 `is processing = false` 后再写入 |
| 服务在其中执行 | ✅ | 窗口内出现 `pretending to fail now...` |
| 登录 shell 生效 | ✅ | `node`→`~/.nvm/...`、`python3`→miniforge、`SHELL=/bin/zsh` |
| 退出码可见 | ✅ | `── exited with code 7 ──` |
| hold 留住窗口 | ✅ | `Press Enter to close this window...` |
| 按 tty 关闭窗口 | ✅ | 窗口数 1 → 0 |
| Terminal.app 通道 | ✅ | 按 custom title 关闭成功 |
| `.command + open` 通道 | ✅ | 窗口内输出与退出码完整 |

### 三个必须写进驱动的实现细节（都踩过坑）

1. **`write text` 必须等会话就绪。** 刚 `create window` 完立刻写，命令会被**静默丢弃**：
   窗口开出、什么也不做、按 close-on-exit 配置直接消失。看起来像驱动坏了，其实是时序。
   驱动必须轮询 `is processing of session = false` 再写入。
2. **iTerm 没有可用的 `sessions of window` 集合**（`-1728`），`id of window` 也不可靠。
   正确取法是 `current session of window i`，并且**每次访问都包 `try`**——某些窗口没有
   current session，会直接中断整个脚本。
3. **Terminal.app 在 `/System/Applications/Utilities/`**，只查 `/Applications` 和
   `/System/Applications` 会误报"未安装"。

### iTerm 的窗口句柄：不能用 `id`

iTerm 3.x 的 window **不暴露可用的 `id`**（`id of w` 在 3.7.4 上不可靠）。
稳定句柄是**会话的 `tty` 路径**（如 `/dev/ttys005`）：

- 开窗后从 `current session` 读 `tty`，落进 `.devpad/run/<svc>.json`；
- 关窗时按索引遍历 `current session of window i`，匹配 `tty` 后关闭；
- Terminal.app 更弱：`do script` 返回的 tab 引用**跨 osascript 调用即失效**，
  只能靠我们设置的 `custom title` 反查。这就是为什么标题模板 `{project} · {service}`
  是功能的一部分，不是装饰。

### VS Code：CLI 做不到（已验证）

`code --help` 里**没有**任何"开集成终端并执行命令"的选项
（`--locate-shell-integration-path` 只是打印一个路径）。驱动 VS Code 终端必须用扩展 API。
所以它不是 CLI 的一个 driver，而是**复用同一核心的独立交付物**（第 9 节第 6 步）。
文档里不假装 CLI 能做这件事。

---

## 6. 配置

沿用已被 113 条测试覆盖的服务模型，只新增 `terminal` 段与少量键，避免重新发明：

```yaml
version: 1

terminal:
  driver: auto            # auto | iterm | apple-terminal | tmux | vscode | direct | command-file
  hold: true              # 进程退出后留住窗口
  title: "{project} · {service}"   # iTerm/Terminal 支持设标题
  reuse: false            # true 则复用同名窗口而非每次新建

services:
  infra:
    kind: docker
    dir: ./infra
    compose: docker-compose.yml
    services: [mysql, redis]
    ready: { tcp: 3306, timeout: 120 }
    terminal: false       # 容器不需要单开窗口，日志用 devpad logs infra
  api:
    kind: proc
    dir: ./server
    command: uvicorn app.main:app --reload --port 8080
    env: { PYTHONUNBUFFERED: "1" }
    port: 8080
    ready: { http: "http://127.0.0.1:8080/health", timeout: 90 }
    depends_on: [infra]
  app:
    kind: proc
    dir: ./mobile
    command: npx react-native start
    port: 8081
    depends_on: [api]
    ready: { tcp: 8081, timeout: 180 }
    terminal: { hold: false }   # Metro 支持热键，退出即关
```

服务级 `terminal: false | {hold, title, driver}` 覆盖全局。`devpad detect` 生成的草案里
`docker` 类服务默认 `terminal: false`——给数据库单开窗口是噪音。

---

## 7. 分发（npm）

```bash
npx @mengnan578/devpad up --terminal     # 零安装试用
npm i -g @mengnan578/devpad              # 常用则全局
```

- **零运行时依赖**，纯 ESM，`engines: { node: ">=18" }`。
- `bin` 暴露 `devpad`。安装**不执行 postinstall 脚本**（供应链安全，且许多 CI 环境禁它）。
- 首启不弹授权、不写 `$HOME` 以外目录；所有状态在项目内 `.devpad/`。
- 之后可选加 `brew` formula 与 VS Code 扩展，两者都复用同一核心，无逻辑分叉。

包名 `devpad` 需在发布前确认 npm 上可用；`devdp`/`devpad-cli` 作为备选。

---

## 8. 复用与新增

| 部分 | 来源 | 状态 |
|---|---|---|
| 进程组所有权判定（pgid / 可执行身份 / 启动时间三重证据） | 现有实现 | 直接复用，是这块最有价值的部分 |
| 僵尸进程回收、崩溃立即上报真实退出码 | 现有实现 | 直接复用 |
| 端口守卫、`ports`/`kill`、陈旧状态自愈 | 现有实现 | 直接复用 |
| 依赖拓扑、`doctor`、`rebuild --dry-run` | 现有实现 | 直接复用 |
| `detect`（RN/Python/compose 探测，含 `/health` 升级探测） | 现有实现 | 直接复用 |
| 113 条测试（76 动态 + 37 静态） | 现有实现 | 直接复用并扩展驱动相关用例 |
| **终端驱动（iterm / apple-terminal / tmux / vscode / command-file）** | 新增 | ★ 核心新工作 |
| **窗口生命周期与句柄管理** | 新增 | ★ |
| **npm 打包、README、skill 适配层** | 新增 | ★ |

---

## 9. 实施顺序：先证明唯一的不确定点

整个方案只有一个**我无法在当前 agent 沙箱里验证**的技术点：**iTerm 窗口的真实开法与关闭**
（沙箱禁止发 Apple Event）。其余部分都有现成实现和测试兜底。所以顺序必须是先打穿这一点。

### 第 1 步已完成并**真机验证通过**：PoC 探针（`poc/terminal_probe.py`）

```bash
cd devpad
python3 poc/terminal_probe.py                 # 阶段一：只读侦察
python3 poc/terminal_probe.py --spawn         # 阶段二：真的开窗
python3 poc/terminal_probe.py --close         # 阶段三：验证关闭路径
```

三个通道（iTerm / Terminal.app / `.command + open`）**全部实测可用**，
包括建窗、执行、日志留存、退出码、hold、关闭。证据见第 5 节的链路表。

已执行的真实验证：

| 验证项 | 结果 |
|---|---|
| iTerm 建窗 + 拿 tty 句柄 | ✅ `/dev/ttys005` |
| 服务经**登录 shell**执行，`node`/`python3` 解析到 nvm 与 miniforge | ✅ |
| 退出码原样打印（`exit 7` → `exited with code 7`） | ✅ |
| `hold` 生效：停在 `Press Enter to close this window...` | ✅ |
| 按 tty 关闭窗口（1 → 0） | ✅ |
| Terminal.app 通道 + 按 custom title 关闭 | ✅ |
| `.command + open` 通道（无 Apple Event） | ✅ |
| 五个 driver 与五个 closer 均已注册 | ✅ |

**唯一仍需注意的前提**：agent 替用户开窗需要宿主允许跨 App Apple Event（本会话需要
`danger-full-access`）。终端用户自己运行则无此限制。

### 第 2–5 步：已完成（选型 A：Python 核心 + npm 薄壳）

| 步骤 | 状态 | 交付物 |
|---|---|---|
| 2. 驱动层 | ✅ | `scripts/terminals.py`：5 个 driver、`plan()`、46 条无副作用测试 |
| 3. 打包改造 | ✅ | `bin/devpad.mjs`（Node 薄壳）+ `package.json`，零运行时依赖 |
| 4. `up --terminal` 接线 | ✅ | 编排/`down`/`status` 全部打通，真机验证 |
| 5. npm 发布准备 | ✅ | `README.md`、`skills/devpad/SKILL.md`、`doctor` 驱动矩阵、`references/` |
| 6. VS Code 扩展 | ⏸ 后置 | CLI 做不到（已验证），需扩展 API |

**为什么是 A（Python 核心 + npm 薄壳）**：这套实现里最值钱的是经过真机验证的边界处理
——进程组三重身份判定、僵尸回收、端口守卫、窗口时序。重写成 JS 等于把它们重新踩一遍。
npm 层只负责定位 `python3` 并转发退出码，因此 `npx @mengnan578/devpad` 依旧零安装。

### 真机验收结果（全部通过）

| 验收项 | 结果 |
|---|---|
| `npx @mengnan578/devpad up --terminal` 每个服务一个窗口 | ✅ 2 服务 2 窗口 |
| 窗口内实时日志 | ✅ `bundling... tick 1/2/3` 滚动 |
| 进程崩溃后窗口留住 + 退出码 | ✅ 停在 `Press Enter`，`exited with code 7` |
| 登录 shell（nvm / miniforge 生效） | ✅ |
| `restart api --terminal` 不动 web 的窗口 | ✅ web 的 tty/pid 未变，运行时长继续 |
| `down` 关窗且不留孤儿 | ✅ 窗口 1→0，端口释放，无残留进程 |
| 从 npm 真实安装后可用 | ✅ `npm install <tgz>` 后 `up --terminal` 成功 |
| 未授权时退化到 `command-file` | ✅ 该通道无需 Apple Event |

### 交付清单

```
devpad/
├── bin/devpad.mjs           Node 薄壳：定位 python3、转发参数与退出码
├── scripts/devpad.py        引擎（编排、所有权、端口、窗口接线）
├── scripts/terminals.py     ★ 终端驱动：iterm / apple-terminal / tmux / command-file / direct
├── scripts/detect.py        项目探测，生成配置草案
├── tests/                   39 静态 + 76 端到端 + 46 驱动 = 161 项
├── poc/terminal_probe.py    在任意机器上验证开窗/关窗通道
├── skills/devpad/SKILL.md           agent 适配层
├── references/              字段全表、驱动矩阵、排错
└── README.md                面向市场
```

第 1 步不通过，后面全部重设计——所以先做它。

---

## 10. 验收标准

- [ ] `devpad up --terminal` 在 iTerm 里按依赖顺序开出窗口，每个窗口里有对应服务的实时输出
- [ ] 服务崩溃时窗口**不关**，屏幕上留有退出码和完整日志，可选中复制
- [ ] `devpad restart api` 只重开后端的窗口，Metro 的窗口和进程完全不受影响
- [ ] `devpad down` 关掉所有由它开的窗口，且不留任何占端口的孤儿进程
- [ ] 在未授权的机器上 `driver: auto` 能退化到 `.command + open` 通道并仍然可用
- [ ] `devpad doctor` 能说清"哪个驱动可用、为什么不可用、怎么修"
- [ ] 三条驱动通道都有 `plan()` 单元测试，CI 里不需要真实终端
