"""批量水印小窗（Tkinter）：选好「输入 / 配置 / 输出」，点一下开始。

用法::

    python scripts/batch_panel.py      （或双击 scripts/batch_panel.bat）

界面只做三件事：**收参数**、**刷进度**、**给人看结果**；真正的活儿全在
scripts/batch_core.py 里（与命令行同一个内核 ⇒ 界面出片与命令行出片完全一样）。

几点约定：
  - 渲染在**后台线程**里跑，主线程只刷进度 ⇒ 窗口不卡，随时能「取消」
  - 「只试运行」= 照常渲染但不写盘：坏图 / 缺图片水印这类问题开跑前就能看出来
  - 逐张结果写进日志（可选中复制），结束时给一行汇总；「打开输出文件夹」直接看片
  - 「写报告」= 输出目录下的 batch_report.json（逐张 src/dest/失败原因/回显）
  - 三个路径记在 %LOCALAPPDATA%\\camera-watermark\\panel_state.json，下次打开原样带回

**不碰 uploads/**（图片水印 / 品牌贴图 / 字体都是只读引用），也绝不覆盖原图。
"""
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import batch_core as BC  # noqa: E402


# ---------------------------------------------------------------------------
# 记住上次用的路径（放在 %LOCALAPPDATA%，与 configs/ 一样是「跨会话保留」的口径）
# ---------------------------------------------------------------------------
def state_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "camera-watermark", "panel_state.json")


def load_state():
    try:
        with open(state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(data):
    path = state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass            # 记不住路径不影响出片（下次重填一次而已），不必打扰用户


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class BatchPanel:

    def __init__(self, master):
        self.master = master
        self.state = load_state()
        self.busy = False
        self.queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker = None
        self.summary = None

        master.title("批量水印生成器")
        master.geometry("900x660")
        master.minsize(800, 580)
        master.columnconfigure(1, weight=1)
        master.rowconfigure(9, weight=1)

        self.var_input = tk.StringVar(value=self.state.get("input", ""))
        self.var_config = tk.StringVar(value=self.state.get("config", ""))
        self.var_out = tk.StringVar(value=self.state.get("out", ""))
        self.var_suffix = tk.StringVar(value=self.state.get("suffix", BC.DEFAULT_SUFFIX))
        self.var_format = tk.StringVar(value=self.state.get("format", ""))
        self.var_quality = tk.StringVar(value=str(self.state.get("quality", "") or ""))
        self.var_recursive = tk.BooleanVar(value=bool(self.state.get("recursive", True)))
        self.var_force = tk.BooleanVar(value=bool(self.state.get("force", False)))
        self.var_dry = tk.BooleanVar(value=bool(self.state.get("dry_run", False)))
        self.var_report = tk.BooleanVar(value=bool(self.state.get("report", True)))

        self._build_widgets()
        master.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_status(log_notes=True)

    # -- 控件 --------------------------------------------------------------
    def _row(self, row, label):
        tk.Label(self.master, text=label, anchor="e", width=10).grid(
            row=row, column=0, sticky="e", padx=(10, 4), pady=3)

    def _build_widgets(self):
        m = self.master
        self._row(0, "输入图片")
        tk.Entry(m, textvariable=self.var_input).grid(row=0, column=1, sticky="ew", pady=3)
        tk.Button(m, text="选文件夹…", width=11, command=self.pick_input_dir).grid(
            row=0, column=2, padx=4)
        tk.Button(m, text="重新扫描", width=11,
                  command=lambda: self.refresh_status(True)).grid(row=0, column=3, padx=(0, 10))

        self.label_scan = tk.Label(m, text="", anchor="w", fg="#555")
        self.label_scan.grid(row=1, column=1, columnspan=3, sticky="w")

        self._row(2, "配置文件")
        tk.Entry(m, textvariable=self.var_config).grid(row=2, column=1, sticky="ew", pady=3)
        tk.Button(m, text="浏览…", width=11, command=self.pick_config).grid(row=2, column=2, padx=4)
        tk.Button(m, text="用自动保存的", width=13,
                  command=self.use_current_config).grid(row=2, column=3, padx=(0, 10))

        self.label_config = tk.Label(m, text="", anchor="w", fg="#555")
        self.label_config.grid(row=3, column=1, columnspan=3, sticky="w")

        self._row(4, "输出文件夹")
        tk.Entry(m, textvariable=self.var_out).grid(row=4, column=1, sticky="ew", pady=3)
        tk.Button(m, text="选文件夹…", width=11, command=self.pick_out_dir).grid(
            row=4, column=2, padx=4)
        tk.Button(m, text="用输入目录\\_watermarked", width=21,
                  command=self.suggest_out).grid(row=4, column=3, padx=(0, 10))

        options = tk.Frame(m)
        options.grid(row=5, column=1, columnspan=3, sticky="w", pady=6)
        tk.Label(options, text="文件名后缀").pack(side="left")
        tk.Entry(options, textvariable=self.var_suffix, width=8).pack(side="left", padx=(4, 12))
        tk.Label(options, text="导出格式").pack(side="left")
        ttk.Combobox(options, textvariable=self.var_format, width=9, state="readonly",
                     values=("", "jpeg", "png", "original")).pack(side="left", padx=(4, 12))
        tk.Label(options, text="JPEG 质量").pack(side="left")
        tk.Spinbox(options, textvariable=self.var_quality, width=5, from_=1, to=100).pack(
            side="left", padx=(4, 12))
        tk.Label(options, text="（留空 = 用配置里存的那份）", fg="#777").pack(side="left")

        checks = tk.Frame(m)
        checks.grid(row=6, column=0, columnspan=4, sticky="w", padx=10, pady=2)
        tk.Checkbutton(checks, text="递归子目录", variable=self.var_recursive).pack(side="left")
        tk.Checkbutton(checks, text="覆盖已存在", variable=self.var_force).pack(side="left", padx=8)
        tk.Checkbutton(checks, text="只试运行（不写盘）", variable=self.var_dry).pack(
            side="left", padx=8)
        tk.Checkbutton(checks, text="写报告 batch_report.json",
                       variable=self.var_report).pack(side="left", padx=8)

        actions = tk.Frame(m)
        actions.grid(row=7, column=0, columnspan=4, sticky="ew", padx=10, pady=6)
        self.btn_start = tk.Button(actions, text="开始生成", width=14, command=self.on_start)
        self.btn_start.pack(side="left")
        self.btn_cancel = tk.Button(actions, text="取消", width=8, state="disabled",
                                    command=self.on_cancel)
        self.btn_cancel.pack(side="left", padx=6)
        self.btn_open = tk.Button(actions, text="打开输出文件夹", width=15,
                                  command=self.open_out_dir)
        self.btn_open.pack(side="left", padx=6)
        tk.Button(actions, text="清空日志", width=10, command=self.clear_log).pack(side="left")

        progress = tk.Frame(m)
        progress.grid(row=8, column=0, columnspan=4, sticky="ew", padx=10)
        self.bar = ttk.Progressbar(progress, mode="determinate", maximum=100)
        self.bar.pack(side="left", fill="x", expand=True)
        self.label_now = tk.Label(progress, text="", anchor="w", width=30)
        self.label_now.pack(side="left", padx=(8, 0))

        log_box = tk.Frame(m)
        log_box.grid(row=9, column=0, columnspan=4, sticky="nsew", padx=10, pady=(6, 2))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)
        self.log = tk.Text(log_box, wrap="word", height=14, font=("Consolas", 9),
                           state="disabled", background="#fbfbfb")
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_box, command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)
        self.log.tag_configure("err", foreground="#b00020")
        self.log.tag_configure("warn", foreground="#a06000")
        self.log.tag_configure("info", foreground="#555555")
        self.log.tag_configure("ok", foreground="#0b6b1f")

        self.label_summary = tk.Label(m, text="就绪", anchor="w", justify="left", wraplength=860)
        self.label_summary.grid(row=10, column=0, columnspan=4, sticky="w", padx=10, pady=(2, 8))

    # -- 选路径 ------------------------------------------------------------
    def pick_input_dir(self):
        chosen = filedialog.askdirectory(title="选要处理的图片文件夹", mustexist=True)
        if not chosen:
            return
        self.var_input.set(os.path.normpath(chosen))
        self.suggest_out()
        self.refresh_status(log_notes=True)

    def pick_out_dir(self):
        chosen = filedialog.askdirectory(title="选输出文件夹", mustexist=False)
        if chosen:
            self.var_out.set(os.path.normpath(chosen))
            self.refresh_status(log_notes=True)

    def pick_config(self):
        chosen = filedialog.askopenfilename(
            title="选配置文件（界面「导出配置」下来的 .json 也行）",
            filetypes=[("配置文件", "*.json"), ("全部文件", "*.*")])
        if chosen:
            self.var_config.set(os.path.normpath(chosen))
            self.refresh_status(log_notes=True)

    def use_current_config(self):
        self.var_config.set("")
        self.refresh_status(log_notes=True)

    def suggest_out(self):
        base = self.var_input.get().strip()
        if base and not self.var_out.get().strip():
            folder = os.path.dirname(base) if os.path.isfile(base) else base
            self.var_out.set(os.path.join(folder, "_watermarked"))

    # -- 收参数 ------------------------------------------------------------
    def collect_options(self):
        quality = self.var_quality.get().strip()
        try:
            quality = int(quality) if quality else None
        except ValueError:
            quality = None
        return BC.build_options(
            [self.var_input.get().strip()],
            self.var_out.get().strip(),
            self.var_suffix.get(),
            self.var_force.get(),
            self.var_format.get(),
            quality,
            self.var_recursive.get(),
            self.var_dry.get())

    def config_path(self):
        return self.var_config.get().strip()

    def load_config(self):
        path = self.config_path()
        return BC.load_config(path) if path else BC.load_config()

    # -- 状态 / 体检 -------------------------------------------------------
    def refresh_status(self, log_notes=False):
        loaded = self.load_config()
        self.label_config.configure(text=BC.describe_config(loaded),
                                    fg="#333" if loaded.get("ok") else "#b00020")
        options = self.collect_options()
        scan = BC.scan_for(options)
        stats = scan["stats"]
        if scan["files"]:
            self.label_scan.configure(
                text=f"待处理 {len(scan['files'])} 张"
                     + (f"（跳过成品 {stats['products_skipped']} 张）"
                        if stats.get("products_skipped") else ""),
                fg="#333")
        else:
            self.label_scan.configure(text="没有找到可处理的图片", fg="#b00020")
        if log_notes:
            self._write("配置：" + BC.describe_config(loaded),
                        "ok" if loaded.get("ok") else "err")
            if loaded.get("ok"):
                self._write("      " + str(loaded.get("path") or ""), "info")
            for level, text in BC.validate_plan(scan, loaded, options):
                self._write(("  [错误] " if level == "error" else
                             "  [提醒] " if level == "warning" else "  ") + text,
                            {"error": "err", "warning": "warn"}.get(level, "info"))
        return loaded, scan, options

    # -- 开跑 --------------------------------------------------------------
    def on_start(self):
        if self.busy:
            return
        loaded, scan, options = self.refresh_status(log_notes=True)
        notes = BC.validate_plan(scan, loaded, options)
        if any(level == "error" for level, _text in notes):
            messagebox.showerror("还不能开始", "请先解决日志里标着 [错误] 的问题。")
            return
        if self.var_report.get():
            base = options["out_root"] or os.path.dirname(os.path.abspath(scan["files"][0]))
            options["report_path"] = os.path.join(base, BC.REPORT_NAME)

        self.busy = True
        self.cancel_event.clear()
        self.summary = None
        self.bar.configure(value=0, maximum=max(1, len(scan["files"])))
        self.label_now.configure(text=f"0 / {len(scan['files'])}")
        self.label_summary.configure(text="正在生成…", fg="#333")
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self._write(f"===== 开始生成（{len(scan['files'])} 张）=====", "ok")
        self.worker = threading.Thread(target=self._work, args=(loaded, scan, options), daemon=True)
        self.worker.start()
        self.master.after(120, self._poll)

    def on_cancel(self):
        if self.busy:
            self.cancel_event.set()
            self.btn_cancel.configure(state="disabled")
            self.label_now.configure(text="正在取消（跑完当前这张就停）…")
            self._write("已请求取消：跑完当前这张就停", "warn")

    def _work(self, loaded, scan, options):
        """后台线程：真正跑批（绝不碰 Tk 控件，只往队列里塞事件）。"""
        try:
            summary = BC.run_batch(scan, loaded, options,
                                   on_event=lambda kind, payload: self.queue.put((kind, payload)),
                                   should_cancel=self.cancel_event.is_set)
            report_path = options.get("report_path")
            if report_path and not options.get("dry_run"):
                ok, error = BC.write_report(summary, report_path)
                self.queue.put(("report", {"ok": ok, "path": report_path, "error": error}))
        except Exception as exc:                  # 兜底：别让窗口卡在「正在生成…」
            self.queue.put(("fatal", {"error": f"{exc.__class__.__name__}: {exc}"}))

    def _poll(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "item":
                    self._on_item(payload)
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "report":
                    if payload.get("ok"):
                        self._write("报告：" + payload["path"], "info")
                    else:
                        self._write("报告写入失败：" + str(payload.get("error")), "err")
                elif kind == "fatal":
                    self._write("意外错误：" + str(payload.get("error")), "err")
                    self._finish()
        except queue.Empty:
            pass
        if self.busy:
            self.master.after(120, self._poll)

    def _on_item(self, row):
        total = int(self.bar.cget("maximum") or 1)
        self.bar.configure(value=row.get("index", 0))
        self.label_now.configure(text=f"{row.get('index', 0)} / {total}")
        tag = "err" if row.get("error") else ("ok" if row.get("ok") else "info")
        for line in BC.format_item_line(row, total).splitlines():
            self._write(line, tag)

    def _on_done(self, summary):
        self.summary = summary
        for line in BC.format_summary_lines(summary):
            self._write(line, "err" if summary.get("failed") else "ok")
        note = "完成" if not summary.get("failed") else "有失败项，看上面的日志"
        if summary.get("cancelled"):
            note += f"（已取消 {summary['cancelled']} 张）"
        self.label_summary.configure(
            text=f"{note}：成功 {summary['ok_count']} / 失败 {summary['failed']} / "
                 f"跳过 {summary['skipped']}，用时 {summary['seconds']:.1f}s"
                 + ("（试运行，没有写盘）" if summary.get("dry_run") else ""),
            fg="#b00020" if summary.get("failed") else "#0b6b1f")
        self._finish()

    def _finish(self):
        self.busy = False
        self.btn_start.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self.label_now.configure(text="")

    # -- 日志 / 打开文件夹 --------------------------------------------------
    def _write(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def open_out_dir(self):
        target = self.var_out.get().strip()
        if not target:
            messagebox.showinfo("没有输出文件夹",
                                "还没选输出文件夹：默认写到每张输入图旁边。")
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(target)                  # noqa: S606 - Windows 打开资源管理器
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception as exc:
            messagebox.showerror("打不开", f"{target}\n{exc}")

    # -- 退出 --------------------------------------------------------------
    def on_close(self):
        if self.busy and not messagebox.askokcancel("还在生成", "正在生成中，确定关闭窗口？"):
            return
        self.cancel_event.set()
        save_state({
            "input": self.var_input.get().strip(), "config": self.config_path(),
            "out": self.var_out.get().strip(), "suffix": self.var_suffix.get(),
            "format": self.var_format.get(), "quality": self.var_quality.get().strip(),
            "recursive": self.var_recursive.get(), "force": self.var_force.get(),
            "dry_run": self.var_dry.get(), "report": self.var_report.get(),
        })
        self.master.destroy()


def main():
    root = tk.Tk()
    BatchPanel(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
