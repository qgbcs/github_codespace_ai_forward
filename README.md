# 用命令行访问远程 Codespace 里的 AI

通过本机 MQTT-PTY 桥接，在 Windows 命令行里直接向远程 GitHub Codespace
中的 **GitHub Copilot CLI** 提问。无需开放远程端口、无需 ssh。

## 链路

```
本机 codespace_ai.py
  └─ HTTP POST  http://192.168.1.100:1188/r=ai_bridge.run(...)
       └─ 本机常驻窗口 pty_client_mqtt.py -t co        (1188 控制口)
            └─ MQTT 公共 broker (~13 个广播)
                 └─ 远端 codespace: server_mqtt.py → 常驻 PTY → copilot -p
```

关键点：所有 MQTT/broker 连接由**本机一个常驻 PTY 窗口**持有，脚本只发一次
HTTP，不重连 broker。该窗口必须由人在真终端启动（PTY 要求 TTY，脚本无法自启）。

## 前提

1. 本机已开着常驻窗口（窗口同时是人工监控屏，每条命令在其中可见）：

   ```powershell
   C:\QGB\miniforge3\python.exe client/pty_client_mqtt.py -t co
   ```

   工作目录为 `c:\test\github\multi_mqtt`（即桥接默认监听 `1188`）。
2. 远程 codespace 在线，且装有 GitHub Copilot CLI（本机实测 `1.0.92`，
   路径 `/usr/local/bin/copilot`，codespace 内用 GitHub 凭证免登录）。
3. 本机运行 Python 3（建议 `C:\QGB\miniforge3\python.exe`，脚本只用标准库）。

## 用法

```powershell
$py = "C:\QGB\miniforge3\python.exe"

# 1) 探活：看 PTY 是否连着、broker 在线数
& $py C:\test\py\codespace_ai.py status

# 2) 查实际生效模型（权威值取自 copilot 的遥测事件，而非模型自报）
& $py C:\test\py\codespace_ai.py model

# 3) 提问（问答默认超时 300s）
& $py C:\test\py\codespace_ai.py ask "你是什么模型？用一句话回答"
"用 python 写个快排" | & $py C:\test\py\codespace_ai.py ask

# 4) 指定远程仓库目录 / 指定模型 / 看原始 JSONL
& $py C:\test\py\codespace_ai.py ask "总结这个项目" --cwd /workspaces/build_xime_home
& $py C:\test\py\codespace_ai.py ask "hi" --model auto --raw

# 5) 透传任意 shell
& $py C:\test\py\codespace_ai.py run "copilot --version; uname -a"
```

## 长时 agent 任务（Allow All + 网页可见）

`ask` 是一问一答；让 Copilot 自主跑十几分钟的分析/改造任务用 `agent` 系列。
交互模式（`copilot -i`）才支持 `--remote` 网页实时会话，因此用远端
**detached tmux** 承载，本地经同一座 HTTP 桥监控/操控，断网也不杀任务：

```powershell
$py = "C:\QGB\miniforge3\python.exe"
$cwd = "/workspaces/build_xime_home/xime"

# 启动（首次会自动应答"信任文件夹=记住"、跳过终端快捷键弹窗；需远端 tmux，
# 没有就加 --install-tmux）
& $py C:\test\py\codespace_ai.py agent start "对当前仓库做只读分析并输出 Markdown 总结；禁止改文件" --cwd $cwd --name xime-analysis

# 看实时状态/窗格尾部；或轮询到本轮结束并自动抽取总结
& $py C:\test\py\codespace_ai.py agent status
& $py C:\test\py\codespace_ai.py agent follow --cwd $cwd -o C:\test\py\xime-analysis.md

# 只取该目录最近一次会话的最终总结（从 ~/.copilot/session-state/events.jsonl 解析）
& $py C:\test\py\codespace_ai.py agent report --cwd $cwd -o C:\test\py\report.md

# 中途追加指令（等价在网页里发消息）/ 发原始键 / 停止
& $py C:\test\py\codespace_ai.py agent send "再补充一下 CI 部分"
& $py C:\test\py\codespace_ai.py agent send --key C-c
& $py C:\test\py\codespace_ai.py agent stop
```

要点：

- 权限等价于截图里 Manual permissions 拉满：`--allow-all --no-ask-user`，
  无人值守不弹确认；只读约束写在提示词里（实测遥测回传
  `filesModified: 0, linesAdded: 0`，模型确实没动文件）。
- 报告不是截屏扒字，而是解析远端会话语义日志 `events.jsonl`
  （`assistant.message / final_answer`），拿到的是完整 Markdown 原文。
- 远端 tmux 会话名固定 `csai`，产物在 `/tmp/csai/`；`agent start --force`
  可重启；任务在 nohup/tmux 里，与 PTY 生命周期解耦。

### 网页可见的三条路（按本环境实测）

| 方式 | 本环境结果 |
|---|---|
| `--remote` 实时会话（github 上 `OWNER/REPO/tasks/<id>`、VS Code、手机可看可操控） | **被组织策略拦截**：`Remote sessions are not enabled. Contact your organization administrator`。需 GitHub 组织管理员开启 Copilot CLI remote sessions 策略，脚本无需改。 |
| `copilot -p --share-gist` / `gh gist create` | **403 Resource not accessible by integration**：codespace 内置令牌（`ghu_`）无 gist 写权限。 |
| 用户自己的 classic PAT（带 gist scope）调 `POST /gists` | **可行**。已用 qgbcs 的 PAT 把"总结 + 46 次只读命令完整时间线"发成 secret gist：<https://gist.github.com/qgbcs/5170da2fb58697ef618407077a9eadf8> |

后台 gist 任务子命令（令牌权限具备时）：

```powershell
& $py C:\test\py\codespace_ai.py agent gist "任务提示..." --cwd $cwd
& $py C:\test\py\codespace_ai.py agent gist-status   # 完成后打印 gist 链接
```

> 安全提醒：xime 仓库 `git remote` URL 里内嵌了一个 classic PAT（cjqbj 的），
> 本次发布 gist 用的是你另行提供的 qgbcs PAT。令牌出现在了远端进程命令行/
> 本机监控窗口缓冲里，建议任务结束后到 GitHub Settings → Developer settings
> 轮换这两个 token，并改用 git credential helper 存放。

## 网页端（推荐）：命令调用 + 实时同步，不用 tmux

`webui.py` 是零依赖（Python 标准库）的本地网页控制台：直接以**非交互命令**
`copilot -p ... --allow-all --output-format json` 驱动远端 agent（不经 tmux
TUI），JSONL 事件流经同一座 MQTT-PTY 桥增量拉回，浏览器实时同步显示：

- AI 调用的每条命令（工具名/描述/命令原文，含运行中 spinner）
- 命令的 **stdout/stderr 实时回显**、退出码（绿/红）
- 助手思考过程（折叠）与最终回复（Markdown 渲染，流式增量）
- 路由模型（实测长任务 = gpt-6-luna）、任务耗时
- 一键停止（杀远端进程组，rc=137）
- 左侧会话历史：所有任务落盘 `web_sessions/<id>/`（events.jsonl + meta），
  点击即可回放；**后端有守护轮询线程**，浏览器关闭/刷新甚至重启 webui
  都不影响长任务继续记录，重启后自动追上

一键启动（自动开浏览器，地址 <http://127.0.0.1:1189>，端口占用自动顺延）：

```powershell
C:\test\py\start_webui.bat
# 或: C:\QGB\miniforge3\python.exe C:\test\py\webui.py
```

界面顶栏可改远端工作目录（cwd）和模型（auto / gpt-6-luna /
mai-code-1.1-flash），状态灯实时反映桥接连接（红=断联，按前述人工恢复步骤
处理，页面不做无效重连轰炸）。

API（可供其他客户端复用）：

| 接口 | 说明 |
|---|---|
| `GET /api/status` | 桥接探活 |
| `POST /api/ask` | `{prompt,cwd,model}` 启动远端任务，返回 id |
| `GET /api/poll?id=&offset=N` | 按字节游标增量取事件（数据源为本地落盘文件） |
| `POST /api/stop` | 停止任务 |
| `GET /api/sessions` / `/api/session/<id>/events` | 历史索引 / 回放 |

> 关于 Allow All：`--allow-all-tools` 只管工具确认，**不含路径/URL 权限**，
> 含 `$PWD` 之类的复合命令在非交互模式会被拒
> （`Permission denied because no interactive user response was available`）。
> 网页端与 CLI 都已改用 `--allow-all`（= tools + paths + urls 全开，
> 等价 TUI 的 Allow All）。

桥接地址可用 `--bridge http://x:1188` 或环境变量 `CODESPACE_AI_BRIDGE` 覆盖。

输出说明：

- `[model]` 本次会话 CLI 实际选用的模型、路由方式与可用模型列表；
- `[answer]` 最终回答（解析 JSONL 中 `final_answer` 阶段内容得到）；
- `[usage]` 末尾用量检查点（若事件里带 token/credit 字段）。

## 实测结论：远程 AI 是什么模型？

- CLI 遥测事件 `session.auto_mode_resolved` 显示
  `chosenModel = mai-code-1.1-flash`，`routingMethod = auto_v2`，
  当前账号/实例下 `availableModels` 仅有该模型；`model.call_start`
  事件同样记录为 `mai-code-1.1-flash`。
- 模型自身的回答是 “Auto mode selects the model dynamically; I don't have
  a fixed single model name/version.” —— 它并不知道自己的部署别名，
  **以 CLI 遥测事件为准**。
- 远程环境：GitHub Codespace（Ubuntu 22.04 / kernel 6.8 azure），
  用户 `qgbcs`，仓库 `cjqbcs/build_xime_home`。

## 断联恢复（重要约定）

codespace 网页长时间无响应会休眠/断联，导致 PTY 会话结束。此时脚本**不会
反复重连**，而是退出码 `3` 并打印恢复提示。处理步骤：

1. 重新打开 codespace 网页，确认远程实例已唤醒、在线；
2. 在本机真终端重新启动常驻窗口：
   `C:\QGB\miniforge3\python.exe client/pty_client_mqtt.py -t co`
3. 重新执行命令（`status` 验证）。

判断依据：`ai_bridge.status()` 中 `attached=false` 或 `end_reason != null`，
或本机到 1188 口的 HTTP 直接连不通。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 参数错误 / 人为中断 |
| 3 | 桥接/PTY/codespace 断联，需按上面步骤人工恢复 |
| 4 | 远端命令或 copilot 自身失败（如未登录，需在 codespace 内 `copilot login`） |
| 5 | PTY 一直忙（上一条命令未结束，可稍后再试） |

## 实现备注

- copilot 非交互模式：`copilot -p <问题> -s --allow-all-tools
  --output-format json --no-color`。`-p` 跑完即退；JSONL 事件流含
  `session.auto_mode_resolved / model.call_start / assistant.message_delta /
  assistant.message / session.usage_checkpoint / result` 等类型。
- 问题字符串经 POSIX 单引号转义后下发 PTY，避免注入与引号问题。
- 安全提示：`--allow-all-tools` 意味着模型可在 codespace 内非交互执行命令；
  该 codespace 是独立开发容器，仍建议只让它操作仓库目录，可用 `--cwd` 限定。
