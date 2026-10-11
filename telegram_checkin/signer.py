# -*- coding: utf-8 -*-
"""tg-signer 调用层：写出配置、执行一次任务、抽取证据。

关键约束（实测）：
- 会话文件 my_account.session 位于仓库根，tg-signer 必须在**仓库根**为工作目录启动，
  否则会认不到会话而进入交互式登录 → 计划任务里必然失败。
- 全局选项（-a/-w/-p/--log-dir）必须写在子命令 run-once **之前**，否则 click 直接报错。
"""
from __future__ import annotations

import os
import subprocess
import time

import clash
import store

CREATE_NO_WINDOW = 0x08000000
ACCOUNT = os.environ.get("TG_ACCOUNT", "my_account")


def exe_path():
    return clash.find_tg_signer()


def _env() -> dict:
    e = dict(os.environ)
    e["TG_PROXY"] = e.get("TG_PROXY") or clash.PROXY_URL
    e["PYTHONUTF8"] = "1"
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def _base_cmd():
    exe = exe_path()
    if not exe:
        raise FileNotFoundError(
            "找不到 tg-signer 命令。请在命令行执行：pip install -U \"tg-signer[gui,speedup,yaml]\"")
    return [exe, "-a", ACCOUNT, "-w", ".signer", "-p", clash.PROXY_URL,
            "--log-dir", ".signer/logs"]


def run_once(task_name: str, logf=print, timeout: int = 1200):
    """执行一次 tg-signer run-once <task>。返回 (退出码, 完整输出)。"""
    logf = logf or (lambda m: None)
    cmd = _base_cmd() + ["run-once", task_name]
    logf("调用：" + " ".join(cmd))
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(store.repo_root()), env=_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW)
    except OSError as e:
        logf(f"启动 tg-signer 失败：{e}")
        return -1, ""
    lines, timed_out = [], False
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.rstrip("\r\n")
            if line.strip():
                lines.append(line)
                logf("  " + line)
            if timeout and (time.time() - t0) > timeout:
                timed_out = True
                logf(f"超时 {timeout}s → 终止 tg-signer")
                proc.kill()
                break
    except Exception as e:  # noqa: BLE001 - 读流异常不该让整条任务崩掉
        logf(f"读取输出异常：{type(e).__name__}: {e}")
    finally:
        try:
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
    rc = proc.returncode if proc.returncode is not None else -1
    logf(f"tg-signer 结束：退出码={rc}，耗时 {time.time() - t0:.1f}s，输出 {len(lines)} 行"
         + ("（超时）" if timed_out else ""))
    return rc, "\n".join(lines)


def count_sent(out: str) -> int:
    """tg-signer 每条指令执行成功都会打印「处理完成: action=...」。"""
    return (out or "").count("处理完成")


def records(task_name: str, limit: int = 5) -> str:
    """读签到记录（SQLite 里的真实结果，比聊天窗口可靠——机器人会清理会话）。"""
    try:
        cmd = _base_cmd() + ["list-sign-records", task_name]
    except FileNotFoundError as e:
        return str(e)
    try:
        p = subprocess.run(cmd, cwd=str(store.repo_root()), env=_env(),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180,
                           creationflags=CREATE_NO_WINDOW)
    except Exception as e:  # noqa: BLE001
        return f"读取签到记录失败：{e}"
    txt = (p.stdout or "").strip()
    if not txt:
        return "（暂无签到记录）"
    return "\n".join(txt.splitlines()[-limit:])


def version() -> str:
    exe = exe_path()
    if not exe:
        return "未安装"
    try:
        p = subprocess.run([exe, "version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60,
                           creationflags=CREATE_NO_WINDOW)
        return (p.stdout or p.stderr or "").strip().splitlines()[-1] if (p.stdout or p.stderr) else "未知"
    except Exception:  # noqa: BLE001
        return "未知"
