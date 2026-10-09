# -*- coding: utf-8 -*-
"""
Codespace Copilot 长时 agent 网页端
====================================

不经 tmux TUI，直接命令式调用远端 `copilot -p`（Allow All），JSONL 事件流
经同一座 MQTT-PTY HTTP 桥接增量拉回，浏览器实时同步显示：AI 调用的命令、
stdout/stderr 回显、退出码、流式回复；支持停止任务、历史会话回放。

零额外依赖（Python 标准库 + 内嵌单页 HTML/JS）。

API:
  GET  /                        单页
  GET  /api/status              桥接探活
  POST /api/ask   {prompt,cwd,model}  远端 setsid+nohup 启动任务, 返回 id
  GET  /api/poll?id=&offset=N   增量事件 {events,offset,done,rc}
  POST /api/stop?id=            杀远端进程组
  GET  /api/sessions            本地历史索引
  GET  /api/session/<id>        元信息
  GET  /api/session/<id>/events?offset=N  回放事件
"""

import base64
import json
import os
import re
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from codespace_ai import (  # noqa: E402
    rpc, bridge_status, remote_run, shq, BridgeDown)

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_DIR = os.path.join(HERE, "web_sessions")
REMOTE_DIR = "/tmp/csai-web"
DEFAULT_CWD = "/workspaces/build_xime_home/xime"
DEFAULT_BRIDGE = os.environ.get("CODESPACE_AI_BRIDGE", "http://192.168.1.100:1188/")
HOST, PORT = "127.0.0.1", int(os.environ.get("CSAI_WEB_PORT", "1189"))

# PTY 是串行资源; 所有远端调用经一把锁串行化(轮询/启动/停止互不踩)
REMOTE_LOCK = threading.RLock()
# id -> {"tail": b"未换行的残余字节"}
_PARSE_BUF = {}
HISTORY_LOCK = threading.RLock()

os.makedirs(STORE_DIR, exist_ok=True)


# ------------------------- 远端任务控制 -------------------------

def _fbase(sid):
    return "%s/%s" % (REMOTE_DIR, sid)


def remote_start(prompt, cwd, model):
    """在远端 setsid+nohup 启动 copilot -p, 返回会话 id。"""
    sid = uuid.uuid4().hex[:12]
    fb = _fbase(sid)
    pb64 = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
    args = ["copilot", "-p", '"$(cat %s.prompt)"' % fb,
            "-s", "--allow-all", "--output-format", "json",
            "--no-color"]
    if cwd:
        args += ["-C", shq(cwd)]
    if model and model != "auto":
        args += ["--model", shq(model)]
    inner = ("mkdir -p %s && printf %%s %s | base64 -d > %s.prompt && "
             "setsid bash -c %s </dev/null >/dev/null 2>&1 & "
             "echo $! > %s.pid; echo STARTED %s"
             % (REMOTE_DIR, shq(pb64), fb,
                shq(" ".join(args) + " >%s.jsonl 2>%s.err; echo $? >%s.rc"
                    % (fb, fb, fb)),
                fb, sid))
    with REMOTE_LOCK:
        r = remote_run(inner, DEFAULT_BRIDGE, timeout=60, busy_wait=30)
    out = (r.get("out") or "").strip()
    if not r.get("ok") or ("STARTED %s" % sid) not in out:
        raise RuntimeError("远端启动失败: %s" % out[-300:])
    return sid


def remote_poll(sid, offset):
    """增量读远端 jsonl 字节; 返回 (new_bytes, total_size, rc_or_None, err_tail)。"""
    fb = _fbase(sid)
    cmd = (
        "b=%s; f=$b.jsonl; "
        "if [ -f \"$f\" ]; then printf 'BYTES='; wc -c < \"$f\"; else echo BYTES=0; fi; "
        "if [ -f \"$b.rc\" ]; then printf 'RC='; cat \"$b.rc\"; fi; "
        "tail -c +%d \"$f\" 2>/dev/null | base64 -w0; echo; "
        "if [ -f \"$b.rc\" ] && [ ! -s \"$f\" ]; then echo '--ERR--'; tail -5 \"$b.err\" 2>/dev/null; fi"
        % (fb, offset + 1))
    with REMOTE_LOCK:
        r = remote_run(cmd, DEFAULT_BRIDGE, timeout=45, busy_wait=15)
    out = r.get("out") or ""
    total, rc, chunk, err_tail = None, None, b"", ""
    nl = out.find("\n")
    head, rest = (out[:nl], out[nl + 1:]) if nl >= 0 else (out, "")
    mb = re.search(r"BYTES=\s*(\d+)", head)
    if mb:
        total = int(mb.group(1))
    # RC= 行在 base64 之前(独立一行), 剥掉它
    mrc = re.search(r"^RC=(\d+)\s*$", rest, re.M)
    if mrc:
        rc = int(mrc.group(1))
        rest = rest[:mrc.start()] + rest[mrc.end():].lstrip("\n")
    b64lines = []
    for ln in rest.splitlines():
        if ln.strip() == "--ERR--":
            idx = rest.splitlines().index(ln)
            err_tail = "\n".join(rest.splitlines()[idx + 1:])
            break
        b64lines.append(ln.strip())
    if b64lines:
        try:
            chunk = base64.b64decode("".join(b64lines))
        except Exception:
            chunk = b""
    return chunk, total, rc, err_tail


def remote_stop(sid):
    fb = _fbase(sid)
    cmd = ("if [ -f %s.pid ]; then p=$(cat %s.pid); "
           "kill -TERM -$p 2>/dev/null; kill -TERM $p 2>/dev/null; "
           "sleep 1; pkill -9 -P $p 2>/dev/null; kill -9 -$p 2>/dev/null; "
           "kill -9 $p 2>/dev/null; fi; "
           "echo 137 > %s.rc; echo STOPPED" % (fb, fb, fb))
    with REMOTE_LOCK:
        r = remote_run(cmd, DEFAULT_BRIDGE, timeout=30, busy_wait=15)
    return "STOPPED" in (r.get("out") or "")


# ------------------------- 本地会话存储 -------------------------

def _sdir(sid):
    return os.path.join(STORE_DIR, sid)


def save_meta(sid, meta):
    os.makedirs(_sdir(sid), exist_ok=True)
    p = os.path.join(_sdir(sid), "meta.json")
    with HISTORY_LOCK:
        old = {}
        if os.path.exists(p):
            try:
                old = json.load(open(p, encoding="utf-8"))
            except Exception:
                pass
        old.update(meta)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False, indent=1)


def append_bytes(sid, data):
    if not data:
        return
    d = _sdir(sid)
    os.makedirs(d, exist_ok=True)
    with HISTORY_LOCK:
        with open(os.path.join(d, "events.jsonl"), "ab") as f:
            f.write(data)


def parse_lines(sid, chunk):
    """把新增字节按 JSONL 行切成事件对象, 残余半包按 sid 留存。"""
    buf = _PARSE_BUF.setdefault(sid, {"tail": b""})
    raw = buf["tail"] + chunk
    lines = raw.split(b"\n")
    buf["tail"] = lines.pop()
    events = []
    for ln in lines:
        ln = ln.strip()
        if not ln.startswith(b"{"):
            continue
        try:
            o = json.loads(ln.decode("utf-8", "replace"))
            events.append({"type": o.get("type"), "data": o.get("data") or {}})
        except Exception:
            continue
    return events


def list_sessions():
    out = []
    if not os.path.isdir(STORE_DIR):
        return out
    for name in os.listdir(STORE_DIR):
        mp = os.path.join(STORE_DIR, name, "meta.json")
        if not os.path.isfile(mp):
            continue
        try:
            m = json.load(open(mp, encoding="utf-8"))
            ep = os.path.join(STORE_DIR, name, "events.jsonl")
            m["id"] = name
            m["bytes"] = os.path.getsize(ep) if os.path.exists(ep) else 0
            out.append(m)
        except Exception:
            continue
    out.sort(key=lambda m: m.get("created", 0), reverse=True)
    return out


def local_events(sid, offset):
    ep = os.path.join(_sdir(sid), "events.jsonl")
    if not os.path.isfile(ep):
        return [], offset, True
    with open(ep, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    events = parse_lines("hist-" + sid, chunk)
    return events, offset + len(chunk), True


# --------------------- 后台守护轮询(浏览器无关) ---------------------

WATCHERS = set()
WATCH_LOCK = threading.Lock()


def ensure_watcher(sid):
    """幂等起 daemon 线程从远端拉事件落盘; 浏览器关闭/服务重启都不影响。"""
    with WATCH_LOCK:
        if sid in WATCHERS:
            return
        WATCHERS.add(sid)
    threading.Thread(target=_watch, args=(sid,), daemon=True).start()


def _watch(sid):
    # 从本地已落盘大小继续(服务重启后可自愈追完)
    ep = os.path.join(_sdir(sid), "events.jsonl")
    off = os.path.getsize(ep) if os.path.isfile(ep) else 0
    misses = 0
    try:
        while True:
            try:
                chunk, total, rc, _err = remote_poll(sid, off)
                misses = 0
                if chunk:
                    append_bytes(sid, chunk)
                off = total if total is not None else off
                if rc is not None:
                    save_meta(sid, {"finished": time.time(), "rc": rc,
                                    "stopped": rc == 137})
                    break
            except BridgeDown:
                # 断联温和退避, 不轰炸; 桥接恢复后自动追上
                misses += 1
                time.sleep(min(10 * misses, 60))
            except Exception:
                time.sleep(5)
            else:
                time.sleep(1.2)
    finally:
        with WATCH_LOCK:
            WATCHERS.discard(sid)


def resume_watchers():
    """服务启动时恢复所有未完成会话的守护拉取。"""
    for m in list_sessions():
        if not m.get("finished"):
            ensure_watcher(m["id"])


# ------------------------- HTTP -------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "csai-web/1.0"

    def log_message(self, *a):
        pass

    # ---- helpers ----
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _html(self, text):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- routing ----
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self._html(PAGE_HTML)
            if u.path == "/api/status":
                return self._api_status()
            if u.path == "/api/sessions":
                return self._json({"sessions": list_sessions()})
            if u.path.startswith("/api/session/"):
                rest = u.path[len("/api/session/"):]
                if rest.endswith("/events"):
                    sid = rest[:-len("/events")]
                    off = int(q.get("offset", "0"))
                    ev, newoff, _ = local_events(sid, off)
                    mp = os.path.join(_sdir(sid), "meta.json")
                    meta = json.load(open(mp, encoding="utf-8")) if os.path.isfile(mp) else {}
                    return self._json({"events": ev, "offset": newoff,
                                       "done": True, "meta": meta})
                mp = os.path.join(_sdir(rest), "meta.json")
                if os.path.isfile(mp):
                    return self._json(json.load(open(mp, encoding="utf-8")))
                return self._json({"error": "not found"}, 404)
            if u.path == "/api/poll":
                return self._api_poll(q)
            return self._json({"error": "not found"}, 404)
        except BridgeDown as e:
            return self._json({"error": "bridge_down", "message": str(e)}, 502)
        except Exception as e:
            return self._json({"error": "server_error", "message": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        body = self._body_json()
        try:
            if u.path == "/api/ask":
                return self._api_ask(body)
            if u.path == "/api/stop":
                sid = body.get("id") or q.get("id")
                if not sid:
                    return self._json({"error": "missing id"}, 400)
                ok = remote_stop(sid)
                save_meta(sid, {"finished": time.time(), "stopped": True, "rc": 137})
                return self._json({"ok": ok})
            return self._json({"error": "not found"}, 404)
        except BridgeDown as e:
            return self._json({"error": "bridge_down", "message": str(e)}, 502)
        except Exception as e:
            return self._json({"error": "server_error", "message": str(e)}, 500)

    # ---- endpoints ----
    def _api_status(self):
        try:
            st = bridge_status(DEFAULT_BRIDGE)
            self._json({"ok": True, "attached": True, "sid": st.get("sid"),
                        "busy": st.get("busy"),
                        "brokers": "%s/%s" % (st.get("brokers_online"),
                                              st.get("brokers_total"))})
        except BridgeDown:
            self._json({"ok": False, "attached": False}, 200)

    def _api_ask(self, body):
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return self._json({"error": "empty prompt"}, 400)
        cwd = (body.get("cwd") or DEFAULT_CWD).strip() or None
        model = (body.get("model") or "auto").strip()
        sid = remote_start(prompt, cwd, model)
        save_meta(sid, {"id": sid, "prompt": prompt, "cwd": cwd,
                        "model": model, "created": time.time(),
                        "finished": None, "rc": None, "stopped": False})
        # 守护线程立即接管远端拉取/落盘, 与浏览器是否在线无关
        ensure_watcher(sid)
        self._json({"id": sid})

    def _api_poll(self, q):
        sid = q.get("id")
        if not sid:
            return self._json({"error": "missing id"}, 400)
        # 服务重启后打开旧的"运行中"会话也能继续追
        ensure_watcher(sid)
        offset = int(q.get("offset", "0"))
        # 数据源一律以 watcher 落盘的本地文件为准(快速、可回放、多端一致)
        events, newoff, _ = local_events(sid, offset)
        mp = os.path.join(_sdir(sid), "meta.json")
        meta = {}
        if os.path.isfile(mp):
            try:
                meta = json.load(open(mp, encoding="utf-8"))
            except Exception:
                pass
        done = bool(meta.get("finished"))
        self._json({"events": events, "offset": newoff,
                    "done": done, "rc": meta.get("rc"), "err": ""})


# ------------------------- 前端页面 -------------------------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codespace Copilot · Agent 网页端</title>
<style>
:root{
  --bg:#0f1115; --panel:#161a22; --panel2:#1c2230; --line:#272e3d;
  --txt:#e6e9ef; --mut:#8b94a7; --acc:#5aa7ff; --ok:#39d98a; --err:#ff6b6b;
  --warn:#ffc857; --code:#0b0d12; --user:#1b3a5f;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--txt);
  font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:14px;padding:10px 16px;
  background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
header h1{font-size:15px;margin:0;font-weight:600;white-space:nowrap}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mut);display:inline-block}
.dot.on{background:var(--ok);box-shadow:0 0 6px var(--ok)}
.dot.off{background:var(--err);box-shadow:0 0 6px var(--err)}
#stat{color:var(--mut);font-size:12px}
input,select,textarea,button{font:inherit;color:var(--txt)}
input,select,textarea{background:var(--panel2);border:1px solid var(--line);
  border-radius:7px;padding:7px 10px;outline:none}
input:focus,textarea:focus{border-color:var(--acc)}
button{background:var(--acc);color:#041226;border:0;border-radius:7px;
  padding:8px 16px;font-weight:600;cursor:pointer}
button.ghost{background:var(--panel2);color:var(--txt);border:1px solid var(--line)}
button:disabled{opacity:.45;cursor:not-allowed}
.layout{flex:1;display:flex;min-height:0}
aside{width:248px;min-width:200px;background:var(--panel);border-right:1px solid var(--line);
  overflow-y:auto;padding:8px}
aside .h{color:var(--mut);font-size:12px;padding:6px 8px;display:flex;justify-content:space-between}
.sitem{padding:8px 10px;border-radius:8px;cursor:pointer;margin-bottom:4px;border:1px solid transparent}
.sitem:hover{background:var(--panel2)}
.sitem.active{background:var(--panel2);border-color:var(--line)}
.sitem .p{font-size:12px;overflow:hidden;text-overflow:ellipsis;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.sitem .t{font-size:11px;color:var(--mut);margin-top:3px;display:flex;gap:6px;align-items:center}
.tag{font-size:10px;padding:0 6px;border-radius:10px;background:var(--panel);color:var(--mut)}
.tag.run{background:#3a2f10;color:var(--warn)}
.tag.ok{background:#103a26;color:var(--ok)}
.tag.bad{background:#3d1518;color:var(--err)}
main{flex:1;display:flex;flex-direction:column;min-width:0}
#stream{flex:1;overflow-y:auto;padding:18px 22px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:920px;width:100%;margin:0 auto;animation:fade .18s ease}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1}}
.bubble{padding:10px 14px;border-radius:10px;white-space:normal}
.bubble.user{background:var(--user);border:1px solid #28527f;margin-left:60px}
.bubble.final{background:var(--panel);border:1px solid var(--line)}
.bubble.thinking{background:transparent;color:var(--mut);border:1px dashed var(--line);
  font-size:13px;cursor:pointer}
.bubble.thinking .body{display:none;margin-top:8px;white-space:pre-wrap}
.bubble.thinking.open .body{display:block}
.bubble.sys{background:transparent;border:0;color:var(--mut);font-size:12px;padding:2px 4px}
.bubble.md{white-space:normal}
.bubble.md pre{background:var(--code);border:1px solid var(--line);border-radius:8px;
  padding:10px 12px;overflow-x:auto;font-size:12.5px}
.bubble.md code{font-family:Cascadia Code,Consolas,monospace;font-size:12.5px}
.bubble.md :not(pre)>code{background:var(--code);padding:1px 5px;border-radius:4px;
  border:1px solid var(--line)}
.bubble.md h1,.bubble.md h2,.bubble.md h3{margin:.7em 0 .35em}
.bubble.md ul{margin:.3em 0;padding-left:1.4em}
.tool{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.tool .hd{display:flex;align-items:center;gap:8px;padding:8px 12px;cursor:pointer;user-select:none}
.tool .idx{color:var(--mut);font-size:12px;font-variant-numeric:tabular-nums}
.tool .tname{font-size:11px;font-weight:700;background:#243b5e;color:#9cc8ff;
  padding:1px 8px;border-radius:10px;text-transform:uppercase;letter-spacing:.04em}
.tool .desc{color:var(--mut);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.tool .rc{font-size:11px;padding:1px 8px;border-radius:10px}
.tool .rc.run{background:#3a2f10;color:var(--warn)}
.tool .rc.ok{background:#103a26;color:var(--ok)}
.tool .rc.bad{background:#3d1518;color:var(--err)}
.tool .bd{display:none;border-top:1px solid var(--line)}
.tool.open .bd{display:block}
.tool .cmd{font-family:Cascadia Code,Consolas,monospace;font-size:12.5px;
  background:var(--code);padding:9px 12px;color:#cfe3ff;white-space:pre-wrap;word-break:break-all}
.tool .cmd::before{content:"$ ";color:var(--mut)}
.tool .out{font-family:Cascadia Code,Consolas,monospace;font-size:12px;
  background:#0a0c10;padding:9px 12px;max-height:340px;overflow:auto;white-space:pre-wrap;
  color:#b8c2d4;word-break:break-word}
.tool .out .errline{color:#ff9a9a}
.tool .out:empty{display:none}
.spin{width:12px;height:12px;border:2px solid var(--warn);border-top-color:transparent;
  border-radius:50%;animation:sp .7s linear infinite;flex:none}
@keyframes sp{to{transform:rotate(360deg)}}
.composer{padding:12px 18px;border-top:1px solid var(--line);background:var(--panel)}
.composer .row{max-width:920px;margin:0 auto;display:flex;gap:10px;align-items:flex-end}
.composer textarea{flex:1;resize:none;min-height:46px;max-height:220px;padding:10px 12px}
.hint{max-width:920px;margin:6px auto 0;color:var(--mut);font-size:11px}
#empty{color:var(--mut);text-align:center;margin-top:14vh;font-size:13px}
#empty b{color:var(--txt)}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;justify-content:center}
.chips span{border:1px solid var(--line);border-radius:14px;padding:3px 12px;cursor:pointer;color:var(--mut)}
.chips span:hover{border-color:var(--acc);color:var(--txt)}
</style>
</head>
<body>
<header>
  <h1>🛰 Codespace Copilot</h1>
  <span class="dot" id="dot"></span><span id="stat">检测中…</span>
  <span style="flex:1"></span>
  <input id="cwd" size="34" value="/workspaces/build_xime_home/xime" title="远端工作目录">
  <select id="model" title="模型">
    <option value="auto">auto</option>
    <option value="gpt-6-luna">gpt-6-luna</option>
    <option value="mai-code-1.1-flash">mai-code-1.1-flash</option>
  </select>
</header>
<div class="layout">
  <aside>
    <div class="h"><span>会话历史</span><span id="hcount"></span></div>
    <div id="hist"></div>
  </aside>
  <main>
    <div id="stream">
      <div id="empty">
        <b>命令式调用 · Allow All · 网页实时同步</b><br>
        AI 执行的每条命令、stdout/stderr 回显都会实时出现在这里<br>
        <div class="chips">
          <span data-q="只读分析当前仓库并给出中文 Markdown 总结；禁止修改任何文件、禁止运行写操作">只读分析整个项目</span>
          <span data-q="查看 git 状态和最近 5 条提交，用中文总结">git 状态速览</span>
          <span data-q="统计这个仓库的代码规模（各语言文件数与行数），中文回答">代码规模统计</span>
        </div>
      </div>
    </div>
    <div class="composer">
      <div class="row">
        <textarea id="inp" placeholder="给远端 Copilot 下任务…（Ctrl+Enter 发送，长任务可跑十几分钟）"></textarea>
        <button id="send">发送</button>
        <button id="stop" class="ghost" disabled>■ 停止</button>
      </div>
      <div class="hint">非交互 copilot -p · --allow-all-tools · JSONL 事件流经 MQTT-PTY 桥接增量同步 · 历史存本机 web_sessions</div>
    </div>
  </main>
</div>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const stream=$('#stream'), inp=$('#inp');

// ---------- 工具 ----------
function esc(s){return (s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function md2html(md){ // 最小 Markdown: 围栏代码/行内代码/标题/粗体/列表/换行
  const codes=[];
  md=md.replace(/```(\w*)\n?([\s\S]*?)```/g,(_,l,c)=>{codes.push(`<pre><code>${esc(c.replace(/\n$/,''))}</code></pre>`);return`\u0000${codes.length-1}\u0000`;});
  let html=esc(md);
  html=html.replace(/`([^`]+?)`/g,(m,c)=>codes.push(`<code>${esc(c)}</code>`)?`\u0001${codes.length-1}\u0001`:m);
  html=html.replace(/^### (.*)$/gm,'<h3>$1</h3>').replace(/^## (.*)$/gm,'<h2>$1</h2>').replace(/^# (.*)$/gm,'<h1>$1</h1>');
  html=html.replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>');
  html=html.replace(/^[-*] (.*)$/gm,'<li>$1</li>').replace(/(<li>[\s\S]*?<\/li>)/g,'<ul>$1</ul>');
  html=html.split(/\n{2,}/).map(p=>p.startsWith('<h')||p.startsWith('<ul')||p.startsWith('\u0000')?p:`<p>${p.replace(/\n/g,'<br>')}</p>`).join('');
  html=html.replace(/\u0001(\d+)\u0001/g,(_,i)=>codes[+i]);
  html=html.replace(/\u0000(\d+)\u0000/g,(_,i)=>codes[+i]);
  return html;
}
function scroll(){stream.scrollTop=stream.scrollHeight;}
function addMsg(cls, htmlText, raw){
  const d=document.createElement('div');d.className='msg';
  const b=document.createElement('div');b.className='bubble '+cls;
  if(raw){b.textContent=raw;}else{b.innerHTML=htmlText;}
  d.appendChild(b);stream.appendChild(d);scroll();return b;
}
function sysLine(t){const b=addMsg('sys','',t);b.style.textAlign='center';return b;}

// ---------- 工具卡片 ----------
const tools=new Map();
function toolStart(d){
  toolSeq++;
  const el=document.createElement('div');el.className='msg tool open';
  const args=d.arguments||{};
  const target=args.command||args.path||args.pattern||args.query||JSON.stringify(args).slice(0,200);
  el.innerHTML=`<div class="hd">
      <span class="idx">#${toolSeq}</span><span class="tname">${esc(d.toolName||'tool')}</span>
      <span class="desc">${esc(args.description||d.toolTitle||'')}</span>
      <span class="rc run"><span class="spin" style="display:inline-block;margin-right:5px;vertical-align:-1px"></span>运行中</span>
    </div>
    <div class="bd"><div class="cmd">${esc(target)}</div><div class="out"></div></div>`;
  el.querySelector('.hd').onclick=()=>el.classList.toggle('open');
  stream.appendChild(el);scroll();
  tools.set(d.toolCallId,{el,out:el.querySelector('.out'),done:false});
}
function toolOut(d){
  const t=tools.get(d.toolCallId);if(!t)return;
  const span=document.createElement('span');
  if(d.stream==='stderr')span.className='errline';
  span.textContent=d.text||'';t.out.appendChild(span);scroll();
}
function toolDone(d){
  const t=tools.get(d.toolCallId);if(!t||t.done)return;t.done=true;
  const rc=t.el.querySelector('.rc');
  const code=(d.shellExecution&&d.shellExecution.exitCode);
  const ok=d.success!==false&&(code===0||code===undefined||code===null);
  rc.className='rc '+(ok?'ok':'bad');
  rc.textContent=(code===undefined||code===null)?(ok?'完成':'失败'):('退出码 '+code);
  // 最终结果补全(非 shell_output 通道的工具)
  if((!t.out.textContent||t.out.textContent.length<20)&&d.result&&d.result.content&&!t.out.dataset.final){
    t.out.textContent=String(d.result.content).slice(0,8000);t.out.dataset.final='1';
  }
  scroll();
}

// ---------- 助手消息 ----------
const msgs=new Map(), phases=new Map();
function msgStart(d){phases.set(d.messageId,d.phase||'');}
function msgDelta(d){
  const id=d.messageId;let m=msgs.get(id);
  if(!m){
    const phase=phases.get(id);
    const cls=phase==='final_answer'?'final md':'thinking';
    const el=addMsg(cls,'');
    el.dataset.raw='';
    if(cls==='thinking'){el.innerHTML='<span>💭 思考过程（点击展开）</span><div class="body"></div>';}
    m={el,text:'',phase:phase||''};msgs.set(id,m);
  }
  m.text+=d.deltaContent||'';
  if(m.phase==='final_answer')m.el.innerHTML=md2html(m.text);
  else m.el.querySelector('.body').textContent=m.text;
  scroll();
}
function msgFull(d){
  const id=d.messageId||d.id;let content=d.content;
  if(typeof content!=='string'&&Array.isArray(content))
    content=content.map(x=>typeof x==='string'?x:(x.text||x.content||'')).join('');
  if(!content)return;
  let m=msgs.get(id);
  if(!m){m={el:addMsg('final md',''),text:'',phase:'final_answer'};msgs.set(id,m);}
  m.text=content;
  if(m.phase==='final_answer'||m.el.classList.contains('md'))m.el.innerHTML=md2html(content);
  else{m.el.classList.add('open');m.el.querySelector('.body').textContent=content;}
  scroll();
}

// ---------- 事件分发 ----------
function onEvent(ev){
  const t=ev.type,d=ev.data;
  if(t==='user.message'){/* 发送时已本地渲染 */}
  else if(t==='session.auto_mode_resolved')
    sysLine('🧠 路由模型：'+(d.chosenModel||'?')+(d.routingMethod?'（'+d.routingMethod+'）':''));
  else if(t==='model.call_start'&&d.model)sysLine('▶ 模型调用：'+d.model);
  else if(t==='assistant.message_start')msgStart(d);
  else if(t==='assistant.message_delta')msgDelta(d);
  else if(t==='assistant.message')msgFull(d);
  else if(t==='tool.execution_start')toolStart(d);
  else if(t==='tool.shell_output')toolOut(d);
  else if(t==='tool.execution_complete')toolDone(d);
  else if(t==='session.usage_checkpoint'){
    const u=JSON.stringify(d);if(u.length<260)sysLine('用量：'+u);
  }
  else if(t==='result')sysLine(d.durationMs?('── 任务结束（'+Math.round(d.durationMs/1000)+'s）'):'── 任务结束');
}

// ---------- 会话视图(实时/历史统一) ----------
let cur=null, viewId=null, pollTimer=null, toolSeq=0;
function clearEmpty(){const e=$('#empty');if(e)e.remove();}
function resetView(){
  clearEmpty();
  stream.innerHTML='';toolSeq=0;tools.clear();msgs.clear();phases.clear();
}
async function poll(){
  if(!cur)return;
  try{
    const r=await(await fetch(`/api/poll?id=${cur.id}&offset=${cur.off||0}`)).json();
    if(r.error){if(viewId===cur.id)sysLine('⚠ '+(r.message||r.error));return finish();}
    // 只在主区正展示该实时会话时渲染; 看别的历史时数据照常落盘, 不串台
    if(viewId===cur.id)(r.events||[]).forEach(onEvent);
    cur.off=r.offset;
    if(viewId===cur.id&&r.err)sysLine('⚠ 远端 stderr：'+esc(r.err));
    if(r.done){
      if(viewId===cur.id&&r.rc!==0)sysLine('⚠ copilot 退出码 '+r.rc);
      return finish();
    }
  }catch(e){if(viewId===cur.id)sysLine('⚠ 轮询失败：'+e.message);}
}
function finish(){
  clearInterval(pollTimer);pollTimer=null;
  $('#send').disabled=false;$('#stop').disabled=true;
  if(cur){cur.running=false;loadHistory();}
  cur=null;
}
function startTimer(){
  clearInterval(pollTimer);
  pollTimer=setInterval(poll,1000);
}

async function sendTask(){
  const prompt=inp.value.trim();if(!prompt||$('#send').disabled)return;
  let st=await status();
  if(!st||!st.attached){alert('桥接/PTY 未连接，请先恢复 codespace 与常驻 PTY。');return;}
  inp.value='';autoGrow();
  $('#send').disabled=true;$('#stop').disabled=false;
  let r;
  try{
    r=await(await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({prompt,cwd:$('#cwd').value.trim(),model:$('#model').value})})).json();
  }catch(e){alert('发送失败：'+e.message);$('#send').disabled=false;$('#stop').disabled=true;return;}
  if(r.error){alert('发送失败：'+(r.message||r.error));$('#send').disabled=false;$('#stop').disabled=true;return;}
  cur={id:r.id,off:0,running:true};
  await openSession(r.id);
  startTimer();loadHistory();
}
async function stopTask(){
  if(!cur)return;
  await fetch('/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:cur.id})});
  if(viewId===cur.id)sysLine('■ 已发送停止信号');
}

// ---------- 历史 / 会话打开 ----------
function fmtTime(ts){if(!ts)return'';const d=new Date(ts*1000);
  return (d.getMonth()+1)+'/'+d.getDate()+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');}
async function loadHistory(){
  const r=await(await fetch('/api/sessions')).json();
  const h=$('#hist');$('#hcount').textContent=(r.sessions||[]).length;
  h.innerHTML=(r.sessions||[]).map(m=>{
    const running=!m.finished;
    const tag=running?'<span class="tag run">运行中</span>'
      :m.rc===0?'<span class="tag ok">成功</span>':'<span class="tag bad">rc '+(m.rc??'?')+'</span>';
    return `<div class="sitem${m.id===viewId?' active':''}" data-id="${m.id}"><div class="p">${esc((m.prompt||'').slice(0,90))}</div>
      <div class="t">${tag}<span>${fmtTime(m.created)}</span></div></div>`;
  }).join('');
  $$('.sitem').forEach(el=>el.onclick=()=>openSession(el.dataset.id));
}
async function openSession(id){
  viewId=id;
  $$('.sitem').forEach(x=>x.classList.toggle('active',x.dataset.id===id));
  resetView();
  let meta={};
  try{meta=await(await fetch('/api/session/'+id)).json();}catch(e){}
  const live=!!(cur&&cur.id===id&&cur.running);
  sysLine((live?'🚀 实时任务':'📚 历史会话')+' · '+id+' · '+fmtTime(meta.created)+' · '+(meta.cwd||''));
  addMsg('user','',meta.prompt||'');
  // 已落盘事件全量回放(本地文件, 秒开); 实时新增由 poll 增量追加
  let off=0,guard=0;
  while(guard++<50){
    const r=await(await fetch(`/api/session/${id}/events?offset=${off}`)).json();
    if(r.events)(r.events||[]).forEach(onEvent);
    off=r.offset;
    if(r.done)break;
    await new Promise(res=>setTimeout(res,150));
  }
  if(!live)sysLine(meta.rc===0?'── 已完成':'── 已结束 rc='+(meta.rc??'?'));
  if(cur&&cur.id===id)cur.off=off;
  scroll();
}

// ---------- 状态灯 ----------
async function status(){
  try{return await(await fetch('/api/status')).json();}catch(e){return null;}
}
async function refreshStatus(){
  const st=await status(),dot=$('#dot'),s=$('#stat');
  if(st&&st.attached){dot.className='dot on';
    s.textContent='已连接 · '+st.brokers+(st.busy?' · PTY 忙':'');}
  else{dot.className='dot off';s.textContent='桥接断联（需人工恢复 codespace / PTY）';}
}
function autoGrow(){inp.style.height='auto';inp.style.height=Math.min(inp.scrollHeight,220)+'px';}

inp.addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();sendTask();}});
inp.addEventListener('input',autoGrow);
$('#send').onclick=sendTask;$('#stop').onclick=stopTask;
document.addEventListener('click',e=>{const c=e.target.closest('.chips span');if(c)inp.value=c.dataset.q;});
refreshStatus();setInterval(refreshStatus,5000);loadHistory();
</script>
</body></html>"""


def main():
    global PORT
    httpd = None
    for p in range(PORT, PORT + 8):
        try:
            httpd = ThreadingHTTPServer((HOST, p), Handler)
            PORT = p
            break
        except OSError:
            continue
    if httpd is None:
        print("[!] 端口 %s 起均被占用" % list(range(PORT, PORT + 8)))
        sys.exit(4)
    url = "http://%s:%d/" % (HOST, PORT)
    print("=" * 60)
    print(" Codespace Copilot 网页端已启动")
    print(" 地址: %s" % url)
    print(" 桥接: %s" % DEFAULT_BRIDGE)
    print(" 历史: %s" % STORE_DIR)
    print(" Ctrl+C 停止")
    print("=" * 60)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    resume_watchers()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n再见")


if __name__ == "__main__":
    main()
