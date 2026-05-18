#!/usr/bin/env python3
"""
ig_gui.py - Desktop GUI for ig_download.

Usage:
    python ig_gui.py

No dependencies beyond Python's standard library (tkinter is included with Python).
"""

import json
import os
import queue
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
import tkinter as tk

import ig_download

SCRIPT_DIR = Path(__file__).parent.resolve()


class IGDownloaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("IG Downloader")
        self.root.geometry("900x680")
        self.root.minsize(700, 540)

        self._q: queue.Queue = queue.Queue()
        self._stop_event: threading.Event | None = None
        self._ok_count = 0
        self._fail_count = 0
        self._total = 0
        self._lv_all: list[dict] = []
        self._lv_filtered: list[dict] = []
        self._lv_sorted: list[dict] = []
        self._lv_sort_col = "num"
        self._lv_sort_rev = False

        self._build_ui()
        self._poll_queue()
        self._refresh_log_list()

    # ─────────────────────────────────────────────────────────────────
    # UI construction
    # ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Use the best available theme
        style = ttk.Style(self.root)
        for theme in ("vista", "winnative", "clam", "alt", "default"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        dl_tab = ttk.Frame(nb, padding=4)
        nb.add(dl_tab, text="  Download  ")
        self._build_download_tab(dl_tab)

        lv_tab = ttk.Frame(nb, padding=4)
        nb.add(lv_tab, text="  Log Viewer  ")
        self._build_log_viewer_tab(lv_tab)

        nb.bind("<<NotebookTabChanged>>", lambda e: self._on_tab_change(e, nb))

    def _on_tab_change(self, _event, nb):
        if "Log Viewer" in nb.tab(nb.select(), "text"):
            self._refresh_log_list()

    # ── Download tab ──────────────────────────────────────────────────

    def _build_download_tab(self, parent):
        # Config section
        cfg = ttk.LabelFrame(parent, text="Configuration", padding=(12, 8))
        cfg.pack(fill="x", padx=6, pady=(6, 4))
        cfg.columnconfigure(1, weight=1)

        labels = ["URL file (.txt)", "Collection name", "Log file", "Cookies file (optional)"]
        self._url_var = tk.StringVar()
        self._col_var = tk.StringVar()
        self._logf_var = tk.StringVar()
        self._logf_auto = True  # True = log file was auto-derived from collection
        self._ck_var = tk.StringVar()
        vars_ = [self._url_var, self._col_var, self._logf_var, self._ck_var]

        for row, (lbl, var) in enumerate(zip(labels, vars_)):
            ttk.Label(cfg, text=lbl).grid(row=row, column=0, sticky="w",
                                           padx=(0, 12), pady=3)
            if row in (0, 3):  # rows with Browse buttons
                fr = ttk.Frame(cfg)
                fr.grid(row=row, column=1, sticky="ew", pady=3)
                fr.columnconfigure(0, weight=1)
                ttk.Entry(fr, textvariable=var).grid(row=0, column=0, sticky="ew", padx=(0, 6))
                cmd = self._browse_url if row == 0 else self._browse_cookies
                ttk.Button(fr, text="Browse…", width=9, command=cmd).grid(row=0, column=1)
            else:
                ttk.Entry(cfg, textvariable=var).grid(row=row, column=1, sticky="ew", pady=3)

        # Options row
        opts = ttk.Frame(cfg)
        opts.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 2))
        self._dry_var = tk.BooleanVar()
        self._retry_var = tk.BooleanVar()
        ttk.Checkbutton(opts, text="Dry run (no download)", variable=self._dry_var).pack(side="left", padx=(0, 20))
        ttk.Checkbutton(opts, text="Retry previously failed", variable=self._retry_var).pack(side="left")

        # Filename template row
        ttk.Label(cfg, text="Filename template").grid(row=5, column=0, sticky="w",
                                                      padx=(0, 12), pady=3)
        tmpl_row = ttk.Frame(cfg)
        tmpl_row.grid(row=5, column=1, sticky="ew", pady=3)
        tmpl_row.columnconfigure(0, weight=1)
        self._tmpl_var = tk.StringVar(value="{shortcode}")
        presets = list(ig_download.FILENAME_PRESETS.keys())
        tmpl_combo = ttk.Combobox(tmpl_row, textvariable=self._tmpl_var,
                                   values=presets, width=36)
        tmpl_combo.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(tmpl_row, text="?", width=3,
                   command=self._show_template_help).grid(row=0, column=1)

        # Auto-link collection → log file
        self._col_var.trace_add("write", self._on_collection_change)
        # Detect manual logf edits
        self._logf_var.trace_add("write", self._on_logf_edit)

        # Buttons
        btn_row = ttk.Frame(parent)
        btn_row.pack(fill="x", padx=6, pady=(4, 4))
        self._start_btn = ttk.Button(btn_row, text="▶  Start Download", command=self._start_download)
        self._start_btn.pack(side="left", padx=(0, 8))
        self._stop_btn = ttk.Button(btn_row, text="■  Stop", command=self._stop_download, state="disabled")
        self._stop_btn.pack(side="left")

        # Progress section
        prog = ttk.LabelFrame(parent, text="Progress", padding=(10, 6))
        prog.pack(fill="x", padx=6, pady=4)

        hdr = ttk.Frame(prog)
        hdr.pack(fill="x", pady=(0, 4))
        self._status_lbl = ttk.Label(hdr, text="Ready", foreground="gray")
        self._status_lbl.pack(side="left")
        self._pct_lbl = ttk.Label(hdr, text="")
        self._pct_lbl.pack(side="right")

        self._pbar = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self._pbar.pack(fill="x", pady=(0, 6))

        stats = ttk.Frame(prog)
        stats.pack(fill="x")
        self._ok_lbl = ttk.Label(stats, text="OK: 0", foreground="green")
        self._ok_lbl.pack(side="left", padx=(0, 16))
        self._fail_lbl = ttk.Label(stats, text="Failed: 0", foreground="red")
        self._fail_lbl.pack(side="left", padx=(0, 16))
        self._rl_lbl = ttk.Label(stats, text="", foreground="#b45309")
        self._rl_lbl.pack(side="left")

        # Output log
        out = ttk.LabelFrame(parent, text="Output", padding=4)
        out.pack(fill="both", expand=True, padx=6, pady=(4, 6))

        self._log_text = tk.Text(out, wrap="none", state="disabled",
                                  font=("Consolas", 9), relief="flat",
                                  bg="#1e1e1e", fg="#d4d4d4", insertbackground="white")
        vsb = ttk.Scrollbar(out, orient="vertical", command=self._log_text.yview)
        hsb = ttk.Scrollbar(out, orient="horizontal", command=self._log_text.xview)
        self._log_text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        self._log_text.pack(fill="both", expand=True)

        self._log_text.tag_configure("ok",   foreground="#4ec9b0")
        self._log_text.tag_configure("fail", foreground="#f44747")
        self._log_text.tag_configure("rl",   foreground="#dcdcaa")
        self._log_text.tag_configure("info", foreground="#858585")
        self._log_text.tag_configure("head", foreground="#9cdcfe")

    def _on_collection_change(self, *_):
        if self._logf_auto:
            col = self._col_var.get().strip()
            self._logf_auto = True  # keep flag while we set it programmatically
            self._logf_var.set(f"{col}.json" if col else "")
            self._logf_auto = True  # trace fires on set; re-arm

    def _on_logf_edit(self, *_):
        # If value matches collection-derived name, stay in auto mode; else mark manual
        col = self._col_var.get().strip()
        derived = f"{col}.json" if col else ""
        if self._logf_var.get() != derived:
            self._logf_auto = False

    def _show_template_help(self):
        win = tk.Toplevel(self.root)
        win.title("Filename Template Help")
        win.resizable(False, False)
        txt = tk.Text(win, wrap="word", width=64, height=20,
                      font=("Consolas", 9), relief="flat",
                      bg="#1e1e1e", fg="#d4d4d4")
        txt.pack(padx=12, pady=12)
        txt.insert("1.0", ig_download.FILENAME_VARIABLE_DOCS.strip())
        txt.insert("end", "\n\nPresets:\n")
        for tmpl, desc in ig_download.FILENAME_PRESETS.items():
            txt.insert("end", f"  {tmpl:<42} {desc}\n")
        txt.config(state="disabled")
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 10))

    # ── Log viewer tab ────────────────────────────────────────────────

    def _build_log_viewer_tab(self, parent):
        # File selector row
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=6, pady=(6, 4))
        ttk.Label(top, text="Log file:").pack(side="left", padx=(0, 6))
        self._lv_file_var = tk.StringVar()
        self._lv_combo = ttk.Combobox(top, textvariable=self._lv_file_var,
                                       width=34, state="readonly")
        self._lv_combo.pack(side="left", padx=(0, 6))
        self._lv_combo.bind("<<ComboboxSelected>>", lambda _e: self._load_log())
        ttk.Button(top, text="Refresh", command=self._refresh_log_list).pack(side="left", padx=(0, 6))
        ttk.Button(top, text="Load file…", command=self._browse_log_file).pack(side="left")

        # Stats bar
        stats_bar = ttk.Frame(parent, relief="sunken")
        stats_bar.pack(fill="x", padx=6, pady=(0, 6))
        self._lv_stats: dict[str, tk.Label] = {}
        for key, label, color in [
            ("total",    "Total",      None),
            ("ok",       "Downloaded", "green"),
            ("failed",   "Failed",     "red"),
            ("video",    "Videos",     "#1d6fad"),
            ("carousel", "Carousels",  "#6d28d9"),
            ("image",    "Images",     "#b45309"),
        ]:
            cell = ttk.Frame(stats_bar)
            cell.pack(side="left", padx=(10, 10), pady=6)
            ttk.Label(cell, text=label, font=("Segoe UI", 8), foreground="gray").pack(anchor="w")
            lbl = tk.Label(cell, text="—", font=("Segoe UI", 16, "bold"),
                           fg=color if color else "black")
            lbl.pack(anchor="w")
            self._lv_stats[key] = lbl

        # Filter row
        flt = ttk.Frame(parent)
        flt.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(flt, text="Search:").pack(side="left", padx=(0, 4))
        self._lv_q = tk.StringVar()
        self._lv_q.trace_add("write", lambda *_: self._apply_filter())
        ttk.Entry(flt, textvariable=self._lv_q, width=22).pack(side="left", padx=(0, 12))

        ttk.Label(flt, text="Status:").pack(side="left", padx=(0, 4))
        self._lv_st = tk.StringVar(value="All")
        sc = ttk.Combobox(flt, textvariable=self._lv_st, width=9,
                          values=["All", "ok", "failed", "dry_run"], state="readonly")
        sc.pack(side="left", padx=(0, 12))
        sc.bind("<<ComboboxSelected>>", lambda _e: self._apply_filter())

        ttk.Label(flt, text="Type:").pack(side="left", padx=(0, 4))
        self._lv_ty = tk.StringVar(value="All")
        tc = ttk.Combobox(flt, textvariable=self._lv_ty, width=11,
                          values=["All", "video", "carousel", "image", "unknown"], state="readonly")
        tc.pack(side="left", padx=(0, 12))
        tc.bind("<<ComboboxSelected>>", lambda _e: self._apply_filter())

        self._lv_count = ttk.Label(flt, text="", foreground="gray")
        self._lv_count.pack(side="right")

        # Treeview
        tv_fr = ttk.Frame(parent)
        tv_fr.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        cols = ("num", "author", "caption", "shortcode", "type", "status", "date")
        self._tree = ttk.Treeview(tv_fr, columns=cols, show="headings", selectmode="browse")

        widths = {"num": 40, "author": 130, "caption": 200, "shortcode": 110,
                  "type": 65, "status": 65, "date": 125}
        anchors = {"num": "e", "type": "center", "status": "center"}

        for col in cols:
            self._tree.heading(col, text=col.capitalize(),
                               command=lambda c=col: self._sort(c))
            self._tree.column(col, width=widths[col], anchor=anchors.get(col, "w"),
                              stretch=col not in ("num", "type", "status", "date"))

        self._tree.tag_configure("ok",   foreground="#16a34a")
        self._tree.tag_configure("fail", foreground="#dc2626")
        self._tree.tag_configure("dry",  foreground="gray")

        ysb = ttk.Scrollbar(tv_fr, orient="vertical", command=self._tree.yview)
        xsb = ttk.Scrollbar(tv_fr, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        ysb.pack(side="right", fill="y")
        xsb.pack(side="bottom", fill="x")
        self._tree.pack(fill="both", expand=True)

        self._tree.bind("<Button-3>", self._on_right_click)
        self._tree.bind("<Double-1>", self._on_double_click)

    # ─────────────────────────────────────────────────────────────────
    # Download tab logic
    # ─────────────────────────────────────────────────────────────────

    def _browse_url(self):
        p = filedialog.askopenfilename(
            initialdir=str(SCRIPT_DIR), title="Select URL file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if p:
            self._url_var.set(p)

    def _browse_cookies(self):
        p = filedialog.askopenfilename(
            initialdir=str(SCRIPT_DIR), title="Select cookies file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if p:
            self._ck_var.set(p)

    def _start_download(self):
        url_file  = self._url_var.get().strip()
        collection = self._col_var.get().strip()
        log_file  = self._logf_var.get().strip() or "ig_download_log.json"
        cookies   = self._ck_var.get().strip() or None
        dry_run   = self._dry_var.get()
        retry     = self._retry_var.get()

        if not url_file:
            messagebox.showerror("Missing input", "Please select a URL file.")
            return
        if not os.path.isfile(url_file):
            messagebox.showerror("File not found", f"URL file not found:\n{url_file}")
            return
        if not collection:
            messagebox.showerror("Missing input", "Please enter a collection name.")
            return

        self._ok_count = self._fail_count = self._total = 0
        self._clear_log()
        self._pbar["value"] = 0
        self._status_lbl.config(text="Starting…", foreground="gray")
        self._pct_lbl.config(text="")
        self._ok_lbl.config(text="OK: 0")
        self._fail_lbl.config(text="Failed: 0")
        self._rl_lbl.config(text="")
        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="normal")

        self._stop_event = threading.Event()
        orig_cwd = os.getcwd()
        os.chdir(str(SCRIPT_DIR))

        def _run():
            try:
                ig_download.run_download(
                    url_file=url_file,
                    collection=collection,
                    log_file=log_file or None,
                    cookies=cookies,
                    retry_failed=retry,
                    dry_run=dry_run,
                    filename_template=self._tmpl_var.get().strip() or "{shortcode}",
                    progress_cb=self._q.put,
                    stop_event=self._stop_event,
                )
            except Exception as exc:
                self._q.put({"type": "error", "message": str(exc)})
            finally:
                os.chdir(orig_cwd)

        threading.Thread(target=_run, daemon=True).start()

    def _stop_download(self):
        if self._stop_event:
            self._stop_event.set()
        self._stop_btn.config(state="disabled")

    def _poll_queue(self):
        try:
            while True:
                self._handle(self._q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _handle(self, ev: dict):
        t = ev.get("type")

        if t == "start":
            self._total = ev.get("total", 0)
            n = ev.get("to_process", self._total)
            self._append(f"Starting: {n} URLs to process ({self._total} total)", "head")

        elif t == "downloading":
            sc = ev.get("shortcode", "?")
            idx = ev.get("index", 0)
            tot = ev.get("total", self._total)
            self._status_lbl.config(text=f"[{idx+1}/{tot}] {sc} …", foreground="gray")
            self._append(f">>>  [{idx+1}/{tot}]  {ev.get('url', sc)}", "info")

        elif t == "progress":
            self._total = ev.get("total", self._total)
            st = ev.get("status", "")
            if st == "ok":
                self._ok_count += 1
            elif st == "failed":
                self._fail_count += 1

            idx = ev.get("index", 0)
            pct = (idx + 1) / self._total * 100 if self._total else 0
            self._pbar["value"] = pct
            self._status_lbl.config(
                text=f"[{idx+1}/{self._total}]  {ev.get('shortcode', '')}",
                foreground="black")
            self._pct_lbl.config(text=f"{pct:.0f}%")
            self._ok_lbl.config(text=f"OK: {self._ok_count}")
            self._fail_lbl.config(text=f"Failed: {self._fail_count}")
            self._rl_lbl.config(text="")

            author = ev.get("author") or ev.get("username") or "?"
            sc = ev.get("shortcode", "")
            if st == "ok":
                self._append(f"OK    {author:<20} {sc}", "ok")
            elif st == "failed":
                cat = ev.get("error_category") or "error"
                detail = ev.get("error_detail") or ""
                self._append(f"FAIL  {author:<20} {sc}  [{cat}]", "fail")
                if detail:
                    self._append(f"      {detail[:180]}", "fail")
            else:
                self._append(f"DRY   {author:<20} {sc}", "info")

        elif t == "rate_limited":
            wait = ev.get("wait_seconds", 0)
            n = ev.get("consecutive", 1)
            self._append(f"⚠ Rate limited ({n}x) — waiting {wait}s…", "rl")
            self._rl_lbl.config(text=f"⚠ Rate limited — {wait}s wait")

        elif t == "rate_limit_tick":
            rem = ev.get("remaining", 0)
            self._rl_lbl.config(text=f"⚠ Rate limited — {rem}s remaining")

        elif t in ("rate_limit_abort",):
            self._append("Too many consecutive rate limits — aborted.", "fail")
            self._finish()

        elif t == "stopped":
            self._append("Stopped by user.", "info")
            self._finish()

        elif t == "done":
            s = ev.get("summary", {})
            self._append(
                f"Done — {s.get('ok',0)} OK, {s.get('failed',0)} failed, "
                f"{s.get('skipped_auto',0)} skipped",
                "ok")
            self._pbar["value"] = 100
            self._status_lbl.config(text="Complete", foreground="green")
            self._rl_lbl.config(text="")
            self._finish()

        elif t == "error":
            self._append(f"ERROR: {ev.get('message', '')}", "fail")
            self._finish()

        elif t in ("skip_info", "skipped_disk", "skipped"):
            sc = ev.get("shortcode") or ev.get("skipped") or ""
            reason = "already on disk" if t == "skipped_disk" else "already logged"
            self._append(f"Skip  {sc}  ({reason})", "info")

    def _finish(self):
        self._start_btn.config(state="normal")
        self._stop_btn.config(state="disabled")

    def _append(self, msg: str, tag: str = "info"):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)  # also visible in terminal
        self._log_text.config(state="normal")
        self._log_text.insert("end", f"[{ts}] {msg}\n", tag)
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    def _clear_log(self):
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")

    # ─────────────────────────────────────────────────────────────────
    # Log viewer logic
    # ─────────────────────────────────────────────────────────────────

    def _refresh_log_list(self):
        files = sorted(
            f.name for f in SCRIPT_DIR.glob("*.json")
            if f.name != "ig_logs_manifest.json"
        )
        current = self._lv_file_var.get()
        self._lv_combo["values"] = files
        if files and current not in files:
            self._lv_file_var.set(files[0])
            self._load_log()
        elif current in files:
            pass  # keep current without reloading

    def _browse_log_file(self):
        p = filedialog.askopenfilename(
            initialdir=str(SCRIPT_DIR), title="Select log file",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if p:
            self._lv_file_var.set(p)
            self._load_log()

    def _load_log(self):
        name = self._lv_file_var.get()
        if not name:
            return
        path = Path(name) if Path(name).is_absolute() else SCRIPT_DIR / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            messagebox.showerror("Error", f"Could not read log:\n{e}")
            return

        items = data.get("items", [])
        seen: dict = {}
        for it in items:
            seen[it.get("url", id(it))] = it
        self._lv_all = list(seen.values())

        ok = sum(1 for i in self._lv_all if i.get("status") == "ok")
        fail = sum(1 for i in self._lv_all if i.get("status") == "failed")
        video = sum(1 for i in self._lv_all if i.get("media_type") == "video")
        carousel = sum(1 for i in self._lv_all if i.get("media_type") == "carousel")
        image = sum(1 for i in self._lv_all if i.get("media_type") in ("image", "image_only"))

        for k, v in [("total", len(self._lv_all)), ("ok", ok), ("failed", fail),
                     ("video", video), ("carousel", carousel), ("image", image)]:
            self._lv_stats[k].config(text=str(v))

        self._lv_sort_col = "num"
        self._lv_sort_rev = False
        self._apply_filter()

    def _apply_filter(self):
        q = self._lv_q.get().lower()
        fst = self._lv_st.get()
        fty = self._lv_ty.get()

        filtered = []
        for item in self._lv_all:
            if fst != "All" and item.get("status") != fst:
                continue
            mt = item.get("media_type", "")
            if fty != "All" and mt != fty and not (fty == "image" and mt == "image_only"):
                continue
            if q:
                hay = " ".join(str(v) for v in [
                    item.get("shortcode"), item.get("author"), item.get("username"),
                    item.get("caption"), item.get("error_category"), item.get("error_detail"),
                ]).lower()
                if q not in hay:
                    continue
            filtered.append(item)

        self._lv_filtered = filtered
        total = len(self._lv_all)
        shown = len(filtered)
        self._lv_count.config(
            text=f"{shown} of {total}" if shown != total else f"{total} entries")
        self._render_tree()

    def _sort(self, col: str):
        if self._lv_sort_col == col:
            self._lv_sort_rev = not self._lv_sort_rev
        else:
            self._lv_sort_col = col
            self._lv_sort_rev = False
        self._render_tree()

    def _render_tree(self):
        col = self._lv_sort_col
        rev = self._lv_sort_rev

        def key(item: dict):
            if col == "num":
                return 0
            if col == "author":
                return (item.get("author") or item.get("username") or "").lower()
            return str(item.get(col) or "").lower()

        self._lv_sorted = sorted(self._lv_filtered, key=key, reverse=rev)

        for r in self._tree.get_children():
            self._tree.delete(r)

        for i, item in enumerate(self._lv_sorted):
            author = item.get("author") or item.get("username") or "—"
            caption = (item.get("caption") or "").replace("\n", " ")
            if len(caption) > 80:
                caption = caption[:79] + "…"
            sc  = item.get("shortcode") or "?"
            mt  = item.get("media_type") or "?"
            st  = item.get("status") or "?"
            ts  = item.get("timestamp", "")
            if ts:
                try:
                    ts = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass

            tag = "ok" if st == "ok" else "fail" if st == "failed" else "dry" if st == "dry_run" else ""
            self._tree.insert("", "end", iid=str(i),
                              values=(i + 1, author, caption, sc, mt, st, ts),
                              tags=(tag,) if tag else ())

    def _on_right_click(self, event):
        row_id = self._tree.identify_row(event.y)
        if not row_id:
            return
        self._tree.selection_set(row_id)
        idx = int(row_id)
        if idx >= len(self._lv_sorted):
            return
        item = self._lv_sorted[idx]

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Open in browser",
                         command=lambda: webbrowser.open(item.get("url", "")))
        menu.add_command(label="Copy URL",
                         command=lambda: self._clip(item.get("url", "")))
        menu.add_command(label="Copy shortcode",
                         command=lambda: self._clip(item.get("shortcode", "")))
        if item.get("caption"):
            menu.add_command(label="Copy caption",
                             command=lambda: self._clip(item.get("caption", "")))
        menu.tk_popup(event.x_root, event.y_root)

    def _on_double_click(self, event):
        row_id = self._tree.identify_row(event.y)
        if not row_id:
            return
        idx = int(row_id)
        if idx < len(self._lv_sorted):
            url = self._lv_sorted[idx].get("url", "")
            if url:
                webbrowser.open(url)

    def _clip(self, text: str):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)


def main():
    root = tk.Tk()
    app = IGDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
