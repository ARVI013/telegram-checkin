# -*- coding: utf-8 -*-
"""Clash for Windows 管控：预检、启动、选节点、连通性验证、**从托盘菜单优雅退出**。

为什么不直接用 taskkill：
  CFW 会自己设置系统代理（HKCU Internet Settings -> 127.0.0.1:7890）。强杀进程后
  代理配置还指向已死的 7890，于是「整个网络都用不了」。托盘菜单里的「退出」走的是
  CFW 自己的退出流程，会把系统代理、内核一并收拾干净 —— 这才是正确的收尾方式。
  托盘退出失败时才降级到 taskkill，并强制修复悬空的系统代理。

纯标准库 + uiautomation（用来定位托盘图标；托盘菜单本身是 Win32 菜单，用 ctypes 读）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import glob
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- 常量
CLASH_LNK = r"C:\Users\Public\Desktop\Clash for Windows.lnk"
CLASH_GUI = "Clash for Windows.exe"
CLASH_CORE = "clash-win64.exe"
CLASH_CFG = os.path.join(os.environ.get("USERPROFILE", ""), ".config", "clash", "config.yaml")
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7890
PROXY_URL = f"socks5://{PROXY_HOST}:{PROXY_PORT}"
DELAY_PROBE_URL = "http://www.gstatic.com/generate_204"
GROUP_PRIMARY = "节点选择"
GROUP_AUTO = "自动选择"
MAX_MANUAL_CANDIDATES = 12
CREATE_NO_WINDOW = 0x08000000
EXIT_WORDS = ("退出", "Exit", "Quit", "退出程序")
FAKE_NODE_HINT = ("剩余流量", "套餐到期", "到期时间", "官网", "过滤", "DIRECT", "REJECT", "GLOBAL")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowExW.restype = wt.HWND
user32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [wt.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
user32.GetMenuStringW.argtypes = [wt.HMENU, ctypes.c_uint, wt.LPWSTR, ctypes.c_int, ctypes.c_uint]
user32.GetMenuItemCount.argtypes = [wt.HMENU]
user32.GetMenuItemCount.restype = ctypes.c_int
user32.GetMenuItemID.argtypes = [wt.HMENU, ctypes.c_int]
user32.GetMenuItemID.restype = ctypes.c_uint
user32.GetMenuItemRect.argtypes = [wt.HWND, wt.HMENU, ctypes.c_int, ctypes.POINTER(wt.RECT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.mouse_event.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]

MN_GETHMENU = 0x01E1
MF_BYPOSITION = 0x0400
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- 基础查询
def _run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW, errors="replace")
        return p.stdout or ""
    except Exception:
        return ""


def pids_of(name):
    """按镜像名取 pid，不依赖 psutil。"""
    out = _run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"])
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == name.lower():
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def listening_ports(pid):
    """该进程正在 LISTEN 的 TCP 端口。"""
    ports = set()
    for line in _run(["netstat", "-ano", "-p", "tcp"]).splitlines():
        f = line.split()
        if len(f) >= 5 and f[3].upper() == "LISTENING" and f[4] == str(pid):
            m = re.search(r":(\d+)$", f[1])
            if m:
                ports.add(int(m.group(1)))
    return sorted(ports)


def port_listening(port):
    for line in _run(["netstat", "-ano", "-p", "tcp"]).splitlines():
        f = line.split()
        if len(f) >= 4 and f[3].upper() == "LISTENING" and f[1].endswith(f":{port}"):
            return True
    return False


def find_tg_signer():
    cand = shutil.which("tg-signer") or shutil.which("tg-signer.exe")
    if cand:
        return cand
    pats = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Python3*", "Scripts", "tg-signer.exe"),
        os.path.join(os.environ.get("APPDATA", ""), "Python", "Python3*", "Scripts", "tg-signer.exe"),
        r"C:\Python3*\Scripts\tg-signer.exe",
    ]
    for pat in pats:
        hit = glob.glob(pat)
        if hit:
            return hit[0]
    return None


# ---------------------------------------------------------------- 控制器 / 连通性
def read_controller_cfg():
    """从 clash 的 config.yaml 里取 external-controller 端口与 secret。"""
    try:
        with open(CLASH_CFG, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return None, ""
    m = re.search(r"^external-controller:\s*['\"]?([\d.]+):(\d+)", txt, re.M)
    port = int(m.group(2)) if m else None
    s = re.search(r"^secret:\s*['\"]?([^\s'\"]+)", txt, re.M)
    return port, (s.group(1) if s else "")


def _api(port, secret, path, method="GET", payload=None, timeout=6):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    if secret:
        req.add_header("Authorization", f"Bearer {secret}")
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        req.data = data
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


def find_controller(timeout=60, interval=3):
    """返回 (port, secret) —— 端口对 /version 有真实响应才算数。"""
    deadline = time.time() + timeout
    while True:
        cand = []
        cfg_port, secret = read_controller_cfg()
        if cfg_port:
            cand.append(cfg_port)
        for pid in pids_of(CLASH_CORE):
            cand.extend(listening_ports(pid))
        for port in dict.fromkeys(cand):
            try:
                info = _api(port, secret, "/version", timeout=3)
                if info.get("version"):
                    return port, secret
            except Exception:
                continue
        if time.time() >= deadline:
            return None, ""
        time.sleep(interval)


def proxy_egress_ok(timeout=8.0):
    """手写 CONNECT + TLS：验证「代理 -> 节点 -> 目标站」整条链路真的通。"""
    for host in ("api.telegram.org", "www.gstatic.com"):
        s = None
        try:
            s = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=timeout)
            s.settimeout(timeout)
            s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = s.recv(4096)
                if not chunk:
                    break
                resp += chunk
            if not resp.startswith(b"HTTP/"):
                s.close()
                continue
            if int(resp.split(b" ")[1]) != 200:
                s.close()
                continue
            ctx = ssl.create_default_context()
            tls = ctx.wrap_socket(s, server_hostname=host)
            tls.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            data = tls.recv(64)
            try:
                tls.close()
            except Exception:
                s.close()
            if data.startswith(b"HTTP/"):
                return True, f"{host} → {data.split(b'\r\n')[0].decode('latin-1')}"
        except Exception as e:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
            last = f"{type(e).__name__}: {e}"
    return False, locals().get("last", "代理不可用（7890 未监听或节点全部不通）")


# ---------------------------------------------------------------- 选节点
def is_fake_node(name):
    return any(h in name for h in FAKE_NODE_HINT)


def node_delay(port, secret, node, timeout_ms=3000):
    q = urllib.parse.quote(node, safe="")
    url = urllib.parse.quote(DELAY_PROBE_URL, safe="")
    try:
        info = _api(port, secret, f"/proxies/{q}/delay?timeout={timeout_ms}&url={url}", timeout=(timeout_ms / 1000 + 4))
        d = info.get("delay")
        return int(d) if isinstance(d, int) else None
    except Exception:
        return None


def switch_node(port, secret, group, node):
    _api(port, secret, f"/proxies/{urllib.parse.quote(group, safe='')}", method="PUT", payload={"name": node})


def current_node(port, secret, group=GROUP_PRIMARY):
    try:
        info = _api(port, secret, f"/proxies/{urllib.parse.quote(group, safe='')}")
        return info.get("now")
    except Exception:
        return None


def group_candidates(port, secret, group=GROUP_PRIMARY):
    try:
        info = _api(port, secret, f"/proxies/{urllib.parse.quote(group, safe='')}")
        return [n for n in info.get("all", [])]
    except Exception:
        return []


def ensure_usable_node(port, secret, force_switch=False, already_ok=False, logf=log):
    """保证「节点选择」下的当前节点真的能连通 Telegram。"""
    if already_ok and not force_switch:
        logf(f"节点选择：当前「{current_node(port, secret) or '未知'}」已连通，保持不动")
        return True
    allnodes = group_candidates(port, secret)
    logf(f"目标组 '{GROUP_PRIMARY}'，当前选择 '{current_node(port, secret)}'，候选 {len(allnodes)} 个")
    # 先让 URLTest 组自己选
    if GROUP_AUTO in allnodes:
        best, best_d = None, None
        for n in group_candidates(port, secret, GROUP_AUTO):
            if is_fake_node(n):
                continue
            d = node_delay(port, secret, n)
            if d is not None and (best_d is None or d < best_d):
                best, best_d = n, d
        if best:
            logf(f"URLTest 组 '{GROUP_AUTO}' 测速完成，最优延迟 {best_d} ms")
            switch_node(port, secret, GROUP_PRIMARY, GROUP_AUTO)
            ok, why = proxy_egress_ok()
            logf(f"节点选择：切到「{GROUP_AUTO}」后{'连通：' + why if ok else '仍不通：' + why}")
            if ok:
                return True
    # 兜底：逐个测速真实节点
    nodes = [n for n in allnodes if n != GROUP_AUTO and not is_fake_node(n)]
    scored = []
    for n in nodes[:40]:
        d = node_delay(port, secret, n)
        if d is not None:
            scored.append((d, n))
    scored.sort()
    if not scored:
        logf("节点选择：所有候选都测速失败")
        return False
    for d, n in scored[:MAX_MANUAL_CANDIDATES]:
        logf(f"尝试节点「{n}」（{d} ms）")
        switch_node(port, secret, GROUP_PRIMARY, n)
        ok, why = proxy_egress_ok()
        if ok:
            logf(f"节点选择：成功 — {why}")
            return True
        logf("  ...仍不通，换下一个")
    return False


# ---------------------------------------------------------------- 启停
CFW_EXE_FALLBACK = r"D:\Clash for Windows\Clash for Windows.exe"


def _wait(pred, timeout=60, interval=1.0):
    """轮询等待条件成立。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _lnk_target(lnk):
    """解析快捷方式指向的 exe（避免依赖 pywin32）。"""
    try:
        out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    f"(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}').TargetPath"],
                   timeout=30)
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return lines[-1] if lines else ""
    except Exception:
        return ""


def _launch_via_explorer(path):
    """交给资源管理器去启动 —— 实测这是唯一能让 CFW 真正起来的路径。"""
    subprocess.Popen(["explorer.exe", path], creationflags=CREATE_NO_WINDOW)


def start_clash(logf=log, wait_seconds=60):
    """启动 CFW。

    实测根因（决定性证据）：从本进程直接 CreateProcess / ShellExecute 启动 CFW
    （os.startfile、subprocess.Popen、PowerShell Start-Process 都一样），CFW 会在
    5 秒内以**退出码 0、零输出**静默退出（Electron 拿不到它要的运行上下文/实例锁）；
    而把启动动作交给 `explorer.exe`（挂到资源管理器下）后，10 秒内就出现
    4 个 GUI 进程 + 内核 + 7890 监听。所以 explorer 是第一选择，其余方式仅作兜底。
    """
    if clash_running():
        logf("启动 Clash：已经在运行，无需启动")
        return True
    targets = []
    if os.path.exists(CLASH_LNK):
        targets.append(("explorer 打开快捷方式", "explorer", CLASH_LNK))
    if os.path.exists(CFW_EXE_FALLBACK):
        targets.append(("explorer 打开 exe", "explorer", CFW_EXE_FALLBACK))
    if os.path.exists(CLASH_LNK):
        targets.append(("快捷方式(ShellExecute 兜底)", "shell", CLASH_LNK))
    if os.path.exists(CLASH_LNK):
        tgt = _lnk_target(CLASH_LNK)
        if tgt and os.path.exists(tgt):
            targets.append(("快捷方式目标 exe(ShellExecute 兜底)", "shell", tgt))
    tried = set()
    for label, how, path in targets:
        key = (how, path)
        if key in tried or not os.path.exists(path):
            continue
        tried.add(key)
        try:
            if how == "explorer":
                _launch_via_explorer(path)
            else:
                os.startfile(path)  # noqa: S606 - 启动用户自己的程序
            logf(f"启动 Clash：已通过{label}发起启动（{os.path.basename(path)}）")
        except OSError as e:
            logf(f"启动 Clash：{label}发起启动失败 {e}")
            continue
        if _wait(clash_running, wait_seconds, 1.0):
            logf(f"启动 Clash：进程已出现（{label}）")
            return True
        logf(f"启动 Clash：{label} 发起后 {wait_seconds}s 内未见进程，换下一种方式")
    logf("启动 Clash：所有方式都未能让进程出现")
    return False


def wait_ready(timeout=180, logf=log):
    """等 CFW 内核起来且 7890 监听；状态变化才打日志，避免刷屏。"""
    t0 = time.time()
    deadline = t0 + timeout
    last = None
    while time.time() < deadline:
        gui = bool(pids_of(CLASH_GUI))
        core = bool(pids_of(CLASH_CORE))
        port = port_listening(PROXY_PORT)
        cur = (gui, core, port)
        if cur != last:
            logf(f"等待 Clash 就绪：GUI={gui} 内核={core} 7890={port}（t+{int(time.time() - t0)}s）")
            last = cur
        if core and port:
            return True
        if not gui and not core and time.time() - t0 > 30:
            logf("等待 Clash 就绪：30s 内进程始终没有出现 → 判定启动失败")
            return False
        time.sleep(2)
    return False


def clash_running():
    return bool(pids_of(CLASH_GUI) or pids_of(CLASH_CORE))


# ---------------------------------------------------------------- 托盘退出
def _move_click(x, y, right=False):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.1)
    if right:
        user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
    else:
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _read_popup_menu():
    """读 #32768 弹出菜单（Electron 的托盘菜单就是 Win32 菜单）。"""
    h = user32.FindWindowW("#32768", None)
    if not h:
        return None
    hmenu = user32.SendMessageW(h, MN_GETHMENU, None, None)
    if not hmenu:
        return None
    n = user32.GetMenuItemCount(hmenu)
    if n <= 0:
        return None
    items = []
    for i in range(n):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetMenuStringW(hmenu, i, buf, 256, MF_BYPOSITION)
        items.append((i, buf.value.strip()))
    return {"hwnd": h, "hmenu": hmenu, "items": items}


def _click_menu_item(hmenu, index):
    r = wt.RECT()
    if not user32.GetMenuItemRect(None, hmenu, index, ctypes.byref(r)):
        return False
    _move_click((r.left + r.right) // 2, (r.top + r.bottom) // 2, right=False)
    return True


def _walk(ctrl, max_depth=8):
    """广度优先遍历控件树（只依赖 GetChildren，避免踩 uiautomation 的接口差异）。"""
    stack = [(ctrl, 0)]
    while stack:
        node, depth = stack.pop()
        yield node
        if depth >= max_depth:
            continue
        try:
            kids = node.GetChildren()
        except Exception:
            kids = None
        for k in (kids or []):
            stack.append((k, depth + 1))


def _find_tray_ctrl(root, aids, name_ok=None):
    """在控件树里找托盘按钮；name_ok 用来排除「网络 Clash」（网络图标名里也含 Clash）。"""
    for node in _walk(root):
        try:
            if (node.AutomationId or "") not in aids:
                continue
            if (node.ControlTypeName or "") != "ButtonControl":
                continue
            nm = (node.Name or "").strip()
            if name_ok is not None and not name_ok(nm):
                continue
            return node
        except Exception:
            continue
    return None


def _is_clash_icon_name(nm):
    low = nm.lower()
    return low.startswith("clash for windows") or low.startswith("clash win")


def _is_chevron_name(nm):
    low = nm.lower()
    return "隐藏" in nm or "hidden" in low


def _click_ctrl(ctrl):
    r = ctrl.BoundingRectangle
    _move_click(int((r.left + r.right) / 2), int((r.top + r.bottom) / 2))


def _dump_tree(root, logf, limit=30):
    """失败时把控件树样本打进日志，便于定位（只依赖 GetChildren）。"""
    n = 0
    for node in _walk(root, 10):
        try:
            logf(f"   [{n}] type={node.ControlTypeName!r} aid={node.AutomationId!r} name={(node.Name or '')[:60]!r}")
        except Exception:
            pass
        n += 1
        if n >= limit:
            break
    return n


def _visible_windows():
    """枚举可见顶层窗口：hwnd -> {cls,title,rect,pid}。"""
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    res = {}

    def _cb(hwnd, lp):
        if user32.IsWindowVisible(hwnd):
            cb = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cb, 256)
            tb = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, tb, 256)
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            res[int(hwnd)] = {"cls": cb.value, "title": tb.value,
                              "rect": (r.left, r.top, r.right, r.bottom), "pid": pid.value}
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return res


def _find_menu_window(before, timeout=6.0, logf=log):
    """右键后等 CFW 的托盘菜单窗口。

    重要事实（实测）：CFW 的托盘菜单 **不是** Win32 的 `#32768` 菜单，
    而是 Electron 自绘窗口 —— class `Chrome_WidgetWin_1`、无标题、窄条（约 192x613），
    紧贴托盘图标上方。所以只能靠「右键后新出现的窄 Chrome_WidgetWin_1」来认它。
    """
    end = time.time() + timeout
    seen = set()
    while time.time() < end:
        for h, v in _visible_windows().items():
            if h in before or h in seen:
                continue
            seen.add(h)
            w = v["rect"][2] - v["rect"][0]
            ht = v["rect"][3] - v["rect"][1]
            if v["cls"] == "Chrome_WidgetWin_1" and 0 < w < 500 and ht > 60:
                logf(f"菜单窗口：hwnd={h} pid={v['pid']} 尺寸={w}x{ht} rect={v['rect']}")
                return h
            logf(f"忽略新窗口 hwnd={h} pid={v['pid']} class={v['cls']!r} 尺寸={w}x{ht}")
        time.sleep(0.15)
    return None


def _menu_items(menu_h):
    """读自绘菜单里的条目：(名字, rect, 控件)。"""
    import uiautomation as auto

    ctrl = auto.ControlFromHandle(menu_h)
    items = []
    for node in _walk(ctrl, 12):
        try:
            nm = (node.Name or "").strip()
            r = node.BoundingRectangle
            if nm and r.right > r.left and r.bottom > r.top:
                items.append((nm, (r.left, r.top, r.right, r.bottom), node))
        except Exception:
            continue
    return items


def _esc():
    user32.keybd_event(0x1B, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(0x1B, 0, 2, 0)


# 托盘图标若不在可见区域，就藏在这个 XAML 浮窗里（Win11）
OVERFLOW_CLASS = "TopLevelWindowForOverflowXamlIsland"


def _scan_flyout(logf=None, timeout=0.0):
    """在溢出浮窗里找 CFW 图标；每次轮询都重新取句柄（Win11 的 XAML 浮窗会被重建）。"""
    import uiautomation as auto

    end = time.time() + timeout
    dumped = False
    while True:
        for cls in (OVERFLOW_CLASS, "NotifyIconOverflowWindow"):
            h = user32.FindWindowW(cls, None)
            if not h:
                continue
            fly = auto.ControlFromHandle(h)
            ic = _find_tray_ctrl(fly, {"NotifyItemIcon", "SystemTrayIcon"}, _is_clash_icon_name)
            if ic is not None:
                return ic, cls
            if logf is not None and not dumped:
                logf(f"托盘：{cls} 里暂未找到，控件树样本：")
                _dump_tree(fly, logf)
                dumped = True
        if time.time() >= end:
            return None, None
        time.sleep(0.15)


def _tray_icon_uia(logf=log, fresh=False):
    """用 UIA 找到 CFW 托盘图标；必要时先展开「显示隐藏的图标」溢出窗。

    两个实测事实：
    1. 溢出浮窗的顶层窗口 **一直存在**（关闭时 UIA 树里只有 1 个空 Pane），
       所以「窗口存在」不等于「浮窗已展开」——只能靠树里有没有图标判断；
    2. 视觉上已展开、但不是本进程刚点开的浮窗**不接受输入**（右键没有菜单），
       所以 fresh=True 时先点一次收起、再点开，拿到「刚被激活」的浮窗。
    """
    import uiautomation as auto

    auto.SetGlobalSearchTimeout(2.0)
    shell_h = user32.FindWindowW("Shell_TrayWnd", None)
    if not shell_h:
        logf("托盘：找不到任务栏 Shell_TrayWnd")
        return None
    shell = auto.ControlFromHandle(shell_h)

    icon = _find_tray_ctrl(shell, {"SystemTrayIcon"}, _is_clash_icon_name)
    if icon is not None:
        logf("托盘：CFW 图标在可见区域")
        return icon

    chevron = _find_tray_ctrl(shell, {"SystemTrayIcon"}, _is_chevron_name)
    if chevron is None:
        logf("托盘：找不到「显示隐藏的图标」按钮")
        return None

    icon, cls = _scan_flyout()
    if icon is not None:
        if not fresh:
            logf(f"托盘：{cls} 已经展开，直接命中 CFW 图标")
            return icon
        _click_ctrl(chevron)
        logf("托盘：先收起视觉上已展开的浮窗（僵状态不接受输入）")
        time.sleep(0.8)

    for attempt in (1, 2, 3):
        _click_ctrl(chevron)
        logf(f"托盘：点开「显示隐藏的图标」（第 {attempt} 次）")
        time.sleep(0.6)
        icon, cls = _scan_flyout(logf, 6)
        if icon is not None:
            logf(f"托盘：在 {cls} 里找到 CFW 图标")
            return icon
        logf(f"托盘：第 {attempt} 次点开后 6s 内没找到 CFW 图标")
        time.sleep(0.3)

    logf("托盘：溢出窗口里始终没有找到 CFW 图标")
    return None


def tray_quit(timeout=25, logf=log, attempts=3):
    """从托盘菜单选「退出」——CFW 会自己收拾系统代理与内核，不会像 taskkill 那样断网。"""
    if not clash_running():
        logf("托盘退出：Clash 本来就没在运行")
        return True

    for attempt in range(1, attempts + 1):
        logf(f"托盘退出：第 {attempt}/{attempts} 次尝试")
        try:
            icon = _tray_icon_uia(logf, fresh=True)
            if icon is None:
                continue
            r = icon.BoundingRectangle
            cx, cy = int((r.left + r.right) / 2), int((r.top + r.bottom) / 2)
            before = set(_visible_windows())
            logf(f"托盘：右键 CFW 图标 @({cx},{cy})")
            _move_click(cx, cy, right=True)
            menu_h = _find_menu_window(before, 6.0, logf)
            if menu_h is None:
                logf("托盘：右键后没有出现 CFW 菜单窗口")
                continue
            items = _menu_items(menu_h)
            texts = [n for n, _, _ in items]
            logf(f"托盘菜单项：{texts}")
            # 认门：CFW 的菜单里有「系统代理」
            if not any("系统代理" in t for t in texts):
                logf("托盘：菜单没有 CFW 特征（缺「系统代理」），Esc 关闭后重试")
                _esc()
                continue
            target = next((it for it in items
                           if it[0] in EXIT_WORDS or it[0].lower() in ("exit", "quit")
                           or it[0].startswith("退出")), None)
            if target is None:
                logf(f"托盘：菜单里没有退出项（{texts}）")
                _esc()
                continue
            nm, rect, _node = target
            tx, ty = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
            logf(f"托盘：点击「{nm}」@({tx},{ty})")
            _move_click(tx, ty)
        except Exception as e:
            logf(f"托盘退出异常：{type(e).__name__}: {e}")
            continue

        deadline = time.time() + timeout
        while time.time() < deadline:
            if not clash_running():
                logf("托盘退出：Clash 已优雅退出")
                return True
            time.sleep(0.5)
        logf(f"托盘退出：点击后 {timeout}s 内进程仍在")

    logf("托盘退出：所有尝试都失败")
    return False


def proxy_enable():
    """系统代理开关当前值（1 开 / 0 关），读不到返回 None。"""
    import winreg
    key = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ) as k:
            return int(winreg.QueryValueEx(k, "ProxyEnable")[0])
    except OSError:
        return None


def fix_dangling_system_proxy(logf=log):
    """强杀兜底：把悬空的系统代理关掉，否则全网断。"""
    import winreg
    key = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
            server = ""
            try:
                server, _ = winreg.QueryValueEx(k, "ProxyServer")
            except FileNotFoundError:
                pass
            if enable and str(PROXY_PORT) in str(server):
                winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 0)
                logf(f"已关闭悬空系统代理（原 ProxyServer={server}），避免断网")
            else:
                return False
    except OSError as e:
        logf(f"修复系统代理失败：{e}")
        return False
    try:
        wininet = ctypes.WinDLL("wininet")
        wininet.InternetSetOptionW(0, 39, 0, 0)  # SETTINGS_CHANGED
        wininet.InternetSetOptionW(0, 37, 0, 0)  # REFRESH
    except Exception:
        pass
    return True


def stop_clash(by_tray=True, logf=log):
    """收尾：优先托盘退出，失败降级 taskkill，最后保证系统代理不悬空。"""
    if not clash_running():
        logf("关闭 Clash：本来就没运行")
        return True
    if by_tray and tray_quit(logf=logf):
        return True
    logf("关闭 Clash：托盘退出不可用，降级为 taskkill（先优雅后强杀）")
    _run(["taskkill", "/IM", CLASH_GUI, "/T"])
    deadline = time.time() + 15
    while time.time() < deadline and clash_running():
        time.sleep(0.5)
    if clash_running():
        for name in (CLASH_CORE, CLASH_GUI):
            _run(["taskkill", "/F", "/IM", name, "/T"])
        time.sleep(1.5)
    ok = not clash_running()
    fix_dangling_system_proxy(logf)
    logf(f"关闭 Clash：{'已结束进程' if ok else '进程仍在'}")
    return ok


# ---------------------------------------------------------------- 状态
def status():
    port, secret = read_controller_cfg()
    live = None
    if port:
        try:
            live = _api(port, secret, "/version", timeout=2).get("version")
        except Exception:
            live = None
    ok, why = proxy_egress_ok(timeout=6) if (pids_of(CLASH_CORE) and port_listening(PROXY_PORT)) else (False, "未监听 7890")
    return {
        "gui": len(pids_of(CLASH_GUI)),
        "core": len(pids_of(CLASH_CORE)),
        "port7890": port_listening(PROXY_PORT),
        "controller": port if live else None,
        "version": live,
        "node": current_node(port, secret) if live else None,
        "egress_ok": ok,
        "egress": why,
        "tg_signer": find_tg_signer(),
    }


# ---------------------------------------------------------------- 自测 CLI
def _main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        log(json.dumps(status(), ensure_ascii=False, indent=2))
    elif cmd == "preflight":
        log(f"Clash GUI={bool(pids_of(CLASH_GUI))} 内核={bool(pids_of(CLASH_CORE))}")
        log(f"网络：{proxy_egress_ok()}")
    elif cmd == "tray-quit":
        log(f"托盘退出结果：{tray_quit()}")
        log(f"此后 Clash GUI={bool(pids_of(CLASH_GUI))} 内核={bool(pids_of(CLASH_CORE))} 7890={port_listening(PROXY_PORT)}")
    elif cmd == "stop":
        stop_clash()
    elif cmd == "start":
        start_clash()
    else:
        log(f"未知命令：{cmd}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
