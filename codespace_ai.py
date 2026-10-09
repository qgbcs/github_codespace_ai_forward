#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
codespace_ai.py
===============
经本机 MQTT-PTY 桥接，用命令行访问远程 GitHub Codespace 里的 AI
(GitHub Copilot CLI)，支持问答 / 查模型 / 透传 shell。

链路:
  本机本脚本 --HTTP--> 本机常驻 pty_client_mqtt.py(1188 口)
       --MQTT--> 远端 server_mqtt.py --PTY--> 远端 `copilot -p ...`

前提: 本机有一个开着的常驻 PTY 监控窗口(由人类在真终端启动, AI 无法自行启动):
  C:\\QGB\\miniforge3\\python.exe client/pty_client_mqtt.py -t co
  桥接默认地址 http://192.168.1.100:1188 (可用 --bridge 或环境变量
  CODESPACE_AI_BRIDGE 修改)。

断开策略(重要):
  桥接不可达 / PTY 已结束(end_reason) / 远端 copilot 长时间无响应导致
  codespace 网页休眠断联时, 本脚本【不做反复重连】, 直接退出码 3 并提示
  人工恢复: 重新打开 codespace 网页唤醒实例 -> 在本机重启 PTY 窗口。

用法:
  python codespace_ai.py status                       # 探活
  python codespace_ai.py model                        # 查询实际生效模型(权威:CLI 遥测)
  python codespace_ai.py ask "你是什么模型? 一句话回答" # 提问
  echo "问题" | python codespace_ai.py ask            # 从 stdin 读问题
  python codespace_ai.py run "uname -a"               # 透传远端 shell
  python codespace_ai.py ask "重构这段代码" --cwd /workspaces/xxx --model auto

长时 agent 任务(Allow All, detached tmux, 可网页查看):
  python codespace_ai.py agent start "只读分析当前仓库并总结" --cwd /workspaces/xime
  python codespace_ai.py agent status                 # 看进度
  python codespace_ai.py agent follow --cwd /workspaces/xime -o report.md
  python codespace_ai.py agent report --cwd /workspaces/xime
  python codespace_ai.py agent send "再补充 CI 部分"  # 中途追加指令
  python codespace_ai.py agent stop
  python codespace_ai.py agent gist "任务" --cwd /workspaces/xime   # --share-gist 备选
  python codespace_ai.py agent gist-status

注: --remote 网页实时会话需要 GitHub 组织管理员开启 remote sessions 策略;
    --share-gist 需要令牌有 gist 写权限(codespace 内置 ghu_ 令牌默认 403)。

仅依赖 Python 标准库。建议用 C:\\QGB\\miniforge3\\python.exe (3.14) 运行。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BRIDGE = "http://192.168.1.100:1188/"

# 退出码约定
EXIT_OK = 0
EXIT_ARGS = 2
EXIT_BRIDGE_DOWN = 3      # 桥接/PTY/codespace 断联 —— 需人工恢复, 脚本不重试
EXIT_REMOTE_FAIL = 4      # 远端命令 / copilot 自身失败(非断联)
EXIT_BUSY_TIMEOUT = 5     # PTY 一直忙(上一条命令未结束)

RESTORE_HINT = (
    "[!] 需要人工恢复链路(脚本不会自动重试):\n"
    "  1) 打开 codespace 网页确认远程实例在线(长时间无响应会休眠/断联);\n"
    "  2) 在本机真终端重新拉起常驻 PTY 窗口:\n"
    "     C:\\QGB\\miniforge3\\python.exe client/pty_client_mqtt.py -t co\n"
    "  3) 恢复后重新执行本命令。"
)


class BridgeDown(Exception):
    """桥接不可达或 PTY 会话已结束 —— 必须人工恢复。"""


# ----------------------------- 输出工具 -----------------------------

def _setup_console():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def info(msg):
    print(msg)


def warn(msg):
    print("[!] " + msg, file=sys.stderr)


# ----------------------------- HTTP RPC -----------------------------

def rpc(expr, bridge, timeout):
    """在常驻 pty_client_mqtt 进程里执行一段 Python 表达式, 返回解析后的对象。

    等价于仓库 client/pty_client_mqtt.py 里的 _call_ai_bridge / local_rpc,
    这里自包含实现, 不 import 仓库代码。
    """
    code = "import json as _j; p.set_data(_j.dumps(%s, ensure_ascii=False))" % expr
    url = bridge.rstrip("/") + "/" + urllib.parse.quote(code, safe="")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, data=b"", method="POST")
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise BridgeDown("桥接 HTTP %s: %s" % (e.code, e.reason))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BridgeDown("无法连接桥接 %s (%s)" % (bridge, e))
    try:
        return json.loads(body)
    except Exception:
        # 非 JSON 通常是 RPC 口返回的纯文本错误
        raise BridgeDown("桥接返回非预期内容: %r" % body[:200])


def bridge_status(bridge):
    st = rpc("ai_bridge.status()", bridge, timeout=15)
    if not st.get("attached") or st.get("end_reason") is not None:
        raise BridgeDown("PTY 未连接 (attached=%s, end_reason=%r)"
                         % (st.get("attached"), st.get("end_reason")))
    return st


def remote_run(cmd, bridge, timeout, busy_wait=60):
    """经 PTY 在远端跑一条 shell 命令; busy 时按 busy_wait 秒数轮询等待。"""
    deadline = time.time() + busy_wait
    while True:
        r = rpc("ai_bridge.run(%r, timeout=%r, acquire_timeout=%r)"
                % (str(cmd), float(timeout), 2.0),
                bridge, timeout=float(timeout) + 30)
        if not r.get("busy"):
            return r
        if time.time() > deadline:
            return r
        print("[.] PTY 正忙, 5s 后重试(最多等待 %ss)..." % busy_wait,
              file=sys.stderr)
        time.sleep(5)


# ----------------------------- copilot JSONL 解析 -----------------------------

def _join_content(content):
    """assistant.message 的 content 可能是 str 或分段数组, 尽力取文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for seg in content:
            if isinstance(seg, str):
                parts.append(seg)
            elif isinstance(seg, dict):
                t = seg.get("text") or seg.get("content") or ""
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts)
    return ""


def parse_copilot_jsonl(text):
    """解析 `copilot -p --output-format json` 的 JSONL 流。

    返回 dict: model / available / router / answer / usage / models / events
    """
    res = {"model": None, "available": [], "router": None, "answer": None,
           "usage": None, "models": [], "events": 0, "saw_json": False}
    phases = {}                 # messageId -> phase
    deltas = {}                 # messageId -> 累积文本
    complete = {}               # messageId -> 完整文本
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        res["saw_json"] = True
        res["events"] += 1
        t = obj.get("type")
        d = obj.get("data") or {}
        if t == "session.auto_mode_resolved":
            res["model"] = d.get("chosenModel") or res["model"]
            if d.get("availableModels"):
                res["available"] = d["availableModels"]
            res["router"] = d.get("routingMethod")
        elif t == "model.call_start" and d.get("model"):
            if d["model"] not in res["models"]:
                res["models"].append(d["model"])
        elif t == "assistant.message_start":
            phases[d.get("messageId")] = d.get("phase")
        elif t == "assistant.message_delta":
            mid = d.get("messageId")
            deltas[mid] = deltas.get(mid, "") + d.get("deltaContent", "")
        elif t == "assistant.message":
            mid = d.get("messageId") or d.get("id")
            txt = _join_content(d.get("content"))
            if txt:
                complete[mid] = txt
        elif t == "session.usage_checkpoint":
            res["usage"] = d
    # 最终答案: 优先 final_answer 阶段的完整消息, 其次该阶段 delta 拼接
    final_ids = [m for m, p in phases.items() if p == "final_answer"]
    if final_ids:
        mid = final_ids[0]
        res["answer"] = complete.get(mid) or deltas.get(mid)
    if not res["answer"]:
        if complete:
            res["answer"] = complete[next(reversed(complete))]
        elif deltas:
            res["answer"] = deltas[next(reversed(deltas))]
    if not res["model"] and res["models"]:
        res["model"] = res["models"][-1]
    return res


def shq(s):
    """POSIX shell 单引号安全转义。"""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def build_copilot_cmd(prompt, model=None, cwd=None, extra_args=None):
    parts = ["copilot", "-p", shq(prompt),
             "-s", "--allow-all", "--output-format", "json",
             "--no-color"]
    if model:
        parts += ["--model", shq(model)]
    if cwd:
        parts += ["-C", shq(cwd)]
    if extra_args:
        parts += extra_args
    parts.append("2>&1")
    return " ".join(parts)


def format_usage(usage):
    if not isinstance(usage, dict):
        return None
    keep = {}
    for k, v in usage.items():
        kl = k.lower()
        if any(w in kl for w in ("token", "credit", "cost", "model")):
            keep[k] = v
    if not keep:
        keep = usage
    return json.dumps(keep, ensure_ascii=False)


# ----------------------------- 子命令 -----------------------------

def cmd_status(args):
    try:
        st = bridge_status(args.bridge)
    except BridgeDown as e:
        warn(str(e))
        print(RESTORE_HINT)
        return EXIT_BRIDGE_DOWN
    print("[OK] PTY 已连接")
    print("     sid            : %s" % st.get("sid"))
    print("     busy           : %s" % st.get("busy"))
    print("     brokers_online : %s/%s"
          % (st.get("brokers_online"), st.get("brokers_total")))
    return EXIT_OK


def _ask(prompt, args, want_answer=True):
    try:
        bridge_status(args.bridge)
    except BridgeDown as e:
        warn(str(e))
        print(RESTORE_HINT)
        return EXIT_BRIDGE_DOWN

    cmd = build_copilot_cmd(prompt, model=args.model, cwd=args.cwd)
    r = remote_run(cmd, args.bridge, timeout=args.timeout,
                   busy_wait=args.busy_wait)
    if r.get("busy"):
        warn("PTY 在 %ss 内一直忙, 上一条远程命令可能尚未结束" % args.busy_wait)
        return EXIT_BUSY_TIMEOUT
    if not r.get("ok"):
        warn("远程执行失败: %r" % r)
        return EXIT_REMOTE_FAIL
    out = r.get("out", "") or ""
    if args.raw:
        print(out)
        return EXIT_OK
    p = parse_copilot_jsonl(out)
    if not p["saw_json"]:
        warn("远端 copilot 未返回 JSON(rc=%s, timed_out=%s)。原始输出:"
             % (r.get("rc"), r.get("timed_out")))
        print(out[-2000:])
        if "login" in out.lower() or "auth" in out.lower():
            warn("看起来未登录: 请在 codespace 里先运行 copilot login")
        return EXIT_REMOTE_FAIL
    if r.get("timed_out"):
        warn("回收超时(远端命令可能仍在运行), 以下为已收到的内容")

    if p["model"]:
        line = "[model] %s" % p["model"]
        if p["router"]:
            line += " (router=%s)" % p["router"]
        if p["available"]:
            line += " available=%s" % ",".join(p["available"])
        print(line)
    else:
        warn("未从事件流解析到模型名")
    if want_answer:
        print("[answer]")
        print(p["answer"] or "(未解析到回答, 可用 --raw 查看原始 JSONL)")
    u = format_usage(p["usage"])
    if u:
        print("[usage] %s" % u)
    return EXIT_OK


def cmd_model(args):
    # 用最短提示触发一次会话, 模型名取自 CLI 遥测事件(比模型自报更权威)
    args.raw = False
    return _ask("hi", args, want_answer=args.verbose)


def cmd_ask(args):
    prompt = args.prompt
    if not prompt:
        prompt = sys.stdin.read()
    prompt = prompt.strip()
    if not prompt:
        warn("没有提供问题(参数或 stdin)")
        return EXIT_ARGS
    return _ask(prompt, args)


def cmd_run(args):
    try:
        bridge_status(args.bridge)
    except BridgeDown as e:
        warn(str(e))
        print(RESTORE_HINT)
        return EXIT_BRIDGE_DOWN
    r = remote_run(args.command, args.bridge, timeout=args.timeout,
                   busy_wait=args.busy_wait)
    if r.get("busy"):
        warn("PTY 在 %ss 内一直忙" % args.busy_wait)
        return EXIT_BUSY_TIMEOUT
    print(r.get("out", "") or "")
    rc = r.get("rc")
    if not r.get("ok"):
        return EXIT_REMOTE_FAIL
    return EXIT_OK if rc == 0 else EXIT_REMOTE_FAIL


# --------------------- 长时 agent 任务(tmux 交互 / nohup gist) ---------------------
#
# 背景: copilot 的网页实时会话(--remote)【只支持交互模式】, 且要求:
#   1) 真实 TTY(经 tmux 提供);  2) GitHub 组织管理员开启 remote sessions 策略。
# 因此长任务用 detached tmux 跑 `copilot -i <prompt> --allow-all --remote`,
# 本地用 tmux capture-pane / send-keys 监控与操控; 若组织策略禁用 remote,
# 退而用 `copilot -p --share-gist` 后台跑, 完成后得到网页可看的 secret gist。

AGENT_SESSION = "csai"        # 远端 tmux 会话名
AGENT_DIR = "/tmp/csai"       # 交互模式产物(prompt/run.sh/镜像日志)
GIST_DIR = "/tmp/csai-gist"  # 非交互 gist 任务产物
_PROMPT_DELIM = "CSAI_PROMPT_EOF_7F3A9C21"

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
TASKS_URL_RE = re.compile(r"https://github\.com/[^\s'\"]+/tasks/[^\s'\"]+")
GIST_URL_RE = re.compile(r"https://gist\.github\.com/[^\s'\"]+")
BUSY_RE = re.compile(r"Working|esc interrupt|\bspinner\b", re.I)


def _rr(cmd, bridge, timeout=60):
    """跑远端命令并断言成功, 返回 out 文本。"""
    r = remote_run(cmd, bridge, timeout=timeout)
    if r.get("busy"):
        raise RuntimeError("PTY 正忙, 稍后再试")
    if not r.get("ok"):
        raise RuntimeError("远端执行失败: %r" % r)
    return r.get("out", "") or ""


def _strip_ansi(text):
    return ANSI_RE.sub("", text)


def _pane(bridge, history=80):
    """抓取 tmux 窗格文本(去 ANSI)。"""
    out = _rr("tmux capture-pane -p -t %s -S -%d" % (AGENT_SESSION, history),
              bridge, timeout=30)
    return _strip_ansi(out)


def _keys(bridge, *keys):
    _rr("tmux send-keys -t %s %s"
        % (AGENT_SESSION, " ".join(shq(k) for k in keys)), bridge, timeout=20)


def _literal(bridge, text, enter=False):
    _rr("tmux send-keys -t %s -l %s" % (AGENT_SESSION, shq(text)),
        bridge, timeout=20)
    if enter:
        _keys(bridge, "Enter")


def _ensure_tmux(bridge, install=False):
    out = _rr("command -v tmux || true", bridge, timeout=20)
    if out.strip():
        return
    if not install:
        raise RuntimeError(
            "远端未安装 tmux。加 --install-tmux 可自动执行 "
            "'sudo apt-get install -y tmux' 安装")
    warn("远端安装 tmux ...")
    _rr("sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive "
        "apt-get install -y -qq tmux >/tmp/csai-apt.log 2>&1",
        bridge, timeout=300)
    if not _rr("command -v tmux || true", bridge, timeout=20).strip():
        raise RuntimeError("tmux 安装失败, 见远端 /tmp/csai-apt.log")


def _write_prompt_and_runner(bridge, prompt, cwd, name, model, use_remote):
    if _PROMPT_DELIM in prompt:
        raise RuntimeError("提示词中不能包含保留串 %s" % _PROMPT_DELIM)
    copilot_parts = ["copilot", "-i", '"$PROMPT"', "--allow-all",
                     "--no-ask-user", "--no-color", "--name", shq(name)]
    if use_remote:
        copilot_parts.append("--remote")
    if model:
        copilot_parts += ["--model", shq(model)]
    runner = (
        "#!/usr/bin/env bash\n"
        "cd %s || exit 1\n"
        "PROMPT=$(cat %s/prompt.txt)\n"
        "%s\n"
        'echo "==COPILOT_EXITED rc=$?=="\n'
        "exec bash\n"
    ) % (shq(cwd), AGENT_DIR, " ".join(copilot_parts))
    if _PROMPT_DELIM in runner:
        raise RuntimeError("启动脚本中出现保留串")
    cmd = (
        "mkdir -p %s\n"
        "cat > %s/prompt.txt <<'%s'\n%s\n%s\n"
        "cat > %s/run.sh <<'CSAI_RUNNER_EOF'\n%sCSAI_RUNNER_EOF\n"
        "chmod +x %s/run.sh\n"
    ) % (AGENT_DIR, AGENT_DIR, _PROMPT_DELIM, prompt, _PROMPT_DELIM,
         AGENT_DIR, runner, AGENT_DIR)
    _rr(cmd, bridge, timeout=30)


def agent_start(args):
    try:
        bridge_status(args.bridge)
        _ensure_tmux(args.bridge, install=args.install_tmux)
    except (BridgeDown, RuntimeError) as e:
        warn(str(e))
        if isinstance(e, BridgeDown):
            print(RESTORE_HINT)
            return EXIT_BRIDGE_DOWN
        return EXIT_REMOTE_FAIL

    prompt = args.prompt or sys.stdin.read()
    prompt = prompt.strip()
    if not prompt:
        warn("没有提供任务提示(参数或 stdin)")
        return EXIT_ARGS
    if not args.cwd:
        warn("agent start 需要 --cwd 指定仓库目录")
        return EXIT_ARGS

    exists = _rr("tmux has-session -t %s 2>/dev/null && echo YES || echo NO"
                 % AGENT_SESSION, args.bridge, timeout=20).strip()
    if exists == "YES":
        if not args.force:
            warn("已有 agent tmux 会话在跑; 用 agent status 查看, 或加 --force 重启")
            return EXIT_REMOTE_FAIL
        _rr("tmux kill-session -t %s" % AGENT_SESSION, args.bridge, timeout=20)

    info("[*] 写入任务并启动 tmux(detached) ...")
    _rr("rm -f %s/pane.raw" % AGENT_DIR, args.bridge, timeout=20)
    _write_prompt_and_runner(args.bridge, prompt, args.cwd,
                             args.name, args.model, use_remote=not args.no_remote)
    _rr("tmux new-session -d -s %s -x %d -y %d -c %s"
        % (AGENT_SESSION, args.width, args.height, shq(args.cwd)),
        args.bridge, timeout=20)
    _rr("tmux pipe-pane -o -t %s 'cat >> %s/pane.raw'"
        % (AGENT_SESSION, AGENT_DIR), args.bridge, timeout=20)
    _literal(args.bridge, "bash %s/run.sh" % AGENT_DIR, enter=True)

    # 处理启动期两个模态框 + 等 remote 链接
    url, policy, answered = None, False, set()
    for _ in range(12):
        time.sleep(6)
        pane = _pane(args.bridge, history=60)
        low = pane
        if "Do you trust the files in this folder?" in low and "trust" not in answered:
            # 选项 2 = Yes, and remember this folder(焦点默认在 1, Down 一次)
            _keys(args.bridge, "Down", "Enter")
            answered.add("trust")
            info("[*] 已回答文件夹信任: Yes, and remember")
        elif "multi-line input support" in low and "ttysetup" not in answered:
            _keys(args.bridge, "Escape")
            answered.add("ttysetup")
            info("[*] 已跳过多行输入快捷键配置")
        m = TASKS_URL_RE.search(pane)
        if m:
            url = m.group(0).rstrip(".,)")
        if "not enabled. Contact your organization administrator" in low:
            policy = True
        if url or policy:
            break

    info("[OK] agent 已启动: tmux=%s 目录=%s 名称=%s"
         % (AGENT_SESSION, args.cwd, args.name))
    if url:
        info("[OK] 网页实时会话(可看可操控): %s" % url)
    elif policy:
        warn("网页实时会话被 GitHub 组织策略禁用, 需组织管理员开启 remote sessions")
        warn("替代方案: 用 'agent gist' 子命令跑 --share-gist, 完成后给网页链接")
    else:
        info("[.] 暂未取到 tasks 链接, 用 agent status 再看")
    info("[.] 进度查看: agent status / agent follow; 任务总结: agent report")
    return EXIT_OK


def _session_alive(bridge):
    out = _rr("tmux has-session -t %s 2>/dev/null && echo YES || echo NO"
              % AGENT_SESSION, bridge, timeout=20)
    return "YES" in out


def agent_status(args):
    try:
        bridge_status(args.bridge)
        if not _session_alive(args.bridge):
            warn("tmux 会话 %s 不存在(任务未启动或已 stop)" % AGENT_SESSION)
            return EXIT_REMOTE_FAIL
    except BridgeDown as e:
        warn(str(e)); print(RESTORE_HINT)
        return EXIT_BRIDGE_DOWN
    pane = _pane(args.bridge, history=args.tail)
    visible = _strip_ansi(_rr("tmux capture-pane -p -t %s" % AGENT_SESSION,
                              args.bridge, timeout=30))
    url = TASKS_URL_RE.search(pane)
    # 忙闲只看当前可见屏幕底部(历史里的 "Thought for" 不能算工作中)
    screen_bottom = "\n".join(
        [l for l in visible.splitlines() if l.strip()][-8:])
    busy = bool(BUSY_RE.search(screen_bottom))
    info("[*] %s | %s"
         % ("工作中" if busy else "空闲(可能本轮已完成)",
            ("网页: " + url.group(0).rstrip(".,)")) if url else "无 tasks 链接"))
    print("\n".join([l.rstrip() for l in pane.splitlines() if l.strip()][-args.tail:]))
    return EXIT_OK


# 从 ~/.copilot/session-state/*/events.jsonl 抽取某工作目录最近会话的最终回复
_REPORT_PY = r"""
import json, glob, os, sys
target = %r
base = os.path.expanduser('~/.copilot/session-state')
cands = []
for f in glob.glob(base + '/*/events.jsonl'):
    try:
        o = json.loads(open(f, encoding='utf-8').readline())
        cwd = ((o.get('data') or {}).get('context') or {}).get('cwd')
    except Exception:
        continue
    if cwd == target:
        cands.append((os.path.getmtime(f), f))
if not cands:
    sys.exit(3)
f = sorted(cands)[-1][1]
phases, deltas, finals = {}, {}, []
model = started = None
for line in open(f, encoding='utf-8'):
    line = line.strip()
    if not line.startswith('{'):
        continue
    try:
        o = json.loads(line)
    except Exception:
        continue
    t = o.get('type'); d = o.get('data') or {}
    if t == 'session.start':
        started = d.get('startTime'); model = None
    if t == 'session.auto_mode_resolved':
        model = d.get('chosenModel')
    if t == 'assistant.message_start':
        phases[d.get('messageId')] = d.get('phase')
    elif t == 'assistant.message_delta':
        k = d.get('messageId')
        deltas[k] = deltas.get(k, '') + d.get('deltaContent', '')
    elif t == 'assistant.message':
        c = d.get('content')
        if isinstance(c, list):
            c = ''.join(s.get('text', '') if isinstance(s, dict) else str(s)
                        for s in c)
        if isinstance(c, str) and c.strip():
            finals.append((d.get('phase'), c))
        if d.get('model'):
            model = d.get('model')
ans = None
for ph, c in reversed(finals):
    if ph == 'final_answer':
        ans = c; break
if ans is None:
    for ph, c in reversed(finals):
        ans = c; break
if ans is None and deltas:
    ans = deltas[next(reversed(deltas))]
print('@@FILE', f)
print('@@MODEL', model or '?')
print('@@STARTED', started or '?')
print('@@LEN', len(ans or ''))
print(ans or '')
"""


def _extract_report(bridge, cwd):
    out = _rr("python3 -c %s" % shq(_REPORT_PY % cwd), bridge, timeout=60)
    if "@@FILE" not in out:
        return None
    meta, _, body = out.partition("\n")
    lines = out.splitlines()
    info_map = {}
    idx = 0
    for i, l in enumerate(lines):
        if l.startswith("@@"):
            parts = l.split(" ", 1)
            info_map[parts[0]] = parts[1] if len(parts) > 1 else ""
            idx = i
    answer = "\n".join(lines[idx + 1:])
    info_map["answer"] = answer
    return info_map


def agent_report(args):
    try:
        bridge_status(args.bridge)
        rep = _extract_report(args.bridge, args.cwd)
    except BridgeDown as e:
        warn(str(e)); print(RESTORE_HINT)
        return EXIT_BRIDGE_DOWN
    if not rep:
        warn("没找到 %s 对应会话的 events.jsonl(任务可能还没产生回复)" % args.cwd)
        return EXIT_REMOTE_FAIL
    header = ("<!-- 来源: codespace GitHub Copilot CLI 会话 %s | 模型 %s | 开始 %s -->\n"
              % (rep.get("@@FILE", "?").split("/")[-2], rep.get("@@MODEL"),
                 rep.get("@@STARTED")))
    text = header + rep.get("answer", "")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        info("[OK] 报告已保存: %s (%d 字, 模型 %s)"
             % (args.output, len(rep.get("answer", "")), rep.get("@@MODEL")))
    else:
        print(text)
    return EXIT_OK


def agent_follow(args):
    try:
        bridge_status(args.bridge)
        if not _session_alive(args.bridge):
            raise RuntimeError("tmux 会话不存在")
    except (BridgeDown, RuntimeError) as e:
        warn(str(e))
        if isinstance(e, BridgeDown):
            print(RESTORE_HINT); return EXIT_BRIDGE_DOWN
        return EXIT_REMOTE_FAIL

    deadline = time.time() + args.max
    last_sig, stable, polls = None, 0, 0
    while time.time() < deadline:
        visible = _strip_ansi(_rr("tmux capture-pane -p -t %s" % AGENT_SESSION,
                                  args.bridge, timeout=30))
        tail = [l.rstrip() for l in visible.splitlines() if l.strip()]
        bottom = "\n".join(tail[-8:])
        busy = bool(BUSY_RE.search(bottom))
        sig = ("B" if busy else "I") + str(len(tail))
        progress = tail[-1][:90] if tail else ""
        info("[%4ds] %s %s" % (polls * args.interval,
                               "WORKING" if busy else "idle?", progress))
        polls += 1
        if not busy and polls >= 3:
            if sig == last_sig:
                stable += 1
            else:
                stable = 0
            if stable >= args.stable:
                info("[OK] 画面连续 %d 轮静止, 判定本轮完成" % args.stable)
                a2 = argparse.Namespace(**vars(args))
                a2.output = getattr(args, "output", None)
                return agent_report(a2)
        last_sig = sig
        time.sleep(args.interval)
    warn("达到 --max %ss 仍未结束, 保留 tmux 会话, 稍后可 agent status/report"
         % args.max)
    return EXIT_REMOTE_FAIL


def agent_send(args):
    try:
        bridge_status(args.bridge)
        if args.key:
            _keys(args.bridge, *args.key)
        if args.text:
            _literal(args.bridge, args.text, enter=not args.no_enter)
    except BridgeDown as e:
        warn(str(e)); print(RESTORE_HINT)
        return EXIT_BRIDGE_DOWN
    return EXIT_OK


def agent_stop(args):
    try:
        bridge_status(args.bridge)
        _rr("tmux kill-session -t %s 2>/dev/null; echo stopped" % AGENT_SESSION,
            args.bridge, timeout=20)
        info("[OK] 已停止 agent tmux 会话")
    except BridgeDown as e:
        warn(str(e)); print(RESTORE_HINT)
        return EXIT_BRIDGE_DOWN
    return EXIT_OK


# -------- 非交互 gist 模式(网页可见的替代方案) --------

def _gist_paths_cmd(action):
    if action == "start":
        return (
            "mkdir -p %s\n"
            "cat > %s/prompt.txt <<'%s'\n%%s\n%s\n"
            "cat > %s/gist.sh <<'CSAI_GIST_RUNNER'\n"
            "#!/usr/bin/env bash\ncd %%s || exit 1\n"
            "copilot -p \"$(cat %s/prompt.txt)\" -s --allow-all --no-ask-user "
            "--share-gist --output-format json --no-color "
            "> %s/out.jsonl 2> %s/err.log\n"
            "echo $? > %s/rc\nCSAI_GIST_RUNNER\n"
            "chmod +x %s/gist.sh\n"
            "rm -f %s/rc %s/out.jsonl %s/err.log\n"
            "nohup bash %s/gist.sh >/dev/null 2>&1 & echo $! > %s/pid\n"
            "sleep 1; echo pid=$(cat %s/pid)\n"
        ) % (GIST_DIR, GIST_DIR, _PROMPT_DELIM, _PROMPT_DELIM, GIST_DIR,
             GIST_DIR, GIST_DIR, GIST_DIR, GIST_DIR, GIST_DIR, GIST_DIR,
             GIST_DIR, GIST_DIR, GIST_DIR, GIST_DIR, GIST_DIR, GIST_DIR)


def agent_gist_start(args):
    try:
        bridge_status(args.bridge)
    except BridgeDown as e:
        warn(str(e)); print(RESTORE_HINT)
        return EXIT_BRIDGE_DOWN
    prompt = args.prompt or sys.stdin.read()
    prompt = prompt.strip()
    if not prompt or not args.cwd:
        warn("agent gist 需要提示词与 --cwd")
        return EXIT_ARGS
    cmd = _gist_paths_cmd("start") % (prompt, shq(args.cwd))
    out = _rr(cmd, args.bridge, timeout=30)
    info("[OK] gist 后台任务已启动: %s" % out.strip().replace("\n", " "))
    info("[.] 用 agent gist-status 轮询; 完成后输出 secret gist 链接(网页可看)")
    return EXIT_OK


def agent_gist_status(args):
    try:
        bridge_status(args.bridge)
    except BridgeDown as e:
        warn(str(e)); print(RESTORE_HINT)
        return EXIT_BRIDGE_DOWN
    rc = _rr("cat %s/rc 2>/dev/null || echo RUNNING" % GIST_DIR,
             args.bridge, timeout=20).strip()
    urls = _rr("grep -aoE 'https://gist\\.github\\.com/[^ \"\\x27]+' %s/out.jsonl "
               "2>/dev/null | sort -u | head -3" % GIST_DIR,
               args.bridge, timeout=30).strip()
    if rc == "RUNNING":
        tail = _rr("tail -c 600 %s/out.jsonl 2>/dev/null | "
                   "python3 -c \"import sys,json;\\n"
                   "ts=[json.loads(l).get('type') for l in sys.stdin if "
                   "l.strip().startswith('{')]; print(ts[-3:])\" 2>/dev/null"
                   % GIST_DIR, args.bridge, timeout=20).strip()
        info("[*] RUNNING ... 最近事件: %s" % tail)
        return EXIT_OK
    info("[*] 已结束 rc=%s" % rc)
    if urls:
        info("[OK] 网页会话(secret gist):\n%s" % urls)
    else:
        warn("未发现 gist 链接(可能账号缺 gist 权限)。末尾输出:")
        print(_rr("tail -n 8 %s/err.log 2>/dev/null; tail -c 800 %s/out.jsonl"
                  % (GIST_DIR, GIST_DIR), args.bridge, timeout=20))
        return EXIT_REMOTE_FAIL
    return EXIT_OK


# ----------------------------- 参数解析 -----------------------------

def build_parser():
    p = argparse.ArgumentParser(
        description="经 MQTT-PTY 桥接用命令行访问 codespace 里的 GitHub Copilot")
    p.add_argument("--bridge",
                   default=os.environ.get("CODESPACE_AI_BRIDGE", DEFAULT_BRIDGE),
                   help="PTY HTTP 桥接地址(默认 %(default)s 或环境变量 "
                        "CODESPACE_AI_BRIDGE)")
    p.add_argument("--timeout", type=float, default=300,
                   help="远端命令超时秒数, 默认 300")
    p.add_argument("--busy-wait", type=float, default=60,
                   help="PTY 忙时最多等待秒数, 默认 60")
    sub = p.add_subparsers(dest="action", required=True)

    sps = sub.add_parser("status", help="探测桥接/PTY 是否在线")
    sps.set_defaults(func=cmd_status)

    pa = sub.add_parser("ask", help="向远程 Copilot 提问")
    pa.add_argument("prompt", nargs="?", help="问题(不给则从 stdin 读)")
    pa.add_argument("--model", default=None,
                   help="指定模型(如 auto / gpt-5.4); 默认走 auto 路由")
    pa.add_argument("--cwd", default=None,
                   help="远端工作目录(映射 copilot -C), 如 /workspaces/<repo>")
    pa.add_argument("--raw", action="store_true",
                   help="直接打印原始 JSONL, 不做解析")
    pa.set_defaults(func=cmd_ask)

    pm = sub.add_parser("model", help="查询实际生效模型(读 CLI 遥测事件)")
    pm.add_argument("--model", default=None, help=argparse.SUPPRESS)
    pm.add_argument("--cwd", default=None, help=argparse.SUPPRESS)
    pm.add_argument("--raw", action="store_true", help=argparse.SUPPRESS)
    pm.add_argument("--verbose", action="store_true",
                   help="同时打印模型对 hi 的回答")
    pm.set_defaults(func=cmd_model)

    pr = sub.add_parser("run", help="透传执行一条远端 shell 命令")
    pr.add_argument("command", help="shell 命令, 例如 \"uname -a\"")
    pr.set_defaults(func=cmd_run)

    # ---- 长时 agent 任务 ----
    pag = sub.add_parser("agent",
                         help="长时 agent 任务(tmux 交互 + 网页会话, 或 gist 模式)")
    asub = pag.add_subparsers(dest="agent_action", required=True)

    pst = asub.add_parser("start", help="detached tmux 里启动交互 copilot 长任务")
    pst.add_argument("prompt", nargs="?", help="任务(不给则从 stdin 读)")
    pst.add_argument("--cwd", required=True, help="远端仓库目录, 如 /workspaces/xime")
    pst.add_argument("--name", default="csai-agent", help="会话名(网页端可识别)")
    pst.add_argument("--model", default=None, help="指定模型, 默认 auto")
    pst.add_argument("--width", type=int, default=220)
    pst.add_argument("--height", type=int, default=50)
    pst.add_argument("--no-remote", action="store_true",
                     help="不请求 --remote(组织策略禁用时可省掉警告)")
    pst.add_argument("--install-tmux", action="store_true",
                     help="远端缺 tmux 时自动 apt-get 安装")
    pst.add_argument("--force", action="store_true", help="杀掉已有会话再启动")
    pst.set_defaults(func=agent_start)

    ps2 = asub.add_parser("status", help="查看 agent 运行状态与窗格尾部")
    ps2.add_argument("--tail", type=int, default=40)
    ps2.set_defaults(func=agent_status)

    pf = asub.add_parser("follow", help="轮询至本轮完成并自动抽取总结")
    pf.add_argument("--cwd", required=True, help="会话工作目录(用于定位会话记录)")
    pf.add_argument("--interval", type=float, default=20)
    pf.add_argument("--stable", type=int, default=3, help="连续 N 轮静止判定完成")
    pf.add_argument("--max", type=float, default=1800, help="最长等待秒数")
    pf.add_argument("--output", "-o", default=None, help="总结保存为本地 Markdown")
    pf.set_defaults(func=agent_follow)

    prp = asub.add_parser("report", help="抽取该目录最近一次会话的最终总结")
    prp.add_argument("--cwd", required=True)
    prp.add_argument("--output", "-o", default=None, help="保存为本地 Markdown")
    prp.set_defaults(func=agent_report)

    pse = asub.add_parser("send", help="向 agent 发文字(可在网页之外继续操控)")
    pse.add_argument("text", nargs="?", help="要发送的文字")
    pse.add_argument("--no-enter", action="store_true")
    pse.add_argument("--key", nargs="+", help="发送原始键名, 如 C-c / PageUp")
    pse.set_defaults(func=agent_send)

    psp = asub.add_parser("stop", help="杀掉 agent tmux 会话")
    psp.set_defaults(func=agent_stop)

    pgs = asub.add_parser("gist", help="后台跑非交互任务并 --share-gist(网页备选)")
    pgs.add_argument("prompt", nargs="?", help="任务(不给则从 stdin 读)")
    pgs.add_argument("--cwd", required=True)
    pgs.set_defaults(func=agent_gist_start)

    pgst = asub.add_parser("gist-status", help="查询 gist 后台任务, 完成则给链接")
    pgst.set_defaults(func=agent_gist_status)
    return p


def main(argv=None):
    _setup_console()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        warn("已中断")
        return EXIT_ARGS


if __name__ == "__main__":
    sys.exit(main())
