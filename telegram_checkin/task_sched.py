# -*- coding: utf-8 -*-
"""Windows 计划任务同步：每个签到任务对应一个计划任务 tg-signer-<任务名>。

时间来自任务自己的配置（"time": "HH:MM"），所以「每个任务单独设置执行时间」是这么落地的：
改时间 → 程序重新 Register-ScheduledTask（-Force 覆盖）→ 触发器跟着变。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import store

PREFIX = "tg-signer-"
CREATE_NO_WINDOW = 0x08000000
_PS_HEAD = ("$ErrorActionPreference='Stop';"
            "$OutputEncoding=[Text.Encoding]::UTF8;"
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;")


def full_name(name: str) -> str:
    return PREFIX + name


def _ps(script: str, timeout: int = 120):
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", _PS_HEAD + script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def _q(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def entry():
    """计划任务要执行的命令。打包成 exe 后就是 exe 自己；开发态用 pythonw + cli.py。"""
    root = str(store.repo_root())
    if getattr(sys, "frozen", False):
        return sys.executable, "", root
    cli = Path(__file__).resolve().with_name("cli.py")
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    exe = str(pyw if pyw.exists() else py)
    return exe, f'"{cli}"', root


def _user() -> str:
    dom = os.environ.get("USERDOMAIN", "")
    usr = os.environ.get("USERNAME", "")
    return f"{dom}\\{usr}" if dom else usr


def sync_one(task: dict, logf=None):
    """按任务配置创建/更新计划任务。任务未启用则删掉它。返回 (ok, 消息)。"""
    logf = logf or (lambda m: None)
    name = task["name"]
    full = full_name(name)
    if not task.get("enabled", True):
        ok, msg = remove_one(name, logf=logf)
        return ok, f"任务「{name}」已停用 → {msg}"
    exe, pre, workdir = entry()
    args = (pre + " " if pre else "") + f"--task {name}"
    script = (
        f"$a=New-ScheduledTaskAction -Execute {_q(exe)} -Argument {_q(args)} "
        f"-WorkingDirectory {_q(workdir)};"
        f"$t=New-ScheduledTaskTrigger -Daily -At '{task['time']}';"
        "$s=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30) "
        "-MultipleInstances IgnoreNew;"
        f"$p=New-ScheduledTaskPrincipal -UserId {_q(_user())} -LogonType Interactive -RunLevel Limited;"
        f"Register-ScheduledTask -TaskName {_q(full)} -Action $a -Trigger $t -Settings $s "
        "-Principal $p -Force | Out-Null;"
        # 回读一遍实际注册进去的动作 + 下次运行时间：光看"命令返回 0"不算成功
        f"$x=Get-ScheduledTask -TaskName {_q(full)};"
        f"$i=Get-ScheduledTaskInfo -TaskName {_q(full)};"
        "\"REG|\" + $x.Actions[0].Execute + \"|\" + $x.Actions[0].Arguments + \"|\" + "
        "$x.Triggers[0].StartBoundary + \"|\" + $i.NextRunTime"
    )
    rc, out = _ps(script)
    line = ""
    for ln in (out or "").splitlines():
        if ln.startswith("REG|"):
            line = ln
    ok = rc == 0 and bool(line)
    if ok:
        parts = line.split("|")
        detail = parts[1] if len(parts) > 1 else ""
        nextrun = parts[4] if len(parts) > 4 else ""
        msg = f"计划任务 {full} 已就绪（每天 {task['time']}，执行 {detail}，下次 {nextrun}）"
    else:
        msg = f"计划任务 {full} 创建失败：{out[:300]}"
    logf(msg)
    return ok, msg


def remove_one(name: str, logf=None):
    logf = logf or (lambda m: None)
    full = full_name(name)
    rc, out = _ps(f"Unregister-ScheduledTask -TaskName {_q(full)} -Confirm:$false "
                  "-ErrorAction SilentlyContinue; 'OK'")
    ok = rc == 0
    msg = f"已删除计划任务 {full}" if ok else f"删除计划任务 {full} 失败：{out[:200]}"
    logf(msg)
    return ok, msg


def status_map() -> dict:
    """一次性取回所有 tg-signer-* 计划任务的状态（避免逐条起 PowerShell）。"""
    script = ("$o=@();"
              "Get-ScheduledTask | Where-Object {$_.TaskName -like 'tg-signer-*'} | ForEach-Object {"
              "$i=$_|Get-ScheduledTaskInfo;"
              "$o+=[pscustomobject]@{name=$_.TaskName;state=[string]$_.State;"
              "next=[string]$i.NextRunTime;last=$i.LastTaskResult;missed=$i.NumberOfMissedRuns}};"
              "ConvertTo-Json -InputObject $o -Compress")
    rc, out = _ps(script)
    if rc != 0 or not out.strip():
        return {}
    txt = out.strip().splitlines()[-1]
    try:
        data = json.loads(txt)
    except ValueError:
        return {}
    if isinstance(data, dict):
        data = [data]
    return {d["name"]: d for d in data if isinstance(d, dict) and d.get("name")}


def sync_all(tasks=None, logf=None):
    """同步所有任务，并清理已经不存在于任务列表里的遗留计划任务。"""
    logf = logf or (lambda m: None)
    tasks = store.list_tasks() if tasks is None else tasks
    msgs = []
    for t in tasks:
        ok, msg = sync_one(t, logf=logf)
        msgs.append(msg)
    keep = {full_name(t["name"]) for t in tasks if t.get("enabled", True)}
    for nm in status_map():
        if nm not in keep:
            logf(f"清理：计划任务 {nm} 已无对应任务 → 删除")
            rc, out = _ps(f"Unregister-ScheduledTask -TaskName {_q(nm)} -Confirm:$false; 'OK'")
            msgs.append(f"已清理 {nm}" if rc == 0 else f"清理 {nm} 失败：{out[:150]}")
    return msgs
