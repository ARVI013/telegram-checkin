# -*- coding: utf-8 -*-
"""任务注册表。

- 任务定义：<repo>\\.signer\\checkin_tasks\\<name>.json（本程序自己的模型：任务名/执行时间/步骤/Clash 开关）
- 同步产物：<repo>\\.signer\\signs\\<name>\\config.json（tg-signer 0.9.1 的 SignConfigV3 格式，执行时只认这个）

调度由 Windows 计划任务负责（见 sched.py），所以 tg-signer 自己的 sign_at 不承担调度职责；
仍然写进去是为了让 `tg-signer run <task>` 手动跑时也有个合理时间。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
_HERE = Path(__file__).resolve().parent
CFG_NAME = "telegram_checkin.config.json"


# ---------------------------------------------------------------- 路径
def config_paths():
    """配置文件候选位置：exe 同级 → 包目录 → 用户主目录（前面的优先）。"""
    out = []
    if getattr(sys, "frozen", False):
        out.append(Path(sys.executable).resolve().parent / CFG_NAME)
    out.append(_HERE / CFG_NAME)
    out.append(_HERE / "local.config.json")
    out.append(Path.home() / ".telegram_checkin.json")
    return out


def load_config() -> dict:
    for p in config_paths():
        try:
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
    return {}


def save_config(data: dict) -> Path:
    p = config_paths()[0]
    try:
        merged = load_config()
        merged.update(data)
        p.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return p


def repo_root() -> Path:
    """tg-signer 工作目录（会话文件 my_account.session 所在处）。

    解析顺序：环境变量 TG_SIGNER_REPO → 配置文件里的 repo_root →
    自动探测（包目录上一级 / 当前目录，取第一个含 .signer 或 my_account.session 的）→ 包目录上一级。
    """
    env = os.environ.get("TG_SIGNER_REPO")
    if env:
        return Path(env)
    cfg = load_config().get("repo_root")
    if cfg:
        return Path(cfg)
    for c in (_HERE.parent, Path.cwd()):
        try:
            if (c / ".signer").exists() or (c / "my_account.session").exists():
                return c
        except OSError:
            continue
    return _HERE.parent


def workdir() -> Path:
    return repo_root() / ".signer"


def tasks_dir() -> Path:
    return workdir() / "checkin_tasks"


def signs_dir() -> Path:
    return workdir() / "signs"


def run_logs_dir() -> Path:
    return workdir() / "logs" / "checkin"


def mkdirs() -> None:
    for d in (tasks_dir(), signs_dir(), run_logs_dir()):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 模型
def new_task(name: str = "") -> dict:
    return {
        "name": name,
        "time": "06:00",
        "enabled": True,
        "manage_clash": True,
        "stop_clash_after": True,
        "close_clash_always": True,
        "step_interval": 30,
        "steps": [],
    }


def valid_name(name: str) -> bool:
    return bool(NAME_RE.fullmatch((name or "").strip()))


def parse_chat_id(text: str):
    """'@xxx' 保留字符串；纯数字转 int；其他原样交给 tg-signer 解析。"""
    t = (text or "").strip()
    if t.startswith("@"):
        return t
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    return t


def _norm_time(v) -> str:
    s = str(v or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if not m:
        return "06:00"
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return "06:00"
    return f"{h:02d}:{mi:02d}"


def normalize(task: dict) -> dict:
    t = new_task(str((task or {}).get("name") or "").strip())
    for k in list(t):
        if k in (task or {}):
            t[k] = task[k]
    t["name"] = str(t["name"]).strip()
    t["time"] = _norm_time(t.get("time"))
    try:
        t["step_interval"] = max(0, int(t.get("step_interval") or 0))
    except (TypeError, ValueError):
        t["step_interval"] = 30
    t["enabled"] = bool(t.get("enabled", True))
    t["manage_clash"] = bool(t.get("manage_clash", True))
    t["stop_clash_after"] = bool(t.get("stop_clash_after", True))
    t["close_clash_always"] = bool(t.get("close_clash_always", True))
    steps = []
    for s in (task or {}).get("steps") or []:
        if not isinstance(s, dict):
            continue
        cmd = str(s.get("command") or "").strip()
        if not cmd:
            continue
        raw_id = s.get("chat_id", "")
        steps.append({
            "chat_id": raw_id if isinstance(raw_id, int) else parse_chat_id(str(raw_id)),
            "label": str(s.get("label") or "").strip() or str(raw_id),
            "command": cmd,
        })
    t["steps"] = steps
    return t


# ---------------------------------------------------------------- 读写
def list_tasks() -> list:
    out = []
    d = tasks_dir()
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            out.append(normalize(json.loads(f.read_text(encoding="utf-8-sig"))))
        except (OSError, ValueError):
            continue
    return out


def get_task(name: str):
    for t in list_tasks():
        if t["name"] == name:
            return t
    return None


def save_task(task: dict) -> dict:
    t = normalize(task)
    if not valid_name(t["name"]):
        raise ValueError("任务名只能用字母、数字、下划线、短横线，长度 1-40")
    if not t["steps"]:
        raise ValueError("至少需要 1 条待发送指令")
    mkdirs()
    (tasks_dir() / f"{t['name']}.json").write_text(
        json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
    write_signer_config(t)
    return t


def delete_task(name: str) -> bool:
    f = tasks_dir() / f"{name}.json"
    hit = f.is_file()
    if hit:
        f.unlink()
    d = signs_dir() / name
    if d.is_dir():
        for p in d.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            d.rmdir()
        except OSError:
            pass
    return hit


def write_signer_config(task: dict) -> Path:
    """写出 tg-signer 的 signs\\<name>\\config.json（字段与 0.9.1 SignConfigV3 对齐）。"""
    t = normalize(task)
    chats = [{
        "chat_id": s["chat_id"],
        "message_thread_id": None,
        "name": s["label"],
        "delete_after": None,
        "actions": [{"action": 1, "text": s["command"]}],  # 1 == SEND_TEXT
        "action_interval": 1.0,
    } for s in t["steps"]]
    cfg = {
        "chats": chats,
        "sign_at": t["time"] + ":00",
        "random_seconds": 0,
        "sign_interval": max(1, t["step_interval"]),  # 相邻两条指令之间的间隔（秒）
    }
    p = signs_dir() / t["name"] / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def summarize(task: dict) -> str:
    n = len(task.get("steps") or [])
    first = task["steps"][0]["command"] if n else ""
    return f"{n} 条指令" + (f"（首条 {first}）" if first else "")
