# -*- coding: utf-8 -*-
"""「Telegram签到」图形界面（tkinter，标准库；打包成 exe 后不需要额外依赖）。

界面自检点：
- 任务列表：任务名 / 执行时间 / 发送的指令 / 启用 / 计划任务（状态 + 下次运行）
- 两个固定按钮：**发送检测指令**、**测试TG网络连通性**
- 任务可新增、修改、删除；每个任务单独设置执行时间（落到各自的 Windows 计划任务）
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import tkinter as tk  # noqa: E402
from tkinter import messagebox, ttk  # noqa: E402

import orchestrator  # noqa: E402
import task_sched  # noqa: E402
import signer  # noqa: E402
import store  # noqa: E402

APP_TITLE = "Telegram签到"
UI_FONT = ("Microsoft YaHei UI", 10)
BOLD_FONT = ("Microsoft YaHei UI", 12, "bold")
MONO = ("Consolas", 9)


def icon_path():
    cands = []
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", "")
        if base:
            cands.append(Path(base) / "telegram.ico")
        cands.append(Path(sys.executable).with_name("telegram.ico"))
    cands.append(Path(_HERE) / "telegram.ico")
    for c in cands:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            pass
    return None


# ---------------------------------------------------------------- 步骤编辑
class StepDialog(tk.Toplevel):
    def __init__(self, master, step=None):
        super().__init__(master)
        self.title("步骤（机器人 + 指令）")
        self.transient(master)
        self.resizable(False, False)
        self.result = None
        s = step or {"chat_id": "", "label": "", "command": ""}
        self.v_id = tk.StringVar(value=str(s.get("chat_id", "")))
        self.v_label = tk.StringVar(value=str(s.get("label", "")))
        self.v_cmd = tk.StringVar(value=str(s.get("command", "")))
        f = ttk.Frame(self, padding=14)
        f.pack(fill="both", expand=True)
        rows = (("机器人 chat_id / @用户名", self.v_id),
                ("备注名称", self.v_label),
                ("发送的指令", self.v_cmd))
        first = None
        for i, (lab, var) in enumerate(rows):
            ttk.Label(f, text=lab).grid(row=i, column=0, sticky="w", pady=4)
            e = ttk.Entry(f, textvariable=var, width=44, font=UI_FONT)
            e.grid(row=i, column=1, sticky="we", pady=4)
            if first is None:
                first = e
        ttk.Label(f, text="例：6993835070 或 @JiuGuanAIBot；指令如 /checkin 或 📅 每日签到",
                  foreground="#888").grid(row=3, column=0, columnspan=2, sticky="w")
        bar = ttk.Frame(f)
        bar.grid(row=4, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(bar, text="确定", command=self._ok).pack(side="left", padx=4)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="left")
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        first.focus_set()
        self.grab_set()

    def _ok(self):
        cid = self.v_id.get().strip()
        cmd = self.v_cmd.get().strip()
        if not cid or not cmd:
            messagebox.showwarning("内容不全", "chat_id 与指令都不能为空", parent=self)
            return
        self.result = {"chat_id": store.parse_chat_id(cid),
                       "label": self.v_label.get().strip() or cid,
                       "command": cmd}
        self.destroy()


# ---------------------------------------------------------------- 任务编辑
class TaskDialog(tk.Toplevel):
    def __init__(self, master, task=None):
        super().__init__(master)
        self.editing = task is not None
        t = store.normalize(task or store.new_task(""))
        self.title(("编辑任务 - " + t["name"]) if self.editing else "新建任务")
        self.transient(master)
        self.result = None
        self.steps = list(t["steps"])
        self.v_name = tk.StringVar(value=t["name"])
        self.v_time = tk.StringVar(value=t["time"])
        self.v_iv = tk.StringVar(value=str(t["step_interval"]))
        self.v_en = tk.BooleanVar(value=t["enabled"])
        self.v_mc = tk.BooleanVar(value=t["manage_clash"])
        self.v_sc = tk.BooleanVar(value=t["stop_clash_after"])
        self.v_ca = tk.BooleanVar(value=t["close_clash_always"])
        self._build()
        self.bind("<Escape>", lambda e: self.destroy())
        if not self.editing:
            self.name_entry.focus_set()
        self.grab_set()

    def _build(self):
        f = ttk.Frame(self, padding=14)
        f.pack(fill="both", expand=True)
        f.columnconfigure(2, weight=1)
        r = 0
        ttk.Label(f, text="任务名").grid(row=r, column=0, sticky="w", pady=3)
        self.name_entry = ttk.Entry(f, textvariable=self.v_name, width=28, font=UI_FONT)
        self.name_entry.grid(row=r, column=1, sticky="w", pady=3)
        if self.editing:
            self.name_entry.configure(state="readonly")
        ttk.Label(f, text="字母/数字/下划线/短横线（同时是计划任务名后缀）",
                  foreground="#888").grid(row=r, column=2, sticky="w", padx=8)
        r += 1
        ttk.Label(f, text="执行时间").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(f, textvariable=self.v_time, width=10, font=UI_FONT).grid(row=r, column=1, sticky="w", pady=3)
        ttk.Label(f, text="每天该时间由 Windows 计划任务自动执行，格式 06:00",
                  foreground="#888").grid(row=r, column=2, sticky="w", padx=8)
        r += 1
        ttk.Label(f, text="指令间隔(秒)").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Spinbox(f, from_=0, to=600, width=8, textvariable=self.v_iv).grid(row=r, column=1, sticky="w", pady=3)
        ttk.Label(f, text="相邻两条指令之间的等待秒数（两条签到之间要求 ≥30）",
                  foreground="#888").grid(row=r, column=2, sticky="w", padx=8)
        r += 1
        opts = ttk.Frame(f)
        opts.grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 2))
        ttk.Checkbutton(opts, text="启用此任务", variable=self.v_en).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(opts, text="执行前检测并启动 Clash", variable=self.v_mc).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(opts, text="完成后关闭 Clash", variable=self.v_sc).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(opts, text="Clash 本来在运行也一并关闭", variable=self.v_ca).pack(side="left")
        r += 1
        ttk.Label(f, text="发送的指令（按顺序执行，间隔见上）").grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 2))
        r += 1
        area = ttk.Frame(f)
        area.grid(row=r, column=0, columnspan=3, sticky="nsew")
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        f.rowconfigure(r, weight=1)
        cols = ("cid", "label", "cmd")
        self.stree = ttk.Treeview(area, columns=cols, show="headings", height=7, selectmode="browse")
        for c, tt, w in (("cid", "机器人 chat_id / @用户名", 190), ("label", "备注", 140), ("cmd", "指令", 280)):
            self.stree.heading(c, text=tt)
            self.stree.column(c, width=w, anchor="w")
        self.stree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(area, orient="vertical", command=self.stree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.stree.configure(yscrollcommand=sb.set)
        self.stree.bind("<Double-1>", lambda e: self.edit_step())
        sbtn = ttk.Frame(area)
        sbtn.grid(row=0, column=2, sticky="n", padx=(8, 0))
        ttk.Button(sbtn, text="添加", width=8, command=self.add_step).pack(pady=2)
        ttk.Button(sbtn, text="修改", width=8, command=self.edit_step).pack(pady=2)
        ttk.Button(sbtn, text="删除", width=8, command=self.del_step).pack(pady=2)
        r += 1
        bar = ttk.Frame(f)
        bar.grid(row=r, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(bar, text="保存", command=self.save).pack(side="left", padx=4)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="left")
        self._fill_steps()

    def _fill_steps(self):
        for i in self.stree.get_children():
            self.stree.delete(i)
        for idx, s in enumerate(self.steps):
            self.stree.insert("", "end", iid=str(idx),
                              values=(s.get("chat_id", ""), s.get("label", ""), s.get("command", "")))

    def add_step(self):
        d = StepDialog(self, None)
        self.wait_window(d)
        if d.result:
            self.steps.append(d.result)
            self._fill_steps()

    def edit_step(self):
        sel = self.stree.selection()
        if not sel:
            return
        i = int(sel[0])
        d = StepDialog(self, self.steps[i])
        self.wait_window(d)
        if d.result:
            self.steps[i] = d.result
            self._fill_steps()

    def del_step(self):
        sel = self.stree.selection()
        if not sel:
            return
        del self.steps[int(sel[0])]
        self._fill_steps()

    def save(self):
        t = store.new_task(self.v_name.get().strip())
        t.update({
            "time": self.v_time.get().strip(),
            "step_interval": self.v_iv.get().strip(),
            "enabled": self.v_en.get(),
            "manage_clash": self.v_mc.get(),
            "stop_clash_after": self.v_sc.get(),
            "close_clash_always": self.v_ca.get(),
            "steps": self.steps,
        })
        try:
            self.result = store.save_task(t)
        except ValueError as e:
            messagebox.showerror("保存失败", str(e), parent=self)
            return
        self.destroy()


# ---------------------------------------------------------------- 主窗口
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1020x700")
        self.minsize(900, 580)
        ip = icon_path()
        if ip:
            try:
                self.iconbitmap(ip)
            except Exception:
                pass
        self.q = queue.Queue()
        self.busy = False
        self._buttons = []
        self._build()
        self.refresh()
        self.after(120, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.log.insert("end", f"「{APP_TITLE}」已启动 · 账号 {signer.ACCOUNT} · "
                              f"tg-signer {signer.version()}\n")
        self.log.insert("end", f"工作目录：{store.workdir()}\n"
                              f"代理：{os.environ.get('TG_PROXY', 'socks5://127.0.0.1:7890')}\n")

    # ---------- 布局
    def _build(self):
        top = ttk.Frame(self, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="任务列表", font=BOLD_FONT).pack(side="left")
        self.head_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.head_var, foreground="#666").pack(side="left", padx=12)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(top, textvariable=self.status_var, foreground="#0a7a3d",
                  font=UI_FONT).pack(side="right")

        mid = ttk.Frame(self, padding=(12, 0))
        mid.pack(fill="both", expand=False)
        cols = ("name", "time", "steps", "enabled", "schedule")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=9, selectmode="browse")
        for c, tt, w, a in (("name", "任务名", 180, "w"), ("time", "执行时间", 90, "center"),
                            ("steps", "发送的指令", 330, "w"), ("enabled", "启用", 60, "center"),
                            ("schedule", "计划任务", 260, "w")):
            self.tree.heading(c, text=tt)
            self.tree.column(c, width=w, anchor=a)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        tsb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.bind("<Double-1>", lambda e: self.edit_task())

        btns = ttk.Frame(self, padding=(12, 8, 12, 4))
        btns.pack(fill="x")

        def mk(text, cmd, side="left"):
            w = ttk.Button(btns, text=text, command=cmd)
            w.pack(side=side, padx=(0 if side == "right" else 0, 6))
            self._buttons.append(w)
            return w

        mk("新建任务", self.new_task)
        mk("编辑", self.edit_task)
        mk("删除", self.delete_task)
        ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=10)
        mk("发送检测指令", self.send_detect)
        mk("测试TG网络连通性", self.test_network)
        ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=10)
        mk("同步计划任务", self.sync_schedules)
        mk("打开数据目录", self.open_dir, "right")
        mk("清空日志", lambda: self.log.delete("1.0", "end"), "right")

        box = ttk.Frame(self, padding=(12, 0, 12, 10))
        box.pack(fill="both", expand=True)
        self.log = tk.Text(box, height=16, wrap="word", font=MONO,
                           background="#101418", foreground="#d8e0e8",
                           insertbackground="#d8e0e8", relief="flat")
        self.log.pack(side="left", fill="both", expand=True)
        lsb = ttk.Scrollbar(box, orient="vertical", command=self.log.yview)
        lsb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=lsb.set)

    # ---------- 线程/日志
    def logline(self, msg):
        self.q.put(("log", str(msg)))

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log.insert("end", payload + "\n")
                    self.log.see("end")
                elif kind == "status":
                    self._apply_status(payload)
                elif kind == "done":
                    self.busy = False
                    self.status_var.set(payload)
                    self._set_buttons(True)
                    self.refresh()
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _set_buttons(self, enable):
        for w in self._buttons:
            try:
                w.configure(state="normal" if enable else "disabled")
            except tk.TclError:
                pass

    def run_job(self, title, fn):
        if self.busy:
            messagebox.showinfo(APP_TITLE, "还有任务在执行，请等它结束")
            return
        self.busy = True
        self._set_buttons(False)
        self.status_var.set(title + " …")
        self.logline(f"\n===== {title} @ {time.strftime('%Y-%m-%d %H:%M:%S')} =====")

        def worker():
            tail = title + " 完成"
            try:
                fn(self.logline)
            except Exception as e:  # noqa: BLE001
                self.logline(f"执行异常：{type(e).__name__}: {e}")
                tail = title + " 异常结束"
            self.q.put(("done", tail))

        threading.Thread(target=worker, daemon=True).start()

    def _on_close(self):
        if self.busy and not messagebox.askokcancel(APP_TITLE, "还有任务在执行，确定要退出吗？"):
            return
        self.destroy()

    # ---------- 列表
    def refresh(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        for t in store.list_tasks():
            self.tree.insert("", "end", iid=t["name"], values=(
                t["name"], t["time"], store.summarize(t),
                "是" if t["enabled"] else "否", "查询中…"))
        self.head_var.set(f"tg-signer: {signer.exe_path() or '未安装'}    工作目录: {store.workdir()}")

        def worker():
            try:
                self.q.put(("status", task_sched.status_map()))
            except Exception as e:  # noqa: BLE001
                self.logline(f"读取计划任务状态失败：{e}")
                self.q.put(("status", {}))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_status(self, smap):
        for name in self.tree.get_children():
            info = (smap or {}).get(task_sched.full_name(name))
            if not info:
                txt = "未创建"
            else:
                st = {"Ready": "就绪", "Running": "运行中", "Disabled": "已停用",
                      "Queued": "排队中"}.get(str(info.get("state")), str(info.get("state") or "?"))
                txt = st
                nxt = str(info.get("next") or "").replace("/", "-")
                if nxt.startswith("20"):
                    txt += f" · 下次 {nxt}"
                last = info.get("last")
                if last not in (None, 0, "0"):
                    txt += f" · 上次结果 {last}"
            self.tree.set(name, "schedule", txt)

    def selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    # ---------- 增删改
    def new_task(self):
        dlg = TaskDialog(self, None)
        self.wait_window(dlg)
        if dlg.result:
            t = dlg.result
            self.run_job(f"同步计划任务（{t['name']}）",
                         lambda logf: self._after_save(t, logf))

    def edit_task(self):
        name = self.selected()
        if not name:
            return
        t = store.get_task(name)
        if not t:
            return
        dlg = TaskDialog(self, t)
        self.wait_window(dlg)
        if dlg.result:
            self.run_job(f"同步计划任务（{dlg.result['name']}）",
                         lambda logf: self._after_save(dlg.result, logf))

    def _after_save(self, task, logf):
        logf(f"任务「{task['name']}」已保存：执行时间 {task['time']}，"
             f"{store.summarize(task)}，指令间隔 {task['step_interval']}s")
        task_sched.sync_one(task, logf=logf)
        cfg = store.signs_dir() / task["name"] / "config.json"
        logf(f"tg-signer 配置：{cfg}")

    def delete_task(self):
        name = self.selected()
        if not name:
            return
        if not messagebox.askyesno(APP_TITLE, f"删除任务「{name}」并移除对应计划任务？"):
            return
        store.delete_task(name)
        self.refresh()
        self.run_job(f"删除计划任务（{name}）", lambda logf: task_sched.remove_one(name, logf=logf))

    # ---------- 两个固定按钮
    def send_detect(self):
        name = self.selected()
        if not name:
            messagebox.showinfo(APP_TITLE, "先在列表里选中一个任务")
            return
        task = store.get_task(name)
        self.run_job(f"发送检测指令：{name}",
                     lambda logf: orchestrator.run_task(task, logf=logf))

    def test_network(self):
        self.run_job("测试TG网络连通性", lambda logf: orchestrator.test_network(logf))

    def sync_schedules(self):
        self.run_job("同步全部计划任务",
                     lambda logf: task_sched.sync_all(store.list_tasks(), logf=logf))

    def open_dir(self):
        p = store.workdir()
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(str(p))  # noqa: S606


def main():
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
