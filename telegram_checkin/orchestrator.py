# -*- coding: utf-8 -*-
"""编排：按需求串起整条链路。

    预检 Clash 是否打开 / 网络是否畅通
      → 没开就启动（快捷方式）并选一个真正可用的节点
      → 发送签到指令（两条指令之间按 step_interval 秒隔开）
      → 完成后按需求关闭 clash 进程（走托盘菜单优雅退出，不 taskkill）

单条指令的成功判据是 tg-signer 打印的「处理完成」，最终判据是 SQLite 里的签到记录。
"""
from __future__ import annotations

import time

import clash
import signer
import store


def prepare_network(logf=print, manage_clash: bool = True,
                    force_switch: bool = False, controller_timeout: int = None):
    """预检并（必要时）拉起 Clash、切换可用节点。返回 (ok, 是否由我们启动, 说明)。"""
    logf = logf or (lambda m: None)
    if not manage_clash:
        ok, why = clash.proxy_egress_ok()
        logf(f"预检：跳过 Clash 管理，直接测代理链路 → {why}")
        return ok, False, why

    running = clash.clash_running()
    started = False
    if running and clash.port_listening(clash.PROXY_PORT):
        logf(f"预检：Clash 已在运行（GUI={bool(clash.pids_of(clash.CLASH_GUI))}，"
             f"内核={bool(clash.pids_of(clash.CLASH_CORE))}，7890 监听中）")
    else:
        logf("预检：Clash 未运行或 7890 未监听 → 打开 Clash")
        if not clash.start_clash(logf):
            return False, False, "启动 Clash 失败（进程未能起来）"
        started = True
        if not clash.wait_ready(timeout=180, logf=logf):
            return False, started, "Clash 内核/7890 未在 180s 内就绪"

    ok, why = clash.proxy_egress_ok()
    logf(f"预检：代理链路 → {why}")

    ctl = clash.find_controller(timeout=controller_timeout or (120 if started else 30))
    port, secret = ctl if ctl else (None, "")
    if not port:
        return False, started, "找不到可用的 Clash 控制器端口（external-controller 无响应）"
    logf(f"预检：控制器 127.0.0.1:{port}")

    good = clash.ensure_usable_node(port, secret, force_switch=force_switch,
                                   already_ok=ok, logf=logf)
    if not good:
        return False, started, "没有可用节点"
    ok2, why2 = clash.proxy_egress_ok()
    return ok2, started, why2


def stop_clash_and_verify(logf=print) -> bool:
    """关闭 Clash 并确认系统代理不悬空（不 taskkill，走托盘菜单）。"""
    logf = logf or (lambda m: None)
    if not clash.clash_running():
        logf("收尾：Clash 已不在运行")
        clash.fix_dangling_system_proxy(logf)
        return True
    logf("收尾：按需求关闭 clash 进程 —— 走托盘菜单「退出」，不用 taskkill")
    ok = False
    try:
        ok = clash.tray_quit(logf=logf)
    except Exception as e:  # noqa: BLE001
        logf(f"收尾：托盘退出异常 {type(e).__name__}: {e}")
    if ok:
        clash.fix_dangling_system_proxy(logf)
    else:
        logf("收尾：托盘退出失败 → 降级为结束进程，并修复悬空的系统代理")
        clash.stop_clash(by_tray=False, logf=logf)
    time.sleep(1)
    left = clash.clash_running()
    logf(f"收尾：Clash 运行中={left}，系统代理 ProxyEnable={clash.proxy_enable()}")
    return not left


def run_task(task: dict, logf=print, skip_clash: bool = False,
             force_stop_clash: bool = False, manage_clash=None) -> dict:
    """执行一次完整任务（计划任务触发 / 「发送检测指令」按钮 / 手动 run-once 都走这里）。"""
    logf = logf or (lambda m: None)
    task = store.normalize(task)
    name = task["name"]
    want = len(task["steps"])
    res = {"task": name, "ok": False, "sent": 0, "want": want, "detail": "",
           "started_clash": False, "stopped_clash": False}
    store.write_signer_config(task)
    logf(f"任务「{name}」：{want} 条指令，计划时间 {task['time']}，"
         f"指令间隔 {task['step_interval']}s")

    do_clash = task["manage_clash"] if manage_clash is None else bool(manage_clash)
    if skip_clash:
        logf("本次跳过 Clash 管理（--skip-clash）")
        do_clash = False
    started = False
    try:
        ok_net, started, why = prepare_network(logf, manage_clash=do_clash)
        res["started_clash"] = started
        if not ok_net:
            res["detail"] = f"网络预检失败：{why}"
            logf("放弃本次任务 → " + res["detail"])
            return res
        rc, out = signer.run_once(name, logf=logf)
        sent = signer.count_sent(out)
        res["sent"] = sent
        if rc == 0 and want > 0 and sent >= want:
            res["ok"] = True
            res["detail"] = f"已发送 {sent}/{want} 条指令，tg-signer 退出码 0"
        else:
            res["detail"] = f"tg-signer 退出码={rc}，成功发送 {sent}/{want} 条"
        logf("执行结果：" + res["detail"])
        logf("签到记录（真实证据）：")
        for line in signer.records(name).splitlines():
            logf("  " + line)
        return res
    finally:
        should_stop = (do_clash and task["stop_clash_after"]
                       and (started or force_stop_clash or task["close_clash_always"]))
        if should_stop:
            res["stopped_clash"] = stop_clash_and_verify(logf)
        elif do_clash:
            logf("收尾：Clash 本来就在运行，且任务未要求关闭 → 保持不动")


def test_network(logf=print):
    """「测试TG网络连通性」按钮：必要时临时拉起 Clash 验证链路，然后恢复原状。"""
    logf = logf or (lambda m: None)
    was_running = clash.clash_running()
    logf("网络测试：检查 Clash 与「代理 → 节点 → Telegram」整条链路"
         + ("" if was_running else "（Clash 未运行，将临时启动）"))
    ok, started, why = prepare_network(logf, manage_clash=True)
    detail = f"Clash={'运行中' if clash.clash_running() else '未运行'}；链路：{why}"
    if ok:
        st = clash.status()
        detail += f"；节点={st.get('node')}；控制器={st.get('controller')}"
    if started and not was_running:
        logf("网络测试：恢复原状（关闭本次临时启动的 Clash）")
        stop_clash_and_verify(logf)
        detail += "；已恢复为未运行"
    return ok, detail
