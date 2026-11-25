# -*- coding: utf-8 -*-
"""专辑制作工作台 v1.0 (AlbumWorkbench)

整合：
  ① 音频排序 & 封面：鼠标拖拽排序 / 纯文本曲目表匹配 / 安全改名写标签与撤销 /
     FLAC 内嵌或联网高清封面直存目录 / 发行日期与片头两行文案一键复制
  ② 歌词合并 SRT：自动沿用音频顺序与时长 / 可选每首歌开头自动插编号歌名（第一句歌词前）
  ③ 时间轴 & 发布辅助：自动生成时间轴 + 全局加减秒数（支持外部粘贴时间轴偏移）/
     QQ音乐简介纯文本抓取 / 自动推荐 6 个符合专辑的 B 站标签
  + 置顶迷你歌单条：点一下复制去后缀歌名并变灰打勾，方便达芬奇手动贴歌名
"""

import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import traceback

from core_audio import (
    APP_VER,
    AUDIO_EXTS,
    UNDO_FILE,
    apply_plan,
    extract_folder_embedded_cover,
    fmt_dur,
    list_audio_files,
    load_cfg,
    load_json,
    make_new_name,
    make_new_stem,
    match_tracks,
    parse_tracklist,
    plan_order,
    read_audio_info,
    save_cfg,
    undo_plan,
    write_log,
)
from core_srt import (
    MODE_APPEND,
    MODE_AUDIO,
    align_srts_to_tracks,
    generate_timeline,
    list_srt_files,
    merge_album_srts,
    shift_timeline_text,
    write_srt,
)
from core_online import (
    download_cover_image,
    fetch_album_metadata,
    recommend_album_tags,
)

APP_TITLE = "专辑制作工作台 v%s" % APP_VER
EXTRA_SRT_DIR = r"D:\MP3音乐包\歌词包.srt"


# ===================================================================== Windows 资源管理器拖拽
WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4
_HAS_WIN_DROP = (os.name == "nt")

if _HAS_WIN_DROP:
    _LRESULT = ctypes.c_ssize_t
    _WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    _user32.GetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.CallWindowProcW.restype = _LRESULT
    _user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
    _user32.GetParent.restype = wintypes.HWND
    _user32.GetParent.argtypes = [wintypes.HWND]
    _shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    _shell32.DragQueryFileW.restype = wintypes.UINT
    _shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                        ctypes.c_wchar_p, wintypes.UINT]
    _shell32.DragFinish.argtypes = [ctypes.c_void_p]


class DropTarget:
    def __init__(self, tk_win, on_paths, interval=150):
        self.tk = tk_win
        self.on_paths = on_paths
        self.interval = interval
        self._alive = True
        self._queue = []
        self._old = 0
        self._proc = None
        self._proc_addr = 0
        self.hwnd = None
        self._accept = []
        self.ok = False
        if not _HAS_WIN_DROP:
            return
        try:
            tk_win.update()
            child = tk_win.winfo_id() or 0
            parent = _user32.GetParent(child) if child else 0
            self.hwnd = child or parent
            self._accept = [h for h in (child, parent) if h]
            for h in self._accept:
                _shell32.DragAcceptFiles(h, True)
            self._proc = _WNDPROC(self._handle)
            self._proc_addr = int(ctypes.cast(self._proc, ctypes.c_void_p).value or 0)
            old = _user32.SetWindowLongPtrW(self.hwnd, GWLP_WNDPROC, self._proc_addr)
            self._old = int(old or 0)
            self.ok = bool(old)
        except Exception:
            self.ok = False
        self.tk.after(self.interval, self._tick)

    def _handle(self, hwnd, msg, wparam, lparam):
        if msg == WM_DROPFILES:
            try:
                cnt = _shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
                buf = ctypes.create_unicode_buffer(32768)
                paths = []
                for i in range(cnt):
                    _shell32.DragQueryFileW(wparam, i, buf, 32768)
                    if buf.value:
                        paths.append(buf.value)
                _shell32.DragFinish(wparam)
                if paths:
                    self._queue.append(paths)
            except Exception:
                pass
            return 0
        if self._old:
            try:
                return _user32.CallWindowProcW(self._old, hwnd, msg, wparam, lparam)
            except Exception:
                pass
        return 0

    def _tick(self):
        if not self._alive:
            return
        while self._queue:
            paths = self._queue.pop(0)
            try:
                self.on_paths(paths)
            except Exception:
                write_log("拖拽处理失败:\n%s" % traceback.format_exc())
        try:
            self.tk.after(self.interval, self._tick)
        except Exception:
            pass

    def close(self):
        self._alive = False
        for h in self._accept:
            try:
                _shell32.DragAcceptFiles(h, False)
            except Exception:
                pass
        if self.ok and self.hwnd and self._old:
            try:
                _user32.SetWindowLongPtrW(self.hwnd, GWLP_WNDPROC, self._old)
            except Exception:
                pass


# ===================================================================== 置顶迷你歌单条
class FloatingTrackBar(tk.Toplevel):
    """置顶歌单条：点击某行自动复制去后缀歌名，并自动变灰打勾。"""

    def __init__(self, master, get_names_cb, on_copy_cb):
        super().__init__(master)
        self.get_names_cb = get_names_cb
        self.on_copy_cb = on_copy_cb
        self.done_set = set()
        self.title("置顶歌单条（点一下即复制）")
        self.geometry("360x480")
        self.attributes("-topmost", True)
        self.configure(bg="#1E2028")

        top = tk.Frame(self, bg="#1E2028", padx=8, pady=6)
        top.pack(fill="x")
        tk.Label(top, text="点击歌名直接复制（已去后缀）", bg="#1E2028",
                 fg="#9CA3AF", font=("Microsoft YaHei UI", 9)).pack(side="left")
        tk.Button(top, text="重置勾选", command=self.reset_marks,
                  bg="#2D313E", fg="#E5E7EB", relief="flat",
                  padx=8, pady=2, cursor="hand2").pack(side="right")

        frame = tk.Frame(self, bg="#1E2028")
        frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self.canvas = tk.Canvas(frame, bg="#1E2028", highlightthickness=0)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg="#1E2028")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._win_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win_id, width=e.width))
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", self._on_wheel, add="+")

        self.buttons = []
        self.refresh()

    def _on_wheel(self, event):
        try:
            if self.winfo_exists() and self.focus_displayof() is not None:
                w = self.winfo_containing(event.x_root, event.y_root)
                if w is not None and str(w).startswith(str(self)):
                    self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass

    def reset_marks(self):
        self.done_set.clear()
        self.refresh()

    def refresh(self):
        for w in self.inner.winfo_children():
            w.destroy()
        self.buttons = []
        names = self.get_names_cb()
        if not names:
            tk.Label(self.inner, text="当前没有加载歌曲", bg="#1E2028",
                     fg="#6B7280", pady=20).pack()
            return
        for name in names:
            is_done = name in self.done_set
            txt = ("✓  " + name) if is_done else name
            bg = "#252932" if is_done else "#2C3240"
            fg = "#6B7280" if is_done else "#F3F4F6"
            btn = tk.Button(
                self.inner, text=txt, anchor="w", justify="left",
                bg=bg, fg=fg, activebackground="#3B82F6", activeforeground="#FFFFFF",
                relief="flat", padx=10, pady=6, cursor="hand2",
                font=("Microsoft YaHei UI", 9),
                command=lambda n=name: self._copy_item(n),
            )
            btn.pack(fill="x", pady=2)
            self.buttons.append(btn)

    def _copy_item(self, name):
        self.clipboard_clear()
        self.clipboard_append(name)
        self.done_set.add(name)
        self.refresh()
        self.on_copy_cb("已复制歌名：%s" % name)


# ===================================================================== 主工作台
class AlbumWorkbenchApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1120x740")
        self.root.minsize(960, 620)

        self.cfg = load_cfg()
        self.folder = ""
        self.tracks = []       # Tab 1 音频列表（顺序即全局顺序）
        self.srt_rows = []     # Tab 2 歌词对齐行 [{"track", "srt", "score", "label", "duration"}]
        self.tag_pool = []
        self.tag_page = 0
        self.last_meta = {}
        self.float_bar = None

        self._drag_audio = None
        self._drag_srt = None

        # 变量
        self.var_folder = tk.StringVar()
        self.var_album = tk.StringVar()
        self.var_artist = tk.StringVar()
        self.var_date = tk.StringVar()
        self.var_topmost = tk.BooleanVar(value=False)
        self.var_write_tags = tk.BooleanVar(value=True)
        self.var_intro_line1 = tk.StringVar()
        self.var_intro_line2 = tk.StringVar()

        self.var_srt_mode = tk.StringVar(value="按音频时长（最准）")
        self.var_srt_gap = tk.StringVar(value="0.0")
        self.var_insert_title = tk.BooleanVar(value=True)
        self.var_save_extra_srt = tk.BooleanVar(value=True)
        self.var_srt_out = tk.StringVar()

        self.var_tl_offset = tk.StringVar(value="3")
        self.var_tl_index = tk.BooleanVar(value=True)
        self.var_tl_artist = tk.BooleanVar(value=True)
        self.var_tl_frames = tk.BooleanVar(value=False)
        self.var_status = tk.StringVar(value="把专辑文件夹拖进来，或点左上角「选择文件夹」开始。")

        self.var_album.trace_add("write", lambda *_: self._refresh_intro_lines())
        self.var_artist.trace_add("write", lambda *_: self._refresh_intro_lines())
        self.var_date.trace_add("write", lambda *_: self._refresh_intro_lines())

        self._setup_style()
        self._build_ui()

        self.drop = DropTarget(self.root, self._on_drop_paths)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 如果命令行传了文件夹直接打开
        if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
            self.load_folder(sys.argv[1])
        elif self.cfg.get("last_folder") and os.path.isdir(self.cfg["last_folder"]):
            self.var_folder.set(self.cfg["last_folder"])

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure(".", font=("Microsoft YaHei UI", 9))
        style.configure("Treeview", rowheight=26, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TNotebook.Tab", padding=(14, 6), font=("Microsoft YaHei UI", 10))

    def _build_ui(self):
        # 顶部公共栏
        top = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        top.pack(fill="x")

        r1 = ttk.Frame(top)
        r1.pack(fill="x", pady=(0, 5))
        ttk.Label(r1, text="专辑目录:").pack(side="left")
        ttk.Entry(r1, textvariable=self.var_folder).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r1, text="选择文件夹", command=self.pick_folder).pack(side="left", padx=2)
        ttk.Button(r1, text="刷新", command=self.reload_folder).pack(side="left", padx=2)
        ttk.Button(r1, text="打开目录", command=self.open_folder).pack(side="left", padx=2)
        ttk.Separator(r1, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(r1, text="置顶歌单条", command=self.open_float_bar).pack(side="left", padx=2)
        ttk.Checkbutton(r1, text="窗口置顶", variable=self.var_topmost,
                        command=self._toggle_topmost).pack(side="left", padx=(6, 0))

        r2 = ttk.Frame(top)
        r2.pack(fill="x")
        ttk.Label(r2, text="专辑名:").pack(side="left")
        ttk.Entry(r2, textvariable=self.var_album, width=22).pack(side="left", padx=(4, 10))
        ttk.Label(r2, text="歌手:").pack(side="left")
        ttk.Entry(r2, textvariable=self.var_artist, width=18).pack(side="left", padx=(4, 10))
        ttk.Label(r2, text="发行日期:").pack(side="left")
        ttk.Entry(r2, textvariable=self.var_date, width=12).pack(side="left", padx=(4, 10))
        ttk.Button(r2, text="搜ID/联网查询", style="Accent.TButton",
                   command=self.fetch_online_all).pack(side="left", padx=(0, 6))
        self.btn_qq_link = ttk.Button(r2, text="复制QQ ID",
                                      command=lambda: self._copy_album_link("qq"))
        self.btn_qq_link.pack(side="left", padx=2)
        self.btn_ne_link = ttk.Button(r2, text="复制网易 ID",
                                      command=lambda: self._copy_album_link("netease"))
        self.btn_ne_link.pack(side="left", padx=2)

        # 3 个精简标签页
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(4, 4))

        self.tab_audio = ttk.Frame(self.nb, padding=8)
        self.tab_srt = ttk.Frame(self.nb, padding=8)
        self.tab_pub = ttk.Frame(self.nb, padding=8)

        self.nb.add(self.tab_audio, text=" ① 音频排序 & 封面 ")
        self.nb.add(self.tab_srt, text=" ② 歌词合并 SRT ")
        self.nb.add(self.tab_pub, text=" ③ 时间轴 & 发布辅助 ")

        self._build_tab_audio()
        self._build_tab_srt()
        self._build_tab_pub()

        # 底部状态栏
        bar = ttk.Frame(self.root, padding=(10, 3, 10, 6))
        bar.pack(fill="x")
        self.lbl_status = ttk.Label(bar, textvariable=self.var_status, foreground="#374151")
        self.lbl_status.pack(side="left", fill="x", expand=True)

    # ----------------------------------------------------------------- Tab 1: 音频排序 & 封面
    def _build_tab_audio(self):
        # 列表（直接拖动行调顺序，无废按钮）
        hint = ttk.Frame(self.tab_audio)
        hint.pack(fill="x", pady=(0, 4))
        ttk.Label(hint, text="直接鼠标上下拖动行即可调整顺序；双击行可直接输入第几位",
                  foreground="#6B7280").pack(side="left")
        ttk.Checkbutton(hint, text="写入音频标签（音轨号/歌名/歌手/专辑）",
                        variable=self.var_write_tags,
                        command=self.refresh_audio_tree).pack(side="right")

        tf = ttk.Frame(self.tab_audio)
        tf.pack(fill="both", expand=True)
        cols = ("idx", "dur", "match", "old", "new")
        self.tree_audio = ttk.Treeview(tf, columns=cols, show="headings", selectmode="browse")
        self.tree_audio.heading("idx", text="序号")
        self.tree_audio.heading("dur", text="时长")
        self.tree_audio.heading("match", text="匹配")
        self.tree_audio.heading("old", text="原文件名")
        self.tree_audio.heading("new", text="编号后文件名")
        self.tree_audio.column("idx", width=52, anchor="center", stretch=False)
        self.tree_audio.column("dur", width=68, anchor="center", stretch=False)
        self.tree_audio.column("match", width=66, anchor="center", stretch=False)
        self.tree_audio.column("old", width=420, anchor="w")
        self.tree_audio.column("new", width=420, anchor="w")
        self.tree_audio.tag_configure("warn", background="#FEF3C7")
        self.tree_audio.tag_configure("good", background="#ECFDF5")

        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree_audio.yview)
        self.tree_audio.configure(yscrollcommand=sb.set)
        self.tree_audio.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.tree_audio.bind("<ButtonPress-1>", self._audio_drag_start)
        self.tree_audio.bind("<B1-Motion>", self._audio_drag_motion)
        self.tree_audio.bind("<ButtonRelease-1>", self._audio_drag_end)
        self.tree_audio.bind("<Double-1>", self._audio_double_click)

        # 纯文本曲目表区（仅保留文本粘贴 + 匹配排序 + 清空）
        pf = ttk.LabelFrame(self.tab_audio, text="曲目表文本排序（从 QQ音乐/网易云 复制歌单 Ctrl+V 粘贴即自动匹配）",
                            padding=6)
        pf.pack(fill="x", pady=(8, 6))
        self.txt_tracklist = tk.Text(pf, height=4, font=("Microsoft YaHei UI", 9))
        self.txt_tracklist.pack(side="left", fill="both", expand=True)
        self.txt_tracklist.bind("<Control-v>", self._on_tracklist_paste)
        self.txt_tracklist.bind("<Control-V>", self._on_tracklist_paste)

        pbtns = ttk.Frame(pf, padding=(8, 0, 0, 0))
        pbtns.pack(side="right", fill="y")
        ttk.Button(pbtns, text="匹配排序", width=11, command=self.match_tracklist).pack(pady=(0, 4))
        ttk.Button(pbtns, text="清空", width=11,
                   command=lambda: self.txt_tracklist.delete("1.0", "end")).pack()

        # 底部操作行 + 片头文案（分两行，避免高分屏挤压裁切）
        bottom = ttk.Frame(self.tab_audio)
        bottom.pack(fill="x", pady=(4, 0))

        act = ttk.Frame(bottom)
        act.pack(fill="x", pady=(0, 6))
        ttk.Button(act, text="▶ 应用改名 + 写标签", style="Accent.TButton",
                   command=self.do_apply_rename).pack(side="left", padx=(0, 6))
        ttk.Button(act, text="撤销上次", command=self.do_undo_rename).pack(side="left", padx=(0, 10))
        ttk.Button(act, text="提取/下载封面到目录", command=self.do_save_cover).pack(side="left")

        intro = ttk.Frame(bottom)
        intro.pack(fill="x")
        ttk.Label(intro, text="片头文案:").pack(side="left", padx=(0, 6))
        ttk.Entry(intro, textvariable=self.var_intro_line1).pack(side="left", fill="x", expand=True, padx=2)
        ttk.Button(intro, text="复制", width=6,
                   command=lambda: self._copy(self.var_intro_line1.get(), "片头第1行")).pack(side="left", padx=(2, 10))
        ttk.Entry(intro, textvariable=self.var_intro_line2).pack(side="left", fill="x", expand=True, padx=2)
        ttk.Button(intro, text="复制", width=6,
                   command=lambda: self._copy(self.var_intro_line2.get(), "片头第2行")).pack(side="left", padx=(2, 0))

    # ----------------------------------------------------------------- Tab 2: 歌词合并 SRT
    def _build_tab_srt(self):
        top = ttk.Frame(self.tab_srt)
        top.pack(fill="x", pady=(0, 6))
        ttk.Button(top, text="添加歌词文件", command=self.pick_srts).pack(side="left", padx=(0, 4))
        ttk.Button(top, text="扫描当前目录歌词", command=self.scan_folder_srts).pack(side="left", padx=4)
        ttk.Button(top, text="按音频顺序重新对齐", command=self.realign_srts).pack(side="left", padx=4)
        ttk.Button(top, text="清空歌词", command=self.clear_srts).pack(side="left", padx=4)
        ttk.Label(top, text="（可直接拖动行调整顺序，也可把 .srt 文件拖进窗口）",
                  foreground="#6B7280").pack(side="left", padx=8)

        tf = ttk.Frame(self.tab_srt)
        tf.pack(fill="both", expand=True)
        cols = ("idx", "dur", "track", "srt", "state")
        self.tree_srt = ttk.Treeview(tf, columns=cols, show="headings", selectmode="browse")
        self.tree_srt.heading("idx", text="序号")
        self.tree_srt.heading("dur", text="音频时长")
        self.tree_srt.heading("track", text="对应歌曲（编号歌名）")
        self.tree_srt.heading("srt", text="对应 SRT 字幕文件")
        self.tree_srt.heading("state", text="匹配")
        self.tree_srt.column("idx", width=52, anchor="center", stretch=False)
        self.tree_srt.column("dur", width=72, anchor="center", stretch=False)
        self.tree_srt.column("track", width=360, anchor="w")
        self.tree_srt.column("srt", width=420, anchor="w")
        self.tree_srt.column("state", width=70, anchor="center", stretch=False)
        self.tree_srt.tag_configure("warn", background="#FEF3C7")
        self.tree_srt.tag_configure("good", background="#ECFDF5")

        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree_srt.yview)
        self.tree_srt.configure(yscrollcommand=sb.set)
        self.tree_srt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.tree_srt.bind("<ButtonPress-1>", self._srt_drag_start)
        self.tree_srt.bind("<B1-Motion>", self._srt_drag_motion)
        self.tree_srt.bind("<ButtonRelease-1>", self._srt_drag_end)

        # 合并选项
        opt = ttk.LabelFrame(self.tab_srt, text="合并选项与导出", padding=8)
        opt.pack(fill="x", pady=(8, 6))

        o1 = ttk.Frame(opt)
        o1.pack(fill="x", pady=(0, 6))
        ttk.Label(o1, text="合并方式:").pack(side="left")
        ttk.Combobox(o1, textvariable=self.var_srt_mode, width=20, state="readonly",
                     values=("按音频时长（最准）", "顺序拼接")).pack(side="left", padx=(4, 12))
        ttk.Label(o1, text="间隔(秒):").pack(side="left")
        ttk.Entry(o1, textvariable=self.var_srt_gap, width=6).pack(side="left", padx=(4, 14))
        ttk.Checkbutton(o1, text="每首歌开头自动插入编号歌名（第一句歌词出现前）",
                        variable=self.var_insert_title).pack(side="left")
        ttk.Button(o1, text="备用：置顶歌单条", command=self.open_float_bar).pack(side="right")

        o2 = ttk.Frame(opt)
        o2.pack(fill="x", pady=(0, 4))
        ttk.Label(o2, text="导出路径:").pack(side="left")
        ttk.Entry(o2, textvariable=self.var_srt_out).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(o2, text="浏览...", command=self.pick_srt_out).pack(side="left", padx=(0, 6))
        ttk.Button(o2, text="▶ 合并并导出 SRT", style="Accent.TButton",
                   command=self.do_merge_srt).pack(side="left")

        o3 = ttk.Frame(opt)
        o3.pack(fill="x")
        ttk.Checkbutton(o3, text="同步存一份同名歌词到 %s" % EXTRA_SRT_DIR,
                        variable=self.var_save_extra_srt).pack(side="left")

        # 合并结果与提醒日志
        self.txt_srt_log = tk.Text(self.tab_srt, height=5, font=("Microsoft YaHei UI", 9),
                                   bg="#F9FAFB")
        self.txt_srt_log.pack(fill="x")

    # ----------------------------------------------------------------- Tab 3: 时间轴 & 发布辅助
    def _build_tab_pub(self):
        wrap = ttk.Frame(self.tab_pub)
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(0, weight=55, uniform="pub")
        wrap.columnconfigure(1, weight=45, uniform="pub")
        wrap.rowconfigure(0, weight=1)

        # 左侧：时间轴（选项和按钮分两行，绝不裁切）
        left = ttk.LabelFrame(wrap, text="时间轴（按音频时长自动算 / 支持粘贴外部时间轴偏移）",
                              padding=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        ctrl1 = ttk.Frame(left)
        ctrl1.pack(fill="x", pady=(0, 5))
        ttk.Label(ctrl1, text="偏移(秒):").pack(side="left")
        ttk.Entry(ctrl1, textvariable=self.var_tl_offset, width=6).pack(side="left", padx=(4, 10))
        ttk.Checkbutton(ctrl1, text="序号", variable=self.var_tl_index,
                        command=self.rebuild_timeline).pack(side="left", padx=4)
        ttk.Checkbutton(ctrl1, text="歌手", variable=self.var_tl_artist,
                        command=self.rebuild_timeline).pack(side="left", padx=4)
        ttk.Checkbutton(ctrl1, text="精确到帧", variable=self.var_tl_frames,
                        command=self.rebuild_timeline).pack(side="left", padx=4)

        ctrl2 = ttk.Frame(left)
        ctrl2.pack(fill="x", pady=(0, 6))
        ttk.Button(ctrl2, text="按音频生成", command=self.rebuild_timeline).pack(side="left", padx=(0, 6))
        ttk.Button(ctrl2, text="对当前文本偏移", command=self.apply_shift_to_text).pack(side="left")
        ttk.Button(ctrl2, text="复制时间轴", style="Accent.TButton",
                   command=lambda: self._copy(self.txt_timeline.get("1.0", "end").strip(), "时间轴")
                   ).pack(side="right")

        tl_box = ttk.Frame(left)
        tl_box.pack(fill="both", expand=True)
        self.txt_timeline = tk.Text(tl_box, font=("Consolas", 10))
        tl_sb = ttk.Scrollbar(tl_box, orient="vertical", command=self.txt_timeline.yview)
        self.txt_timeline.configure(yscrollcommand=tl_sb.set)
        self.txt_timeline.pack(side="left", fill="both", expand=True)
        tl_sb.pack(side="right", fill="y")

        # 右侧：QQ 音乐简介（纯文本）+ 自动推荐 6 个符合专辑的标签
        right = ttk.Frame(wrap)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        intro_box = ttk.LabelFrame(right, text="QQ 音乐专辑简介（纯文本）", padding=8)
        intro_box.pack(fill="both", expand=True, pady=(0, 8))

        ib_top = ttk.Frame(intro_box)
        ib_top.pack(fill="x", pady=(0, 4))
        self.lbl_intro_src = ttk.Label(ib_top, text="点上方「搜ID/联网查询」自动拉取",
                                       foreground="#6B7280")
        self.lbl_intro_src.pack(side="left")
        ttk.Button(ib_top, text="复制简介",
                   command=lambda: self._copy(self.txt_qq_intro.get("1.0", "end").strip(), "专辑简介")
                   ).pack(side="right")

        ib_txt = ttk.Frame(intro_box)
        ib_txt.pack(fill="both", expand=True)
        self.txt_qq_intro = tk.Text(ib_txt, height=9, wrap="word", font=("Microsoft YaHei UI", 9))
        ib_sb = ttk.Scrollbar(ib_txt, orient="vertical", command=self.txt_qq_intro.yview)
        self.txt_qq_intro.configure(yscrollcommand=ib_sb.set)
        self.txt_qq_intro.pack(side="left", fill="both", expand=True)
        ib_sb.pack(side="right", fill="y")
        self._intro_timer = None
        self.txt_qq_intro.bind("<KeyRelease>", self._on_intro_edited)
        self.txt_qq_intro.bind("<<Paste>>", self._on_intro_edited)

        tag_box = ttk.LabelFrame(right, text="推荐标签（6 个符合该专辑，点击单个即复制）", padding=8)
        tag_box.pack(fill="x")

        self.tag_grid = ttk.Frame(tag_box)
        self.tag_grid.pack(fill="x", pady=(0, 6))

        tb_act = ttk.Frame(tag_box)
        tb_act.pack(fill="x")
        ttk.Button(tb_act, text="复制全部 6 个", command=self.copy_all_tags).pack(side="left")
        ttk.Button(tb_act, text="换一批", command=self.next_tag_page).pack(side="left", padx=6)

    # ===================================================================== 公共动作
    def set_status(self, msg):
        self.var_status.set(msg)

    def _copy(self, text, label="内容"):
        if not text:
            self.set_status("%s 为空，未复制" % label)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status("已复制 %s" % label)

    def _toggle_topmost(self):
        self.root.attributes("-topmost", bool(self.var_topmost.get()))

    def _refresh_intro_lines(self):
        alb = self.var_album.get().strip() or "专辑名"
        art = self.var_artist.get().strip() or "歌手"
        dt = self.var_date.get().strip() or "YYYY-MM-DD"
        self.var_intro_line1.set("《%s 神专推荐》" % alb)
        self.var_intro_line2.set("（%s-%s）" % (art, dt))

    def open_float_bar(self):
        if self.float_bar is not None and self.float_bar.winfo_exists():
            self.float_bar.lift()
            self.float_bar.refresh()
            return
        self.float_bar = FloatingTrackBar(
            self.root,
            self._current_clean_names,
            self.set_status,
        )

    def _current_clean_names(self):
        use_tags = bool(self.var_write_tags.get())
        return [make_new_stem(i + 1, info, use_tags) for i, info in enumerate(self.tracks)]

    def _refresh_float_bar(self):
        if self.float_bar is not None and self.float_bar.winfo_exists():
            self.float_bar.refresh()

    def pick_folder(self):
        init = self.folder or self.cfg.get("last_folder") or os.path.expanduser("~")
        d = filedialog.askdirectory(title="选择专辑文件夹", initialdir=init)
        if d:
            self.load_folder(d)

    def reload_folder(self):
        d = self.var_folder.get().strip()
        if d and os.path.isdir(d):
            self.load_folder(d)

    def open_folder(self):
        d = self.var_folder.get().strip()
        if d and os.path.isdir(d):
            if os.name == "nt":
                os.startfile(d)
            else:
                subprocess.Popen(["xdg-open", d])

    def _on_drop_paths(self, paths):
        srts = [p for p in paths if os.path.isfile(p) and p.lower().endswith(".srt")]
        dirs = [p for p in paths if os.path.isdir(p)]
        audios = [p for p in paths if os.path.isfile(p) and os.path.splitext(p)[1].lower() in AUDIO_EXTS]
        if dirs:
            self.load_folder(dirs[0])
            return
        if srts and not audios:
            self._load_srt_paths(srts)
            self.nb.select(self.tab_srt)
            return
        if audios:
            self.load_folder(os.path.dirname(audios[0]))

    def load_folder(self, folder):
        self.folder = os.path.abspath(folder)
        self.var_folder.set(self.folder)
        self.cfg["last_folder"] = self.folder
        save_cfg(self.cfg)

        paths = list_audio_files(self.folder)
        self.tracks = [read_audio_info(p) for p in paths]

        # 自动提取专辑名 / 歌手 / 发行日期
        albums = [t["album"] for t in self.tracks if t.get("album")]
        artists = [t["artist"] for t in self.tracks if t.get("artist")]
        dates = [t["date"] for t in self.tracks if t.get("date")]

        album_name = max(set(albums), key=albums.count) if albums else os.path.basename(self.folder)
        artist_name = max(set(artists), key=artists.count) if artists else ""
        date_val = dates[0][:10] if dates else ""

        self.var_album.set(album_name)
        self.var_artist.set(artist_name)
        self.var_date.set(date_val)
        self.var_srt_out.set(os.path.join(self.folder, "%s_合并字幕.srt" % album_name))

        self.refresh_audio_tree()
        self.scan_folder_srts()
        self.rebuild_timeline()
        self.refresh_tags()
        self.set_status("已加载 %d 首音频（%s），正在后台自动查发行日期与简介..." % (len(self.tracks), album_name))
        # 自动联网补全发行日期、QQ音乐简介与推荐标签（不擅自覆盖封面）
        if album_name or artist_name:
            self.fetch_online_all(download_cover=False)

    # ===================================================================== Tab 1 逻辑
    def refresh_audio_tree(self):
        tree = self.tree_audio
        tree.delete(*tree.get_children())
        use_tags = bool(self.var_write_tags.get())
        for i, info in enumerate(self.tracks):
            new_name = make_new_name(i + 1, info, use_tags)
            conf = info.get("conf")
            conf_txt = "" if conf is None else ("%d%%" % int(round(conf * 100)))
            tags = []
            if info.get("unmatched"):
                conf_txt = "未匹配"
                tags.append("warn")
            elif conf is not None and conf < 0.65:
                tags.append("warn")
            elif conf is not None:
                tags.append("good")
            tree.insert("", "end", iid=info["key"],
                        values=("%02d" % (i + 1) if i < 99 else str(i + 1),
                                fmt_dur(info.get("dur")), conf_txt,
                                info["name"], new_name),
                        tags=tuple(tags))
        self._refresh_float_bar()

    def _sync_audio_order(self):
        by_key = {t["key"]: t for t in self.tracks}
        self.tracks = [by_key[iid] for iid in self.tree_audio.get_children() if iid in by_key]
        self.refresh_audio_tree()
        self.realign_srts()
        self.rebuild_timeline()

    def _audio_drag_start(self, event):
        row = self.tree_audio.identify_row(event.y)
        self._drag_audio = [row, event.y] if row else None

    def _audio_drag_motion(self, event):
        if not self._drag_audio:
            return
        item, last_y = self._drag_audio
        if abs(event.y - last_y) < 8:
            return
        target = self.tree_audio.identify_row(event.y)
        if target and target != item:
            self.tree_audio.move(item, "", self.tree_audio.index(target))
            self._drag_audio = [item, event.y]

    def _audio_drag_end(self, _event):
        if self._drag_audio:
            self._drag_audio = None
            self._sync_audio_order()

    def _audio_double_click(self, event):
        row = self.tree_audio.identify_row(event.y)
        if not row:
            return
        idx = list(self.tree_audio.get_children()).index(row)
        num = simpledialog.askinteger(
            "调整位置", "把《%s》放到第几位？（1 - %d）" % (self.tracks[idx]["name"], len(self.tracks)),
            initialvalue=idx + 1, minvalue=1, maxvalue=len(self.tracks), parent=self.root)
        if not num:
            return
        item = self.tracks.pop(idx)
        self.tracks.insert(num - 1, item)
        self.refresh_audio_tree()
        self.realign_srts()
        self.rebuild_timeline()

    def _on_tracklist_paste(self, _event=None):
        self.root.after(40, self.match_tracklist)

    def match_tracklist(self):
        if not self.tracks:
            return
        text = self.txt_tracklist.get("1.0", "end").strip()
        if not text:
            return
        entries = parse_tracklist(text)
        if not entries:
            self.set_status("未能从粘贴文本中识别出曲目")
            return
        matched = match_tracks(entries, self.tracks)
        new_order = []
        used = set()
        for ei in range(len(entries)):
            if ei in matched:
                fi, sc = matched[ei]
                info = self.tracks[fi]
                info["conf"] = sc
                info["unmatched"] = False
                new_order.append(info)
                used.add(fi)
        for fi, info in enumerate(self.tracks):
            if fi not in used:
                info["conf"] = None
                info["unmatched"] = True
                new_order.append(info)
        self.tracks = new_order
        self.refresh_audio_tree()
        self.realign_srts()
        self.rebuild_timeline()
        self.set_status("文本匹配完成：已匹配 %d / %d 首" % (len(used), len(self.tracks)))

    def do_apply_rename(self):
        if not self.tracks:
            return
        use_tags = bool(self.var_write_tags.get())
        album = self.var_album.get().strip()
        plan = plan_order(self.tracks, use_tags)
        if not messagebox.askyesno("确认改名",
                                   "确定按当前顺序重命名 %d 首音频%s？\n（改错可随时点「撤销上次」还原）"
                                   % (len(plan), "并写入标签" if use_tags else "")):
            return
        try:
            apply_plan(plan, album, write_tags=use_tags)
            self.load_folder(self.folder)
            self.set_status("已成功重命名 %d 首音频！" % len(plan))
        except Exception as exc:
            messagebox.showerror("应用失败", str(exc))

    def do_undo_rename(self):
        undo = load_json(UNDO_FILE)
        if not undo:
            messagebox.showinfo("提示", "没有找到可撤销的记录")
            return
        if not messagebox.askyesno("确认撤销", "确定还原上次改名的 %d 首文件？" % len(undo.get("files") or [])):
            return
        try:
            cnt = undo_plan(undo)
            if self.folder:
                self.load_folder(self.folder)
            self.set_status("已还原 %d 首文件" % cnt)
        except Exception as exc:
            messagebox.showerror("撤销失败", str(exc))

    # ===================================================================== 封面 & 联网信息
    def do_save_cover(self):
        if not self.folder or not os.path.isdir(self.folder):
            messagebox.showinfo("提示", "请先选择专辑文件夹")
            return
        # 优先从 FLAC 提取内嵌封面
        embedded = extract_folder_embedded_cover([t["path"] for t in self.tracks], self.folder)
        if embedded:
            self.set_status("已从 FLAC 直接提取内嵌封面：%s" % os.path.basename(embedded))
            messagebox.showinfo("封面已提取", "已从音频内嵌封面直接保存到目录：\n%s" % embedded)
            return
        # 没有内嵌封面则联网下载
        self.set_status("音频中无内嵌封面，正在联网下载高清封面...")
        self.fetch_online_all(download_cover=True)

    def fetch_online_all(self, download_cover=True):
        album = self.var_album.get().strip()
        artist = self.var_artist.get().strip()
        if not album and not artist:
            messagebox.showinfo("提示", "请先加载文件夹或填写专辑名/歌手")
            return
        self.set_status("正在联网查询发行日期、高清封面、QQ音乐简介与推荐标签...")

        def worker():
            meta = fetch_album_metadata(album, artist)
            saved_cover = None
            if download_cover and self.folder and os.path.isdir(self.folder):
                # 先看文件夹里是否已有内嵌封面可提
                saved_cover = extract_folder_embedded_cover([t["path"] for t in self.tracks], self.folder)
                if not saved_cover and meta.get("cover_urls"):
                    saved_cover = download_cover_image(meta["cover_urls"], self.folder, "cover.jpg")
            self.root.after(0, lambda: self._on_online_done(meta, saved_cover))

        threading.Thread(target=worker, daemon=True).start()

    def _on_online_done(self, meta, saved_cover):
        self.last_meta = meta or {}
        if meta.get("date"):
            self.var_date.set(meta["date"])
        if meta.get("artist") and not self.var_artist.get().strip():
            self.var_artist.set(meta["artist"])
        self._refresh_intro_lines()

        # QQ 音乐简介
        self.txt_qq_intro.delete("1.0", "end")
        if meta.get("intro"):
            self.txt_qq_intro.insert("1.0", meta["intro"])
            self.lbl_intro_src.config(text="已自动抓取 QQ 音乐纯文本简介", foreground="#059669")
        else:
            self.lbl_intro_src.config(text="未抓到 QQ 音乐简介，请手动从 QQ 音乐复制", foreground="#B45309")

        # 推荐 6 个标签
        self.refresh_tags()

        qq_id = meta.get("qq_id") or meta.get("qq_mid") or ""
        ne_id = meta.get("netease_id") or ""
        self.btn_qq_link.config(text=("QQ ID: %s" % qq_id) if qq_id else "复制QQ ID")
        self.btn_ne_link.config(text=("网易 ID: %s" % ne_id) if ne_id else "复制网易 ID")

        msgs = []
        if meta.get("date"):
            msgs.append("日期 %s" % meta["date"])
        if qq_id:
            msgs.append("QQ ID %s" % qq_id)
        if ne_id:
            msgs.append("网易 ID %s" % ne_id)
        if saved_cover:
            msgs.append("封面已存 %s" % os.path.basename(saved_cover))
        if meta.get("intro"):
            msgs.append("已抓简介")
        self.set_status("联网查询完成：" + (" | ".join(msgs) if msgs else "未查到匹配信息"))

    def _copy_album_link(self, platform):
        meta = self.last_meta or {}
        if platform == "qq":
            # 优先复制数字 ID，没有数字 ID 时自动降级复制链接或 MID
            val = meta.get("qq_id") or meta.get("qq_url") or meta.get("qq_mid") or ""
            label = "QQ音乐专辑ID/链接"
        else:
            val = meta.get("netease_id") or meta.get("netease_url") or ""
            label = "网易云专辑ID/链接"
        if val:
            self._copy(val, "%s（%s）" % (label, val))
        else:
            self.set_status("暂未查到 %s，请填好专辑名/歌手后点「搜ID/联网查询」" % label)

    # ===================================================================== Tab 2 歌词合并逻辑
    def scan_folder_srts(self):
        srts = list_srt_files(self.folder) if self.folder else []
        self._load_srt_paths(srts)

    def pick_srts(self):
        init = self.folder or os.path.expanduser("~")
        files = filedialog.askopenfilenames(
            title="选择 SRT 字幕文件", initialdir=init,
            filetypes=[("SRT 字幕", "*.srt"), ("所有文件", "*.*")])
        if files:
            self._load_srt_paths(list(files))

    def clear_srts(self):
        self.srt_rows = []
        self.refresh_srt_tree()

    def _load_srt_paths(self, srt_paths):
        self._raw_srts = list(srt_paths)
        self.realign_srts()

    def realign_srts(self):
        raw = getattr(self, "_raw_srts", [])
        use_tags = bool(self.var_write_tags.get())
        if self.tracks:
            aligned, leftovers = align_srts_to_tracks(raw, self.tracks)
            rows = []
            for i, item in enumerate(aligned):
                t = item["track"]
                rows.append({
                    "key": "r_%d_%s" % (i, t["key"]),
                    "label": make_new_stem(i + 1, t, use_tags),
                    "duration": t.get("dur"),
                    "srt": item["srt"],
                    "score": item["score"],
                })
            for j, sp in enumerate(leftovers):
                rows.append({
                    "key": "extra_%d_%s" % (j, sp),
                    "label": os.path.splitext(os.path.basename(sp))[0],
                    "duration": None,
                    "srt": sp,
                    "score": 0.0,
                })
            self.srt_rows = rows
        else:
            self.srt_rows = [{
                "key": "s_%d_%s" % (i, sp),
                "label": os.path.splitext(os.path.basename(sp))[0],
                "duration": None,
                "srt": sp,
                "score": 1.0,
            } for i, sp in enumerate(raw)]
        self.refresh_srt_tree()

    def refresh_srt_tree(self):
        tree = self.tree_srt
        tree.delete(*tree.get_children())
        for i, row in enumerate(self.srt_rows):
            srt_name = os.path.basename(row["srt"]) if row.get("srt") else "（无对应字幕）"
            sc = row.get("score") or 0.0
            st = ("%d%%" % int(round(sc * 100))) if row.get("srt") else "缺失"
            tag = ("good",) if (row.get("srt") and sc >= 0.65) else ("warn",)
            tree.insert("", "end", iid=row["key"],
                        values=("%02d" % (i + 1), fmt_dur(row.get("duration")),
                                row["label"], srt_name, st),
                        tags=tag)

    def _srt_drag_start(self, event):
        row = self.tree_srt.identify_row(event.y)
        self._drag_srt = [row, event.y] if row else None

    def _srt_drag_motion(self, event):
        if not self._drag_srt:
            return
        item, last_y = self._drag_srt
        if abs(event.y - last_y) < 8:
            return
        target = self.tree_srt.identify_row(event.y)
        if target and target != item:
            self.tree_srt.move(item, "", self.tree_srt.index(target))
            self._drag_srt = [item, event.y]

    def _srt_drag_end(self, _event):
        if self._drag_srt:
            self._drag_srt = None
            by_key = {r["key"]: r for r in self.srt_rows}
            self.srt_rows = [by_key[iid] for iid in self.tree_srt.get_children() if iid in by_key]
            self.refresh_srt_tree()

    def pick_srt_out(self):
        init = self.folder or os.path.expanduser("~")
        p = filedialog.asksaveasfilename(
            title="保存合并后的 SRT", initialdir=init,
            defaultextension=".srt", filetypes=[("SRT 字幕", "*.srt")])
        if p:
            self.var_srt_out.set(p)

    def do_merge_srt(self):
        if not self.srt_rows:
            messagebox.showinfo("提示", "没有可合并的字幕")
            return
        out = self.var_srt_out.get().strip()
        if not out:
            self.pick_srt_out()
            out = self.var_srt_out.get().strip()
            if not out:
                return
        mode = MODE_AUDIO if "音频" in self.var_srt_mode.get() else MODE_APPEND
        try:
            gap = float(self.var_srt_gap.get().strip() or "0")
        except Exception:
            gap = 0.0

        cues, warns, stats = merge_album_srts(
            self.srt_rows, mode=mode, gap_sec=gap,
            insert_title_cue=bool(self.var_insert_title.get()),
        )
        write_srt(out, cues, bom=True)
        extra_msg = ""
        if self.var_save_extra_srt.get():
            try:
                if os.path.isdir(EXTRA_SRT_DIR) or not os.path.exists(EXTRA_SRT_DIR):
                    os.makedirs(EXTRA_SRT_DIR, exist_ok=True)
                    extra_out = os.path.join(EXTRA_SRT_DIR, os.path.basename(out))
                    if os.path.normcase(os.path.abspath(extra_out)) != os.path.normcase(os.path.abspath(out)):
                        write_srt(extra_out, cues, bom=True)
                        extra_msg = "\n✅ 已同步备份同名歌词 -> %s" % extra_out
            except Exception as exc:
                extra_msg = "\n⚠ 同步保存到 %s 失败：%s" % (EXTRA_SRT_DIR, exc)

        self.txt_srt_log.delete("1.0", "end")
        log_lines = ["✅ 合并完成：%d 条字幕 -> %s%s" % (len(cues), out, extra_msg)]
        if warns:
            log_lines.append("")
            log_lines.append("需要核对的提醒：")
            log_lines.extend(warns)
        self.txt_srt_log.insert("1.0", "\n".join(log_lines))
        self.set_status("已导出合并字幕：%s（共 %d 条）" % (os.path.basename(out), len(cues)))

    # ===================================================================== Tab 3 时间轴 & 标签
    def rebuild_timeline(self):
        if not self.tracks:
            return
        try:
            offset = int(round(float(self.var_tl_offset.get().strip() or "0")))
        except Exception:
            offset = 0
        txt, missing = generate_timeline(
            self.tracks,
            offset_sec=offset,
            include_index=bool(self.var_tl_index.get()),
            include_artist=bool(self.var_tl_artist.get()),
            show_frames=bool(self.var_tl_frames.get()),
        )
        self.txt_timeline.delete("1.0", "end")
        self.txt_timeline.insert("1.0", txt)
        if missing:
            self.set_status("时间轴已生成（注意：%d 首缺时长）" % len(missing))

    def apply_shift_to_text(self):
        try:
            delta = int(round(float(self.var_tl_offset.get().strip() or "0")))
        except Exception:
            delta = 0
        cur = self.txt_timeline.get("1.0", "end").strip()
        if not cur:
            return
        shifted = shift_timeline_text(cur, delta)
        self.txt_timeline.delete("1.0", "end")
        self.txt_timeline.insert("1.0", shifted)
        self.set_status("已对文本框内时间轴整体偏移 %+d 秒" % delta)

    def _on_intro_edited(self, _event=None):
        if self._intro_timer is not None:
            try:
                self.root.after_cancel(self._intro_timer)
            except Exception:
                pass
        self._intro_timer = self.root.after(300, self.refresh_tags)

    def refresh_tags(self):
        genre = (self.last_meta.get("genre") or
                 next((t.get("genre") for t in self.tracks if t.get("genre")), ""))
        intro_txt = self.txt_qq_intro.get("1.0", "end").strip()
        self.tag_pool = recommend_album_tags(
            album=self.var_album.get().strip(),
            artist=self.var_artist.get().strip(),
            date_str=self.var_date.get().strip(),
            genre=genre,
            tracks=self.tracks,
            intro=intro_txt,
            hot_songs=self.last_meta.get("hot_songs") or [],
        )
        self.tag_page = 0
        self._render_tags()

    def next_tag_page(self):
        if len(self.tag_pool) <= 6:
            return
        self.tag_page = (self.tag_page + 1) % max(1, (len(self.tag_pool) + 5) // 6)
        self._render_tags()

    def _current_six_tags(self):
        if not self.tag_pool:
            return []
        start = self.tag_page * 6
        chunk = self.tag_pool[start:start + 6]
        if len(chunk) < 6 and len(self.tag_pool) >= 6:
            chunk = (chunk + self.tag_pool)[:6]
        return chunk

    def _render_tags(self):
        for w in self.tag_grid.winfo_children():
            w.destroy()
        tags = self._current_six_tags()
        for i, tag in enumerate(tags):
            btn = ttk.Button(self.tag_grid, text=tag, width=16,
                             command=lambda t=tag: self._copy(t, "标签「%s」" % t))
            btn.grid(row=i // 2, column=i % 2, sticky="ew", padx=3, pady=3)
        self.tag_grid.columnconfigure(0, weight=1)
        self.tag_grid.columnconfigure(1, weight=1)

    def copy_all_tags(self):
        tags = self._current_six_tags()
        if tags:
            self._copy(",".join(tags), "全部 6 个推荐标签")

    def _on_close(self):
        try:
            self.drop.close()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    AlbumWorkbenchApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
