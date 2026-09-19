#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import base64
import ftplib
import ipaddress
import json
import posixpath
import queue
import socket
import stat
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from collector_core import (
    CancelledError,
    build_fan_hosts,
    CollectorError,
    InsufficientSpaceError,
    MASTER_TYPES,
    NETWORK_PRESETS,
    NoMatchingFilesError,
    VIBRATION_TYPES,
    VIBRATION_TYPE_LABELS,
    SmbConnection,
    SystemAwake,
    classify_master_file,
    compact_master_fan_input,
    copy_ftp_atomic,
    copy_local_atomic,
    copy_sftp_atomic,
    human_size,
    iter_dates,
    load_json,
    master_file_selected,
    master_file_matches_day,
    master_output_directory,
    normalize_keywords,
    parse_date_value,
    parse_fan_spec,
    parse_master_targets,
    fan_from_master_ip,
    pitch_file_matches,
    pitch_output_directory,
    partial_resume_offset,
    render_remote_directory,
    require_private_ipv4,
    require_transfer_space,
    save_json_atomic,
    safe_windows_relative_segments,
    transient_file_fan,
    transient_file_info,
    transient_output_directory,
    vibration_output_directory,
    vibration_remote_directory,
    validate_network_address,
)


APP_NAME = "风机文件拷取工具"
APP_DATA = Path(os.environ.get("APPDATA", Path.home())) / "WindFileCollector"
SETTINGS_FILE = APP_DATA / "settings.json"
KNOWN_HOSTS_FILE = APP_DATA / "known_hosts"
DEFAULT_DESTINATION = Path.home() / "Desktop" / "WindFiles"
MASTER_FTP_LOG_ROOTS = ("Log", "LOG", "log")
MASTER_OPERATION_ROOTS = ("operation", "Operation", "OPERATION")
MASTER_AUTO_WORKERS = 4
SFTP_AUTO_WORKERS = 3
MASTER_SFTP_LOG_ROOTS = (
    "/app/gw/ftp/log",
    "/app/gw/ftp/Log",
    "/app/gw/ftp/LOG",
    "/app/gw/ftp/operation",
    "/operation",
)

DEFAULTS = {
    "transient_destination": str(DEFAULT_DESTINATION),
    "master_destination": str(DEFAULT_DESTINATION),
    "transient_host": "192.168.149.222",
    "transient_port": "60022",
    "transient_username": "root",
    "transient_password": "",
    "transient_template": "/opt/goldwind/drbd_dataprocess/LocalData/HistoryFile/{year}/0/RealtimeData/{date}",
    "transient_local_root": "",
    "transient_fans": "1",
    "transient_layout": "按IP/日期",
    "master_prefix": "192.168.151",
    "master_ip_range": "192.168.151.1",
    "master_remote_path": "/app/gw/ftp/log",
    "master_local_root": "",
    "master_log_path": r"FTP\Log",
    "master_username": "",
    "master_password": "",
    "master_fans": "1",
    "master_keywords": "",
    "master_whole_folder": False,
    "pitch_host_prefix": "192.168.180",
    "pitch_port": "22",
    "pitch_username": "root",
    "pitch_password": "",
    "pitch_remote_path": "/app/data/traceLog/",
    "pitch_fans": "1",
    "pitch_destination": str(DEFAULT_DESTINATION),
    "vibration_host": "192.168.160.100",
    "vibration_port": "22",
    "vibration_username": "root",
    "vibration_password": "",
    "vibration_remote_base": "/data/GwTempData/CMSDATA/",
    "vibration_local_root": "",
    "vibration_site": "650227",
    "vibration_fans": "1",
    "vibration_destination": str(DEFAULT_DESTINATION),
    "vibration_types": list(VIBRATION_TYPES),
    "network_mode": "auto",
    "network_adapter_index": None,
    "network_addresses": NETWORK_PRESETS["风机IP"]
    + NETWORK_PRESETS["变桨IP"]
    + NETWORK_PRESETS["净空IP"]
    + NETWORK_PRESETS["雷达IP"]
    + NETWORK_PRESETS["变流IP"]
    + NETWORK_PRESETS["交换机IP"]
    + NETWORK_PRESETS["消防IP"]
    + NETWORK_PRESETS["振动IP"],
}


class ToolTip:
    def __init__(self, widget: tk.Widget, text: str):
        self.widget = widget
        self.text = text
        self.window: tk.Toplevel | None = None
        widget.bind("<Enter>", self.show, add=True)
        widget.bind("<Leave>", self.hide, add=True)

    def show(self, _event=None):
        if self.window or not self.text:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{x}+{y}")
        ttk.Label(
            self.window,
            text=self.text,
            padding=(8, 5),
            relief="solid",
            borderwidth=1,
        ).pack()

    def hide(self, _event=None):
        if self.window:
            self.window.destroy()
            self.window = None


class WindCollectorApp(tk.Tk):
    _known_hosts_lock = threading.Lock()

    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("980x760")
        self.minsize(880, 680)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.settings = load_json(SETTINGS_FILE, DEFAULTS)
        self.events: queue.Queue[tuple] = queue.Queue(maxsize=3000)
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.current_log: Path | None = None

        self._create_variables()
        self._configure_style()
        self._build_ui()
        self._update_transient_preview()
        self.start_date.trace_add("write", self._update_transient_preview)
        self.end_date.trace_add("write", self._update_transient_preview)
        self.transient_template.trace_add("write", self._update_transient_preview)
        for variable in (
            self.start_date,
            self.end_date,
            self.vibration_remote_base,
            self.vibration_site,
            self.vibration_fans,
        ):
            variable.trace_add("write", self._update_vibration_preview)
        self._update_vibration_preview()
        self.after(100, self._drain_events)

    def _create_variables(self):
        today = date.today().strftime("%Y-%m-%d")
        value = self.settings

        previous_destination = value.get("destination", str(DEFAULT_DESTINATION))
        self.transient_destination = tk.StringVar(
            value=value.get("transient_destination", previous_destination)
        )
        self.master_destination = tk.StringVar(
            value=value.get("master_destination", previous_destination)
        )
        self.start_date = tk.StringVar(value=today)
        self.end_date = tk.StringVar(value=today)
        self.overwrite = tk.BooleanVar(value=False)

        self.transient_host = tk.StringVar(value=value["transient_host"])
        self.transient_port = tk.StringVar(value=value["transient_port"])
        self.transient_username = tk.StringVar(value=value["transient_username"])
        self.transient_password = tk.StringVar(value=value.get("transient_password", DEFAULTS["transient_password"]))
        self.transient_template = tk.StringVar(
            value=value.get("transient_template", DEFAULTS["transient_template"])
        )
        self.transient_local_root = tk.StringVar(
            value=value.get("transient_local_root", DEFAULTS["transient_local_root"])
        )
        self.transient_fans = tk.StringVar(value=value["transient_fans"])
        self.transient_layout = tk.StringVar(
            value=value.get("transient_layout", DEFAULTS["transient_layout"])
        )
        self.transient_preview = tk.StringVar()

        self.master_prefix = tk.StringVar(value=value["master_prefix"])
        saved_master_range = value.get(
            "master_ip_range",
            f"{value.get('master_prefix', DEFAULTS['master_prefix'])}.{value.get('master_fans', DEFAULTS['master_fans'])}",
        )
        self.master_ip_range = tk.StringVar(
            value=compact_master_fan_input(saved_master_range, value["master_prefix"])
        )
        self.master_remote_path = tk.StringVar(
            value=value.get("master_remote_path", DEFAULTS["master_remote_path"])
        )
        self.master_local_root = tk.StringVar(
            value=value.get("master_local_root", DEFAULTS["master_local_root"])
        )
        self.master_log_path = tk.StringVar(
            value=value.get("master_log_path", DEFAULTS["master_log_path"])
        )
        self.master_username = tk.StringVar(value=value["master_username"])
        self.master_password = tk.StringVar(value=value.get("master_password", DEFAULTS["master_password"]))
        self.master_fans = tk.StringVar(value=value["master_fans"])
        self.master_keywords = tk.StringVar(value=value["master_keywords"])
        self.master_types = {kind: tk.BooleanVar(value=True) for kind in MASTER_TYPES}
        self.master_whole_folder = tk.BooleanVar(
            value=bool(value.get("master_whole_folder", DEFAULTS.get("master_whole_folder", False)))
        )

        self.pitch_host_prefix = tk.StringVar(value=value.get("pitch_host_prefix", DEFAULTS["pitch_host_prefix"]))
        self.pitch_port = tk.StringVar(value=value.get("pitch_port", DEFAULTS["pitch_port"]))
        self.pitch_username = tk.StringVar(value=value.get("pitch_username", DEFAULTS["pitch_username"]))
        self.pitch_password = tk.StringVar(value=value.get("pitch_password", DEFAULTS["pitch_password"]))
        self.pitch_remote_path = tk.StringVar(value=value.get("pitch_remote_path", DEFAULTS["pitch_remote_path"]))
        self.pitch_fans = tk.StringVar(value=value.get("pitch_fans", DEFAULTS["pitch_fans"]))
        self.pitch_destination = tk.StringVar(value=value.get("pitch_destination", previous_destination))

        self.vibration_host = tk.StringVar(value=value.get("vibration_host", DEFAULTS["vibration_host"]))
        self.vibration_port = tk.StringVar(value=value.get("vibration_port", DEFAULTS["vibration_port"]))
        self.vibration_username = tk.StringVar(value=value.get("vibration_username", DEFAULTS["vibration_username"]))
        self.vibration_password = tk.StringVar(value=value.get("vibration_password", DEFAULTS["vibration_password"]))
        self.vibration_remote_base = tk.StringVar(value=value.get("vibration_remote_base", DEFAULTS["vibration_remote_base"]))
        self.vibration_local_root = tk.StringVar(value=value.get("vibration_local_root", DEFAULTS["vibration_local_root"]))
        self.vibration_site = tk.StringVar(value=value.get("vibration_site", DEFAULTS["vibration_site"]))
        self.vibration_fans = tk.StringVar(value=value.get("vibration_fans", DEFAULTS["vibration_fans"]))
        self.vibration_destination = tk.StringVar(value=value.get("vibration_destination", previous_destination))
        self.vibration_preview = tk.StringVar()
        saved_vibration_types = value.get("vibration_types", DEFAULTS["vibration_types"])
        if not isinstance(saved_vibration_types, list):
            saved_vibration_types = list(VIBRATION_TYPES)
        self.vibration_types = {
            kind: tk.BooleanVar(value=kind in saved_vibration_types) for kind in VIBRATION_TYPES
        }

        self.show_passwords = tk.BooleanVar(value=False)
        self.status_text = tk.StringVar(value="就绪")
        self.counter_text = tk.StringVar(value="0 个文件")
        self.transfer_text = tk.StringVar(value="等待统计")

    def _configure_style(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Section.TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_ui(self):
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="网络适配器", command=self._open_network_settings).grid(
            row=0, column=1, padx=(0, 8)
        )
        ttk.Button(header, text="高级设置", command=self._open_advanced_settings).grid(
            row=0, column=2, padx=(0, 8)
        )
        self.save_settings_button = ttk.Button(
            header,
            text="保存设置",
            style="Primary.TButton",
            command=self._save_settings_now,
        )
        self.save_settings_button.grid(row=0, column=3, padx=(0, 12))
        ttk.Checkbutton(
            header,
            text="显示密码",
            variable=self.show_passwords,
            command=self._toggle_passwords,
        ).grid(row=0, column=4, sticky="e")

        common = ttk.LabelFrame(root, text="日期与文件处理", padding=10, style="Section.TLabelframe")
        common.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(common, text="开始日期").grid(row=0, column=0, sticky="w")
        ttk.Entry(common, width=13, textvariable=self.start_date).grid(row=0, column=1, padx=(6, 18))
        ttk.Label(common, text="结束日期").grid(row=0, column=2, sticky="w")
        ttk.Entry(common, width=13, textvariable=self.end_date).grid(row=0, column=3, padx=(6, 18))
        ttk.Checkbutton(common, text="覆盖已有文件", variable=self.overwrite).grid(row=0, column=4)

        notebook = ttk.Notebook(root)
        notebook.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        self._build_transient_tab(notebook)
        self._build_master_tab(notebook)
        self._build_pitch_tab(notebook)
        self._build_vibration_tab(notebook)

        activity = ttk.LabelFrame(root, text="任务记录", padding=10, style="Section.TLabelframe")
        activity.grid(row=3, column=0, sticky="nsew")
        activity.columnconfigure(0, weight=1)
        activity.rowconfigure(0, weight=1)
        self.log_box = scrolledtext.ScrolledText(
            activity,
            height=12,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
        )
        self.log_box.grid(row=0, column=0, columnspan=4, sticky="nsew")
        self.progress = ttk.Progressbar(activity, mode="indeterminate")
        self.progress.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(activity, textvariable=self.transfer_text, width=24, anchor="e").grid(
            row=1, column=1, padx=(10, 0), pady=(10, 0)
        )
        ttk.Label(activity, textvariable=self.counter_text, width=20, anchor="e").grid(
            row=1, column=2, padx=(10, 0), pady=(10, 0)
        )
        self.cancel_button = ttk.Button(activity, text="■ 停止", command=self._cancel, state="disabled")
        self.cancel_button.grid(row=1, column=3, padx=(10, 0), pady=(10, 0))
        ttk.Label(activity, textvariable=self.status_text, anchor="w").grid(
            row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0)
        )

    def _build_transient_tab(self, notebook: ttk.Notebook):
        frame = ttk.Frame(notebook, padding=14)
        notebook.add(frame, text="瞬态数据")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        ttk.Label(frame, text="服务器").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.transient_host).grid(row=0, column=1, sticky="ew", padx=(8, 18))
        ttk.Label(frame, text="端口").grid(row=0, column=2, sticky="w", pady=5)
        ttk.Entry(frame, width=10, textvariable=self.transient_port).grid(row=0, column=3, sticky="w", padx=(8, 0))

        ttk.Label(frame, text="账号").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.transient_username).grid(row=1, column=1, sticky="ew", padx=(8, 18))
        ttk.Label(frame, text="密码").grid(row=1, column=2, sticky="w", pady=5)
        self.transient_password_entry = ttk.Entry(frame, textvariable=self.transient_password, show="●")
        self.transient_password_entry.grid(row=1, column=3, sticky="ew", padx=(8, 0))

        ttk.Label(frame, text="保存到").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.transient_destination).grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=(8, 6)
        )
        ttk.Button(
            frame,
            text="…",
            width=4,
            command=lambda: self._browse_destination(self.transient_destination),
        ).grid(row=2, column=3, sticky="e")

        ttk.Label(frame, text="服务器远程目录（自动）").grid(row=3, column=0, sticky="w", pady=5)
        preview_entry = ttk.Entry(frame, textvariable=self.transient_preview, state="readonly")
        preview_entry.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(8, 6))
        ToolTip(preview_entry, "根据所选日期自动生成；需要修改目录规则时进入“高级设置”。")
        ttk.Button(frame, text="修改规则", command=self._open_advanced_settings).grid(
            row=3, column=3, sticky="e"
        )

        ttk.Label(frame, text="网络位置来源").grid(row=4, column=0, sticky="w", pady=5)
        transient_local_entry = ttk.Entry(frame, textvariable=self.transient_local_root)
        transient_local_entry.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(8, 6))
        ToolTip(transient_local_entry, "可空。资源管理器能打开的瞬态服务器目录、映射盘或共享目录；支持 {date}、{year}、{ip}。")
        ttk.Button(
            frame,
            text="…",
            width=4,
            command=lambda: self._browse_destination(self.transient_local_root),
        ).grid(row=4, column=3, sticky="e")

        ttk.Label(frame, text="风机号（支持多台）").grid(row=5, column=0, sticky="w", pady=5)
        fan_entry = ttk.Entry(frame, textvariable=self.transient_fans)
        fan_entry.grid(row=5, column=1, sticky="ew", padx=(8, 18))
        ToolTip(fan_entry, "可输入 1-20、1,3,8,13 或单个风机号。")
        ttk.Label(frame, text="整理方式").grid(row=5, column=2, sticky="w", pady=5)
        layout_box = ttk.Combobox(
            frame,
            state="readonly",
            textvariable=self.transient_layout,
            values=("按IP/日期", "按IP/风机号/日期", "保留服务器目录"),
        )
        layout_box.grid(row=5, column=3, sticky="ew", padx=(8, 0))
        ttk.Label(
            frame,
            text="风机号示例：单台 7    连续 1-20    不连续 1,3,8,13",
            foreground="#0b63ce",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=6, column=1, columnspan=3, sticky="w", padx=(8, 0), pady=(2, 8))

        controls = ttk.Frame(frame)
        controls.grid(row=7, column=0, columnspan=4, sticky="e", pady=(12, 0))
        self.transient_test_button = ttk.Button(
            controls, text="连接测试", command=lambda: self._start_transient(test_only=True)
        )
        self.transient_test_button.pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="端口诊断", command=self._diagnose_transient).pack(
            side="left", padx=(0, 8)
        )
        self.transient_start_button = ttk.Button(
            controls,
            text="▶ 开始拷取",
            style="Primary.TButton",
            command=lambda: self._start_transient(test_only=False),
        )
        self.transient_start_button.pack(side="left")

    def _build_master_tab(self, notebook: ttk.Notebook):
        frame = ttk.Frame(notebook, padding=14)
        notebook.add(frame, text="主控故障")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        ttk.Label(frame, text="IP 前缀（不含风机号）").grid(row=0, column=0, sticky="w", pady=5)
        prefix_entry = ttk.Entry(frame, textvariable=self.master_prefix)
        prefix_entry.grid(
            row=0, column=1, columnspan=4, sticky="ew", padx=(8, 0)
        )
        ToolTip(prefix_entry, "只填前三段，例如 192.168.151；风机号在下一行填写。")

        ttk.Label(frame, text="风机号（支持多台）").grid(row=1, column=0, sticky="w", pady=5)
        fan_entry = ttk.Entry(frame, textvariable=self.master_ip_range)
        fan_entry.grid(row=1, column=1, columnspan=3, sticky="ew", padx=(8, 6))
        ToolTip(fan_entry, "输入方式与瞬态相同：单台 7、连续 1-20、不连续 1,3,8,13。")
        ttk.Button(
            frame,
            text="全场 1-20",
            command=lambda: self.master_ip_range.set("1-20"),
        ).grid(row=1, column=4)
        ttk.Label(
            frame,
            text="风机号示例：单台 7    连续 1-20    不连续 1,3,8,13",
            foreground="#0b63ce",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=2, column=1, columnspan=4, sticky="w", padx=(8, 0), pady=(0, 8))

        ttk.Label(frame, text="账号").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.master_username).grid(row=3, column=1, sticky="ew", padx=(8, 18))
        ttk.Label(frame, text="密码").grid(row=3, column=2, sticky="w", pady=5)
        self.master_password_entry = ttk.Entry(frame, textvariable=self.master_password, show="●")
        self.master_password_entry.grid(row=3, column=3, columnspan=2, sticky="ew", padx=(8, 0))

        ttk.Label(frame, text="文件类型").grid(row=4, column=0, sticky="w", pady=5)
        types = ttk.Frame(frame)
        types.grid(row=4, column=1, columnspan=4, sticky="w", padx=(8, 0))
        for kind in MASTER_TYPES:
            ttk.Checkbutton(types, text=kind, variable=self.master_types[kind]).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(
            types,
            text="全选（整日期文件夹）",
            variable=self.master_whole_folder,
        ).pack(side="left", padx=(10, 0))

        ttk.Label(frame, text="文件名关键字").grid(row=5, column=0, sticky="w", pady=5)
        keyword_entry = ttk.Entry(frame, textvariable=self.master_keywords)
        keyword_entry.grid(row=5, column=1, columnspan=4, sticky="ew", padx=(8, 0))
        ToolTip(keyword_entry, "可空。多个关键字用逗号分隔，例如 VibrWarn,BladeLoad。")

        ttk.Label(frame, text="主控 BOF 路径").grid(row=6, column=0, sticky="w", pady=5)
        remote_entry = ttk.Entry(frame, textvariable=self.master_remote_path)
        remote_entry.grid(row=6, column=1, columnspan=4, sticky="ew", padx=(8, 0))
        ToolTip(remote_entry, "主控 BOF 远程路径，现场默认 /app/gw/ftp/log。")

        ttk.Label(frame, text="已有共享文件夹（可选）").grid(row=7, column=0, sticky="w", pady=5)
        local_entry = ttk.Entry(frame, textvariable=self.master_local_root)
        local_entry.grid(row=7, column=1, columnspan=3, sticky="ew", padx=(8, 6))
        ToolTip(local_entry, "仅当资源管理器已经能直接打开服务器文件夹或映射盘时填写；支持 {ip}、{fan}、{fan3}。留空自动使用主控 FTP。")
        ttk.Button(
            frame,
            text="…",
            width=4,
            command=lambda: self._browse_destination(self.master_local_root),
        ).grid(row=7, column=4, sticky="e")

        ttk.Label(
            frame,
            text="程序自动使用可访问的来源；某台失败会记录原因并继续下一台。",
            foreground="#0b63ce",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=8, column=1, columnspan=4, sticky="w", padx=(8, 0), pady=(2, 8))

        ttk.Label(frame, text="保存到").grid(row=9, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.master_destination).grid(
            row=9, column=1, columnspan=3, sticky="ew", padx=(8, 6)
        )
        ttk.Button(
            frame,
            text="…",
            width=4,
            command=lambda: self._browse_destination(self.master_destination),
        ).grid(row=9, column=4, sticky="e")

        controls = ttk.Frame(frame)
        controls.grid(row=10, column=0, columnspan=5, sticky="e", pady=(12, 0))
        self.master_test_button = ttk.Button(
            controls, text="测试主控 BOF（第一台）", command=lambda: self._start_master(test_only=True)
        )
        self.master_test_button.pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="端口诊断", command=self._diagnose_master).pack(
            side="left", padx=(0, 8)
        )
        self.master_start_button = ttk.Button(
            controls,
            text="▶ 开始拷取",
            style="Primary.TButton",
            command=lambda: self._start_master(test_only=False),
        )
        self.master_start_button.pack(side="left")

    def _build_pitch_tab(self, notebook: ttk.Notebook):
        frame = ttk.Frame(notebook, padding=14)
        notebook.add(frame, text="变桨日志")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        ttk.Label(frame, text="IP 前缀（不含风机号）").grid(row=0, column=0, sticky="w", pady=5)
        pitch_prefix_entry = ttk.Entry(frame, textvariable=self.pitch_host_prefix)
        pitch_prefix_entry.grid(
            row=0, column=1, sticky="ew", padx=(8, 18)
        )
        ToolTip(pitch_prefix_entry, "只填前三段，例如 192.168.180；风机号在下方填写。")
        ttk.Label(frame, text="端口").grid(row=0, column=2, sticky="w", pady=5)
        ttk.Entry(frame, width=10, textvariable=self.pitch_port).grid(
            row=0, column=3, sticky="w", padx=(8, 0)
        )

        ttk.Label(frame, text="账号").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.pitch_username).grid(
            row=1, column=1, sticky="ew", padx=(8, 18)
        )
        ttk.Label(frame, text="密码").grid(row=1, column=2, sticky="w", pady=5)
        self.pitch_password_entry = ttk.Entry(frame, textvariable=self.pitch_password, show="●")
        self.pitch_password_entry.grid(row=1, column=3, sticky="ew", padx=(8, 0))

        ttk.Label(frame, text="远程目录").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.pitch_remote_path).grid(
            row=2, column=1, columnspan=3, sticky="ew", padx=(8, 0)
        )
        ToolTip(
            frame,
            "变桨日志通常位于 /app/data/traceLog/；按文件名 PTYYYYMMDD_ 筛选日期。",
        )

        ttk.Label(frame, text="风机号（支持多台）").grid(row=3, column=0, sticky="w", pady=5)
        pitch_fan_entry = ttk.Entry(frame, textvariable=self.pitch_fans)
        pitch_fan_entry.grid(
            row=3, column=1, sticky="ew", padx=(8, 18)
        )
        ToolTip(pitch_fan_entry, "可输入 1-20、1,3,8 或单个风机号；IP 为 192.168.180.风机号。")
        ttk.Label(frame, text="筛选规则").grid(row=3, column=2, sticky="w", pady=5)
        ttk.Label(frame, text="文件名以 PT + 日期开头").grid(
            row=3, column=3, sticky="w", padx=(8, 0)
        )
        ttk.Label(
            frame,
            text="风机号示例：单台 7    连续 1-20    不连续 1,3,8,13",
            foreground="#0b63ce",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=4, column=1, columnspan=3, sticky="w", padx=(8, 0), pady=(2, 8))

        ttk.Label(frame, text="保存到").grid(row=5, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.pitch_destination).grid(
            row=5, column=1, columnspan=2, sticky="ew", padx=(8, 6)
        )
        ttk.Button(
            frame, text="…", width=4, command=lambda: self._browse_destination(self.pitch_destination)
        ).grid(row=5, column=3, sticky="e")

        controls = ttk.Frame(frame)
        controls.grid(row=6, column=0, columnspan=4, sticky="e", pady=(12, 0))
        self.pitch_test_button = ttk.Button(
            controls, text="连接测试", command=lambda: self._start_pitch(test_only=True)
        )
        self.pitch_test_button.pack(side="left", padx=(0, 8))
        self.pitch_start_button = ttk.Button(
            controls,
            text="▶ 开始拷取",
            style="Primary.TButton",
            command=lambda: self._start_pitch(test_only=False),
        )
        self.pitch_start_button.pack(side="left")

    def _build_vibration_tab(self, notebook: ttk.Notebook):
        frame = ttk.Frame(notebook, padding=14)
        notebook.add(frame, text="震动数据")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        ttk.Label(frame, text="服务器").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.vibration_host).grid(
            row=0, column=1, sticky="ew", padx=(8, 18)
        )
        ttk.Label(frame, text="端口").grid(row=0, column=2, sticky="w", pady=5)
        ttk.Entry(frame, width=10, textvariable=self.vibration_port).grid(
            row=0, column=3, sticky="w", padx=(8, 0)
        )

        ttk.Label(frame, text="账号").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.vibration_username).grid(
            row=1, column=1, sticky="ew", padx=(8, 18)
        )
        ttk.Label(frame, text="密码").grid(row=1, column=2, sticky="w", pady=5)
        self.vibration_password_entry = ttk.Entry(
            frame, textvariable=self.vibration_password, show="●"
        )
        self.vibration_password_entry.grid(row=1, column=3, sticky="ew", padx=(8, 0))

        ttk.Label(frame, text="CMSDATA 根目录").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.vibration_remote_base).grid(
            row=2, column=1, columnspan=3, sticky="ew", padx=(8, 0)
        )
        ToolTip(
            frame,
            "BMSDATA、TMSDATA、WAVEDATA 是此目录下的三个同级目录。",
        )

        ttk.Label(frame, text="网络位置来源").grid(row=3, column=0, sticky="w", pady=5)
        vibration_local_entry = ttk.Entry(frame, textvariable=self.vibration_local_root)
        vibration_local_entry.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(8, 6))
        ToolTip(vibration_local_entry, "可空。资源管理器能打开的 CMSDATA 根目录、映射盘或共享目录；填后优先按本地路径查找。")
        ttk.Button(
            frame,
            text="…",
            width=4,
            command=lambda: self._browse_destination(self.vibration_local_root),
        ).grid(row=3, column=3, sticky="e")

        ttk.Label(frame, text="风场号").grid(row=4, column=0, sticky="w", pady=5)
        vibration_site_entry = ttk.Entry(frame, textvariable=self.vibration_site)
        vibration_site_entry.grid(
            row=4, column=1, sticky="ew", padx=(8, 18)
        )
        ToolTip(vibration_site_entry, "例如 650227；一号风机目录为 650227001。")
        ttk.Label(frame, text="风机号（支持多台）").grid(row=4, column=2, sticky="w", pady=5)
        vibration_fan_entry = ttk.Entry(frame, textvariable=self.vibration_fans)
        vibration_fan_entry.grid(
            row=4, column=3, sticky="ew", padx=(8, 0)
        )
        ToolTip(vibration_fan_entry, "可输入 1-20、1,3,8,13 或单个风机号。")
        ttk.Label(
            frame,
            text="风机号示例：单台 7    连续 1-20    不连续 1,3,8,13",
            foreground="#0b63ce",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=5, column=1, columnspan=3, sticky="w", padx=(8, 0), pady=(2, 8))

        ttk.Label(frame, text="数据类型").grid(row=6, column=0, sticky="w", pady=5)
        type_frame = ttk.Frame(frame)
        type_frame.grid(row=6, column=1, columnspan=3, sticky="w", padx=(8, 0))
        for kind in VIBRATION_TYPES:
            ttk.Checkbutton(
                type_frame,
                text=VIBRATION_TYPE_LABELS[kind],
                variable=self.vibration_types[kind],
            ).pack(side="left", padx=(0, 16))

        ttk.Label(frame, text="服务器路径预览").grid(row=7, column=0, sticky="nw", pady=5)
        preview = ttk.Label(
            frame,
            textvariable=self.vibration_preview,
            justify="left",
            anchor="w",
            wraplength=760,
        )
        preview.grid(row=7, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=5)
        ToolTip(preview, "三个路径分别对应三个同级目录；实际进入顺序是类型/风场/月/风机/日期。")

        ttk.Label(frame, text="保存到").grid(row=8, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=self.vibration_destination).grid(
            row=8, column=1, columnspan=2, sticky="ew", padx=(8, 6)
        )
        ttk.Button(
            frame,
            text="…",
            width=4,
            command=lambda: self._browse_destination(self.vibration_destination),
        ).grid(row=8, column=3, sticky="e")

        controls = ttk.Frame(frame)
        controls.grid(row=9, column=0, columnspan=4, sticky="e", pady=(12, 0))
        self.vibration_test_button = ttk.Button(
            controls, text="连接测试", command=lambda: self._start_vibration(test_only=True)
        )
        self.vibration_test_button.pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="端口诊断", command=self._diagnose_vibration).pack(
            side="left", padx=(0, 8)
        )
        self.vibration_start_button = ttk.Button(
            controls,
            text="▶ 开始拷取",
            style="Primary.TButton",
            command=lambda: self._start_vibration(test_only=False),
        )
        self.vibration_start_button.pack(side="left")

    def _toggle_passwords(self):
        mask = "" if self.show_passwords.get() else "●"
        self.transient_password_entry.configure(show=mask)
        self.master_password_entry.configure(show=mask)
        self.pitch_password_entry.configure(show=mask)
        self.vibration_password_entry.configure(show=mask)

    def _browse_destination(self, variable: tk.StringVar):
        selected = filedialog.askdirectory(
            title="选择保存目录",
            initialdir=variable.get() or str(DEFAULT_DESTINATION),
        )
        if selected:
            variable.set(selected)

    def _open_network_settings(self):
        existing = getattr(self, "_network_dialog", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            return

        dialog = tk.Toplevel(self)
        self._network_dialog = dialog
        dialog.title("网络适配器配置")
        dialog.geometry("980x700")
        dialog.minsize(820, 620)
        dialog.transient(self)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(4, weight=1)

        ttk.Label(dialog, text="选择网络适配器:").grid(
            row=0, column=0, sticky="ew", padx=18, pady=(14, 5)
        )
        adapter_line = ttk.Frame(dialog)
        adapter_line.grid(row=1, column=0, sticky="ew", padx=18)
        adapter_line.columnconfigure(0, weight=1)
        self.network_adapter_var = tk.StringVar()
        self.network_adapter_combo = ttk.Combobox(
            adapter_line, textvariable=self.network_adapter_var, state="readonly"
        )
        self.network_adapter_combo.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.network_adapter_combo.bind("<<ComboboxSelected>>", self._network_adapter_selected)
        ttk.Button(
            adapter_line, text="刷新适配器列表", command=self._network_refresh_adapters
        ).grid(row=0, column=1)

        self.network_status = tk.StringVar(value="")
        ttk.Label(dialog, textvariable=self.network_status, foreground="#555555").grid(
            row=2, column=0, sticky="w", padx=18, pady=(4, 8)
        )

        mode_frame = ttk.LabelFrame(dialog, text="配置模式", padding=(12, 8))
        mode_frame.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 8))
        self.network_mode_var = tk.StringVar(
            value=self.settings.get("network_mode", "auto")
        )
        ttk.Radiobutton(
            mode_frame,
            text="自动获取IP地址",
            variable=self.network_mode_var,
            value="auto",
            command=self._network_toggle_mode,
        ).pack(anchor="w")
        ttk.Radiobutton(
            mode_frame,
            text="手动配置IP地址",
            variable=self.network_mode_var,
            value="manual",
            command=self._network_toggle_mode,
        ).pack(anchor="w", pady=(4, 0))

        manual_frame = ttk.LabelFrame(
            dialog, text="手动配置（支持多个IP快捷填充）", padding=(12, 8)
        )
        manual_frame.grid(row=4, column=0, sticky="nsew", padx=18)
        manual_frame.columnconfigure(0, weight=1)
        manual_frame.rowconfigure(1, weight=1)
        self.network_manual_frame = manual_frame

        header_line = ttk.Frame(manual_frame)
        header_line.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        header_line.columnconfigure(0, weight=1)
        header_line.columnconfigure(1, weight=1)
        ttk.Label(header_line, text="IPv4 地址").grid(row=0, column=0, sticky="w")
        ttk.Label(header_line, text="子网掩码").grid(row=0, column=1, sticky="w", padx=(8, 0))

        rows_host = ttk.Frame(manual_frame)
        rows_host.grid(row=1, column=0, sticky="nsew")
        rows_host.columnconfigure(0, weight=1)
        rows_host.rowconfigure(0, weight=1)
        canvas = tk.Canvas(rows_host, highlightthickness=0, height=250)
        scrollbar = ttk.Scrollbar(rows_host, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.network_rows_canvas = canvas
        self.network_rows_host = ttk.Frame(canvas)
        self.network_rows_host.columnconfigure(0, weight=1)
        self.network_rows_window = canvas.create_window(
            (0, 0), window=self.network_rows_host, anchor="nw"
        )
        self.network_rows_host.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(self.network_rows_window, width=event.width),
        )

        add_line = ttk.Frame(manual_frame)
        add_line.grid(row=2, column=0, sticky="ew", pady=(7, 0))
        ttk.Button(add_line, text="添加IP配置", command=self._network_add_row).pack()

        raw_addresses = self.settings.get("network_addresses", DEFAULTS["network_addresses"])
        addresses = []
        for item in raw_addresses if isinstance(raw_addresses, list) else []:
            if isinstance(item, dict):
                ip_value, mask_value = item.get("ip", ""), item.get("mask", "")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                ip_value, mask_value = item[0], item[1]
            else:
                continue
            addresses.append((str(ip_value), str(mask_value)))
        self.network_rows = []
        for ip_value, mask_value in addresses:
            self._network_add_row(ip_value, mask_value, render=False)
        self._network_render_rows()

        preset_frame = ttk.Frame(dialog)
        preset_frame.grid(row=5, column=0, sticky="ew", padx=18, pady=(8, 0))
        for label in NETWORK_PRESETS:
            ttk.Button(
                preset_frame,
                text=label,
                command=lambda name=label: self._network_apply_preset(name),
            ).pack(side="left", padx=(0, 8))

        action_frame = ttk.Frame(dialog)
        action_frame.grid(row=6, column=0, sticky="e", padx=18, pady=(10, 14))
        ttk.Button(action_frame, text="读取当前配置", command=self._network_read_current).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(action_frame, text="取消", command=dialog.destroy).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(
            action_frame, text="应用到系统", style="Primary.TButton", command=self._network_apply
        ).pack(side="left")
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self._network_toggle_mode()
        self._network_refresh_adapters()

    def _network_add_row(self, ip_value="", mask_value="255.255.255.0", render=True):
        self.network_rows.append(
            {"ip": tk.StringVar(value=ip_value), "mask": tk.StringVar(value=mask_value)}
        )
        if render and hasattr(self, "network_rows_host"):
            self._network_render_rows()

    def _network_remove_row(self, index: int):
        if 0 <= index < len(self.network_rows):
            self.network_rows.pop(index)
            self._network_render_rows()

    def _network_render_rows(self):
        for child in self.network_rows_host.winfo_children():
            child.destroy()
        self.network_row_widgets = []
        if not self.network_rows:
            ttk.Label(self.network_rows_host, text="暂无手动 IP，点击“添加IP配置”或选择快捷预设").grid(
                row=0, column=0, sticky="w", pady=8
            )
        for index, row in enumerate(self.network_rows):
            line = ttk.Frame(self.network_rows_host)
            line.grid(row=index, column=0, sticky="ew", pady=2)
            line.columnconfigure(0, weight=1)
            line.columnconfigure(1, weight=1)
            ip_entry = ttk.Entry(line, textvariable=row["ip"])
            mask_entry = ttk.Entry(line, textvariable=row["mask"])
            ip_entry.grid(row=0, column=0, sticky="ew")
            mask_entry.grid(row=0, column=1, sticky="ew", padx=(8, 8))
            delete_button = ttk.Button(
                line, text="×", width=3, command=lambda item=index: self._network_remove_row(item)
            )
            delete_button.grid(row=0, column=2)
            self.network_row_widgets.append((ip_entry, mask_entry, delete_button))
        self._network_toggle_mode()
        self.network_rows_host.update_idletasks()
        self.network_rows_canvas.configure(scrollregion=self.network_rows_canvas.bbox("all"))

    def _network_toggle_mode(self):
        manual = getattr(self, "network_mode_var", tk.StringVar(value="auto")).get() == "manual"
        state = "normal" if manual else "disabled"
        for widgets in getattr(self, "network_row_widgets", []):
            for widget in widgets:
                widget.configure(state=state)

    def _network_apply_preset(self, name: str):
        self.network_rows = []
        for ip_value, mask_value in NETWORK_PRESETS[name]:
            self._network_add_row(ip_value, mask_value, render=False)
        self.network_mode_var.set("manual")
        self._network_render_rows()

    def _network_run_powershell_json(self, script: str):
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=creationflags,
        )
        if result.returncode != 0:
            raise CollectorError(result.stderr.strip() or "读取 Windows 网络适配器失败。")
        output = result.stdout.strip().lstrip("\ufeff")
        return json.loads(output) if output else []

    def _network_refresh_adapters(self):
        try:
            data = self._network_run_powershell_json(
                "Get-NetAdapter | Sort-Object ifIndex | "
                "Select-Object ifIndex,Name,InterfaceDescription,Status,MacAddress | "
                "ConvertTo-Json -Compress"
            )
            if isinstance(data, dict):
                data = [data]
            self.network_adapter_map = {}
            labels = []
            for item in data:
                try:
                    index = int(item["ifIndex"])
                except (KeyError, TypeError, ValueError):
                    continue
                label = (
                    f"{item.get('Name', '')} | {item.get('Status', '')} | "
                    f"{item.get('InterfaceDescription', '')} (#{index})"
                )
                self.network_adapter_map[label] = index
                labels.append(label)
            self.network_adapter_combo.configure(values=labels)
            preferred = self.settings.get("network_adapter_index")
            selected = next((label for label, index in self.network_adapter_map.items() if index == preferred), None)
            if selected is None and labels:
                selected = labels[0]
            self.network_adapter_var.set(selected or "")
            self.network_status.set(f"已发现 {len(labels)} 个网络适配器；应用配置需要管理员权限。")
        except (OSError, subprocess.SubprocessError, ValueError, CollectorError, json.JSONDecodeError) as exc:
            self.network_adapter_map = {}
            self.network_adapter_combo.configure(values=[])
            self.network_adapter_var.set("")
            self.network_status.set(f"读取失败：{exc}")

    def _network_selected_index(self) -> int:
        label = self.network_adapter_var.get()
        try:
            return int(self.network_adapter_map[label])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("请先选择网络适配器。") from exc

    def _network_adapter_selected(self, _event=None):
        if self.network_adapter_var.get():
            self.network_status.set(
                f"已选择：{self.network_adapter_var.get()}；需要读取现有地址时点击“读取当前配置”。"
            )

    def _network_read_current(self):
        try:
            index = self._network_selected_index()
            data = self._network_run_powershell_json(
                f"Get-NetIPAddress -InterfaceIndex {index} -AddressFamily IPv4 "
                "-ErrorAction SilentlyContinue | Select-Object IPAddress,PrefixLength | "
                "ConvertTo-Json -Compress"
            )
            if isinstance(data, dict):
                data = [data]
            rows = []
            for item in data:
                try:
                    ip_value = str(item["IPAddress"])
                    prefix = int(item["PrefixLength"])
                    mask = str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)
                    validate_network_address(ip_value, mask)
                except (KeyError, TypeError, ValueError):
                    continue
                rows.append((ip_value, mask))
            self.network_rows = []
            for ip_value, mask_value in rows:
                self._network_add_row(ip_value, mask_value, render=False)
            self.network_mode_var.set("manual" if rows else "auto")
            self._network_render_rows()
            self.network_status.set(f"已读取 {len(rows)} 个内网 IPv4 地址。")
        except (OSError, subprocess.SubprocessError, ValueError, CollectorError, json.JSONDecodeError) as exc:
            messagebox.showerror("读取失败", str(exc), parent=self._network_dialog)

    def _network_values(self):
        values = []
        seen = set()
        for row in self.network_rows:
            ip_value, mask_value = row["ip"].get().strip(), row["mask"].get().strip()
            if not ip_value and not mask_value:
                continue
            if not ip_value or not mask_value:
                raise ValueError("每个 IP 配置行都必须同时填写 IPv4 地址和子网掩码。")
            address, mask, prefix = validate_network_address(ip_value, mask_value)
            if address in seen:
                raise ValueError(f"存在重复的 IPv4 地址：{address}")
            seen.add(address)
            values.append((address, mask, prefix))
        return values

    def _network_apply(self):
        try:
            index = self._network_selected_index()
            mode = self.network_mode_var.get()
            values = [] if mode == "auto" else self._network_values()
        except ValueError as exc:
            messagebox.showerror("网络配置有误", str(exc), parent=self._network_dialog)
            return

        if mode == "manual" and not values:
            confirm = messagebox.askyesno(
                "确认清空地址",
                "手动模式没有任何 IPv4 地址，应用后该适配器将不再配置 IPv4。继续吗？",
                parent=self._network_dialog,
            )
            if not confirm:
                return
        if mode == "auto":
            script = (
                f"Set-NetIPInterface -InterfaceIndex {index} -AddressFamily IPv4 -Dhcp Enabled -ErrorAction Stop; "
                f"Set-DnsClientServerAddress -InterfaceIndex {index} -ResetServerAddresses -ErrorAction SilentlyContinue"
            )
        else:
            lines = [
                f"Set-NetIPInterface -InterfaceIndex {index} -AddressFamily IPv4 -Dhcp Disabled -ErrorAction Stop",
                f"Get-NetIPAddress -InterfaceIndex {index} -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
                "Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue",
            ]
            lines.extend(
                f"New-NetIPAddress -InterfaceIndex {index} -IPAddress {ip_value} -PrefixLength {prefix} -ErrorAction Stop"
                for ip_value, _mask, prefix in values
            )
            script = "; ".join(lines)
        encoded = base64.b64encode(
            ("$ErrorActionPreference='Stop'; try { " + script + " } catch { Write-Error $_; exit 1 }").encode(
                "utf-16le"
            )
        ).decode("ascii")
        wrapper = (
            "$p=Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru "
            f"-ArgumentList @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-EncodedCommand','{encoded}'); "
            "exit $p.ExitCode"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", wrapper],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                raise CollectorError(result.stderr.strip() or "管理员网络配置未成功应用。")
        except (OSError, subprocess.SubprocessError, CollectorError) as exc:
            messagebox.showerror("应用失败", str(exc), parent=self._network_dialog)
            return

        self.settings["network_mode"] = mode
        self.settings["network_adapter_index"] = index
        self.settings["network_addresses"] = [(ip_value, mask) for ip_value, mask, _prefix in values]
        self._save_settings()
        self.network_status.set("网络配置已应用并保存。")
        messagebox.showinfo("应用成功", "网络适配器配置已应用。", parent=self._network_dialog)

    def _update_transient_preview(self, *_args):
        try:
            start = parse_date_value(self.start_date.get())
            end = parse_date_value(self.end_date.get())
            first = render_remote_directory(self.transient_template.get(), start)
            last = render_remote_directory(self.transient_template.get(), end)
            self.transient_preview.set(first if first == last else f"{first}  ->  {last}")
        except ValueError:
            self.transient_preview.set("请先输入有效的开始和结束日期")

    def _update_vibration_preview(self, *_args):
        try:
            start = parse_date_value(self.start_date.get())
            end = parse_date_value(self.end_date.get())
            fans = parse_fan_spec(self.vibration_fans.get())
            site = self.vibration_site.get().strip()
            if not site.isdigit():
                raise ValueError
            first_fan = fans[0]
            paths = [
                vibration_remote_directory(
                    self.vibration_remote_base.get(), kind, site, start, first_fan
                )
                for kind in VIBRATION_TYPES
            ]
            suffix = ""
            if start != end:
                suffix = f"\n日期范围：{start:%Y-%m-%d} -> {end:%Y-%m-%d}"
            self.vibration_preview.set("\n".join(paths) + suffix)
        except (ValueError, IndexError):
            self.vibration_preview.set("请输入有效日期、风场号和风机号")

    def _open_advanced_settings(self):
        dialog = tk.Toplevel(self)
        dialog.title("高级设置")
        dialog.transient(self)
        dialog.resizable(True, False)
        dialog.columnconfigure(1, weight=1)
        dialog.grab_set()
        transient_value = tk.StringVar(value=self.transient_template.get())

        ttk.Label(dialog, text="瞬态目录规则").grid(
            row=0, column=0, sticky="w", padx=(14, 8), pady=(14, 6)
        )
        transient_entry = ttk.Entry(dialog, textvariable=transient_value, width=72)
        transient_entry.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=(14, 6))
        ToolTip(transient_entry, "{year} 自动替换为年份，{date} 自动替换为 YYYYMMDD 日期。")

        note = ttk.Label(
            dialog,
            text="默认规则通常无需修改。恢复后仍需点击“确定”保存。",
        )
        note.grid(row=1, column=0, columnspan=2, sticky="w", padx=14, pady=(4, 10))

        buttons = ttk.Frame(dialog)
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", padx=14, pady=(0, 14))

        def restore_defaults():
            transient_value.set(DEFAULTS["transient_template"])

        def accept():
            try:
                render_remote_directory(transient_value.get(), date.today())
            except ValueError as exc:
                messagebox.showerror("设置有误", str(exc), parent=dialog)
                return
            self.transient_template.set(transient_value.get())
            dialog.destroy()

        ttk.Button(buttons, text="恢复默认", command=restore_defaults).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="确定", command=accept).pack(side="left")
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.bind("<Return>", lambda _event: accept())
        transient_entry.focus_set()

    def _validated_common(self, destination_value: str):
        destination = Path(destination_value.strip()).expanduser()
        if not destination_value.strip():
            raise ValueError("请选择保存目录。")
        start = parse_date_value(self.start_date.get())
        end = parse_date_value(self.end_date.get())
        days = iter_dates(start, end)
        return destination, days

    def _start_transient(self, test_only: bool):
        try:
            destination, days = self._validated_common(self.transient_destination.get())
            fans = parse_fan_spec(self.transient_fans.get())
            local_root = self.transient_local_root.get().strip()
            port = int(self.transient_port.get())
            if port < 1 or port > 65535:
                raise ValueError("SFTP 端口必须在 1-65535。")
            host = require_private_ipv4(self.transient_host.get(), "瞬态服务器")
            if not local_root and not self.transient_username.get().strip():
                raise ValueError("请输入瞬态服务器账号。")
        except ValueError as exc:
            messagebox.showerror("输入有误", str(exc), parent=self)
            return

        options = {
            "destination": destination,
            "days": days,
            "fans": fans,
            "host": host,
            "port": port,
            "username": self.transient_username.get().strip(),
            "password": self.transient_password.get(),
            "template": self.transient_template.get().strip(),
            "local_root": local_root,
            "overwrite": self.overwrite.get(),
            "layout": self.transient_layout.get(),
            "test_only": test_only,
        }
        self._launch_worker(self._run_transient, options, "瞬态连接测试" if test_only else "瞬态拷取")

    def _start_master(self, test_only: bool):
        try:
            destination, days = self._validated_common(self.master_destination.get())
            targets = build_fan_hosts(
                self.master_prefix.get(), self.master_ip_range.get(), "主控 IP 前缀"
            )
            selected_types = [kind for kind in MASTER_TYPES if self.master_types[kind].get()]
            whole_folder = self.master_whole_folder.get()
            if not selected_types and not whole_folder:
                raise ValueError("至少选择一种主控文件类型，或勾选整日期文件夹。")
            log_parts = safe_windows_relative_segments(self.master_log_path.get())
            remote_path = "/" + self.master_remote_path.get().strip().replace("\\", "/").strip("/")
            if remote_path == "/":
                raise ValueError("请输入主控 BOF 远程路径。")
        except ValueError as exc:
            messagebox.showerror("输入有误", str(exc), parent=self)
            return

        options = {
            "destination": destination,
            "days": days,
            "targets": targets[:1] if test_only else targets,
            "ip_range": self.master_ip_range.get().strip(),
            "log_parts": log_parts,
            "local_root": self.master_local_root.get().strip(),
            "username": self.master_username.get().strip(),
            "password": self.master_password.get(),
            "remote_path": remote_path,
            "selected_types": selected_types,
            "whole_folder": whole_folder,
            "connection_mode": "auto",
            "max_workers": min(MASTER_AUTO_WORKERS, len(targets)),
            "keywords": normalize_keywords(self.master_keywords.get()),
            "overwrite": self.overwrite.get(),
            "test_only": test_only,
        }
        self._launch_worker(self._run_master, options, "主控连接测试" if test_only else "主控拷取")

    def _validate_port(self, value: str, label: str) -> int:
        try:
            port = int(value.strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}必须是数字端口。") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"{label}必须在 1-65535。")
        return port

    def _start_pitch(self, test_only: bool):
        try:
            destination, days = self._validated_common(self.pitch_destination.get())
            port = self._validate_port(self.pitch_port.get(), "变桨 SFTP 端口")
            targets = build_fan_hosts(
                self.pitch_host_prefix.get(), self.pitch_fans.get(), "变桨 IP 前缀"
            )
            prefix = targets[0][0].rsplit(".", 1)[0]
            fans = [fan for _host, fan in targets]
            remote_path = self.pitch_remote_path.get().strip()
            if not remote_path.startswith("/"):
                raise ValueError("变桨远程目录必须是 Linux 绝对路径。")
            if not self.pitch_username.get().strip():
                raise ValueError("请输入变桨服务器账号。")
        except ValueError as exc:
            messagebox.showerror("输入有误", str(exc), parent=self)
            return

        options = {
            "destination": destination,
            "days": days,
            "fans": fans,
            "prefix": prefix,
            "port": port,
            "username": self.pitch_username.get().strip(),
            "password": self.pitch_password.get(),
            "remote_path": remote_path,
            "overwrite": self.overwrite.get(),
            "test_only": test_only,
        }
        self._launch_worker(self._run_pitch, options, "变桨连接测试" if test_only else "变桨日志拷取")

    def _start_vibration(self, test_only: bool):
        try:
            destination, days = self._validated_common(self.vibration_destination.get())
            fans = parse_fan_spec(self.vibration_fans.get())
            local_root = self.vibration_local_root.get().strip()
            port = self._validate_port(self.vibration_port.get(), "震动 SFTP 端口")
            host = require_private_ipv4(self.vibration_host.get(), "震动服务器")
            site = self.vibration_site.get().strip()
            if not site.isdigit() or not 3 <= len(site) <= 20:
                raise ValueError("风场号必须是 3-20 位数字，例如 650227。")
            selected_types = [kind for kind in VIBRATION_TYPES if self.vibration_types[kind].get()]
            if not selected_types:
                raise ValueError("至少选择一种震动数据类型。")
            remote_base = self.vibration_remote_base.get().strip()
            if not local_root and not remote_base.startswith("/"):
                raise ValueError("震动 CMSDATA 根目录必须是 Linux 绝对路径。")
            if not local_root and not self.vibration_username.get().strip():
                raise ValueError("请输入震动服务器账号。")
        except ValueError as exc:
            messagebox.showerror("输入有误", str(exc), parent=self)
            return

        options = {
            "destination": destination,
            "days": days,
            "fans": fans,
            "host": host,
            "port": port,
            "username": self.vibration_username.get().strip(),
            "password": self.vibration_password.get(),
            "remote_base": remote_base,
            "local_root": local_root,
            "site": site,
            "selected_types": selected_types,
            "overwrite": self.overwrite.get(),
            "test_only": test_only,
        }
        self._launch_worker(self._run_vibration, options, "震动连接测试" if test_only else "震动数据拷取")

    def _diagnose_master(self):
        try:
            host = build_fan_hosts(
                self.master_prefix.get(), self.master_ip_range.get(), "主控 IP 前缀"
            )[0][0]
        except ValueError as exc:
            messagebox.showerror("输入有误", str(exc), parent=self)
            return
        sftp_ok = self._probe_tcp(host, 22)
        ftp_ok = self._probe_tcp(host, 21)
        rdp_ok = self._probe_tcp(host, 3389)
        smb_ok = self._probe_tcp(host, 445)
        message = (
            f"{host}\n\n"
            f"SFTP 22：{'可达' if sftp_ok else '不可达'}\n"
            f"FTP 21：{'可达' if ftp_ok else '不可达'}\n"
            f"C$共享 445：{'可达' if smb_ok else '不可达'}\n"
            f"远程桌面 3389：{'可达' if rdp_ok else '不可达'}\n\n"
            "远程桌面可达只能证明主控主机和账号链路可登录；BOF 拷取仍需要 FTP/SFTP 或 C$ 文件通道可访问日志目录。"
        )
        messagebox.showinfo("主控端口诊断", message, parent=self)

    def _diagnose_transient(self):
        try:
            host = require_private_ipv4(self.transient_host.get(), "瞬态服务器")
            port = self._validate_port(self.transient_port.get(), "瞬态 SFTP 端口")
        except ValueError as exc:
            messagebox.showerror("输入有误", str(exc), parent=self)
            return
        ok = self._probe_tcp(host, port)
        message = (
            f"{host}:{port}\n\n"
            f"SFTP 端口：{'可达' if ok else '不可达'}\n\n"
            "端口可达只说明链路通，最终仍以连接测试读取瞬态目录为准。"
        )
        messagebox.showinfo("瞬态端口诊断", message, parent=self)

    def _diagnose_vibration(self):
        try:
            host = require_private_ipv4(self.vibration_host.get(), "震动服务器")
            port = self._validate_port(self.vibration_port.get(), "震动 SFTP 端口")
        except ValueError as exc:
            messagebox.showerror("输入有误", str(exc), parent=self)
            return
        ok = self._probe_tcp(host, port)
        message = (
            f"{host}:{port}\n\n"
            f"SFTP 端口：{'可达' if ok else '不可达'}\n\n"
            "端口可达只说明链路通，最终仍以连接测试读取 CMSDATA 目录为准。"
        )
        messagebox.showinfo("震动端口诊断", message, parent=self)

    def _launch_worker(self, target, options: dict, title: str):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("任务运行中", "请等待当前任务结束或先停止。", parent=self)
            return
        destination: Path = options["destination"]
        destination.mkdir(parents=True, exist_ok=True)
        timestamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
        self.current_log = destination / f"拷取记录_{timestamp}.log"
        self.cancel_event.clear()
        self._set_running(True)
        self._clear_log()
        self._emit("log", f"{title}开始")
        self._emit("log", f"保存目录：{destination}")
        self._emit(
            "log",
            "任务参数："
            f"日期={','.join(day.strftime('%Y-%m-%d') for day in options.get('days', []))}；"
            f"风机={','.join(str(fan) for fan in options.get('fans', [])) or options.get('ip_range', '')}；"
            f"覆盖已有文件={'是' if options.get('overwrite') else '否'}；"
            "密码不会写入日志",
        )
        self.worker = threading.Thread(target=self._worker_wrapper, args=(target, options), daemon=True)
        self.worker.start()

    def _worker_wrapper(self, target, options: dict):
        with SystemAwake():
            try:
                summary = target(options)
                self._emit("done", summary)
            except CancelledError:
                self._emit("cancelled", "任务已停止，未完成的 .part 已保留，下次可断点续传。")
            except Exception as exc:
                self._emit("log", f"错误：{exc}")
                self._emit("debug", "异常堆栈（已写入任务日志）：\n" + traceback.format_exc())
                self._emit("failed", str(exc))

    def _connect_sftp(self, options: dict):
        try:
            import paramiko
        except ImportError as exc:
            raise CollectorError("缺少 SFTP 组件 paramiko，请重新安装或使用已打包版本。") from exc

        client = paramiko.SSHClient()
        KNOWN_HOSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        first_connection = not KNOWN_HOSTS_FILE.exists()
        with self._known_hosts_lock:
            if KNOWN_HOSTS_FILE.exists():
                client.load_host_keys(str(KNOWN_HOSTS_FILE))
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            options["host"],
            port=options["port"],
            username=options["username"],
            password=options["password"] or None,
            timeout=12,
            banner_timeout=15,
            auth_timeout=15,
            look_for_keys=False,
            allow_agent=False,
        )
        with self._known_hosts_lock:
            if KNOWN_HOSTS_FILE.exists():
                client.load_host_keys(str(KNOWN_HOSTS_FILE))
            client.save_host_keys(str(KNOWN_HOSTS_FILE))
        if first_connection:
            self._emit("log", "已保存服务器主机指纹，后续连接将校验是否变化。")
        return client

    def _open_sftp_session(self, options: dict):
        client = self._connect_sftp(options)
        try:
            transport = client.get_transport()
            if transport:
                transport.set_keepalive(30)
            sftp = client.open_sftp()
            sftp.get_channel().settimeout(60)
            return client, sftp
        except Exception:
            client.close()
            raise

    def _open_sftp_session_with_retry(self, options: dict, label: str):
        last_error = None
        for attempt in range(1, 4):
            if self.cancel_event.is_set():
                raise CancelledError()
            try:
                return self._open_sftp_session(options)
            except Exception as exc:
                last_error = exc
                self._emit("log", f"{label}连接第 {attempt} 次失败：{exc}")
                if attempt < 3 and self.cancel_event.wait(2):
                    raise CancelledError()
        raise CollectorError(f"{label}连续 3 次连接失败：{last_error}")

    def _progress_tracker(self, total: int, completed: int = 0):
        state = {"done": completed, "last_emit": 0.0}
        state_lock = threading.Lock()
        self._emit("progress_total", total, completed)

        def add(size: int):
            with state_lock:
                state["done"] = min(total, state["done"] + size)
                now = time.monotonic()
                if now - state["last_emit"] >= 0.25 or state["done"] >= total:
                    state["last_emit"] = now
                    self._emit("progress_bytes", state["done"], total)

        def refresh():
            with state_lock:
                state["last_emit"] = time.monotonic()
                self._emit("progress_bytes", state["done"], total)

        return state, add, refresh

    def _scan_sftp_tree(self, sftp, root: str, max_depth: int = 16) -> list[dict]:
        """Return every regular file below root without silently skipping subdirectories."""
        normalized_root = root.rstrip("/") or "/"
        files = []
        stack = [(normalized_root, "", 0)]
        while stack:
            current, relative_directory, depth = stack.pop()
            if self.cancel_event.is_set():
                raise CancelledError()
            try:
                entries = sorted(sftp.listdir_attr(current), key=lambda item: item.filename)
            except OSError as exc:
                raise CollectorError(f"无法读取远程目录，任务未完整：{current}（{exc}）") from exc
            for entry in entries:
                name = entry.filename
                if name in {".", ".."} or "/" in name or "\\" in name:
                    raise CollectorError(f"远程目录包含不安全的名称：{current}/{name}")
                remote_path = posixpath.join(current.rstrip("/"), name)
                relative_path = (
                    posixpath.join(relative_directory, name) if relative_directory else name
                )
                if stat.S_ISDIR(entry.st_mode):
                    if depth >= max_depth:
                        raise CollectorError(f"远程目录层级超过 {max_depth} 层：{remote_path}")
                    stack.append((remote_path, relative_path, depth + 1))
                elif stat.S_ISREG(entry.st_mode):
                    first_segment, separator, _remainder = relative_path.partition("/")
                    task_group = (
                        posixpath.join(normalized_root, first_segment)
                        if separator
                        else normalized_root
                    )
                    files.append(
                        {
                            "remote_path": remote_path,
                            "relative_path": relative_path,
                            "source_directory": posixpath.dirname(remote_path),
                            "task_group": task_group,
                            "name": name,
                            "size": int(entry.st_size),
                            "modified": int(entry.st_mtime),
                        }
                    )
        return sorted(files, key=lambda item: item["remote_path"])

    @staticmethod
    def _build_transfer_units(plan: list[dict]) -> list[tuple[str, list[dict]]]:
        groups = {}
        targets = {}
        for item in plan:
            if item.get("target") is not None:
                target_key = os.path.normcase(os.path.abspath(str(item["target"])))
                previous = targets.setdefault(target_key, item["remote_path"])
                if previous != item["remote_path"]:
                    raise CollectorError(
                        "多个源文件会写入同一个目标路径，已停止以防覆盖："
                        f"{previous}；{item['remote_path']} -> {item['target']}"
                    )
            group = str(item.get("task_group", item["source_directory"]))
            groups.setdefault(group, []).append(item)
        if len(groups) == 1 and len(plan) > 1:
            units = [
                (f"文件 {item['relative_path']}", [item])
                for item in sorted(plan, key=lambda value: value["remote_path"])
            ]
        else:
            units = [
                (f"目录 {directory}", sorted(items, key=lambda value: value["remote_path"]))
                for directory, items in sorted(groups.items())
            ]
        if len(units) <= SFTP_AUTO_WORKERS:
            return units
        # Keep whole directories together, but reuse one session per balanced lane.
        lanes = [[] for _ in range(SFTP_AUTO_WORKERS)]
        sizes = [0] * SFTP_AUTO_WORKERS
        for label, items in sorted(units, key=lambda unit: sum(item.get("size", 0) for item in unit[1]), reverse=True):
            lane = min(range(len(lanes)), key=lambda index: (sizes[index], len(lanes[index])))
            lanes[lane].extend(items)
            sizes[lane] += sum(item.get("size", 0) for item in items)
        return [(f"任务组 {index + 1}", items) for index, items in enumerate(lanes) if items]

    @staticmethod
    def _close_sftp(client, sftp) -> None:
        try:
            if sftp:
                sftp.close()
        finally:
            if client:
                client.close()

    def _save_incomplete_files(self, options, plan, failed_keys, failures):
        if not failed_keys or not options.get("destination"):
            return
        errors = {item["remote_path"]: item.get("last_error", "传输未完成") for item in failures}
        report = Path(options["destination"]) / f"incomplete_{time.time_ns()}.json"
        data = {"files": [
            {"source": item["remote_path"], "destination": str(item["target"]),
             "expected_size": item["size"], "error": errors.get(item["remote_path"], "目标文件缺失或大小不符")}
            for item in plan if item["remote_path"] in failed_keys
        ]}
        try:
            save_json_atomic(report, data)
            self._emit("log", f"未完成文件清单：{report}")
        except OSError as exc:
            self._emit("log", f"无法保存未完成清单：{exc}")

    def _copy_sftp_items(
        self,
        options: dict,
        label: str,
        items: list[dict],
        add_progress,
        max_attempts: int,
        reset_partial_first: bool = True,
    ) -> tuple[int, int, list[dict]]:
        copied = skipped = 0
        failed_items = []
        client = sftp = None
        try:
            for item in items:
                if self.cancel_event.is_set():
                    raise CancelledError()
                result = None
                last_error = None
                for attempt in range(1, max_attempts + 1):
                    if self.cancel_event.is_set():
                        raise CancelledError()
                    try:
                        if sftp is None:
                            client, sftp = self._open_sftp_session(options)
                        self._emit("status", item["status"])
                        result = copy_sftp_atomic(
                            sftp,
                            item["remote_path"],
                            item["target"],
                            options.get("overwrite", False) or item.get("force_overwrite", False),
                            self.cancel_event.is_set,
                            add_progress,
                            reset_partial=reset_partial_first and attempt == 1,
                        )
                        break
                    except CancelledError:
                        raise
                    except Exception as exc:
                        last_error = exc
                        self._close_sftp(client, sftp)
                        client = sftp = None
                        if attempt < max_attempts:
                            self._emit(
                                "log",
                                f"{label}传输中断，第 {attempt + 1} 次将断点续传："
                                f"{item['remote_path']}（{exc}）",
                            )
                            if self.cancel_event.wait(2):
                                raise CancelledError()
                if result is None:
                    failed_item = dict(item)
                    failed_item["last_error"] = str(last_error)
                    failed_items.append(failed_item)
                    continue
                if result.status == "copied":
                    copied += 1
                    self._emit("log", f"已拷取{item['kind']}：{item['relative_path']}（{human_size(result.size)}）")
                    if result.resumed_from:
                        self._emit(
                            "log",
                            f"断点续传：{item['relative_path']}，从 {human_size(result.resumed_from)} 接续",
                        )
                else:
                    skipped += 1
                    self._emit("log", f"已跳过：{item['target']}")
        finally:
            self._close_sftp(client, sftp)
        return copied, skipped, failed_items

    def _transfer_sftp_plan(
        self,
        options: dict,
        plan: list[dict],
        label: str,
        add_progress,
        refresh_progress,
    ) -> tuple[int, int, int]:
        if not plan:
            return 0, 0, 0
        units = self._build_transfer_units(plan)
        workers = 1 if options.get("_parallel_child") else min(SFTP_AUTO_WORKERS, len(units))
        copied = skipped = 0
        retry_units = []
        if workers > 1:
            mode = "文件夹" if len({item["task_group"] for item in plan}) > 1 else "文件"
            self._emit(
                "log",
                f"{label}单台任务拆分为 {len(units)} 个{mode}任务，使用 {workers} 条独立 SFTP 连接。",
            )
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        self._copy_sftp_items,
                        options,
                        unit_label,
                        items,
                        add_progress,
                        1,
                    ): (unit_label, items)
                    for unit_label, items in units
                }
                for future in as_completed(futures):
                    if self.cancel_event.is_set():
                        raise CancelledError()
                    unit_label, _items = futures[future]
                    unit_copied = unit_skipped = 0
                    try:
                        unit_copied, unit_skipped, failed_items = future.result()
                    except CancelledError:
                        raise
                    except Exception as exc:
                        failed_items = []
                        for item in _items:
                            failed_item = dict(item)
                            failed_item["last_error"] = str(exc)
                            failed_items.append(failed_item)
                    copied += unit_copied
                    skipped += unit_skipped
                    if failed_items:
                        retry_units.append((unit_label, failed_items))
                        self._emit("log", f"{unit_label}并发失败，加入单连接断点重试。")
                    refresh_progress()
        else:
            final_failures = []
            for unit_label, items in units:
                unit_copied, unit_skipped, failed_items = self._copy_sftp_items(
                    options, unit_label, items, add_progress, 3
                )
                copied += unit_copied
                skipped += unit_skipped
                final_failures.extend(failed_items)
                refresh_progress()

        if workers > 1:
            final_failures = []
        for unit_label, items in retry_units:
            self._emit("log", f"{unit_label}降级为单连接重试，已完成文件不会重复下载。")
            unit_copied, unit_skipped, failed_items = self._copy_sftp_items(
                options,
                unit_label,
                items,
                add_progress,
                3,
                reset_partial_first=False,
            )
            copied += unit_copied
            skipped += unit_skipped
            final_failures.extend(failed_items)
            refresh_progress()

        failed_keys = {item["remote_path"] for item in final_failures}
        for item in plan:
            target = item["target"]
            if not target.is_file() or target.stat().st_size != item["size"]:
                failed_keys.add(item["remote_path"])
        if failed_keys:
            self._save_incomplete_files(options, plan, failed_keys, final_failures)
            self._emit("log", f"{label}完整性校验未通过：{len(failed_keys)} 个文件未完整。")
            for item in final_failures:
                self._emit(
                    "log",
                    f"文件失败：{item['remote_path']}（{item.get('last_error', '目标文件不完整')}）",
                )
        else:
            self._emit("log", f"{label}完整性校验通过：{len(plan)} 个文件的路径和大小均正确。")
        return copied, skipped, len(failed_keys)

    def _copy_local_items(
        self,
        options: dict,
        label: str,
        items: list[dict],
        add_progress,
        max_attempts: int,
        reset_partial_first: bool = True,
    ) -> tuple[int, int, list[dict]]:
        copied = skipped = 0
        failed_items = []
        for item in items:
            result = None
            last_error = None
            for attempt in range(1, max_attempts + 1):
                if self.cancel_event.is_set():
                    raise CancelledError()
                try:
                    self._emit("status", item["status"])
                    result = copy_local_atomic(
                        item["source"],
                        item["target"],
                        options.get("overwrite", False),
                        self.cancel_event.is_set,
                        add_progress,
                        reset_partial=reset_partial_first and attempt == 1,
                    )
                    break
                except CancelledError:
                    raise
                except Exception as exc:
                    last_error = exc
                    if attempt < max_attempts:
                        self._emit(
                            "log",
                            f"{label}读取中断，第 {attempt + 1} 次将断点续传："
                            f"{item['source']}（{exc}）",
                        )
                        if self.cancel_event.wait(2):
                            raise CancelledError()
            if result is None:
                failed_item = dict(item)
                failed_item["last_error"] = str(last_error)
                failed_items.append(failed_item)
            elif result.status == "copied":
                copied += 1
                self._emit("log", f"已拷取{item['kind']}：{item['relative_path']}（{human_size(result.size)}）")
            else:
                skipped += 1
                self._emit("log", f"已跳过：{item['target']}")
        return copied, skipped, failed_items

    def _transfer_local_plan(
        self,
        options: dict,
        plan: list[dict],
        label: str,
        add_progress,
        refresh_progress,
    ) -> tuple[int, int, int]:
        if not plan:
            return 0, 0, 0
        units = self._build_transfer_units(plan)
        workers = 1 if options.get("_parallel_child") else min(SFTP_AUTO_WORKERS, len(units))
        copied = skipped = 0
        retry_units = []
        final_failures = []
        if workers > 1:
            mode = "文件夹" if len({item["task_group"] for item in plan}) > 1 else "文件"
            self._emit(
                "log",
                f"{label}拆分为 {len(units)} 个{mode}任务，使用 {workers} 个并发读取任务。",
            )
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        self._copy_local_items,
                        options,
                        unit_label,
                        items,
                        add_progress,
                        1,
                    ): (unit_label, items)
                    for unit_label, items in units
                }
                for future in as_completed(futures):
                    unit_label, items = futures[future]
                    unit_copied = unit_skipped = 0
                    try:
                        unit_copied, unit_skipped, failed_items = future.result()
                    except CancelledError:
                        raise
                    except Exception as exc:
                        failed_items = []
                        for item in items:
                            failed_item = dict(item)
                            failed_item["last_error"] = str(exc)
                            failed_items.append(failed_item)
                    copied += unit_copied
                    skipped += unit_skipped
                    if failed_items:
                        retry_units.append((unit_label, failed_items))
                    refresh_progress()
        else:
            for unit_label, items in units:
                unit_copied, unit_skipped, failed_items = self._copy_local_items(
                    options, unit_label, items, add_progress, 3
                )
                copied += unit_copied
                skipped += unit_skipped
                final_failures.extend(failed_items)
                refresh_progress()

        for unit_label, items in retry_units:
            self._emit("log", f"{unit_label}降级为顺序重试，已完成文件不会重复拷取。")
            unit_copied, unit_skipped, failed_items = self._copy_local_items(
                options,
                unit_label,
                items,
                add_progress,
                3,
                reset_partial_first=False,
            )
            copied += unit_copied
            skipped += unit_skipped
            final_failures.extend(failed_items)
            refresh_progress()

        failed_keys = {item["remote_path"] for item in final_failures}
        for item in plan:
            target = item["target"]
            if not target.is_file() or target.stat().st_size != item["size"]:
                failed_keys.add(item["remote_path"])
        if failed_keys:
            self._save_incomplete_files(options, plan, failed_keys, final_failures)
            self._emit("log", f"{label}完整性校验未通过：{len(failed_keys)} 个文件未完整。")
        else:
            self._emit("log", f"{label}完整性校验通过：{len(plan)} 个文件的路径和大小均正确。")
        return copied, skipped, len(failed_keys)

    def _render_local_source_root(self, template: str, day: date, **values) -> Path:
        if not template.strip():
            raise CollectorError("网络位置来源为空")
        data = {
            "date": day.strftime("%Y%m%d"),
            "date_dash": day.strftime("%Y-%m-%d"),
            "year": day.strftime("%Y"),
            "month": day.strftime("%Y-%m"),
            **values,
        }
        try:
            rendered = template.format(**data)
        except KeyError as exc:
            raise CollectorError(f"网络位置来源包含未知占位符：{exc.args[0]}") from exc
        return Path(rendered)

    def _run_transient_from_local_source(self, options: dict) -> str:
        options["destination"].mkdir(parents=True, exist_ok=True)
        self._emit(
            "log",
            f"瞬态使用网络位置来源：{options['local_root']}；服务器字段仅用于输出归档={options['host']}",
        )
        selected_fans = set(options["fans"])
        copied = skipped = failed = 0
        total_size = 0
        found: set[tuple[str, int]] = set()
        plan = []
        per_fan_source = "{fan}" in options["local_root"] or "{fan3}" in options["local_root"]
        for day in options["days"]:
            fan_roots = []
            if per_fan_source:
                for fan in selected_fans:
                    fan_roots.append(
                        (
                            fan,
                            self._render_local_source_root(
                                options["local_root"],
                                day,
                                ip=options["host"],
                                fan=str(fan),
                                fan3=f"{fan:03d}",
                            ),
                        )
                    )
            else:
                fan_roots.append(
                    (
                        None,
                        self._render_local_source_root(options["local_root"], day, ip=options["host"]),
                    )
                )
            for fixed_fan, root in fan_roots:
                if not root.exists() or not root.is_dir():
                    self._emit("log", f"瞬态网络位置不可访问：{root}")
                    continue
                self._emit("log", f"瞬态网络位置可访问：{root}")
                for source in root.rglob("*"):
                    if self.cancel_event.is_set():
                        raise CancelledError()
                    if not source.is_file():
                        continue
                    if fixed_fan is None:
                        info = transient_file_info(source.name, day.strftime("%Y%m%d"))
                        if not info:
                            continue
                        _site, fan = info
                        if fan not in selected_fans:
                            continue
                    else:
                        fan = fixed_fan
                    found.add((day.strftime("%Y%m%d"), fan))
                    relative_path = source.relative_to(root)
                    target_root = transient_output_directory(
                        options["destination"], options["host"], str(root), day, fan, options["layout"]
                    )
                    stat_info = source.stat()
                    first_segment = relative_path.parts[0]
                    task_group = root / first_segment if len(relative_path.parts) > 1 else root
                    plan.append(
                        {
                            "source": source,
                            "remote_path": str(source.resolve()),
                            "relative_path": relative_path.as_posix(),
                            "source_directory": str(source.parent),
                            "task_group": str(task_group),
                            "target": target_root / relative_path,
                            "size": stat_info.st_size,
                            "modified": stat_info.st_mtime_ns,
                            "kind": "瞬态网络位置文件",
                            "status": (
                                f"瞬态网络位置 {fan:03d}号 {day:%Y-%m-%d}："
                                f"{relative_path.as_posix()}"
                            ),
                        }
                    )
        total_size = sum(item["size"] for item in plan)
        initial_done = 0
        if not options["overwrite"]:
            for item in plan:
                target = item["target"]
                if target.exists() and target.stat().st_size == item["size"]:
                    initial_done += item["size"]
                else:
                    initial_done += partial_resume_offset(
                        target, item["remote_path"], item["size"], item["modified"]
                    )
        remaining = max(0, total_size - initial_done)
        free = require_transfer_space(options["destination"], remaining)
        self._emit(
            "log",
            f"瞬态网络位置已统计 {len(plan)} 个文件，共 {human_size(total_size)}；"
            f"本次还需写入 {human_size(remaining)}，目标盘可用 {human_size(free)}。",
        )
        _state, add_progress, refresh_progress = self._progress_tracker(total_size, initial_done)
        if options["test_only"]:
            return f"瞬态网络位置测试通过：命中文件 {len(plan)} 个"
        copied, skipped, failed = self._transfer_local_plan(
            options, plan, "瞬态网络位置", add_progress, refresh_progress
        )
        self._emit("counter", copied, skipped, failed)
        for day in options["days"]:
            for fan in options["fans"]:
                key = (day.strftime("%Y%m%d"), fan)
                if key not in found:
                    self._emit("log", f"未找到：{fan:03d}号风机 {day:%Y-%m-%d} 瞬态网络位置文件")
        return f"瞬态网络位置完成：拷取 {copied}，跳过 {skipped}，失败 {failed}，共 {human_size(total_size)}"

    def _run_transient(self, options: dict) -> str:
        if options.get("local_root", "").strip():
            return self._run_transient_from_local_source(options)
        if not options.get("test_only") and not options.get("_parallel_child"):
            if len(options["fans"]) > 1:
                units = [(list(options["days"]), [fan]) for fan in options["fans"]]
            elif len(options["days"]) > 1:
                units = [([day], list(options["fans"])) for day in options["days"]]
            else:
                units = []
            if len(units) > 1:
                copied = skipped = failed = total_size = 0
                workers = min(SFTP_AUTO_WORKERS, len(units))
                self._emit("log", f"瞬态自动拆分为 {len(units)} 个任务，使用 {workers} 条独立 SFTP 连接。")
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {
                        executor.submit(
                            self._run_transient,
                            dict(
                                options,
                                days=days,
                                fans=fans,
                                _parallel_child=True,
                                _return_counts=True,
                                _suppress_progress=True,
                            ),
                        ): (days, fans)
                        for days, fans in units
                    }
                    retry_units = []
                    for future in as_completed(futures):
                        if self.cancel_event.is_set():
                            raise CancelledError()
                        try:
                            c, s, f, size = future.result()
                            copied += c
                            skipped += s
                            failed += f
                            total_size += size
                        except CancelledError:
                            raise
                        except Exception as exc:
                            retry_units.append(futures[future])
                            self._emit("log", f"瞬态并行任务失败，稍后降级为单连接重试：{exc}")
                        self._emit("counter", copied, skipped, failed)
                for days, fans in retry_units:
                    self._emit("log", f"瞬态降级重试：风机={fans}，日期={','.join(day.strftime('%Y-%m-%d') for day in days)}")
                    try:
                        c, s, f, size = self._run_transient(
                            dict(options, days=days, fans=fans, _parallel_child=True, _return_counts=True)
                        )
                        copied += c
                        skipped += s
                        failed += f
                        total_size += size
                    except CancelledError:
                        raise
                    except Exception as exc:
                        failed += 1
                        self._emit("log", f"瞬态单连接兜底仍失败，继续下一任务：{exc}")
                    self._emit("counter", copied, skipped, failed)
                return f"瞬态完成：拷取 {copied}，跳过 {skipped}，失败 {failed}，共 {human_size(total_size)}"
        self._emit(
            "log",
            f"瞬态连接参数：服务器={options['host']}:{options['port']}，账号={options['username']}，"
            f"远程规则={options['template']}，整理方式={options['layout']}",
        )
        self._emit("status", f"正在连接 {options['host']}:{options['port']}")
        client, sftp = self._open_sftp_session_with_retry(options, "瞬态")
        copied = skipped = failed = 0
        total_size = 0
        found: set[tuple[str, int]] = set()
        try:
            if options["test_only"]:
                test_path = render_remote_directory(options["template"], options["days"][0])
                sftp.listdir(test_path)
                self._emit("log", f"连接成功：{options['host']}:{options['port']}")
                self._emit("log", f"远程目录可访问：{test_path}")
                return "瞬态连接测试通过"

            selected_fans = set(options["fans"])
            plan = []
            self._emit("status", "正在统计瞬态文件与所需空间…")
            for day in options["days"]:
                if self.cancel_event.is_set():
                    raise CancelledError()
                remote_dir = render_remote_directory(options["template"], day)
                self._emit("log", f"检查瞬态远程目录：{remote_dir}")
                entries = self._scan_sftp_tree(sftp, remote_dir)
                folder_count = len({item["task_group"] for item in entries})
                self._emit(
                    "log",
                    f"瞬态目录递归扫描完成：{remote_dir}，文件={len(entries)}，"
                    f"第一层任务={folder_count}",
                )

                for source in entries:
                    info = transient_file_info(source["name"], day.strftime("%Y%m%d"))
                    if not info:
                        continue
                    _equipment_id, fan = info
                    if fan not in selected_fans:
                        continue
                    found.add((day.strftime("%Y%m%d"), fan))
                    target_root = transient_output_directory(
                        options["destination"],
                        options["host"],
                        remote_dir,
                        day,
                        fan,
                        options["layout"],
                    )
                    item = dict(source)
                    item.update(
                        {
                            "target": target_root.joinpath(*source["relative_path"].split("/")),
                            "day": day,
                            "fan": fan,
                            "kind": "瞬态",
                            "status": (
                                f"瞬态 {fan:03d}号 {day:%Y-%m-%d}："
                                f"{source['relative_path']}"
                            ),
                        }
                    )
                    plan.append(item)

            initial_done = 0
            total_size = sum(item["size"] for item in plan)
            if not options["overwrite"]:
                for item in plan:
                    target = item["target"]
                    if target.exists() and target.stat().st_size == item["size"]:
                        initial_done += item["size"]
                    else:
                        initial_done += partial_resume_offset(
                            target,
                            item["remote_path"],
                            item["size"],
                            item["modified"],
                        )
            remaining = max(0, total_size - initial_done)
            free = require_transfer_space(options["destination"], remaining)
            self._emit(
                "log",
                f"已统计 {len(plan)} 个瞬态文件，共 {human_size(total_size)}；"
                f"本次还需写入 {human_size(remaining)}，目标盘可用 {human_size(free)}。",
            )
            if options.get("_suppress_progress"):
                add_progress = lambda _size: None
                refresh_progress = lambda: None
            else:
                _progress_state, add_progress, refresh_progress = self._progress_tracker(
                    total_size, initial_done
                )
            self._close_sftp(client, sftp)
            client = sftp = None
            copied, skipped, failed = self._transfer_sftp_plan(
                options, plan, "瞬态", add_progress, refresh_progress
            )
            self._emit("counter", copied, skipped, failed)

            for day in options["days"]:
                for fan in options["fans"]:
                    key = (day.strftime("%Y%m%d"), fan)
                    if key not in found:
                        self._emit("log", f"未找到：{fan:03d}号风机 {day:%Y-%m-%d} 瞬态文件")
            if options.get("_return_counts"):
                return copied, skipped, failed, total_size
            return f"瞬态完成：拷取 {copied}，跳过 {skipped}，失败 {failed}，共 {human_size(total_size)}"
        finally:
            self._close_sftp(client, sftp)

    def _run_pitch(self, options: dict) -> str:
        if not options.get("test_only") and not options.get("_parallel_child") and len(options["fans"]) > 1:
            copied = skipped = failed = offline = total_size = 0
            retry_fans = []
            workers = min(SFTP_AUTO_WORKERS, len(options["fans"]))
            self._emit("log", f"变桨自动拆分为 {len(options['fans'])} 台任务，使用 {workers} 条独立 SFTP 连接。")
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        self._run_pitch,
                        dict(
                            options,
                            fans=[fan],
                            _parallel_child=True,
                            _return_counts=True,
                            _suppress_progress=True,
                        ),
                    ): fan
                    for fan in options["fans"]
                }
                for future in as_completed(futures):
                    if self.cancel_event.is_set():
                        raise CancelledError()
                    fan = futures[future]
                    try:
                        c, s, f, o, size = future.result()
                        if o and c == 0 and s == 0 and f == 0:
                            retry_fans.append(fan)
                            self._emit("log", f"变桨 {fan:03d}号并行连接失败，稍后降级为单连接重试。")
                        else:
                            copied += c
                            skipped += s
                            failed += f
                            offline += o
                            total_size += size
                    except CancelledError:
                        raise
                    except Exception as exc:
                        retry_fans.append(fan)
                        self._emit("log", f"变桨 {fan:03d}号并行任务失败：{exc}")
                    self._emit("counter", copied, skipped, failed + offline)
            for fan in retry_fans:
                self._emit("log", f"变桨 {fan:03d}号降级为单连接顺序重试。")
                try:
                    c, s, f, o, size = self._run_pitch(
                        dict(options, fans=[fan], _parallel_child=True, _return_counts=True)
                    )
                    copied += c
                    skipped += s
                    failed += f
                    offline += o
                    total_size += size
                except CancelledError:
                    raise
                except Exception as exc:
                    offline += 1
                    self._emit("log", f"变桨 {fan:03d}号单连接兜底仍失败，继续下一台：{exc}")
                self._emit("counter", copied, skipped, failed + offline)
            return f"变桨完成：拷取 {copied}，跳过 {skipped}，失败 {failed}，连接失败 {offline}，共 {human_size(total_size)}"
        self._emit(
            "log",
            f"变桨连接参数：IP前缀={options['prefix']}，端口={options['port']}，账号={options['username']}，"
            f"远程目录={options['remote_path']}",
        )
        copied = skipped = failed = offline = 0
        total_size = 0
        fans = options["fans"][:1] if options["test_only"] else options["fans"]
        for fan in fans:
            if self.cancel_event.is_set():
                raise CancelledError()
            host = f"{options['prefix']}.{fan}"
            fan_options = dict(options, host=host)
            self._emit("status", f"正在连接变桨 {fan:03d}号（{host}:{options['port']}）")
            client = sftp = None
            try:
                client, sftp = self._open_sftp_session_with_retry(fan_options, f"变桨 {fan:03d}号")
                if options["test_only"]:
                    sftp.listdir(options["remote_path"])
                    self._emit("log", f"变桨连接成功：{host}:{options['port']}")
                    self._emit("log", f"远程目录可访问：{options['remote_path']}")
                    return f"变桨连接测试通过：{fan:03d}号（{host}）"

                plan = []
                self._emit("status", f"正在统计变桨 {fan:03d}号日志…")
                try:
                    entries = sftp.listdir_attr(options["remote_path"])
                except OSError as exc:
                    raise CollectorError(
                        f"变桨目录不可访问：{options['remote_path']}（{exc}）"
                    ) from exc
                self._emit(
                    "log",
                    f"变桨目录可访问：{options['remote_path']}，目录项={len(entries)}；"
                    f"筛选日期={','.join(day.strftime('%Y%m%d') for day in options['days'])}",
                )
                for day in options["days"]:
                    if self.cancel_event.is_set():
                        raise CancelledError()
                    expected = day.strftime("%Y%m%d")
                    for entry in entries:
                        if stat.S_ISREG(entry.st_mode) and pitch_file_matches(entry.filename, expected):
                            remote_path = posixpath.join(options["remote_path"], entry.filename)
                            target = pitch_output_directory(options["destination"], host, day, fan) / entry.filename
                            plan.append((day, entry, remote_path, target))

                fan_total = sum(int(item[1].st_size) for item in plan)
                fan_initial = 0
                if not options["overwrite"]:
                    for _day, entry, remote_path, target in plan:
                        if target.exists() and target.stat().st_size == int(entry.st_size):
                            fan_initial += int(entry.st_size)
                        else:
                            fan_initial += partial_resume_offset(
                                target, remote_path, int(entry.st_size), int(entry.st_mtime)
                            )
                fan_remaining = max(0, fan_total - fan_initial)
                free = require_transfer_space(options["destination"], fan_remaining)
                self._emit(
                    "log",
                    f"变桨 {fan:03d}号已统计 {len(plan)} 个日志，共 {human_size(fan_total)}；"
                    f"本次还需写入 {human_size(fan_remaining)}，目标盘可用 {human_size(free)}。",
                )
                if options.get("_suppress_progress"):
                    add_progress = lambda _size: None
                    refresh_progress = lambda: None
                else:
                    _state, add_progress, refresh_progress = self._progress_tracker(fan_total, fan_initial)
                for day, entry, remote_path, target in plan:
                    if self.cancel_event.is_set():
                        raise CancelledError()
                    self._emit("status", f"变桨 {fan:03d}号 {day:%Y-%m-%d}：{entry.filename}")
                    result = None
                    last_error = None
                    for attempt in range(1, 4):
                        if attempt > 1:
                            try:
                                sftp.close()
                                client.close()
                            except Exception:
                                pass
                            if self.cancel_event.wait(2):
                                raise CancelledError()
                            try:
                                client, sftp = self._open_sftp_session(fan_options)
                            except Exception as exc:
                                last_error = exc
                                continue
                        try:
                            result = copy_sftp_atomic(
                                sftp,
                                remote_path,
                                target,
                                options["overwrite"],
                                self.cancel_event.is_set,
                                add_progress,
                                reset_partial=(attempt == 1),
                            )
                            last_error = None
                            break
                        except CancelledError:
                            raise
                        except Exception as exc:
                            last_error = exc
                            self._emit("log", f"变桨传输中断：{entry.filename}（{exc}）")
                    if result is None:
                        failed += 1
                        self._emit("log", f"变桨文件失败：{remote_path}（{last_error}）")
                    elif result.status == "copied":
                        copied += 1
                        total_size += result.size
                        self._emit("log", f"已拷取变桨：{entry.filename}（{human_size(result.size)}）")
                    else:
                        skipped += 1
                        self._emit("log", f"已跳过变桨：{target}")
                    refresh_progress()
                    self._emit("counter", copied, skipped, failed + offline)
            except CancelledError:
                raise
            except Exception as exc:
                offline += 1
                self._emit("log", f"变桨 {fan:03d}号失败：{exc}")
                self._emit("counter", copied, skipped, failed + offline)
            finally:
                try:
                    if sftp:
                        sftp.close()
                finally:
                    if client:
                        client.close()
        if options.get("_return_counts"):
            return copied, skipped, failed, offline, total_size
        return f"变桨完成：拷取 {copied}，跳过 {skipped}，失败 {failed}，连接失败 {offline}，共 {human_size(total_size)}"

    def _run_vibration(self, options: dict) -> str:
        if options.get("local_root", "").strip():
            return self._run_vibration_from_local_source(options)
        if not options.get("test_only") and not options.get("_parallel_child"):
            units = [(fan, data_type) for fan in options["fans"] for data_type in options["selected_types"]]
            if len(units) > 1:
                copied = skipped = failed = offline = total_size = 0
                retry_units = []
                workers = min(SFTP_AUTO_WORKERS, len(units))
                self._emit("log", f"震动自动拆分为 {len(units)} 个风机/数据类型任务，使用 {workers} 条独立 SFTP 连接。")
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {
                        executor.submit(
                            self._run_vibration,
                            dict(
                                options,
                                fans=[fan],
                                selected_types=[data_type],
                                _parallel_child=True,
                                _return_counts=True,
                                _suppress_progress=True,
                            ),
                        ): (fan, data_type)
                        for fan, data_type in units
                    }
                    for future in as_completed(futures):
                        if self.cancel_event.is_set():
                            raise CancelledError()
                        fan, data_type = futures[future]
                        try:
                            c, s, f, o, size = future.result()
                            if o and c == 0 and s == 0 and f == 0:
                                retry_units.append((fan, data_type))
                                self._emit("log", f"震动 {data_type} {fan:03d}号并行连接失败，稍后降级重试。")
                            else:
                                copied += c
                                skipped += s
                                failed += f
                                offline += o
                                total_size += size
                        except CancelledError:
                            raise
                        except Exception as exc:
                            retry_units.append((fan, data_type))
                            self._emit("log", f"震动 {data_type} {fan:03d}号并行任务失败：{exc}")
                        self._emit("counter", copied, skipped, failed + offline)
                for fan, data_type in retry_units:
                    self._emit("log", f"震动 {data_type} {fan:03d}号降级为单连接顺序重试。")
                    try:
                        c, s, f, o, size = self._run_vibration(
                            dict(
                                options,
                                fans=[fan],
                                selected_types=[data_type],
                                _parallel_child=True,
                                _return_counts=True,
                            )
                        )
                        copied += c
                        skipped += s
                        failed += f
                        offline += o
                        total_size += size
                    except CancelledError:
                        raise
                    except Exception as exc:
                        offline += 1
                        self._emit(
                            "log",
                            f"震动 {data_type} {fan:03d}号单连接兜底仍失败，继续下一任务：{exc}",
                        )
                    self._emit("counter", copied, skipped, failed + offline)
                return f"震动完成：拷取 {copied}，跳过 {skipped}，失败 {failed}，连接失败 {offline}，共 {human_size(total_size)}"
        self._emit(
            "log",
            f"震动连接参数：服务器={options['host']}:{options['port']}，账号={options['username']}，"
            f"CMSDATA根目录={options['remote_base']}，风场={options['site']}，"
            f"数据类型={','.join(options['selected_types'])}",
        )
        copied = skipped = failed = offline = 0
        total_size = 0
        fans = options["fans"][:1] if options["test_only"] else options["fans"]
        for fan in fans:
            if self.cancel_event.is_set():
                raise CancelledError()
            self._emit("status", f"正在连接震动服务器 {options['host']}:{options['port']}")
            client = sftp = None
            try:
                client, sftp = self._open_sftp_session_with_retry(options, f"震动 {fan:03d}号")
                if options["test_only"]:
                    test_type = options["selected_types"][0]
                    test_path = vibration_remote_directory(
                        options["remote_base"], test_type, options["site"], options["days"][0], fan
                    )
                    sftp.listdir(test_path)
                    self._emit("log", f"震动连接成功：{options['host']}:{options['port']}")
                    self._emit("log", f"同级数据目录可访问：{test_path}")
                    return f"震动连接测试通过：{options['site']}{fan:03d}"

                plan = []
                self._emit("status", f"正在统计震动 {options['site']}{fan:03d}…")
                for data_type in options["selected_types"]:
                    for day in options["days"]:
                        if self.cancel_event.is_set():
                            raise CancelledError()
                        remote_dir = vibration_remote_directory(
                            options["remote_base"], data_type, options["site"], day, fan
                        )
                        self._emit("log", f"检查震动远程目录：{remote_dir}")
                        entries = self._scan_sftp_tree(sftp, remote_dir)
                        folder_count = len({item["task_group"] for item in entries})
                        self._emit(
                            "log",
                            f"震动目录递归扫描完成：{remote_dir}，文件={len(entries)}，"
                            f"第一层任务={folder_count}",
                        )
                        target_root = vibration_output_directory(
                            options["destination"],
                            options["host"],
                            options["site"],
                            data_type,
                            day,
                            fan,
                        )
                        for source in entries:
                            item = dict(source)
                            item.update(
                                {
                                    "target": target_root.joinpath(
                                        *source["relative_path"].split("/")
                                    ),
                                    "kind": f"震动 {data_type}",
                                    "status": (
                                        f"震动 {data_type} {options['site']}{fan:03d} "
                                        f"{day:%Y-%m-%d}：{source['relative_path']}"
                                    ),
                                }
                            )
                            plan.append(item)

                fan_total = sum(item["size"] for item in plan)
                fan_initial = 0
                if not options["overwrite"]:
                    for item in plan:
                        target = item["target"]
                        if target.exists() and target.stat().st_size == item["size"]:
                            fan_initial += item["size"]
                        else:
                            fan_initial += partial_resume_offset(
                                target,
                                item["remote_path"],
                                item["size"],
                                item["modified"],
                            )
                fan_remaining = max(0, fan_total - fan_initial)
                free = require_transfer_space(options["destination"], fan_remaining)
                self._emit(
                    "log",
                    f"震动 {options['site']}{fan:03d} 已统计 {len(plan)} 个文件，共 {human_size(fan_total)}；"
                    f"本次还需写入 {human_size(fan_remaining)}，目标盘可用 {human_size(free)}。",
                )
                if options.get("_suppress_progress"):
                    add_progress = lambda _size: None
                    refresh_progress = lambda: None
                else:
                    _state, add_progress, refresh_progress = self._progress_tracker(fan_total, fan_initial)
                self._close_sftp(client, sftp)
                client = sftp = None
                fan_copied, fan_skipped, fan_failed = self._transfer_sftp_plan(
                    options,
                    plan,
                    f"震动 {options['site']}{fan:03d}",
                    add_progress,
                    refresh_progress,
                )
                copied += fan_copied
                skipped += fan_skipped
                failed += fan_failed
                total_size += fan_total
                self._emit("counter", copied, skipped, failed + offline)
            except CancelledError:
                raise
            except Exception as exc:
                offline += 1
                self._emit("log", f"震动风机 {options['site']}{fan:03d} 失败：{exc}")
                self._emit("counter", copied, skipped, failed + offline)
            finally:
                self._close_sftp(client, sftp)
        if options.get("_return_counts"):
            return copied, skipped, failed, offline, total_size
        return f"震动完成：拷取 {copied}，跳过 {skipped}，失败 {failed}，连接失败 {offline}，共 {human_size(total_size)}"

    def _run_vibration_from_local_source(self, options: dict) -> str:
        options["destination"].mkdir(parents=True, exist_ok=True)
        self._emit(
            "log",
            f"震动使用网络位置来源：{options['local_root']}；服务器字段仅用于输出归档={options['host']}，"
            f"风场={options['site']}，类型={','.join(options['selected_types'])}",
        )
        copied = skipped = failed = 0
        total_size = 0
        plan = []
        fans = options["fans"][:1] if options["test_only"] else options["fans"]
        for fan in fans:
            equipment = f"{options['site']}{fan:03d}"
            for data_type in options["selected_types"]:
                for day in options["days"]:
                    if self.cancel_event.is_set():
                        raise CancelledError()
                    root = self._render_local_source_root(
                        options["local_root"],
                        day,
                        ip=options["host"],
                        site=options["site"],
                        fan=str(fan),
                        fan3=f"{fan:03d}",
                        equipment=equipment,
                        type=data_type,
                    )
                    candidates = [
                        root / data_type / options["site"] / day.strftime("%Y-%m") / equipment / day.strftime("%Y-%m-%d"),
                        root / options["site"] / day.strftime("%Y-%m") / equipment / day.strftime("%Y-%m-%d"),
                        root / day.strftime("%Y-%m") / equipment / day.strftime("%Y-%m-%d"),
                        root / equipment / day.strftime("%Y-%m-%d"),
                        root / day.strftime("%Y-%m-%d"),
                        root,
                    ]
                    source_dir = next((candidate for candidate in candidates if candidate.exists() and candidate.is_dir()), None)
                    if source_dir is None:
                        self._emit(
                            "log",
                            f"震动网络位置未找到目录：类型={data_type}，风机={equipment}，日期={day:%Y-%m-%d}，根={root}",
                        )
                        continue
                    self._emit("log", f"震动网络位置命中目录：{source_dir}")
                    for source in source_dir.rglob("*"):
                        if not source.is_file():
                            continue
                        relative_path = source.relative_to(source_dir)
                        target_root = vibration_output_directory(
                            options["destination"], options["host"], options["site"], data_type, day, fan
                        )
                        stat_info = source.stat()
                        first_segment = relative_path.parts[0]
                        task_group = (
                            source_dir / first_segment if len(relative_path.parts) > 1 else source_dir
                        )
                        plan.append(
                            {
                                "source": source,
                                "remote_path": str(source.resolve()),
                                "relative_path": relative_path.as_posix(),
                                "source_directory": str(source.parent),
                                "task_group": str(task_group),
                                "target": target_root / relative_path,
                                "size": stat_info.st_size,
                                "modified": stat_info.st_mtime_ns,
                                "kind": f"震动网络位置 {data_type}",
                                "status": (
                                    f"震动网络位置 {data_type} {fan:03d}号 "
                                    f"{day:%Y-%m-%d}：{relative_path.as_posix()}"
                                ),
                            }
                        )
        total_size = sum(item["size"] for item in plan)
        initial_done = 0
        if not options["overwrite"]:
            for item in plan:
                target = item["target"]
                if target.exists() and target.stat().st_size == item["size"]:
                    initial_done += item["size"]
                else:
                    initial_done += partial_resume_offset(
                        target, item["remote_path"], item["size"], item["modified"]
                    )
        remaining = max(0, total_size - initial_done)
        free = require_transfer_space(options["destination"], remaining)
        self._emit(
            "log",
            f"震动网络位置已统计 {len(plan)} 个文件，共 {human_size(total_size)}；"
            f"本次还需写入 {human_size(remaining)}，目标盘可用 {human_size(free)}。",
        )
        _state, add_progress, refresh_progress = self._progress_tracker(total_size, initial_done)
        if options["test_only"]:
            return f"震动网络位置测试通过：命中文件 {len(plan)} 个"
        copied, skipped, failed = self._transfer_local_plan(
            options, plan, "震动网络位置", add_progress, refresh_progress
        )
        self._emit("counter", copied, skipped, failed)
        return f"震动网络位置完成：拷取 {copied}，跳过 {skipped}，失败 {failed}，共 {human_size(total_size)}"

    @staticmethod
    def _probe_tcp(host: str, port: int, timeout: float = 2.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    @staticmethod
    def _locate_master_date_directory(log_root: Path, day: date) -> Path | None:
        """Accept the date folder spellings used by different master software builds."""
        expected = day.strftime("%Y%m%d")
        candidates = {
            expected,
            day.strftime("%Y-%m-%d"),
            day.strftime("%Y_%m_%d"),
            day.strftime("%Y.%m.%d"),
        }
        try:
            children = list(log_root.iterdir())
        except OSError:
            return None
        for item in children:
            if item.is_dir() and item.name in candidates:
                return item
        # Some versions add a single intermediate folder below FTP\Log.
        for parent in children:
            if not parent.is_dir():
                continue
            try:
                for item in parent.iterdir():
                    if item.is_dir() and item.name in candidates:
                        return item
            except OSError:
                continue
        return None

    @staticmethod
    def _date_folder_names(day: date) -> set[str]:
        return {
            day.strftime("%Y%m%d"),
            day.strftime("%Y-%m-%d"),
            day.strftime("%Y_%m_%d"),
            day.strftime("%Y.%m.%d"),
        }

    @staticmethod
    def _ftp_join(left: str, right: str) -> str:
        if left.startswith("/"):
            return posixpath.join(left.rstrip("/"), right.strip("/"))
        return posixpath.join(left.strip("/"), right.strip("/")).strip("/")

    def _connect_master_ftp(self, host: str) -> ftplib.FTP:
        ftp = ftplib.FTP()
        ftp.encoding = "gbk"
        ftp.connect(host, 21, timeout=12)
        ftp.login()
        ftp.voidcmd("TYPE I")
        return ftp

    def _ftp_list_names(self, ftp: ftplib.FTP, directory: str) -> list[str]:
        current = ftp.pwd()
        try:
            ftp.cwd(directory or "/")
            return [posixpath.basename(name.rstrip("/")) for name in ftp.nlst() if name not in {".", ".."}]
        finally:
            try:
                ftp.cwd(current)
            except Exception:
                pass

    def _ftp_is_dir(self, ftp: ftplib.FTP, path: str) -> bool:
        current = ftp.pwd()
        try:
            ftp.cwd(path)
            return True
        except ftplib.all_errors:
            return False
        finally:
            try:
                ftp.cwd(current)
            except Exception:
                pass

    def _ftp_file_size(self, ftp: ftplib.FTP, path: str) -> int | None:
        try:
            ftp.voidcmd("TYPE I")
            size = ftp.size(path)
            return int(size) if size is not None else None
        except ftplib.all_errors:
            return None

    def _master_ftp_files_in_dir(self, ftp: ftplib.FTP, directory: str, options: dict, day: date):
        files = []
        try:
            names = self._ftp_list_names(ftp, directory)
        except ftplib.all_errors:
            return files
        for name in names:
            remote_path = self._ftp_join(directory, name)
            if not master_file_selected(
                name, options["selected_types"], options["keywords"], options["whole_folder"]
            ):
                continue
            if not master_file_matches_day(name, day) and posixpath.basename(directory) not in self._date_folder_names(day):
                continue
            size = self._ftp_file_size(ftp, remote_path)
            if size is None:
                continue
            files.append((remote_path, name, size))
        return files

    def _master_ftp_files_recursive(
        self, ftp: ftplib.FTP, directory: str, options: dict, max_depth: int = 8
    ):
        files = []
        stack = [(directory.rstrip("/") or "/", "")]
        while stack:
            if self.cancel_event.is_set():
                raise CancelledError()
            current, relative = stack.pop()
            try:
                names = self._ftp_list_names(ftp, current)
            except ftplib.all_errors as exc:
                raise CollectorError(f"FTP 目录无法列出，任务未完整：{current}（{exc}）") from exc
            for name in names:
                remote_path = self._ftp_join(current, name)
                rel_path = posixpath.join(relative, name) if relative else name
                if self._ftp_is_dir(ftp, remote_path):
                    if rel_path.count("/") < max_depth:
                        stack.append((remote_path, rel_path))
                    else:
                        raise CollectorError(f"FTP 目录层级超过 {max_depth} 层：{remote_path}")
                    continue
                if not master_file_selected(name, options["selected_types"], options["keywords"], True):
                    continue
                size = self._ftp_file_size(ftp, remote_path)
                if size is None:
                    raise CollectorError(f"无法读取 FTP 文件大小，任务未完整：{remote_path}")
                files.append((remote_path, rel_path, size))
        return files

    def _master_local_root_for_fan(self, host: str, fan: int, options: dict) -> Path | None:
        template = options.get("local_root", "").strip()
        if not template:
            return None
        rendered = template.replace("{ip}", host).replace("{fan}", str(fan)).replace("{fan3}", f"{fan:03d}")
        return Path(rendered)

    def _master_local_files_in_dir(self, directory: Path, options: dict, day: date):
        files = []
        try:
            children = list(directory.iterdir())
        except OSError:
            return files
        for source in children:
            if not source.is_file():
                continue
            if not master_file_selected(
                source.name, options["selected_types"], options["keywords"], options["whole_folder"]
            ):
                continue
            if not master_file_matches_day(source.name, day) and directory.name not in self._date_folder_names(day):
                continue
            files.append((source, Path(source.name)))
        return files

    def _master_local_files_recursive(self, directory: Path, options: dict):
        files = []
        for source in directory.rglob("*"):
            if not source.is_file():
                continue
            if not master_file_selected(source.name, options["selected_types"], options["keywords"], True):
                continue
            files.append((source, source.relative_to(directory)))
        return files

    def _locate_master_local_sources(self, root: Path, day: date, options: dict):
        if not root.exists() or not root.is_dir():
            raise CollectorError(f"本机来源目录不存在或不是文件夹：{root}")
        source_dir = self._locate_master_date_directory(root, day)
        if source_dir is not None:
            if options["whole_folder"]:
                self._emit("log", f"网络位置来源命中日期目录，按整文件夹递归扫描：{source_dir}")
                return source_dir, self._master_local_files_recursive(source_dir, options)
            self._emit("log", f"网络位置来源命中日期目录，按 B/F/O 扫描：{source_dir}")
            return source_dir, self._master_local_files_in_dir(source_dir, options, day)
        self._emit("log", f"网络位置来源未找到日期目录，尝试直接扫描根目录：{root}")
        return root, self._master_local_files_in_dir(root, options, day)

    def _locate_master_ftp_sources(self, ftp: ftplib.FTP, day: date, options: dict):
        expected = self._date_folder_names(day)
        roots = [options["remote_path"], options["remote_path"].strip("/"), *MASTER_FTP_LOG_ROOTS]
        for root in dict.fromkeys(item.rstrip("/") or "/" for item in roots):
            direct_files = self._master_ftp_files_in_dir(ftp, root, options, day)
            if direct_files:
                return root, direct_files
            try:
                children = self._ftp_list_names(ftp, root)
            except ftplib.all_errors:
                continue
            for name in children:
                if name in expected:
                    directory = self._ftp_join(root, name)
                    if options["whole_folder"]:
                        self._emit("log", f"FTP 命中日期目录，按整文件夹递归扫描：{directory}")
                        files = self._master_ftp_files_recursive(ftp, directory, options)
                    else:
                        files = self._master_ftp_files_in_dir(ftp, directory, options, day)
                    if files:
                        return directory, files
            for parent in children:
                parent_dir = self._ftp_join(root, parent)
                if not self._ftp_is_dir(ftp, parent_dir):
                    continue
                try:
                    nested = self._ftp_list_names(ftp, parent_dir)
                except ftplib.all_errors:
                    continue
                for name in nested:
                    if name in expected:
                        directory = self._ftp_join(parent_dir, name)
                        if options["whole_folder"]:
                            self._emit("log", f"FTP 命中日期目录，按整文件夹递归扫描：{directory}")
                            files = self._master_ftp_files_recursive(ftp, directory, options)
                        else:
                            files = self._master_ftp_files_in_dir(ftp, directory, options, day)
                        if files:
                            return directory, files
        for root in MASTER_OPERATION_ROOTS:
            files = self._master_ftp_files_in_dir(ftp, root, options, day)
            if files:
                return root, files
        return None, []

    def _master_sftp_files_in_dir(self, sftp, directory: str, options: dict, day: date):
        files = []
        try:
            entries = sftp.listdir_attr(directory)
        except OSError:
            return files
        for entry in entries:
            name = entry.filename
            if not stat.S_ISREG(entry.st_mode):
                continue
            if not master_file_selected(
                name, options["selected_types"], options["keywords"], options["whole_folder"]
            ):
                continue
            if not master_file_matches_day(name, day) and posixpath.basename(directory.rstrip("/")) not in self._date_folder_names(day):
                continue
            files.append((posixpath.join(directory.rstrip("/"), name), name, int(entry.st_size), int(entry.st_mtime)))
        return files

    def _master_sftp_files_recursive(self, sftp, directory: str, options: dict, max_depth: int = 8):
        files = []
        stack = [(directory.rstrip("/") or "/", "")]
        while stack:
            current, relative = stack.pop()
            try:
                entries = sftp.listdir_attr(current)
            except OSError as exc:
                self._emit("log", f"SFTP 目录无法列出：{current}（{exc}）")
                continue
            for entry in entries:
                name = entry.filename
                remote_path = posixpath.join(current.rstrip("/"), name)
                rel_path = posixpath.join(relative, name) if relative else name
                if stat.S_ISDIR(entry.st_mode):
                    if rel_path.count("/") < max_depth:
                        stack.append((remote_path, rel_path))
                    continue
                if not stat.S_ISREG(entry.st_mode):
                    continue
                if not master_file_selected(name, options["selected_types"], options["keywords"], True):
                    continue
                files.append((remote_path, rel_path, int(entry.st_size), int(entry.st_mtime)))
        return files

    def _locate_master_sftp_sources(self, sftp, day: date, options: dict):
        expected = self._date_folder_names(day)
        roots = [options["remote_path"], *MASTER_SFTP_LOG_ROOTS]
        for root in dict.fromkeys(item.rstrip("/") or "/" for item in roots):
            try:
                entries = sftp.listdir_attr(root)
            except OSError:
                continue
            files = self._master_sftp_files_in_dir(sftp, root, options, day)
            if files:
                return root, files
            for entry in entries:
                if not stat.S_ISDIR(entry.st_mode) or entry.filename not in expected:
                    continue
                directory = posixpath.join(root.rstrip("/"), entry.filename)
                if options["whole_folder"]:
                    self._emit("log", f"SFTP 命中日期目录，按整文件夹递归扫描：{directory}")
                    files = self._master_sftp_files_recursive(sftp, directory, options)
                else:
                    files = self._master_sftp_files_in_dir(sftp, directory, options, day)
                if files:
                    return directory, files
            for parent in entries:
                if not stat.S_ISDIR(parent.st_mode):
                    continue
                parent_dir = posixpath.join(root.rstrip("/"), parent.filename)
                try:
                    nested = sftp.listdir_attr(parent_dir)
                except OSError:
                    continue
                for entry in nested:
                    if not stat.S_ISDIR(entry.st_mode) or entry.filename not in expected:
                        continue
                    directory = posixpath.join(parent_dir.rstrip("/"), entry.filename)
                    if options["whole_folder"]:
                        self._emit("log", f"SFTP 命中日期目录，按整文件夹递归扫描：{directory}")
                        files = self._master_sftp_files_recursive(sftp, directory, options)
                    else:
                        files = self._master_sftp_files_in_dir(sftp, directory, options, day)
                    if files:
                        return directory, files
        return None, []

    def _copy_master_plan(self, fan: int, plan: list[dict], options: dict, protocol: str):
        copied = skipped = failed = total_size = 0
        plan_total = sum(item["size"] for item in plan)
        initial_done = 0
        if not options["overwrite"]:
            for item in plan:
                target = item["target"]
                if target.exists() and target.stat().st_size == item["size"]:
                    initial_done += item["size"]
                else:
                    initial_done += partial_resume_offset(
                        target, item["source_id"], item["size"], item["modified"]
                    )
        remaining = max(0, plan_total - initial_done)
        free = require_transfer_space(options["destination"], remaining)
        self._emit(
            "log",
            f"{fan:03d}号通过 {protocol} 已统计 {len(plan)} 个文件，共 {human_size(plan_total)}；"
            f"本次还需写入 {human_size(remaining)}，目标盘可用 {human_size(free)}。",
        )
        _progress_state, add_progress, refresh_progress = self._progress_tracker(plan_total, initial_done)

        for item in plan:
            if self.cancel_event.is_set():
                raise CancelledError()
            self._emit("status", f"主控 {fan:03d}号 {item['day']:%Y-%m-%d}：{item['name']}")
            try:
                if item["protocol"] == "smb":
                    result = copy_local_atomic(
                        item["source"],
                        item["target"],
                        options["overwrite"],
                        self.cancel_event.is_set,
                        add_progress,
                    )
                elif item["protocol"] == "ftp":
                    result = copy_ftp_atomic(
                        item["client"],
                        item["source"],
                        item["target"],
                        options["overwrite"],
                        self.cancel_event.is_set,
                        add_progress,
                    )
                else:
                    result = copy_sftp_atomic(
                        item["client"],
                        item["source"],
                        item["target"],
                        options["overwrite"],
                        self.cancel_event.is_set,
                        add_progress,
                    )
                total_size += result.size
                if result.status == "copied":
                    copied += 1
                    category_text = f"/{item['category']}" if item["category"] else ""
                    self._emit("log", f"已拷取：{fan:03d}号{category_text}/{item['name']}")
                    if result.resumed_from:
                        self._emit("log", f"断点续传：{item['name']}，从 {human_size(result.resumed_from)} 接续")
                else:
                    skipped += 1
                    self._emit("log", f"已跳过：{item['target']}")
                refresh_progress()
                self._emit("counter", copied, skipped, failed)
            except CancelledError:
                raise
            except Exception as exc:
                failed += 1
                refresh_progress()
                self._emit("log", f"文件失败：{item['source']}（{exc}）")
                self._emit("counter", copied, skipped, failed)
        return copied, skipped, failed, total_size

    def _collect_master_via_ftp(self, host: str, fan: int, options: dict, port: int, username: str, password: str, label: str):
        ftp = ftplib.FTP()
        ftp.encoding = "gbk"
        self._emit("log", f"{fan:03d}号尝试 {label}：{host}:{port}，目录={options['remote_path']}")
        ftp.connect(host, port, timeout=12)
        try:
            ftp.login(username or "anonymous", password or "")
            ftp.voidcmd("TYPE I")
            self._emit("log", f"{fan:03d}号 {label} 登录成功，开始访问目录 {options['remote_path']}")
            if options["test_only"]:
                root = options["remote_path"]
                names = self._ftp_list_names(ftp, root)
                return 0, 0, 0, 0, (
                    f"主控 BOF 连接测试通过：{fan:03d}号（{host}，{label}，"
                    f"路径 {root} 可访问，目录项 {len(names)} 个）"
                )
            plan = []
            for day in options["days"]:
                directory, files = self._locate_master_ftp_sources(ftp, day, options)
                if not files:
                    self._emit("log", f"{fan:03d}号 {day:%Y-%m-%d}：{label} 未找到 BOF 文件")
                    continue
                self._emit("log", f"{fan:03d}号 {day:%Y-%m-%d}：{label} 找到目录 {directory}")
                for remote_path, name, size in files:
                    display_name = posixpath.basename(name)
                    category = "" if options["whole_folder"] else (classify_master_file(display_name) or "OTHER")
                    target = master_output_directory(options["destination"], host, day, fan, category) / Path(name.replace("/", os.sep))
                    plan.append(
                        {
                            "protocol": "ftp",
                            "client": ftp,
                            "source": remote_path,
                            "source_id": f"ftp://{host}:{port}/{remote_path}",
                            "target": target,
                            "name": display_name,
                            "category": category,
                            "day": day,
                            "size": size,
                            "modified": 0,
                        }
                    )
            if not plan:
                raise NoMatchingFilesError(f"{label} 可连接但没有匹配的 BOF 文件")
            copied, skipped, failed, total_size = self._copy_master_plan(fan, plan, options, label)
            return copied, skipped, failed, total_size, None
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    def _collect_master_via_sftp(self, host: str, fan: int, options: dict, username: str, password: str, label: str):
        sftp_options = dict(options, host=host, port=22, username=username, password=password)
        self._emit("log", f"{fan:03d}号尝试 {label}：{host}:22，目录={options['remote_path']}")
        client, sftp = self._open_sftp_session(sftp_options)
        try:
            self._emit("log", f"{fan:03d}号 {label} 登录成功，开始访问目录 {options['remote_path']}")
            if options["test_only"]:
                root = options["remote_path"]
                names = sftp.listdir(root)
                return 0, 0, 0, 0, (
                    f"主控 BOF 连接测试通过：{fan:03d}号（{host}，{label}，"
                    f"路径 {root} 可访问，目录项 {len(names)} 个）"
                )
            plan = []
            for day in options["days"]:
                directory, files = self._locate_master_sftp_sources(sftp, day, options)
                if not files:
                    self._emit("log", f"{fan:03d}号 {day:%Y-%m-%d}：{label} 未找到 BOF 文件")
                    continue
                self._emit("log", f"{fan:03d}号 {day:%Y-%m-%d}：{label} 找到目录 {directory}")
                for remote_path, name, size, modified in files:
                    display_name = posixpath.basename(name)
                    category = "" if options["whole_folder"] else (classify_master_file(display_name) or "OTHER")
                    target = master_output_directory(options["destination"], host, day, fan, category) / Path(name.replace("/", os.sep))
                    plan.append(
                        {
                            "protocol": "sftp",
                            "client": sftp,
                            "source": remote_path,
                            "source_id": f"sftp://{host}:22{remote_path}",
                            "target": target,
                            "name": display_name,
                            "category": category,
                            "day": day,
                            "size": size,
                            "modified": modified,
                        }
                    )
            if not plan:
                raise NoMatchingFilesError(f"{label} 可连接但没有匹配的 BOF 文件")
            copied, skipped, failed, total_size = self._copy_master_plan(fan, plan, options, label)
            return copied, skipped, failed, total_size, None
        finally:
            try:
                sftp.close()
            finally:
                client.close()

    def _collect_master_via_local_folder(self, host: str, fan: int, options: dict):
        root = self._master_local_root_for_fan(host, fan, options)
        if root is None:
            raise CollectorError("未填写本机来源目录")
        self._emit("log", f"{fan:03d}号尝试网络位置/映射盘来源：{root}")
        plan = []
        for day in options["days"]:
            directory, files = self._locate_master_local_sources(root, day, options)
            if not files:
                self._emit("log", f"{fan:03d}号 {day:%Y-%m-%d}：网络位置来源未找到匹配文件（目录 {directory}）")
                continue
            self._emit("log", f"{fan:03d}号 {day:%Y-%m-%d}：网络位置来源找到目录 {directory}，文件 {len(files)} 个")
            for source, relative in files:
                category = "" if options["whole_folder"] else (classify_master_file(source.name) or "OTHER")
                target = master_output_directory(options["destination"], host, day, fan, category) / relative
                source_stat = source.stat()
                plan.append(
                    {
                        "protocol": "smb",
                        "source": source,
                        "source_id": str(source.resolve()),
                        "target": target,
                        "name": source.name,
                        "category": category,
                        "day": day,
                        "size": source_stat.st_size,
                        "modified": source_stat.st_mtime_ns,
                    }
                )
        if not plan:
            raise NoMatchingFilesError("网络位置/映射盘来源可访问但没有匹配的 BOF 文件")
        if options["test_only"]:
            return 0, 0, 0, 0, f"主控 BOF 连接测试通过：{fan:03d}号（{host}，网络位置/映射盘来源）"
        copied, skipped, failed, total_size = self._copy_master_plan(fan, plan, options, "网络位置/映射盘来源")
        return copied, skipped, failed, total_size, None

    def _collect_master_via_smb(self, host: str, fan: int, options: dict):
        share = rf"\\{host}\C$"
        log_path_text = "\\".join(options["log_parts"])
        self._emit("log", f"尝试 C$ 共享兜底：共享={share}，日志根目录={log_path_text}")
        with SmbConnection(share, options["username"], options["password"]) as smb_connection:
            log_root = Path(share).joinpath(*options["log_parts"])
            if not log_root.is_dir():
                raise CollectorError(f"找不到主控日志目录：{log_root}")
            self._emit(
                "log",
                f"{fan:03d}号 C$ 共享连接成功：{host}（账号格式：{smb_connection.effective_username or '当前会话'}）",
            )
            plan = []
            for day in options["days"]:
                source_dir = self._locate_master_date_directory(log_root, day)
                if source_dir is None:
                    self._emit("log", f"{fan:03d}号 {day:%Y-%m-%d}：C$ 未找到日期目录")
                    continue
                self._emit("log", f"{fan:03d}号 {day:%Y-%m-%d}：C$ 找到目录 {source_dir}")
                sources = source_dir.rglob("*") if options["whole_folder"] else source_dir.iterdir()
                for source in sources:
                    if not source.is_file() or not master_file_selected(
                        source.name,
                        options["selected_types"],
                        options["keywords"],
                        options["whole_folder"],
                    ):
                        continue
                    category = "" if options["whole_folder"] else (classify_master_file(source.name) or "OTHER")
                    relative = source.relative_to(source_dir) if options["whole_folder"] else Path(source.name)
                    target = master_output_directory(options["destination"], host, day, fan, category) / relative
                    source_stat = source.stat()
                    plan.append(
                        {
                            "protocol": "smb",
                            "source": source,
                            "source_id": str(source.resolve()),
                            "target": target,
                            "name": source.name,
                            "category": category,
                            "day": day,
                            "size": source_stat.st_size,
                            "modified": source_stat.st_mtime_ns,
                        }
                    )
            if not plan:
                raise NoMatchingFilesError("C$ 共享可连接但没有匹配的 BOF 文件")
            if options["test_only"]:
                return 0, 0, 0, 0, f"主控 BOF 连接测试通过：{fan:03d}号（{host}，C$共享）"
            copied, skipped, failed, total_size = self._copy_master_plan(fan, plan, options, "C$共享")
            return copied, skipped, failed, total_size, None

    def _master_connection_attempts(self, host: str, fan: int, options: dict):
        ftp_label = "主控FTP(21/填写账号)" if options["username"] else "主控FTP(21/匿名)"
        attempts = []
        if options.get("local_root", "").strip():
            attempts.append(lambda: self._collect_master_via_local_folder(host, fan, options))
        attempts.append(
            lambda: self._collect_master_via_ftp(
                host, fan, options, 21, options["username"], options["password"], ftp_label
            )
        )
        attempts.append(lambda: self._collect_master_via_smb(host, fan, options))
        return attempts

    def _run_master_target(self, host: str, fan: int, options: dict):
        copied = skipped = failed = offline = 0
        total_size = 0
        port_state = {}
        if self.cancel_event.is_set():
            raise CancelledError()
        self._emit("status", f"正在连接主控 {fan:03d}号（{host}）")
        try:
            log_path_text = "\\".join(options["log_parts"])
            date_text = ",".join(day.strftime("%Y%m%d") for day in options["days"])
            type_text = "整日期文件夹" if options["whole_folder"] else ",".join(options["selected_types"])
            port_state = {
                "FTP21": self._probe_tcp(host, 21),
                "SMB445": self._probe_tcp(host, 445),
                "RDP3389": self._probe_tcp(host, 3389),
            }
            source_text = "填写的共享目录、FTP21、系统共享自动尝试"
            self._emit(
                "log",
                f"主控参数：IP={host}，来源自动选择={source_text}，远程路径={options['remote_path']}，日期={date_text}，类型={type_text}",
            )
            if port_state:
                self._emit(
                    "log",
                    "主控端口探测："
                    + "，".join(f"{name}={'可达' if ok else '不可达'}" for name, ok in port_state.items()),
                )
            attempts = self._master_connection_attempts(host, fan, options)
            errors = []
            no_file_messages = []
            for attempt in attempts:
                try:
                    result = attempt()
                    copied += result[0]
                    skipped += result[1]
                    failed += result[2]
                    total_size += result[3]
                    if result[4]:
                        return result[4]
                    break
                except CancelledError:
                    raise
                except NoMatchingFilesError as exc:
                    no_file_messages.append(str(exc))
                    self._emit("log", f"{fan:03d}号无匹配文件：{exc}")
                except Exception as exc:
                    errors.append(str(exc))
                    self._emit("log", f"{fan:03d}号连接失败：{exc}")
            else:
                if no_file_messages and not errors:
                    self._emit("log", f"{fan:03d}号主控可连接，但所选日期/类型没有文件。")
                    return copied, skipped, failed, offline, total_size
                raise CollectorError("；".join((errors + no_file_messages)[-4:]))
        except CancelledError:
            raise
        except InsufficientSpaceError:
            raise
        except Exception as exc:
            offline += 1
            self._emit("log", f"{fan:03d}号主控失败：{exc}")
            if port_state.get("RDP3389"):
                self._emit(
                    "log",
                    f"诊断：{host} 的 RDP3389 可达，但远程桌面不是自动文件传输通道；"
                    "填写的共享目录、FTP21 和 SMB445 均未完成本次考取，只能进入远程桌面人工复制。",
                )
            self._emit("counter", copied, skipped, failed + offline)

        return copied, skipped, failed, offline, total_size

    def _run_master(self, options: dict) -> str:
        if options.get("test_only"):
            result = self._run_master_target(*options["targets"][0], options)
            if isinstance(result, str):
                return result
            copied, skipped, failed, offline, total_size = result
            return (
                f"主控完成：拷取 {copied}，跳过 {skipped}，文件失败 {failed}，"
                f"连接失败 {offline}，共 {human_size(total_size)}"
            )

        copied = skipped = failed = offline = 0
        total_size = 0
        targets = list(options["targets"])
        max_workers = max(1, min(int(options.get("max_workers", 1)), len(targets), 8))
        self._emit("log", f"主控批量任务：{len(targets)} 台，程序自动调度；失败的风机会单独记录并继续。")
        if max_workers == 1:
            for host, fan in targets:
                result = self._run_master_target(host, fan, options)
                if isinstance(result, str):
                    return result
                c, s, f, o, size = result
                copied += c
                skipped += s
                failed += f
                offline += o
                total_size += size
                self._emit("counter", copied, skipped, failed + offline)
        else:
            retry_targets = []
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(self._run_master_target, host, fan, options): (host, fan)
                    for host, fan in targets
                }
                for future in as_completed(futures):
                    if self.cancel_event.is_set():
                        raise CancelledError()
                    host, fan = futures[future]
                    try:
                        result = future.result()
                        if isinstance(result, str):
                            self._emit("log", result)
                            continue
                        c, s, f, o, size = result
                        if o and c == 0 and s == 0 and f == 0:
                            retry_targets.append((host, fan))
                            self._emit("log", f"{fan:03d}号主控并行连接失败，稍后降级为单连接重试。")
                        else:
                            copied += c
                            skipped += s
                            failed += f
                            offline += o
                            total_size += size
                    except CancelledError:
                        raise
                    except Exception as exc:
                        retry_targets.append((host, fan))
                        self._emit("log", f"{fan:03d}号主控任务失败：{host}（{exc}）")
                    self._emit("counter", copied, skipped, failed + offline)
            for host, fan in retry_targets:
                self._emit("log", f"{fan:03d}号主控降级为单连接顺序重试。")
                try:
                    result = self._run_master_target(host, fan, options)
                except CancelledError:
                    raise
                except Exception as exc:
                    offline += 1
                    self._emit("log", f"{fan:03d}号主控单连接兜底仍失败，继续下一台：{exc}")
                    self._emit("counter", copied, skipped, failed + offline)
                    continue
                if isinstance(result, str):
                    self._emit("log", result)
                    continue
                c, s, f, o, size = result
                copied += c
                skipped += s
                failed += f
                offline += o
                total_size += size
                self._emit("counter", copied, skipped, failed + offline)
        return (
            f"主控完成：拷取 {copied}，跳过 {skipped}，文件失败 {failed}，"
            f"连接失败 {offline}，共 {human_size(total_size)}"
        )

    def _emit(self, event: str, *values):
        self.events.put((event, *values))

    def _drain_events(self):
        try:
            for _ in range(200):
                event, *values = self.events.get_nowait()
                if event == "log":
                    self._append_log(values[0])
                elif event == "debug":
                    self._append_log(values[0], persist=False)
                elif event == "status":
                    self.status_text.set(values[0])
                elif event == "counter":
                    copied, skipped, failed = values
                    self.counter_text.set(f"{copied} 完成 / {skipped} 跳过 / {failed} 失败")
                elif event == "progress_total":
                    total, completed = values
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=max(total, 1), value=completed)
                    self.transfer_text.set(f"{human_size(completed)} / {human_size(total)}")
                elif event == "progress_bytes":
                    completed, total = values
                    self.progress.configure(value=min(completed, max(total, 1)))
                    self.transfer_text.set(f"{human_size(completed)} / {human_size(total)}")
                elif event == "done":
                    self._append_log(values[0])
                    self.status_text.set(values[0])
                    self._set_running(False)
                    messagebox.showinfo("任务完成", values[0], parent=self)
                elif event == "cancelled":
                    self._append_log(values[0])
                    self.status_text.set("任务已停止")
                    self._set_running(False)
                elif event == "failed":
                    self.status_text.set("任务失败")
                    self._set_running(False)
                    messagebox.showerror("任务失败", values[0], parent=self)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _append_log(self, message: str, persist: bool = True):
        timestamp = __import__("datetime").datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line)
        visible_lines = int(self.log_box.index("end-1c").split(".")[0])
        if visible_lines > 2500:
            self.log_box.delete("1.0", "501.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        if persist and self.current_log:
            try:
                self.current_log.parent.mkdir(parents=True, exist_ok=True)
                with self.current_log.open("a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError:
                pass

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.counter_text.set("0 个文件")
        self.transfer_text.set("正在统计…")

    def _set_running(self, running: bool):
        state = "disabled" if running else "normal"
        for button in (
            self.transient_test_button,
            self.transient_start_button,
            self.master_test_button,
            self.master_start_button,
            self.pitch_test_button,
            self.pitch_start_button,
            self.vibration_test_button,
            self.vibration_start_button,
        ):
            button.configure(state=state)
        self.cancel_button.configure(state="normal" if running else "disabled")
        if running:
            self.progress.configure(mode="indeterminate", maximum=100, value=0)
            self.progress.start(12)
            self.transfer_text.set("正在统计…")
        else:
            self.progress.stop()

    def _cancel(self):
        self.cancel_event.set()
        self.status_text.set("正在停止…")
        self.cancel_button.configure(state="disabled")

    def _save_settings(self):
        network_mode = getattr(self, "network_mode_var", None)
        network_addresses = getattr(self, "network_rows", None)
        if network_mode is not None and network_addresses is not None:
            saved_network_addresses = [
                (row["ip"].get().strip(), row["mask"].get().strip())
                for row in network_addresses
                if row["ip"].get().strip() or row["mask"].get().strip()
            ]
            self.settings["network_mode"] = network_mode.get()
            self.settings["network_addresses"] = saved_network_addresses
        data = {
            "transient_destination": self.transient_destination.get(),
            "master_destination": self.master_destination.get(),
            "transient_host": self.transient_host.get(),
            "transient_port": self.transient_port.get(),
            "transient_username": self.transient_username.get(),
            "transient_password": self.transient_password.get(),
            "transient_template": self.transient_template.get(),
            "transient_local_root": self.transient_local_root.get(),
            "transient_fans": self.transient_fans.get(),
            "transient_layout": self.transient_layout.get(),
            "master_prefix": self.master_prefix.get(),
            "master_ip_range": self.master_ip_range.get(),
            "master_remote_path": self.master_remote_path.get(),
            "master_local_root": self.master_local_root.get(),
            "master_log_path": self.master_log_path.get(),
            "master_username": self.master_username.get(),
            "master_password": self.master_password.get(),
            "master_fans": self.master_fans.get(),
            "master_keywords": self.master_keywords.get(),
            "master_whole_folder": self.master_whole_folder.get(),
            "pitch_host_prefix": self.pitch_host_prefix.get(),
            "pitch_port": self.pitch_port.get(),
            "pitch_username": self.pitch_username.get(),
            "pitch_password": self.pitch_password.get(),
            "pitch_remote_path": self.pitch_remote_path.get(),
            "pitch_fans": self.pitch_fans.get(),
            "pitch_destination": self.pitch_destination.get(),
            "vibration_host": self.vibration_host.get(),
            "vibration_port": self.vibration_port.get(),
            "vibration_username": self.vibration_username.get(),
            "vibration_password": self.vibration_password.get(),
            "vibration_remote_base": self.vibration_remote_base.get(),
            "vibration_local_root": self.vibration_local_root.get(),
            "vibration_site": self.vibration_site.get(),
            "vibration_fans": self.vibration_fans.get(),
            "vibration_destination": self.vibration_destination.get(),
            "vibration_types": [
                kind for kind in VIBRATION_TYPES if self.vibration_types[kind].get()
            ],
            "network_mode": self.settings.get("network_mode", "auto"),
            "network_adapter_index": self.settings.get("network_adapter_index"),
            "network_addresses": self.settings.get("network_addresses", DEFAULTS["network_addresses"]),
        }
        save_json_atomic(SETTINGS_FILE, data)

    def _save_settings_now(self):
        try:
            self._save_settings()
            self.status_text.set("设置已保存（密码保存在本机用户配置中）")
        except OSError as exc:
            messagebox.showerror("保存失败", f"无法保存设置：{exc}", parent=self)

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("任务运行中", "任务仍在运行，确定停止并退出吗？", parent=self):
                return
            self.cancel_event.set()
        self._save_settings()
        self.destroy()


def packaged_self_test(result_file: Path) -> int:
    """Exercise packaged dependencies and core rules without opening any network."""
    import json
    import tempfile

    results = {}
    try:
        import paramiko

        results["paramiko"] = paramiko.__version__
        results["private_ip"] = require_private_ipv4("192.168.149.222")
        results["fans"] = parse_fan_spec("1-3,7,9,11")
        results["dates"] = [
            item.strftime("%Y%m%d")
            for item in iter_dates(parse_date_value("20260817"), parse_date_value("20260819"))
        ]
        results["transient"] = transient_file_fan(
            "real_650227011_20260818.arc", "20260818"
        )
        results["master"] = master_file_selected(
            "F20260817_224000_VibrWarn.html", ["F"], ["vibrwarn"]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "O20260817.csv"
            target = root / "out" / source.name
            source.write_bytes(b"offline-self-test")
            copied = copy_local_atomic(source, target, overwrite=False)
            results["copy"] = copied.status
            results["copy_content"] = target.read_text(encoding="ascii")
        window = WindCollectorApp()
        window.withdraw()
        window.update_idletasks()
        results["ui_title"] = window.title()
        results["ui_required_size"] = [window.winfo_reqwidth(), window.winfo_reqheight()]
        results["transient_destination_editable"] = bool(window.transient_destination.get())
        results["master_destination_editable"] = bool(window.master_destination.get())
        results["master_ip_prefix"] = window.master_prefix.get()
        results["master_fan_input"] = window.master_ip_range.get()
        results["save_settings_button"] = window.save_settings_button.cget("text")
        results["master_source_selection"] = "auto"
        results["automatic_concurrency"] = {
            "master_workers": MASTER_AUTO_WORKERS,
            "sftp_workers": SFTP_AUTO_WORKERS,
            "serial_fallback": True,
        }
        results["network_presets"] = list(NETWORK_PRESETS)
        results["network_default_rows"] = len(DEFAULTS["network_addresses"])
        results["pitch_defaults"] = {
            "host_prefix": DEFAULTS["pitch_host_prefix"],
            "port": DEFAULTS["pitch_port"],
            "remote_path": DEFAULTS["pitch_remote_path"],
        }
        results["vibration_defaults"] = {
            "host": DEFAULTS["vibration_host"],
            "port": DEFAULTS["vibration_port"],
            "remote_base": DEFAULTS["vibration_remote_base"],
            "sibling_types": list(VIBRATION_TYPES),
        }
        results["event_queue_limit"] = window.events.maxsize
        results["server_directory_preview"] = window.transient_preview.get()
        window.start_date.set("2027-03-05")
        window.end_date.set("2027-03-05")
        window.update_idletasks()
        results["server_directory_2027"] = window.transient_preview.get()
        for index in range(2605):
            window._append_log(f"stress-{index}", persist=False)
        visible_lines = int(window.log_box.index("end-1c").split(".")[0])
        if visible_lines > 2500:
            raise AssertionError(f"Visible log limit failed: {visible_lines}")
        results["visible_log_lines_after_stress"] = visible_lines
        window.destroy()
        results["status"] = "PASS"
        exit_code = 0
    except Exception:
        results["status"] = "FAIL"
        results["traceback"] = traceback.format_exc()
        exit_code = 1
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


def main():
    if "--self-test" in sys.argv:
        index = sys.argv.index("--self-test")
        result = Path(sys.argv[index + 1]) if len(sys.argv) > index + 1 else Path.cwd() / "self-test.json"
        raise SystemExit(packaged_self_test(result))
    WindCollectorApp().mainloop()


if __name__ == "__main__":
    main()
