# -*- coding: utf-8 -*-
"""「Telegram签到」入口。

- 不带参数       → 打开图形界面（任务增删改 / 每任务执行时间 / 发送检测指令 / 测试 TG 网络连通性）
- --task NAME    → 执行指定任务（Windows 计划任务调用的就是这条）
- --test-network → 只测 TG 网络连通性
- --sync-schedules → 按任务时间同步计划任务
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import orchestrator  # noqa: E402
import task_sched  # noqa: E402
import signer  # noqa: E402
import store  # noqa: E402


def file_logger(path):
    fh = open(path, "a", encoding="utf-8")

    def log(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            fh.write(line + "\n")
            fh.flush()
        except Exception:
            pass

    return log, fh


def do_task(name: str, logf, **kw) -> int:
    task = store.get_task(name)
    if not task:
        logf(f"找不到任务「{name}」（任务目录 {store.tasks_dir()}）")
        return 3
    logf(f"=== 任务「{name}」开始 {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    res = orchestrator.run_task(task, logf=logf, **kw)
    logf(f"=== 任务「{name}」结束：ok={res['ok']}；{res['detail']}；"
         f"本次启动Clash={res['started_clash']} 关闭Clash={res['stopped_clash']} ===")
    return 0 if res["ok"] else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="Telegram签到",
        description="tg-signer 签到编排：Clash 预检/启动/选节点 → 发送签到指令 → 托盘关闭 Clash")
    ap.add_argument("--task", metavar="NAME", help="执行指定任务（计划任务使用）")
    ap.add_argument("--all", action="store_true", help="执行所有已启用任务")
    ap.add_argument("--test-network", action="store_true", help="只测试 TG 网络连通性")
    ap.add_argument("--list", action="store_true", help="列出全部任务")
    ap.add_argument("--sync-schedules", action="store_true", help="按任务时间同步 Windows 计划任务")
    ap.add_argument("--skip-clash", action="store_true", help="跳过 Clash 预检/启停")
    ap.add_argument("--force-stop-clash", action="store_true", help="任务结束后无条件关闭 Clash")
    ap.add_argument("--gui", action="store_true", help="打开图形界面")
    args = ap.parse_args(argv)

    # 打包成 --windowed 的 exe 后，stdout 可能是 GBK 编码的控制台流：
    # 打印 📅 这类字符会直接抛 UnicodeEncodeError，所以先统一成 UTF-8 + 容错。
    for _stream in ("stdout", "stderr"):
        try:
            getattr(sys, _stream).reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if args.list:
        ts = store.list_tasks()
        lines = []
        if not ts:
            lines.append(f"暂无任务（{store.tasks_dir()}）")
        for t in ts:
            lines.append(f"{t['name']:20s} {t['time']}  {'启用' if t['enabled'] else '停用'}  "
                         f"{len(t['steps'])} 条指令  间隔 {t['step_interval']}s  "
                         + " | ".join(f"{s['label']}({s['chat_id']})→{s['command']}" for s in t["steps"]))
        lines.append(f"tg-signer: {signer.exe_path() or '未安装'}")
        text = "\n".join(lines)
        print(text)
        # 打包成 --windowed 的 exe 后没有 stdout，顺手落盘一份便于核对
        try:
            store.mkdirs()
            (store.run_logs_dir() / f"list-{time.strftime('%Y%m%d-%H%M%S')}.txt").write_text(
                text, encoding="utf-8")
        except OSError:
            pass
        return 0

    if args.sync_schedules:
        store.mkdirs()
        logf, fh = file_logger(store.run_logs_dir() / f"sync-{time.strftime('%Y%m%d-%H%M%S')}.log")
        try:
            logf("同步计划任务…")
            for m in task_sched.sync_all(logf=logf):
                logf("  " + m)
        finally:
            fh.close()
        return 0

    if args.test_network:
        store.mkdirs()
        logf, fh = file_logger(store.run_logs_dir() / f"network-{time.strftime('%Y%m%d-%H%M%S')}.log")
        try:
            ok, detail = orchestrator.test_network(logf)
            logf(f"网络测试结果：ok={ok}；{detail}")
        finally:
            fh.close()
        return 0 if ok else 2

    if args.task or args.all:
        store.mkdirs()
        kw = {"skip_clash": args.skip_clash, "force_stop_clash": args.force_stop_clash}
        names = [args.task] if args.task else [t["name"] for t in store.list_tasks() if t["enabled"]]
        rc = 0
        for nm in names:
            logf, fh = file_logger(store.run_logs_dir() / f"{nm}-{time.strftime('%Y%m%d-%H%M%S')}.log")
            try:
                r = do_task(nm, logf, **kw)
            finally:
                fh.close()
            rc = r or rc
        return rc

    if args.gui or len(sys.argv) == 1:
        import gui
        return gui.main()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
