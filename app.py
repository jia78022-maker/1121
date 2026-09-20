"""客户管理器 - Windows 本地客户、订单与交期管理工具。"""
import csv
import calendar
import ctypes
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import sys
import ssl
import tempfile
import time
import threading
import urllib.error
import urllib.request
import winreg
import tkinter as tk
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    # 在高分辨率 Windows 显示器上保持字体与控件锐利，必须在创建 Tk 窗口前调用。
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except AttributeError:
        pass


# 打包为单文件 EXE 时，__file__ 位于临时解压目录；数据应保存在 EXE 同级目录。
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DB_PATH = APP_DIR / "delivery_manager.db"
ACCOUNT_PATH = APP_DIR / "account.json"
SESSION_PATH = APP_DIR / "session.json"
UI_SETTINGS_PATH = APP_DIR / "ui_settings.json"
REMINDER_LOG_PATH = APP_DIR / "reminder_log.json"
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "客户管理器"
APP_VERSION = "1.1.5"
GITHUB_REPOSITORY = "jia78022-maker/1121"
GITHUB_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
CLOUD_API_BASE = "https://api.songpornsongx.top"


def github_ssl_context():
    """Use a bundled public CA set so packaged Windows builds can verify GitHub TLS."""
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    ca_bundle = bundle_root / "certs" / "cacert.pem"
    return ssl.create_default_context(cafile=str(ca_bundle))

THEMES = {
    "light": {"bg": "#f3f6fb", "surface": "#ffffff", "border": "#dce5f0", "text": "#1e293b", "muted": "#64748b", "accent": "#2563eb", "accent_active": "#1d4ed8", "head": "#eaf1fb", "selected": "#bfdbfe"},
    "dark": {"bg": "#111827", "surface": "#1f2937", "border": "#374151", "text": "#f1f5f9", "muted": "#a8b5c7", "accent": "#60a5fa", "accent_active": "#3b82f6", "head": "#273548", "selected": "#334e72"},
}


def password_digest(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()


def load_ui_settings():
    try:
        settings = json.loads(UI_SETTINGS_PATH.read_text(encoding="utf-8"))
        return {"theme": settings.get("theme", "light"), "show_price": settings.get("show_price", True)}
    except (OSError, json.JSONDecodeError):
        return {"theme": "light", "show_price": True}


def save_ui_settings(theme, show_price):
    UI_SETTINGS_PATH.write_text(json.dumps({"theme": theme, "show_price": show_price}, ensure_ascii=False), encoding="utf-8")


def load_reminder_log():
    try:
        return set(json.loads(REMINDER_LOG_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return set()


def save_reminder_log(entries):
    # 仅保留近 45 天的记录，避免本地日志无限增长。
    cutoff = (date.today() - timedelta(days=45)).isoformat()
    kept = sorted(entry for entry in entries if entry[:10] >= cutoff)
    REMINDER_LOG_PATH.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")


def cloud_post(path, payload, token=None):
    headers = {"Content-Type": "application/json", "User-Agent": "CustomerManager/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(CLOUD_API_BASE + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError("无法连接云端服务：" + str(error)) from error
    if not result.get("ok"):
        raise RuntimeError(result.get("message", "云端服务操作失败。"))
    return result


def autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{Path(__file__).resolve()}"'


def autostart_is_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            return winreg.QueryValueEx(key, AUTOSTART_NAME)[0] == autostart_command()
    except FileNotFoundError:
        return False


def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass


class LoginDialog(tk.Toplevel):
    """本地离线账号的首次创建及后续登录窗口。"""
    def __init__(self, parent):
        super().__init__(parent)
        self.success = False
        self.account_exists = ACCOUNT_PATH.exists()
        self.title("客户管理器 - " + ("本地登录" if self.account_exists else "创建本地账号"))
        self.resizable(False, False)
        self.configure(bg="white")
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self._build()
        self.after(120, lambda: self.username.focus_set())

    def _build(self):
        for child in self.winfo_children():
            child.destroy()
        frame = tk.Frame(self, bg="white", padx=42, pady=30)
        frame.pack()
        title = "欢迎回来" if self.account_exists else "创建本地账号"
        subtitle = "账号与订单数据都只保存在当前电脑。" if not self.account_exists else "离线登录，数据始终在本机。"
        tk.Label(frame, text=title, bg="white", fg="#173b6c", font=("Microsoft YaHei UI", 16, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(frame, text=subtitle, bg="white", fg="#64748b", font=("Microsoft YaHei UI", 9)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 16))
        tk.Label(frame, text="账号", bg="white", fg="#334155", font=("Microsoft YaHei UI", 10)).grid(row=2, column=0, sticky="w", pady=7)
        self.username = ttk.Entry(frame, width=28, font=("Microsoft YaHei UI", 10))
        self.username.grid(row=2, column=1, pady=7)
        tk.Label(frame, text="密码", bg="white", fg="#334155", font=("Microsoft YaHei UI", 10)).grid(row=3, column=0, sticky="w", pady=7)
        self.password = ttk.Entry(frame, width=28, show="●", font=("Microsoft YaHei UI", 10))
        self.password.grid(row=3, column=1, pady=7)
        options_row = 4
        if not self.account_exists:
            tk.Label(frame, text="确认密码", bg="white", fg="#334155", font=("Microsoft YaHei UI", 10)).grid(row=4, column=0, sticky="w", pady=7)
            self.confirm = ttk.Entry(frame, width=28, show="●", font=("Microsoft YaHei UI", 10))
            self.confirm.grid(row=4, column=1, pady=7)
            options_row = 5
        self.remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="保持登录状态", variable=self.remember).grid(row=options_row, column=1, sticky="w", pady=(8, 12))
        ttk.Button(frame, text="取消", command=self.cancel).grid(row=options_row + 1, column=0, sticky="w")
        ttk.Button(frame, text="创建并登录" if not self.account_exists else "登录", style="Accent.TButton", command=self.submit).grid(row=options_row + 1, column=1, sticky="e")
        self.bind("<Return>", lambda _event: self.submit())

    def submit(self):
        username, password = self.username.get().strip(), self.password.get()
        if len(username) < 2 or len(password) < 4:
            messagebox.showwarning("账号信息不完整", "账号至少 2 个字符，密码至少 4 个字符。", parent=self)
            return
        if not self.account_exists and password != self.confirm.get():
            messagebox.showwarning("密码不一致", "两次输入的密码不一致。", parent=self)
            return
        if self.account_exists:
            try:
                account = json.loads(ACCOUNT_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                messagebox.showerror("账号文件无法读取", "账号数据已损坏，请联系管理员。", parent=self)
                return
            if username != account.get("username") or password_digest(password, account.get("salt", "")) != account.get("password_hash"):
                messagebox.showerror("登录失败", "账号或密码不正确。", parent=self)
                return
        else:
            salt = os.urandom(16).hex()
            ACCOUNT_PATH.write_text(json.dumps({"username": username, "salt": salt, "password_hash": password_digest(password, salt)}, ensure_ascii=False), encoding="utf-8")
        if self.remember.get():
            SESSION_PATH.write_text(json.dumps({"username": username}, ensure_ascii=False), encoding="utf-8")
        else:
            SESSION_PATH.unlink(missing_ok=True)
        self.success = True
        self.destroy()

    def cancel(self):
        self.destroy()


class CloudLoginDialog(tk.Toplevel):
    """手机号/密码云端账户入口；令牌只保存在本机，会话可随时退出。"""
    def __init__(self, parent):
        super().__init__(parent)
        self.success = False
        self.token = None
        self.title("客户管理器 - 云端登录")
        self.configure(bg="white")
        self.resizable(False, False)
        self.transient(parent); self.grab_set()
        frame = tk.Frame(self, bg="white", padx=42, pady=32); frame.pack()
        tk.Label(frame, text="登录客户管理器", bg="white", fg="#172033", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        tk.Label(frame, text="订单将加密传输并同步到你的云端账户。", bg="white", fg="#64748b", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(5, 20))
        tk.Label(frame, text="手机号", bg="white", fg="#334155", font=("Microsoft YaHei UI", 10)).pack(anchor="w")
        self.phone = ttk.Entry(frame, width=31, font=("Microsoft YaHei UI", 10)); self.phone.pack(fill="x", pady=(5, 13))
        tk.Label(frame, text="密码", bg="white", fg="#334155", font=("Microsoft YaHei UI", 10)).pack(anchor="w")
        self.password = ttk.Entry(frame, width=31, show="●", font=("Microsoft YaHei UI", 10)); self.password.pack(fill="x", pady=(5, 18))
        actions = tk.Frame(frame, bg="white"); actions.pack(fill="x")
        ttk.Button(actions, text="注册", command=lambda: self.submit("/api/register")).pack(side="left")
        ttk.Button(actions, text="登录", style="Accent.TButton", command=lambda: self.submit("/api/login")).pack(side="right")
        self.bind("<Return>", lambda _event: self.submit("/api/login"))
        self.after(100, self.phone.focus_set)

    def submit(self, endpoint):
        phone, password = self.phone.get().strip(), self.password.get()
        if not phone.isdigit() or not 6 <= len(phone) <= 20 or len(password) < 6:
            messagebox.showwarning("信息格式不正确", "请输入 6 至 20 位手机号，以及至少 6 位密码。", parent=self)
            return
        try:
            result = cloud_post(endpoint, {"username": phone, "password": password})
        except RuntimeError as error:
            messagebox.showerror("云端登录失败", str(error), parent=self)
            return
        self.success, self.token = True, result["token"]
        SESSION_PATH.write_text(json.dumps({"username": phone, "token": self.token, "cloud": True}, ensure_ascii=False), encoding="utf-8")
        self.destroy()


class DeliveryApp(tk.Tk):
    FIELDS = [
        ("客户名称", "customer", True),
        ("联系人", "contact", False),
        ("联系电话", "phone", False),
        ("产品名称", "product", True),
        ("产品型号 / 规格", "spec", False),
        ("数量", "quantity", False),
        ("产品单价", "price", False),
        ("交货天数", "delivery_days", True),
        ("订单状态", "status", False),
    ]

    def __init__(self):
        super().__init__()
        self.withdraw()
        self.title("客户管理器")
        self.minsize(620, 500)
        self.center_window(1440, 900)
        self.configure(bg="#f4f7fb")
        self.ui_settings = load_ui_settings()
        self.theme_name = self.ui_settings["theme"] if self.ui_settings["theme"] in THEMES else "light"
        self.price_visible = bool(self.ui_settings["show_price"])
        self.selected_id = None
        self.notified_record_ids = set()
        self.reminder_log = load_reminder_log()
        self.autostart_enabled = autostart_is_enabled()
        self.speech_lock = threading.Lock()
        self.update_check_running = False
        self.entries = {}
        self._connect_db()
        self._setup_style()
        self._build_ui()
        self.apply_theme()
        self.refresh_table()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.overrideredirect(True)
        self.bind_all("<Motion>", self._resize_cursor, add="+")
        self.bind_all("<ButtonPress-1>", self._begin_resize, add="+")
        self.bind_all("<B1-Motion>", self._perform_resize, add="+")
        self.bind_all("<ButtonRelease-1>", self._end_resize, add="+")
        self.bind("<Configure>", self._responsive_layout, add="+")

    def authenticate(self):
        """恢复云端会话，或要求手机号/密码登录。"""
        self.cloud_token = None
        try:
            session = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
            self.cloud_token = session.get("token") if session.get("cloud") else None
        except (OSError, json.JSONDecodeError):
            pass
        self.deiconify()
        if not self.cloud_token:
            dialog = CloudLoginDialog(self)
            self.wait_window(dialog)
            if not dialog.success:
                return False
            self.cloud_token = dialog.token
        self.after(300, self.sync_from_cloud)
        self.after(500, self.schedule_next_alarm)
        # 启动后后台检查，不阻塞离线订单录入。
        self.after(900, self.check_for_updates)
        return True

    @staticmethod
    def _version_key(value):
        """将 v1.2.3 形式的版本号转为可比较数字；非数字片段不参与版本升级。"""
        parts = str(value or "").strip().lstrip("vV").split(".")
        key = []
        for part in parts[:4]:
            digits = "".join(char for char in part if char.isdigit())
            key.append(int(digits or 0))
        return tuple((key + [0, 0, 0, 0])[:4])

    def check_for_updates(self, manual=False):
        """从本仓库 GitHub Releases 后台读取最新发布版本。"""
        if self.update_check_running:
            return
        self.update_check_running = True
        if hasattr(self, "update_button"):
            self.update_button.configure(text="↻  正在检查…", state="disabled")

        def worker():
            try:
                request = urllib.request.Request(GITHUB_RELEASE_API, headers={"Accept": "application/vnd.github+json", "User-Agent": "CustomerManager-Updater"})
                with urllib.request.urlopen(request, timeout=8, context=github_ssl_context()) as response:
                    release = json.loads(response.read().decode("utf-8"))
                version = release.get("tag_name") or release.get("name") or ""
                newer = self._version_key(version) > self._version_key(APP_VERSION)
                result = {"release": release, "version": version, "newer": newer}
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as error:
                result = {"error": str(error)}
            self.after(0, lambda: self._finish_update_check(result, manual))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_update_check(self, result, manual):
        self.update_check_running = False
        if hasattr(self, "update_button") and self.update_button.winfo_exists():
            self.update_button.configure(text="↻  检查更新", state="normal")
        if result.get("error"):
            if manual:
                messagebox.showwarning("检查更新失败", "无法连接 GitHub Releases。请检查网络后重试。\n\n" + result["error"], parent=self)
            return
        if not result["newer"]:
            if manual:
                messagebox.showinfo("已是最新版本", f"当前版本 {APP_VERSION} 已是最新版本。", parent=self)
            return
        release = result["release"]
        notes = (release.get("body") or "暂无更新说明").strip()
        prompt = f"发现新版本 {result['version']}（当前 {APP_VERSION}）\n\n{notes[:700]}\n\n现在下载并安装吗？"
        if messagebox.askyesno("发现新版本", prompt, parent=self):
            self.download_and_install_update(release)

    def download_and_install_update(self, release):
        """下载 Release 附件并以 SHA-256 校验后交由独立脚本替换正在运行的 EXE。"""
        if not getattr(sys, "frozen", False):
            messagebox.showinfo("开发环境", "检测到新版本。打包后的客户管理器.exe 会自动下载安装；当前源码运行模式不执行替换。", parent=self)
            return
        assets = release.get("assets", [])
        executable = next((item for item in assets if item.get("name", "").lower().endswith(".exe")), None)
        checksum = next((item for item in assets if item.get("name", "").lower().endswith((".sha256", ".sha256.txt"))), None)
        if not executable or not checksum:
            messagebox.showwarning("发布文件不完整", "该 GitHub Release 必须同时包含“客户管理器.exe”和对应的“.sha256”校验文件，已取消安装。", parent=self)
            return
        self.update_button.configure(text="↓  正在下载…", state="disabled")

        def worker():
            try:
                headers = {"User-Agent": "CustomerManager-Updater"}
                with urllib.request.urlopen(urllib.request.Request(executable["browser_download_url"], headers=headers), timeout=90, context=github_ssl_context()) as response:
                    package = response.read()
                with urllib.request.urlopen(urllib.request.Request(checksum["browser_download_url"], headers=headers), timeout=20, context=github_ssl_context()) as response:
                    expected = response.read().decode("utf-8").strip().split()[0].lower()
                actual = hashlib.sha256(package).hexdigest().lower()
                if not expected or actual != expected:
                    raise ValueError("下载文件的 SHA-256 校验失败")
                package_path = Path(tempfile.gettempdir()) / "客户管理器-update.exe"
                package_path.write_bytes(package)
                self.after(0, lambda: self._replace_with_update(package_path))
            except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as error:
                self.after(0, lambda: self._update_install_failed(str(error)))

        threading.Thread(target=worker, daemon=True).start()

    def _update_install_failed(self, detail):
        if hasattr(self, "update_button") and self.update_button.winfo_exists():
            self.update_button.configure(text="↻  检查更新", state="normal")
        messagebox.showerror("更新失败", "下载或校验新版本时出错，当前版本未被修改。\n\n" + detail, parent=self)

    def _replace_with_update(self, package_path):
        target = Path(sys.executable).resolve()
        script = Path(tempfile.gettempdir()) / "客户管理器-update.bat"
        # /b 等待当前进程结束后再覆盖，随后立即启动新程序；失败不会删除旧程序。
        script.write_text(f"@echo off\r\ntimeout /t 2 /nobreak >nul\r\nmove /y \"{package_path}\" \"{target}\" >nul\r\nstart \"\" \"{target}\"\r\ndel \"%~f0\"\r\n", encoding="gbk", errors="replace")
        subprocess.Popen(["cmd.exe", "/c", str(script)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.on_close()

    def sync_from_cloud(self):
        if not getattr(self, "cloud_token", None): return
        try:
            result = cloud_post("/api/sync/pull", {}, self.cloud_token)
            records = result.get("records", [])
            inventory = result.get("inventory", {})
            if records:
                self.conn.execute("DELETE FROM records")
                columns = ("customer", "contact", "phone", "product", "spec", "quantity", "price", "delivery_date", "status", "notes", "created_at", "shipping_at")
                for row in records:
                    self.conn.execute("INSERT INTO records(" + ",".join(columns) + ") VALUES (" + ",".join("?" * len(columns)) + ")", tuple(row.get(key, "") for key in columns))
                self.conn.execute("DELETE FROM stocking_items")
                self.conn.execute("""INSERT INTO stocking_items(record_id, customer, product, spec, quantity, delivery_date, stock_status, updated_at)
                    SELECT id, customer, product, spec, quantity, delivery_date, '待备货', created_at FROM records""")
            has_inventory = isinstance(inventory, dict) and bool(inventory)
            if has_inventory:
                self._import_inventory(inventory)
            if records or has_inventory:
                self.conn.commit(); self.refresh_all_data(); self.refresh_machine_choices(); self.show_toast("已同步云端数据")
            else:
                self.sync_to_cloud()
        except RuntimeError:
            self.show_toast("云端暂不可用，订单仍保存在本机")

    def sync_to_cloud(self):
        if not getattr(self, "cloud_token", None): return
        columns = ("customer", "contact", "phone", "product", "spec", "quantity", "price", "delivery_date", "status", "notes", "created_at", "shipping_at")
        rows = [dict(row) for row in self.conn.execute("SELECT " + ",".join(columns) + " FROM records").fetchall()]
        inventory = self._export_inventory()
        def worker():
            try: cloud_post("/api/sync/push", {"records": rows, "inventory": inventory}, self.cloud_token)
            except RuntimeError: pass
        threading.Thread(target=worker, daemon=True).start()

    def _connect_db(self):
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer TEXT NOT NULL,
                contact TEXT,
                phone TEXT,
                product TEXT NOT NULL,
                spec TEXT,
                quantity TEXT,
                delivery_date TEXT NOT NULL,
                status TEXT DEFAULT '待交付',
                notes TEXT,
                created_at TEXT NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS stocking_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id INTEGER NOT NULL UNIQUE,
                customer TEXT NOT NULL,
                product TEXT NOT NULL,
                spec TEXT,
                quantity TEXT,
                delivery_date TEXT NOT NULL,
                stock_status TEXT NOT NULL DEFAULT '待备货',
                updated_at TEXT NOT NULL
            )
        """)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(records)")}
        if "price" not in columns:
            self.conn.execute("ALTER TABLE records ADD COLUMN price TEXT")
        if "shipping_at" not in columns:
            self.conn.execute("ALTER TABLE records ADD COLUMN shipping_at TEXT")
        self.conn.execute("""INSERT OR IGNORE INTO stocking_items
            (record_id, customer, product, spec, quantity, delivery_date, stock_status, updated_at)
            SELECT id, customer, product, spec, quantity, delivery_date, '待备货', created_at FROM records""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
            stock_quantity INTEGER NOT NULL DEFAULT 0, unit TEXT NOT NULL DEFAULT '件',
            notes TEXT, updated_at TEXT NOT NULL)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS machines (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
            model TEXT, notes TEXT, updated_at TEXT NOT NULL)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS machine_parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, machine_id INTEGER NOT NULL,
            part_id INTEGER NOT NULL, quantity INTEGER NOT NULL,
            UNIQUE(machine_id, part_id))""")
        self.conn.commit()

    def _export_inventory(self):
        return {
            "parts": [dict(row) for row in self.conn.execute("SELECT name, stock_quantity, unit, notes, updated_at FROM parts").fetchall()],
            "machines": [dict(row) for row in self.conn.execute("SELECT id, name, model, notes, updated_at FROM machines").fetchall()],
            "machine_parts": [dict(row) for row in self.conn.execute("""SELECT m.name AS machine_name, p.name AS part_name, mp.quantity
                FROM machine_parts mp JOIN machines m ON m.id=mp.machine_id JOIN parts p ON p.id=mp.part_id""").fetchall()],
        }

    def _import_inventory(self, inventory):
        """云端库存是完整快照；以名称恢复，避免依赖不同电脑上的 SQLite id。"""
        parts = inventory.get("parts", [])
        machines = inventory.get("machines", [])
        links = inventory.get("machine_parts", [])
        if not isinstance(parts, list) or not isinstance(machines, list) or not isinstance(links, list):
            return
        self.conn.execute("DELETE FROM machine_parts"); self.conn.execute("DELETE FROM machines"); self.conn.execute("DELETE FROM parts")
        for item in parts:
            self.conn.execute("INSERT OR IGNORE INTO parts(name, stock_quantity, unit, notes, updated_at) VALUES (?, ?, ?, ?, ?)",
                              (item.get("name", ""), int(item.get("stock_quantity", 0) or 0), item.get("unit", "件") or "件", item.get("notes", ""), item.get("updated_at", datetime.now().isoformat(timespec="seconds"))))
        for item in machines:
            self.conn.execute("INSERT OR IGNORE INTO machines(name, model, notes, updated_at) VALUES (?, ?, ?, ?)",
                              (item.get("name", ""), item.get("model", ""), item.get("notes", ""), item.get("updated_at", datetime.now().isoformat(timespec="seconds"))))
        for item in links:
            machine = self.conn.execute("SELECT id FROM machines WHERE name=?", (item.get("machine_name", ""),)).fetchone()
            part = self.conn.execute("SELECT id FROM parts WHERE name=?", (item.get("part_name", ""),)).fetchone()
            if machine and part:
                self.conn.execute("INSERT OR IGNORE INTO machine_parts(machine_id, part_id, quantity) VALUES (?, ?, ?)", (machine[0], part[0], max(1, int(item.get("quantity", 1) or 1))))

    def center_window(self, width, height):
        width = min(width, self.winfo_screenwidth() - 32)
        height = min(height, self.winfo_screenheight() - 72)
        x = max(16, (self.winfo_screenwidth() - width) // 2)
        y = max(16, (self.winfo_screenheight() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _setup_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", font=("Microsoft YaHei UI", 10), rowheight=38,
                        background="white", fieldbackground="white", foreground="#233248")
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"), padding=(12, 10),
                        background="#eaf0f9", foreground="#30445f", relief="flat")
        style.map("Treeview", background=[("selected", "#bfdbfe")], foreground=[("selected", "#0f172a")])
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(14, 8), background="#ffffff", foreground="#334155")
        style.map("TButton", background=[("active", "#eaf2ff")])
        style.configure("Accent.TButton", background="#2563eb", foreground="white")
        style.map("Accent.TButton", background=[("active", "#1d4ed8")], foreground=[("active", "white")])

    def _build_ui(self):
        titlebar = tk.Frame(self, height=36, bg="#101827")
        titlebar.pack(fill="x")
        titlebar.pack_propagate(False)
        titlebar.bind("<ButtonPress-1>", self._start_window_move)
        titlebar.bind("<B1-Motion>", self._move_window)
        titlebar.bind("<Double-Button-1>", self._toggle_maximize)
        tk.Label(titlebar, text="客户管理器", bg="#101827", fg="#dbeafe", font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=14)
        tk.Label(titlebar, text="本地订单与交期管理", bg="#101827", fg="#7890ad", font=("Microsoft YaHei UI", 8)).pack(side="left")
        close = tk.Button(titlebar, text="×", command=self.on_close, bg="#101827", fg="#cbd5e1", relief="flat", bd=0,
                          activebackground="#dc2626", activeforeground="white", font=("Segoe UI", 16), width=4, cursor="hand2")
        close.pack(side="right", fill="y")
        self.window_size_button = tk.Button(titlebar, text="□", command=self._toggle_maximize, bg="#101827", fg="#cbd5e1", relief="flat", bd=0,
                                            activebackground="#26354d", activeforeground="white", font=("Segoe UI", 13), width=4, cursor="hand2")
        self.window_size_button.pack(side="right", fill="y")
        minimize = tk.Button(titlebar, text="—", command=self.minimize_window, bg="#101827", fg="#cbd5e1", relief="flat", bd=0,
                             activebackground="#26354d", activeforeground="white", font=("Segoe UI", 11), width=4, cursor="hand2")
        minimize.pack(side="right", fill="y")
        shell = tk.Frame(self, bg="#f5f7fb")
        self.shell = shell
        shell.pack(fill="both", expand=True)
        sidebar = tk.Frame(shell, width=224, bg="#172033")
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self.brand_mark = tk.Label(sidebar, text="Q期", bg="#172033", fg="#73a7ff", font=("Microsoft YaHei UI", 20, "bold"))
        self.brand_mark.pack(anchor="w", padx=26, pady=(30, 2))
        self.brand_name = tk.Label(sidebar, text="客户管理器", bg="#172033", fg="#f8fafc", font=("Microsoft YaHei UI", 17, "bold"))
        self.brand_name.pack(anchor="w", padx=26)
        self.brand_subtitle = tk.Label(sidebar, text="DELIVERY DESK", bg="#172033", fg="#7f8ba3", font=("Segoe UI", 8, "bold"))
        self.brand_subtitle.pack(anchor="w", padx=27, pady=(4, 42))
        self.nav_order = tk.Button(sidebar, text="   订单管理", command=self.show_orders, bg="#253657", fg="white", anchor="w", relief="flat", bd=0,
                                   activebackground="#253657", activeforeground="white", pady=13, font=("Microsoft YaHei UI", 10, "bold"), cursor="hand2")
        self.nav_order.pack(fill="x", padx=14)
        self.nav_stock = tk.Button(sidebar, text="   备货单", command=self.open_stocking, bg="#172033", fg="#97a4ba", anchor="w", relief="flat", bd=0,
                                   activebackground="#253657", activeforeground="white", pady=12, font=("Microsoft YaHei UI", 10), cursor="hand2")
        self.nav_stock.pack(fill="x", padx=14, pady=(3, 0))
        self.nav_parts = tk.Button(sidebar, text="   零件库存", command=self.open_parts, bg="#172033", fg="#97a4ba", anchor="w", relief="flat", bd=0,
                                   activebackground="#253657", activeforeground="white", pady=12, font=("Microsoft YaHei UI", 10), cursor="hand2")
        self.nav_parts.pack(fill="x", padx=14, pady=(3, 0))
        self.nav_machines = tk.Button(sidebar, text="   机器构成", command=self.open_machines, bg="#172033", fg="#97a4ba", anchor="w", relief="flat", bd=0,
                                      activebackground="#253657", activeforeground="white", pady=12, font=("Microsoft YaHei UI", 10), cursor="hand2")
        self.nav_machines.pack(fill="x", padx=14, pady=(3, 0))
        self.nav_shipped = tk.Button(sidebar, text="   已发货", command=self.open_shipped, bg="#172033", fg="#97a4ba", anchor="w", relief="flat", bd=0,
                                     activebackground="#253657", activeforeground="white", pady=12, font=("Microsoft YaHei UI", 10), cursor="hand2")
        self.nav_shipped.pack(fill="x", padx=14, pady=(3, 0))
        self.nav_stats = tk.Button(sidebar, text="   数据统计", command=self.open_statistics, bg="#172033", fg="#97a4ba", anchor="w", relief="flat", bd=0,
                                   activebackground="#253657", activeforeground="white", pady=12, font=("Microsoft YaHei UI", 10), cursor="hand2")
        self.nav_stats.pack(fill="x", padx=14, pady=(3, 0))
        tk.Label(sidebar, text="  客户与交货计划", bg="#172033", fg="#97a4ba", anchor="w", pady=11, font=("Microsoft YaHei UI", 10)).pack(fill="x", padx=16)
        self.sidebar = sidebar
        self.theme_button = tk.Button(sidebar, text="◐  切换深浅色", command=self.toggle_theme, bg="#202d47", fg="#cbd5e1", relief="flat", bd=0,
                                      activebackground="#334a72", activeforeground="white", font=("Microsoft YaHei UI", 9), padx=13, pady=9, cursor="hand2")
        self.theme_button.pack(side="bottom", fill="x", padx=18, pady=(0, 10))
        self.autostart_button = tk.Button(sidebar, text="", command=self.toggle_autostart, bg="#172033", fg="#cbd5e1", relief="flat", bd=0,
                                          activebackground="#253657", activeforeground="white", font=("Microsoft YaHei UI", 9), cursor="hand2", anchor="w")
        self.autostart_button.pack(side="bottom", fill="x", padx=24, pady=(0, 10))
        self.update_autostart_button()
        self.test_sound_button = tk.Button(sidebar, text="♬  测试铃声", command=self.test_reminder_sound, bg="#172033", fg="#cbd5e1", relief="flat", bd=0,
                                           activebackground="#253657", activeforeground="white", font=("Microsoft YaHei UI", 9), cursor="hand2", anchor="w")
        self.test_sound_button.pack(side="bottom", fill="x", padx=24, pady=(0, 10))
        self.update_button = tk.Button(sidebar, text="↻  检查更新", command=lambda: self.check_for_updates(manual=True), bg="#172033", fg="#cbd5e1", relief="flat", bd=0,
                                       activebackground="#253657", activeforeground="white", font=("Microsoft YaHei UI", 9), cursor="hand2", anchor="w")
        self.update_button.pack(side="bottom", fill="x", padx=24, pady=(0, 10))
        tk.Button(sidebar, text="退出登录", command=self.logout, bg="#172033", fg="#97a4ba", relief="flat", bd=0,
                  activebackground="#253657", activeforeground="white", font=("Microsoft YaHei UI", 9), cursor="hand2").pack(side="bottom", anchor="w", padx=25, pady=20)

        self.page_host = tk.Frame(shell, bg="#f5f7fb")
        self.page_host.pack(side="left", fill="both", expand=True)
        body = tk.Frame(self.page_host, bg="#f5f7fb")
        self.body = body
        body.place(relx=0, rely=0, relwidth=1, relheight=1)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)
        top = tk.Frame(body, bg="#f5f7fb")
        top.grid(row=0, column=0, sticky="ew")
        tk.Label(top, text="订单概览", bg="#f5f7fb", fg="#172033", font=("Microsoft YaHei UI", 22, "bold")).pack(side="left")
        tk.Label(top, text="管理客户订单与交货计划", bg="#f5f7fb", fg="#7b879b", font=("Microsoft YaHei UI", 10)).pack(side="left", padx=14, pady=(8, 0))
        self.export_button = tk.Button(top, text="导出 CSV", command=self.export_csv, bg="#ffffff", fg="#334155", relief="flat", bd=0,
                                       font=("Microsoft YaHei UI", 9, "bold"), padx=15, pady=9, cursor="hand2")
        self.export_button.pack(side="right")
        self.refresh_button = tk.Button(top, text="刷新数据", command=self.refresh_all_data, bg="#ffffff", fg="#334155", relief="flat", bd=0,
                                        font=("Microsoft YaHei UI", 9, "bold"), padx=15, pady=9, cursor="hand2")
        self.refresh_button.pack(side="right", padx=(0, 8))

        stats = tk.Frame(body, bg="#f5f7fb")
        stats.grid(row=1, column=0, sticky="ew", pady=(22, 18))
        self.stat_cards, self.stat_values = [], []
        for title, color in (("全部订单", "#2563eb"), ("待交付", "#f59e0b"), ("临近交期", "#ef4444")):
            card = tk.Frame(stats, bg="white", padx=18, pady=13, highlightthickness=1, highlightbackground="#e5eaf1")
            card.pack(side="left", fill="x", expand=True, padx=(0, 12))
            tk.Label(card, text=title, bg="white", fg="#7b879b", font=("Microsoft YaHei UI", 9)).pack(anchor="w")
            value = tk.Label(card, text="0", bg="white", fg=color, font=("Segoe UI", 21, "bold"))
            value.pack(anchor="w", pady=(3, 0))
            self.stat_cards.append(card); self.stat_values.append(value)

        form = tk.Frame(body, bg="white", padx=24, pady=20, highlightthickness=1, highlightbackground="#e5eaf1")
        form.grid(row=2, column=0, sticky="ew")
        self.form = form
        self.form_title = tk.Label(form, text="新建订单", bg="white", fg="#172033", font=("Microsoft YaHei UI", 13, "bold"))
        self.form_title.grid(row=0, column=0, sticky="w")
        self.form_hint = tk.Label(form, text="填写必要信息后保存，系统会自动追踪交货时间。", bg="white", fg="#7b879b", font=("Microsoft YaHei UI", 9))
        self.form_hint.grid(row=0, column=1, columnspan=2, sticky="w", padx=14)
        for col in range(3): form.columnconfigure(col, weight=1)
        self.field_slots = []
        for index, (label, key, required) in enumerate(self.FIELDS):
            row, col = divmod(index, 3)
            slot = tk.Frame(form, bg="white")
            self.field_slots.append(slot)
            slot.grid(row=row + 1, column=col, sticky="ew", padx=(0, 16) if col < 2 else 0, pady=(18, 0))
            suffix = " *" if required else ""
            tk.Label(slot, text=label + suffix, bg="white", fg="#536177", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 6))
            if key == "status":
                field = ttk.Combobox(slot, values=["待交付", "生产中", "待发货", "已发货", "已取消", "已撤销"], state="readonly", font=("Microsoft YaHei UI", 10))
                field.set("待交付")
            elif key == "product":
                field = ttk.Combobox(slot, values=self.get_machine_names(), state="normal", font=("Microsoft YaHei UI", 10))
            else:
                field = ttk.Entry(slot, font=("Microsoft YaHei UI", 10))
            field.pack(fill="x", ipady=5)
            self.entries[key] = field
            if key == "delivery_days":
                self.delivery_preview = tk.Label(slot, text="预计交期：请先输入天数", bg="white", fg="#64748b", font=("Microsoft YaHei UI", 8))
                self.delivery_preview.pack(anchor="w", pady=(5, 0))
                field.bind("<KeyRelease>", lambda _event: self.refresh_delivery_preview())
        note_row = 1 + (len(self.FIELDS) + 2) // 3
        self.note_label = tk.Label(form, text="备注", bg="white", fg="#536177", font=("Microsoft YaHei UI", 9))
        self.note_label.grid(row=note_row, column=0, sticky="w", pady=(18, 6))
        self.notes = tk.Text(form, height=3, font=("Microsoft YaHei UI", 10), relief="flat", bd=0, highlightthickness=1, highlightbackground="#d6deeb", wrap="word")
        self.notes.grid(row=note_row + 1, column=0, columnspan=3, sticky="ew")
        actions = tk.Frame(form, bg="white")
        self.order_actions = actions
        actions.grid(row=note_row + 2, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        self.save_button = tk.Button(actions, text="保存订单", command=self.save_record, bg="#2563eb", fg="white", relief="flat", bd=0,
                                     activebackground="#174fc5", activeforeground="white", font=("Microsoft YaHei UI", 10, "bold"), padx=19, pady=10, cursor="hand2")
        self.save_button.pack(side="left")
        tk.Button(actions, text="清空", command=self.clear_form, bg="white", fg="#526076", relief="flat", bd=0, font=("Microsoft YaHei UI", 10), padx=15, pady=10, cursor="hand2").pack(side="left", padx=8)
        tk.Button(actions, text="删除选中", command=self.delete_record, bg="white", fg="#526076", relief="flat", bd=0, font=("Microsoft YaHei UI", 10), padx=15, pady=10, cursor="hand2").pack(side="left")
        tk.Button(actions, text="撤销订单", command=self.revoke_selected_order, bg="#fff1f2", fg="#be123c", relief="flat", bd=0, font=("Microsoft YaHei UI", 10, "bold"), padx=15, pady=10, cursor="hand2").pack(side="left", padx=8)
        tk.Button(actions, text="发货选中", command=self.ship_selected_order, bg="#e0f2fe", fg="#0369a1", relief="flat", bd=0, font=("Microsoft YaHei UI", 10, "bold"), padx=15, pady=10, cursor="hand2").pack(side="left", padx=8)

        records = tk.Frame(body, bg="white", padx=24, pady=18, highlightthickness=1, highlightbackground="#e5eaf1")
        self.table_box = records
        records.grid(row=4, column=0, sticky="nsew", pady=(18, 0))
        records.columnconfigure(0, weight=1); records.rowconfigure(2, weight=1)
        record_top = tk.Frame(records, bg="white")
        record_top.grid(row=0, column=0, sticky="ew")
        tk.Label(record_top, text="订单列表", bg="white", fg="#172033", font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        self.summary = tk.Label(record_top, text="", bg="white", fg="#7b879b", font=("Microsoft YaHei UI", 9))
        self.summary.pack(side="left", padx=12, pady=(3, 0))
        self.price_button = tk.Button(record_top, text="隐藏价格" if self.price_visible else "显示价格", command=self.toggle_price_visibility, bg="#f1f5f9", fg="#526076", relief="flat", bd=0, font=("Microsoft YaHei UI", 9), padx=10, pady=7, cursor="hand2")
        self.price_button.pack(side="right")
        self.search_var = tk.StringVar()
        search = ttk.Entry(record_top, textvariable=self.search_var, width=28, font=("Microsoft YaHei UI", 10))
        search.pack(side="right", padx=10, ipady=4)
        search.bind("<KeyRelease>", lambda _event: self.refresh_table())
        columns = ("id", "customer", "contact", "phone", "product", "spec", "quantity", "price", "delivery_date", "status")
        headings = ("编号", "客户", "联系人", "电话", "产品", "型号 / 规格", "数量", "单价", "交货期限", "状态")
        widths = (55, 125, 92, 118, 135, 145, 70, 88, 110, 92)
        self.tree = ttk.Treeview(records, columns=columns, show="headings", selectmode="browse")
        for col, heading, width in zip(columns, headings, widths):
            self.tree.heading(col, text=heading)
            self.tree.column(col, width=width, minwidth=55, anchor="center" if col in ("id", "quantity", "delivery_date", "status") else "w")
        scrollbar = ttk.Scrollbar(records, orient="vertical", command=self.tree.yview)
        xscrollbar = ttk.Scrollbar(records, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=xscrollbar.set)
        self.tree.tag_configure("zebra", background="#f8fafc")
        self.tree.tag_configure("shipped", background="#f1f5f9", foreground="#94a3b8")
        self.tree.grid(row=2, column=0, sticky="nsew", pady=(16, 0))
        scrollbar.grid(row=2, column=1, sticky="ns", pady=(16, 0))
        xscrollbar.grid(row=3, column=0, sticky="ew")
        self.card_widgets = [self.form, self.table_box, *self.stat_cards]
        self.tree.bind("<<TreeviewSelect>>", self.load_selected)

    def toggle_theme(self):
        self.theme_name = "dark" if self.theme_name == "light" else "light"
        self.apply_theme()
        save_ui_settings(self.theme_name, self.price_visible)

    def update_autostart_button(self):
        state = "已开启" if self.autostart_enabled else "未开启"
        self.autostart_button.configure(text=f"◉  开机自启动：{state}")

    def toggle_autostart(self):
        try:
            self.autostart_enabled = not self.autostart_enabled
            set_autostart(self.autostart_enabled)
            self.update_autostart_button()
            self.show_toast("开机自启动已" + ("开启" if self.autostart_enabled else "关闭"))
        except OSError:
            self.autostart_enabled = autostart_is_enabled()
            self.update_autostart_button()
            messagebox.showerror("设置失败", "无法修改 Windows 开机启动项。")

    def toggle_price_visibility(self):
        self.price_visible = not self.price_visible
        self.tree.column("price", width=88 if self.price_visible else 0, minwidth=0, stretch=self.price_visible)
        self.tree.heading("price", text="单价" if self.price_visible else "")
        self.price_button.configure(text="隐藏价格" if self.price_visible else "显示价格")
        save_ui_settings(self.theme_name, self.price_visible)

    def apply_theme(self):
        palette = THEMES[self.theme_name]
        self.configure(bg=palette["bg"])
        style = ttk.Style(self)
        style.configure("Treeview", background=palette["surface"], fieldbackground=palette["surface"], foreground=palette["text"], rowheight=38)
        style.configure("Treeview.Heading", background=palette["head"], foreground=palette["text"])
        style.map("Treeview", background=[("selected", palette["selected"])], foreground=[("selected", palette["text"])])
        if hasattr(self, "tree"):
            self.tree.tag_configure("zebra", background="#1b2638" if self.theme_name == "dark" else "#f8fafc")
            self.tree.tag_configure("shipped", background="#1b2330" if self.theme_name == "dark" else "#f1f5f9", foreground="#718096" if self.theme_name == "dark" else "#94a3b8")
        style.configure("TButton", background=palette["surface"], foreground=palette["text"])
        style.map("TButton", background=[("active", palette["head"]), ("pressed", palette["selected"])])
        style.configure("Accent.TButton", background=palette["accent"], foreground="white")
        style.map("Accent.TButton", background=[("active", palette["accent_active"]), ("pressed", palette["accent_active"])])
        self.theme_button.configure(text="☀  浅色模式" if self.theme_name == "dark" else "◐  切换深浅色")
        self._apply_widget_palette(self.body, palette)
        for page_name in ("statistics_page", "stocking_page", "parts_page", "machines_page", "shipped_page"):
            page = getattr(self, page_name, None)
            if page is not None and page.winfo_exists():
                self._apply_surface_theme(page)
        self.table_box.configure(bg=palette["surface"], highlightbackground=palette["border"])
        self.notes.configure(bg=palette["surface"], fg=palette["text"], insertbackground=palette["text"], highlightbackground=palette["border"])
        self.tree.column("price", width=88 if self.price_visible else 0, minwidth=0, stretch=self.price_visible)
        self.tree.heading("price", text="单价" if self.price_visible else "")
        self.price_button.configure(text="隐藏价格" if self.price_visible else "显示价格")
        neutral = "#2b3a50" if self.theme_name == "dark" else "#f1f5f9"
        neutral_text = "#dbeafe" if self.theme_name == "dark" else "#526076"
        for button in (self.export_button, self.price_button):
            button.configure(bg=neutral, fg=neutral_text, activebackground=palette["accent"], activeforeground="white")
        self.save_button.configure(bg=palette["accent"], activebackground=palette["accent_active"])
        self.theme_button.configure(bg="#263b59" if self.theme_name == "dark" else "#202d47")

    def _apply_widget_palette(self, widget, palette):
        """为 Tk 原生控件递归换肤；ttk 控件由 Style 负责。"""
        for child in widget.winfo_children():
            card_context = child in self.card_widgets or self._is_in_card(child)
            if isinstance(child, tk.Frame):
                child.configure(bg=palette["surface"] if card_context else palette["bg"])
            elif isinstance(child, tk.Label):
                child.configure(bg=palette["surface"] if card_context else palette["bg"], fg=palette["text"] if card_context else palette["muted"])
            self._apply_widget_palette(child, palette)

    def _apply_surface_theme(self, root):
        """Apply the same restrained blue operations palette to every lazily-built page."""
        palette = THEMES[self.theme_name]
        backgrounds = {
            "#f5f7fb": palette["bg"], "#f6f8fc": palette["bg"], "#f8fafc": palette["bg"],
            "#edf2f8": palette["head"], "#eaf0f8": palette["head"], "#ffffff": palette["surface"], "white": palette["surface"],
            "#111827": palette["bg"], "#1f2937": palette["surface"], "#273548": palette["head"],
        }
        foregrounds = {
            "#172033": palette["text"], "#334155": palette["text"], "#64748b": palette["muted"],
            "#7b879b": palette["muted"], "#94a3b8": palette["muted"], "#536177": palette["muted"],
            "#f1f5f9": palette["text"], "#a8b5c7": palette["muted"],
        }
        borders = {"#e2e8f0", "#e5eaf1", "#e6eaf0", "#dfe7f1", "#374151"}
        def visit(widget):
            try:
                bg = widget.cget("bg")
                if bg in backgrounds: widget.configure(bg=backgrounds[bg])
            except (tk.TclError, KeyError):
                pass
            try:
                fg = widget.cget("fg")
                if fg in foregrounds: widget.configure(fg=foregrounds[fg])
            except (tk.TclError, KeyError):
                pass
            try:
                border = widget.cget("highlightbackground")
                if border in borders: widget.configure(highlightbackground=palette["border"])
            except tk.TclError:
                pass
            for child in widget.winfo_children(): visit(child)
        visit(root)

    def _is_in_card(self, widget):
        parent = widget.master
        while parent is not None and parent != self.body:
            if parent in self.card_widgets:
                return True
            parent = parent.master
        return False

    def _start_window_move(self, event):
        if self._edge_mode(event) or getattr(self, "_resize_state", None):
            return
        self._drag_offset = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _move_window(self, event):
        if hasattr(self, "_drag_offset") and not getattr(self, "_resize_state", None):
            self.geometry(f"+{event.x_root - self._drag_offset[0]}+{event.y_root - self._drag_offset[1]}")

    def minimize_window(self):
        """无边框窗口需临时恢复系统装饰，才能可靠进入任务栏。"""
        self.overrideredirect(False)
        self.iconify()
        self.bind("<Map>", self._restore_borderless, add="+")

    def _restore_borderless(self, _event=None):
        if self.state() == "normal":
            self.overrideredirect(True)

    def _toggle_maximize(self, _event=None):
        if getattr(self, "_is_maximized", False):
            self.geometry(self._normal_geometry)
            self._is_maximized = False
            if hasattr(self, "window_size_button"): self.window_size_button.configure(text="□")
        else:
            self._normal_geometry = self.geometry()
            self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")
            self._is_maximized = True
            if hasattr(self, "window_size_button"): self.window_size_button.configure(text="❐")

    def _edge_mode(self, event):
        x, y = event.x_root - self.winfo_rootx(), event.y_root - self.winfo_rooty()
        margin, width, height = 7, self.winfo_width(), self.winfo_height()
        horizontal = "W" if x <= margin else "E" if x >= width - margin else ""
        vertical = "N" if y <= margin else "S" if y >= height - margin else ""
        return vertical + horizontal

    def _resize_cursor(self, event):
        mode = self._edge_mode(event)
        cursors = {"N": "sb_v_double_arrow", "S": "sb_v_double_arrow", "E": "sb_h_double_arrow", "W": "sb_h_double_arrow", "NW": "size_nw_se", "SE": "size_nw_se", "NE": "size_ne_sw", "SW": "size_ne_sw"}
        self.configure(cursor=cursors.get(mode, ""))

    def _begin_resize(self, event):
        mode = self._edge_mode(event)
        if mode and not getattr(self, "_is_maximized", False):
            self._resize_state = (mode, event.x_root, event.y_root, self.winfo_x(), self.winfo_y(), self.winfo_width(), self.winfo_height())

    def _perform_resize(self, event):
        if not getattr(self, "_resize_state", None):
            return
        mode, start_x, start_y, window_x, window_y, width, height = self._resize_state
        dx, dy = event.x_root - start_x, event.y_root - start_y
        new_x, new_y, new_w, new_h = window_x, window_y, width, height
        if "E" in mode: new_w = max(860, width + dx)
        if "S" in mode: new_h = max(620, height + dy)
        if "W" in mode:
            new_w = max(860, width - dx); new_x = window_x + (width - new_w)
        if "N" in mode:
            new_h = max(620, height - dy); new_y = window_y + (height - new_h)
        self.geometry(f"{new_w}x{new_h}+{new_x}+{new_y}")

    def _end_resize(self, _event):
        self._resize_state = None
        self._drag_offset = None

    def _responsive_layout(self, event):
        """在窄窗口中保留完整信息：收起导航并将表单由三列自动重排为两列。"""
        if event.widget is not self or not hasattr(self, "sidebar"):
            return
        compact = event.width < 980
        sidebar_width = 68 if compact else 224
        self.sidebar.configure(width=sidebar_width)
        self.brand_mark.configure(text="客" if compact else "Q期")
        self.brand_name.configure(text="" if compact else "客户管理器")
        self.brand_subtitle.configure(text="" if compact else "DELIVERY DESK")
        labels = ((self.nav_order, "订单"), (self.nav_stock, "备货"), (self.nav_parts, "库存"), (self.nav_machines, "机器"), (self.nav_shipped, "发货"), (self.nav_stats, "统计"))
        for button, text in labels:
            button.configure(text=text if compact else "   " + {"订单": "订单管理", "备货": "备货单", "库存": "零件库存", "机器": "机器构成", "发货": "已发货", "统计": "数据统计"}[text], anchor="center" if compact else "w")
        if not hasattr(self, "field_slots"):
            return
        # 三档重排：大窗三列、普通小窗两列、窄窗一列，确保表单控件不被挤出屏幕。
        available = event.width - sidebar_width
        columns = 1 if available < 650 else (2 if available < 1050 else 3)
        if columns == getattr(self, "_form_columns", None):
            return
        self._form_columns = columns
        if columns == 1:
            self.form_title.grid_configure(row=0, column=0, columnspan=1, sticky="w")
            self.form_hint.grid_configure(row=1, column=0, columnspan=1, sticky="w", padx=0, pady=(5, 0))
            field_row_offset = 2
        else:
            self.form_title.grid_configure(row=0, column=0, columnspan=1, sticky="w")
            self.form_hint.grid_configure(row=0, column=1, columnspan=columns - 1, sticky="w", padx=14, pady=0)
            field_row_offset = 1
        for index, slot in enumerate(self.field_slots):
            row, column = divmod(index, columns)
            slot.grid_configure(row=row + field_row_offset, column=column, padx=(0, 12) if column < columns - 1 else 0)
        for column in range(3):
            self.form.columnconfigure(column, weight=1 if column < columns else 0)
        note_row = field_row_offset + (len(self.field_slots) + columns - 1) // columns
        self.note_label.grid_configure(row=note_row, column=0)
        self.notes.grid_configure(row=note_row + 1, column=0, columnspan=columns)
        self.order_actions.grid_configure(row=note_row + 2, column=0, columnspan=columns)

    @staticmethod
    def _number(value):
        try:
            return float(str(value or "0").replace(",", "").replace("￥", "").strip())
        except ValueError:
            return 0.0

    def _show_page(self, page):
        """所有主页面在同一位置叠放，切换时确保只有目标页面可见。"""
        for name in ("body", "statistics_page", "stocking_page", "parts_page", "machines_page", "shipped_page"):
            candidate = getattr(self, name, None)
            if candidate is not None and candidate.winfo_exists():
                candidate.place_forget()
        page.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._apply_surface_theme(page)
        page.lift()

    def open_statistics(self):
        self._set_active_nav("stats")
        if hasattr(self, "statistics_page") and self.statistics_page.winfo_exists():
            self._show_page(self.statistics_page)
            self.render_statistics(self.stat_period.get())
            return
        main = tk.Frame(self.page_host, bg="#f5f7fb", padx=34, pady=28)
        self.statistics_page = main
        self._show_page(main)
        tk.Label(main, text="交易数据中心", bg="#f5f7fb", fg="#172033", font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        tk.Label(main, text="按订单录入日期统计交易数量与金额，并与去年同期对比", bg="#f5f7fb", fg="#7b879b", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 18))
        controls = tk.Frame(main, bg="#f5f7fb")
        controls.pack(fill="x", pady=(0, 18))
        self.stat_period = tk.StringVar(value="month")
        self.period_buttons = {}
        for key, text in (("week", "本周"), ("month", "本月"), ("year", "本年")):
            button = tk.Button(controls, text=text, command=lambda value=key: self.render_statistics(value), relief="flat", bd=0, font=("Microsoft YaHei UI", 9, "bold"), padx=18, pady=8, cursor="hand2")
            button.pack(side="left", padx=(0, 8))
            self.period_buttons[key] = button
        self.period_caption = tk.Label(controls, text="", bg="#f5f7fb", fg="#7b879b", font=("Microsoft YaHei UI", 9))
        self.period_caption.pack(side="right", pady=(6, 0))
        cards = tk.Frame(main, bg="#f5f7fb")
        cards.pack(fill="x")
        self.analytics_values = []
        for title, color in (("交易数量", "#2563eb"), ("交易金额", "#7c3aed"), ("金额同比", "#d97706"), ("数量同比", "#059669")):
            card = tk.Frame(cards, bg="white", padx=16, pady=14, highlightthickness=1, highlightbackground="#e6eaf0")
            card.pack(side="left", fill="x", expand=True, padx=(0, 10))
            tk.Label(card, text=title, bg="white", fg="#7b879b", font=("Microsoft YaHei UI", 9)).pack(anchor="w")
            value = tk.Label(card, text="—", bg="white", fg=color, font=("Segoe UI", 17, "bold"))
            value.pack(anchor="w", pady=(4, 0)); self.analytics_values.append(value)
        chart_card = tk.Frame(main, bg="white", padx=20, pady=18, highlightthickness=1, highlightbackground="#e6eaf0")
        chart_card.pack(fill="both", expand=True, pady=(18, 0))
        tk.Label(chart_card, text="交易趋势", bg="white", fg="#172033", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        legend = tk.Frame(chart_card, bg="white"); legend.pack(anchor="w", pady=(4, 6))
        tk.Label(legend, text="━", bg="white", fg="#7c3aed", font=("Segoe UI", 13, "bold")).pack(side="left")
        tk.Label(legend, text="金额", bg="white", fg="#7b879b", font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(2, 12))
        tk.Label(legend, text="━", bg="white", fg="#2563eb", font=("Segoe UI", 13, "bold")).pack(side="left")
        tk.Label(legend, text="数量", bg="white", fg="#7b879b", font=("Microsoft YaHei UI", 8)).pack(side="left", padx=2)
        self.chart_canvas = tk.Canvas(chart_card, bg="white", highlightthickness=0)
        self.chart_canvas.pack(fill="both", expand=True)
        self.chart_canvas.bind("<Configure>", lambda _event: self.draw_chart())
        self.render_statistics("month")
        self._apply_surface_theme(main)

    def _set_active_nav(self, active):
        for name in ("order", "stock", "parts", "machines", "shipped", "stats"):
            button = getattr(self, "nav_" + name, None)
            if button:
                button.configure(bg="#253657" if name == active else "#172033", fg="white" if name == active else "#97a4ba")

    def get_machine_names(self):
        return [row[0] for row in self.conn.execute("SELECT name FROM machines ORDER BY name").fetchall()]

    def refresh_machine_choices(self):
        field = getattr(self, "entries", {}).get("product")
        if field and isinstance(field, ttk.Combobox):
            field.configure(values=self.get_machine_names())

    def open_parts(self):
        self._set_active_nav("parts")
        if hasattr(self, "parts_page") and self.parts_page.winfo_exists():
            self._show_page(self.parts_page); self.refresh_parts_table(); return
        page = tk.Frame(self.page_host, bg="#f5f7fb", padx=34, pady=28)
        self.parts_page = page; self._show_page(page)
        page.columnconfigure(0, weight=1); page.rowconfigure(3, weight=1)
        tk.Label(page, text="零件库存", bg="#f5f7fb", fg="#172033", font=("Microsoft YaHei UI", 22, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(page, text="先建立自定义零件，再把零件组合为机器；库存只在此处手动加减。", bg="#f5f7fb", fg="#64748b", font=("Microsoft YaHei UI", 9)).grid(row=1, column=0, sticky="w", pady=(5, 16))
        editor = tk.Frame(page, bg="white", padx=20, pady=16, highlightthickness=1, highlightbackground="#e2e8f0")
        editor.grid(row=2, column=0, sticky="ew", pady=(0, 16))
        for col in range(4): editor.columnconfigure(col, weight=1)
        self.part_vars = {key: tk.StringVar() for key in ("name", "quantity", "unit", "notes")}; self.part_vars["unit"].set("件")
        for col, (text, key) in enumerate((("零件名称 *", "name"), ("库存数量 *", "quantity"), ("单位", "unit"), ("备注", "notes"))):
            box = tk.Frame(editor, bg="white"); box.grid(row=0, column=col, sticky="ew", padx=(0, 12) if col < 3 else 0)
            tk.Label(box, text=text, bg="white", fg="#536177", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 5))
            ttk.Entry(box, textvariable=self.part_vars[key], font=("Microsoft YaHei UI", 10)).pack(fill="x", ipady=5)
        actions = tk.Frame(editor, bg="white"); actions.grid(row=1, column=0, columnspan=4, sticky="w", pady=(14, 0))
        tk.Button(actions, text="保存零件", command=self.save_part, bg="#2563eb", fg="white", relief="flat", bd=0, padx=15, pady=8, cursor="hand2").pack(side="left")
        tk.Button(actions, text="批量添加", command=self.open_bulk_parts_dialog, bg="#e8f1fd", fg="#2459a6", relief="flat", bd=0, padx=15, pady=8, cursor="hand2").pack(side="left", padx=8)
        tk.Button(actions, text="库存 +", command=lambda: self.adjust_part_stock(1), relief="flat", bd=0, padx=15, pady=8, cursor="hand2").pack(side="left")
        tk.Button(actions, text="库存 −", command=lambda: self.adjust_part_stock(-1), relief="flat", bd=0, padx=15, pady=8, cursor="hand2").pack(side="left")
        tk.Button(actions, text="删除零件", command=self.delete_part, bg="#fff1f2", fg="#be123c", relief="flat", bd=0, padx=15, pady=8, cursor="hand2").pack(side="left", padx=8)
        card = tk.Frame(page, bg="white", padx=18, pady=16, highlightthickness=1, highlightbackground="#e2e8f0"); card.grid(row=3, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1); card.rowconfigure(2, weight=1)
        board_head = tk.Frame(card, bg="white"); board_head.grid(row=0, column=0, sticky="ew", pady=(0, 9))
        tk.Label(board_head, text="库存看板", bg="white", fg="#172033", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        self.parts_board_caption = tk.Label(board_head, text="", bg="white", fg="#64748b", font=("Microsoft YaHei UI", 9)); self.parts_board_caption.pack(side="left", padx=12)
        tk.Label(card, text="点击任意零件卡片即可编辑或加减库存；阈值：0 为缺货，1–10 为偏低。", bg="white", fg="#94a3b8", font=("Microsoft YaHei UI", 8)).grid(row=1, column=0, sticky="w", pady=(0, 10))
        self.parts_canvas = tk.Canvas(card, bg="#f6f8fc", highlightthickness=0)
        scroll = ttk.Scrollbar(card, orient="vertical", command=self.parts_canvas.yview); self.parts_canvas.configure(yscrollcommand=scroll.set)
        self.parts_board = tk.Frame(self.parts_canvas, bg="#f6f8fc")
        self.parts_board_window = self.parts_canvas.create_window((0, 0), window=self.parts_board, anchor="nw")
        self.parts_board.bind("<Configure>", lambda _event: self.parts_canvas.configure(scrollregion=self.parts_canvas.bbox("all")))
        self.parts_canvas.bind("<Configure>", lambda event: self.parts_canvas.itemconfigure(self.parts_board_window, width=event.width))
        self.parts_canvas.grid(row=2, column=0, sticky="nsew"); scroll.grid(row=2, column=1, sticky="ns")
        self.selected_part_id = None; self.refresh_parts_table()

    def refresh_parts_table(self):
        if not hasattr(self, "parts_board"): return
        for child in self.parts_board.winfo_children(): child.destroy()
        rows = self.conn.execute("SELECT * FROM parts ORDER BY name").fetchall()
        groups = (("缺货", "库存为 0", "#ef4444", [row for row in rows if int(row["stock_quantity"]) == 0]),
                  ("偏低", "库存 1–10", "#f59e0b", [row for row in rows if 0 < int(row["stock_quantity"]) <= 10]),
                  ("充足", "库存大于 10", "#10b981", [row for row in rows if int(row["stock_quantity"]) > 10]))
        for column, (title, hint, color, items) in enumerate(groups):
            self.parts_board.columnconfigure(column, weight=1, uniform="stock-lanes")
            lane = tk.Frame(self.parts_board, bg="#edf2f8", padx=10, pady=10)
            lane.grid(row=0, column=column, sticky="nsew", padx=(0, 9) if column < 2 else 0, pady=0)
            head = tk.Frame(lane, bg="#edf2f8"); head.pack(fill="x")
            tk.Label(head, text=title, bg="#edf2f8", fg=color, font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
            tk.Label(head, text=str(len(items)), bg=color, fg="white", font=("Segoe UI", 8, "bold"), padx=7, pady=2).pack(side="right")
            tk.Label(lane, text=hint, bg="#edf2f8", fg="#7b879b", font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(3, 9))
            if not items:
                tk.Label(lane, text="暂无零件", bg="#edf2f8", fg="#94a3b8", font=("Microsoft YaHei UI", 9)).pack(pady=26)
            for row in items: self._create_part_card(lane, row, color)
        self.parts_board_caption.configure(text=f"共 {len(rows)} 个自定义零件")
        self._apply_surface_theme(self.parts_page)

    def _create_part_card(self, parent, row, color):
        selected = row["id"] == self.selected_part_id
        background, border = ("#eaf2ff", "#60a5fa") if selected else ("white", "#dfe7f1")
        card = tk.Frame(parent, bg=background, padx=11, pady=10, highlightthickness=1, highlightbackground=border, cursor="hand2")
        card.pack(fill="x", pady=(0, 8))
        top = tk.Frame(card, bg=background); top.pack(fill="x")
        tk.Label(top, text=row["name"], bg=background, fg="#172033", font=("Microsoft YaHei UI", 10, "bold"), cursor="hand2").pack(side="left")
        tk.Label(top, text=f"{row['stock_quantity']} {row['unit']}", bg=color, fg="white", font=("Microsoft YaHei UI", 8, "bold"), padx=7, pady=2, cursor="hand2").pack(side="right")
        tk.Label(card, text=row["notes"] or "未填写备注", bg=background, fg="#64748b", font=("Microsoft YaHei UI", 8), cursor="hand2").pack(anchor="w", pady=(7, 0))
        self._bind_part_card(card, row["id"])

    def _bind_part_card(self, widget, part_id):
        widget.bind("<Button-1>", lambda _event, value=part_id: self.load_selected_part(value))
        for child in widget.winfo_children(): self._bind_part_card(child, part_id)

    def load_selected_part(self, event_or_id=None):
        if isinstance(event_or_id, int):
            self.selected_part_id = event_or_id
        else:
            return
        row = self.conn.execute("SELECT * FROM parts WHERE id=?", (self.selected_part_id,)).fetchone()
        if not row: return
        for key in self.part_vars: self.part_vars[key].set(str(row[{"name":"name", "quantity":"stock_quantity", "unit":"unit", "notes":"notes"}[key]] or ""))
        self.refresh_parts_table()

    def save_part(self):
        name = self.part_vars["name"].get().strip()
        try: quantity = int(self.part_vars["quantity"].get().strip())
        except ValueError: messagebox.showwarning("数量不正确", "库存数量请输入整数。", parent=self); return
        if not name or quantity < 0: messagebox.showwarning("请补全信息", "请输入零件名称和不小于 0 的库存数量。", parent=self); return
        now = datetime.now().isoformat(timespec="seconds")
        try:
            if self.selected_part_id: self.conn.execute("UPDATE parts SET name=?, stock_quantity=?, unit=?, notes=?, updated_at=? WHERE id=?", (name, quantity, self.part_vars["unit"].get().strip() or "件", self.part_vars["notes"].get().strip(), now, self.selected_part_id))
            else: self.conn.execute("INSERT INTO parts(name, stock_quantity, unit, notes, updated_at) VALUES (?, ?, ?, ?, ?)", (name, quantity, self.part_vars["unit"].get().strip() or "件", self.part_vars["notes"].get().strip(), now))
        except sqlite3.IntegrityError: messagebox.showwarning("名称重复", "已有同名零件，请修改后保存。", parent=self); return
        self.conn.commit(); self.refresh_parts_table(); self.refresh_machine_choices(); self.sync_to_cloud(); self.show_toast("零件库存已保存")

    def open_bulk_parts_dialog(self):
        """一次录入多种新零件，适合首次建立配件库。"""
        dialog = tk.Toplevel(self)
        dialog.title("批量添加零件")
        dialog.configure(bg="#f5f7fb")
        dialog.transient(self)
        dialog.geometry("820x560")
        dialog.minsize(620, 420)
        dialog.grab_set()
        width, height = 820, 560
        x = max(20, self.winfo_rootx() + (self.winfo_width() - width) // 2)
        y = max(20, self.winfo_rooty() + (self.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")

        shell = tk.Frame(dialog, bg="#f5f7fb", padx=22, pady=18)
        shell.pack(fill="both", expand=True)
        tk.Label(shell, text="批量添加零件", bg="#f5f7fb", fg="#172033", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        tk.Label(shell, text="每行填写一种配件；检查无误后一次保存全部。", bg="#f5f7fb", fg="#64748b", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 13))

        list_area = tk.Frame(shell, bg="#f5f7fb")
        list_area.pack(fill="both", expand=True)
        list_area.rowconfigure(0, weight=1); list_area.columnconfigure(0, weight=1)
        canvas = tk.Canvas(list_area, bg="#f5f7fb", highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_area, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew"); scrollbar.grid(row=0, column=1, sticky="ns")
        table = tk.Frame(canvas, bg="white", padx=12, pady=10, highlightthickness=1, highlightbackground="#e2e8f0")
        table_window = canvas.create_window((0, 0), window=table, anchor="nw")
        table.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(table_window, width=event.width))
        headers = ("零件名称 *", "初始库存 *", "单位", "备注")
        widths = (26, 13, 11, 30)
        for column, (label, width_chars) in enumerate(zip(headers, widths)):
            tk.Label(table, text=label, bg="#eaf1fb", fg="#30445f", font=("Microsoft YaHei UI", 9, "bold"), padx=8, pady=8).grid(row=0, column=column, sticky="ew", padx=(0, 5) if column < 3 else 0, pady=(0, 6))
            table.columnconfigure(column, weight=(3 if column in (0, 3) else 1))

        rows = []
        def add_row():
            row_index = len(rows) + 1
            variables = [tk.StringVar(), tk.StringVar(), tk.StringVar(value="件"), tk.StringVar()]
            for column, (variable, width_chars) in enumerate(zip(variables, widths)):
                ttk.Entry(table, textvariable=variable, width=width_chars, font=("Microsoft YaHei UI", 10)).grid(row=row_index, column=column, sticky="ew", padx=(0, 5) if column < 3 else 0, pady=3, ipady=4)
            rows.append(variables)

        add_row(); add_row(); add_row()
        tk.Button(shell, text="＋ 添加一行", command=add_row, bg="#e8f1fd", fg="#2459a6", relief="flat", bd=0, padx=13, pady=7, cursor="hand2").pack(anchor="w", pady=(8, 0))
        buttons = tk.Frame(shell, bg="#f5f7fb")
        buttons.pack(fill="x", pady=(14, 0))

        def save_all():
            entries = []
            for index, variables in enumerate(rows, start=1):
                name, quantity_text, unit, notes = [variable.get().strip() for variable in variables]
                if not any((name, quantity_text, notes)):
                    continue
                try:
                    quantity = int(quantity_text)
                    if quantity < 0: raise ValueError
                except ValueError:
                    messagebox.showwarning("库存数量不正确", f"第 {index} 行库存需要填写不小于 0 的整数。", parent=dialog)
                    return
                if not name:
                    messagebox.showwarning("零件名称缺失", f"第 {index} 行请填写零件名称。", parent=dialog)
                    return
                entries.append((name, quantity, unit or "件", notes))
            if not entries:
                messagebox.showwarning("没有可保存的零件", "请至少填写一行零件信息。", parent=dialog)
                return
            names = [entry[0] for entry in entries]
            if len(set(names)) != len(names):
                messagebox.showwarning("名称重复", "批量列表中有同名零件，请检查后再保存。", parent=dialog)
                return
            existing = [name for name in names if self.conn.execute("SELECT 1 FROM parts WHERE name=?", (name,)).fetchone()]
            if existing:
                messagebox.showwarning("零件已存在", "以下零件已在库存中，请移除重复项或到库存卡片中编辑：\n" + "、".join(existing), parent=dialog)
                return
            now = datetime.now().isoformat(timespec="seconds")
            self.conn.executemany("INSERT INTO parts(name, stock_quantity, unit, notes, updated_at) VALUES (?, ?, ?, ?, ?)", [(name, quantity, unit, notes, now) for name, quantity, unit, notes in entries])
            self.conn.commit()
            self.refresh_parts_table()
            self.refresh_machine_choices()
            self.sync_to_cloud()
            dialog.destroy()
            self.show_toast(f"已批量添加 {len(entries)} 个零件")

        tk.Button(buttons, text="保存全部零件", command=save_all, bg="#2563eb", fg="white", relief="flat", bd=0, padx=18, pady=9, cursor="hand2").pack(side="right")
        tk.Button(buttons, text="取消", command=dialog.destroy, bg="white", fg="#526076", relief="flat", bd=0, padx=17, pady=9, cursor="hand2").pack(side="right", padx=8)

    def adjust_part_stock(self, direction):
        if not self.selected_part_id: messagebox.showwarning("未选择零件", "请先在下方选择一个零件。", parent=self); return
        try: delta = int(self.part_vars["quantity"].get().strip())
        except ValueError: messagebox.showwarning("数量不正确", "请在库存数量中填写本次加减的整数。", parent=self); return
        if delta < 0: messagebox.showwarning("数量不正确", "请填写正整数，按钮决定加或减。", parent=self); return
        row = self.conn.execute("SELECT stock_quantity FROM parts WHERE id=?", (self.selected_part_id,)).fetchone(); value = max(0, int(row[0]) + direction * delta)
        self.conn.execute("UPDATE parts SET stock_quantity=?, updated_at=? WHERE id=?", (value, datetime.now().isoformat(timespec="seconds"), self.selected_part_id)); self.conn.commit()
        self.part_vars["quantity"].set(str(value)); self.refresh_parts_table(); self.sync_to_cloud(); self.show_toast("库存已更新")

    def delete_part(self):
        if not self.selected_part_id: return
        if not messagebox.askyesno("确认删除", "删除此零件会同时从机器构成中移除它，是否继续？", parent=self): return
        self.conn.execute("DELETE FROM machine_parts WHERE part_id=?", (self.selected_part_id,)); self.conn.execute("DELETE FROM parts WHERE id=?", (self.selected_part_id,)); self.conn.commit()
        self.selected_part_id = None
        for value in self.part_vars.values(): value.set("")
        self.part_vars["unit"].set("件"); self.refresh_parts_table(); self.sync_to_cloud()

    def open_machines(self):
        self._set_active_nav("machines")
        if hasattr(self, "machines_page") and self.machines_page.winfo_exists():
            self._show_page(self.machines_page); self.refresh_machines(); return
        page = tk.Frame(self.page_host, bg="#f5f7fb", padx=34, pady=28); self.machines_page = page; self._show_page(page)
        page.columnconfigure(0, weight=1); page.columnconfigure(1, weight=2); page.rowconfigure(2, weight=1)
        tk.Label(page, text="机器构成", bg="#f5f7fb", fg="#172033", font=("Microsoft YaHei UI", 22, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(page, text="选择已有零件定义机器；订单产品可直接选择这里创建的机器。", bg="#f5f7fb", fg="#64748b", font=("Microsoft YaHei UI", 9)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 16))
        left = tk.Frame(page, bg="white", padx=18, pady=16, highlightthickness=1, highlightbackground="#e2e8f0"); left.grid(row=2, column=0, sticky="nsew", padx=(0, 16)); left.columnconfigure(0, weight=1); left.rowconfigure(3, weight=1)
        self.machine_vars = {key: tk.StringVar() for key in ("name", "model", "notes", "part", "part_quantity")}; self.machine_vars["part_quantity"].set("1")
        for row, (text, key) in enumerate((("机器名称 *", "name"), ("型号 / 说明", "model"), ("备注", "notes"))):
            tk.Label(left, text=text, bg="white", fg="#536177", font=("Microsoft YaHei UI", 9)).grid(row=row*2, column=0, sticky="w", pady=(0, 5))
            ttk.Entry(left, textvariable=self.machine_vars[key]).grid(row=row*2+1, column=0, sticky="ew", pady=(0, 10), ipady=5)
        tk.Button(left, text="保存机器", command=self.save_machine, bg="#2563eb", fg="white", relief="flat", bd=0, padx=14, pady=8, cursor="hand2").grid(row=6, column=0, sticky="w", pady=(2, 12))
        self.machines_tree = ttk.Treeview(left, columns=("name", "model"), show="headings", selectmode="browse", height=10)
        self.machines_tree.heading("name", text="自定义机器"); self.machines_tree.heading("model", text="型号 / 说明"); self.machines_tree.column("name", width=150); self.machines_tree.column("model", width=150)
        self.machines_tree.grid(row=7, column=0, sticky="nsew"); self.machines_tree.bind("<<TreeviewSelect>>", self.load_selected_machine)
        right = tk.Frame(page, bg="white", padx=18, pady=16, highlightthickness=1, highlightbackground="#e2e8f0"); right.grid(row=2, column=1, sticky="nsew"); right.columnconfigure(0, weight=1); right.rowconfigure(4, weight=1)
        tk.Label(right, text="所需配件看板", bg="white", fg="#172033", font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        self.machine_status_bar = tk.Frame(right, bg="#edf2f8", padx=10, pady=8); self.machine_status_bar.grid(row=1, column=0, sticky="ew", pady=(10, 4))
        self.machine_status_values = []
        for title, color in (("缺货", "#ef4444"), ("偏低", "#f59e0b"), ("充足", "#10b981")):
            state = tk.Label(self.machine_status_bar, text=f"{title}  0", bg="#edf2f8", fg=color, font=("Microsoft YaHei UI", 9, "bold"))
            state.pack(side="left", padx=(0, 20)); self.machine_status_values.append(state)
        add = tk.Frame(right, bg="white"); add.grid(row=2, column=0, sticky="ew", pady=12); add.columnconfigure(0, weight=1)
        self.machine_part_combo = ttk.Combobox(add, textvariable=self.machine_vars["part"], values=self.get_part_names(), state="readonly"); self.machine_part_combo.grid(row=0, column=0, sticky="ew", ipady=4)
        ttk.Entry(add, textvariable=self.machine_vars["part_quantity"], width=9).grid(row=0, column=1, padx=8, ipady=4)
        tk.Button(add, text="加入配件", command=self.add_machine_part, bg="#e0f2fe", fg="#0369a1", relief="flat", bd=0, padx=12, pady=7, cursor="hand2").grid(row=0, column=2)
        self.machine_parts_tree = ttk.Treeview(right, columns=("part", "quantity", "stock"), show="headings", selectmode="browse")
        for key, title, width in (("part", "零件", 230), ("quantity", "每台需求", 110), ("stock", "现有库存", 110)):
            self.machine_parts_tree.heading(key, text=title); self.machine_parts_tree.column(key, width=width, anchor="center" if key != "part" else "w")
        self.machine_parts_tree.tag_configure("missing", background="#fff1f2", foreground="#b91c1c")
        self.machine_parts_tree.tag_configure("low", background="#fffbeb", foreground="#a16207")
        self.machine_parts_tree.tag_configure("ready", background="#ecfdf5", foreground="#047857")
        self.machine_parts_tree.grid(row=4, column=0, sticky="nsew")
        tk.Button(right, text="移除选中配件", command=self.remove_machine_part, bg="#fff1f2", fg="#be123c", relief="flat", bd=0, padx=12, pady=8, cursor="hand2").grid(row=5, column=0, sticky="w", pady=(12, 0))
        self.selected_machine_id = None; self.refresh_machines(); self._apply_surface_theme(page)

    def get_part_names(self): return [row[0] for row in self.conn.execute("SELECT name FROM parts ORDER BY name").fetchall()]

    def refresh_machines(self):
        if not hasattr(self, "machines_tree"): return
        self.machines_tree.delete(*self.machines_tree.get_children())
        for row in self.conn.execute("SELECT * FROM machines ORDER BY name").fetchall(): self.machines_tree.insert("", "end", iid=str(row["id"]), values=(row["name"], row["model"] or ""))
        self.machine_part_combo.configure(values=self.get_part_names())
        if self.selected_machine_id: self.refresh_machine_parts()

    def load_selected_machine(self, _event=None):
        selected = self.machines_tree.selection()
        if not selected: return
        self.selected_machine_id = int(selected[0]); row = self.conn.execute("SELECT * FROM machines WHERE id=?", (self.selected_machine_id,)).fetchone()
        self.machine_vars["name"].set(row["name"]); self.machine_vars["model"].set(row["model"] or ""); self.machine_vars["notes"].set(row["notes"] or ""); self.refresh_machine_parts()

    def save_machine(self):
        name = self.machine_vars["name"].get().strip()
        if not name: messagebox.showwarning("请填写名称", "请输入机器名称。", parent=self); return
        now = datetime.now().isoformat(timespec="seconds")
        try:
            if self.selected_machine_id: self.conn.execute("UPDATE machines SET name=?, model=?, notes=?, updated_at=? WHERE id=?", (name, self.machine_vars["model"].get().strip(), self.machine_vars["notes"].get().strip(), now, self.selected_machine_id))
            else:
                cursor = self.conn.execute("INSERT INTO machines(name, model, notes, updated_at) VALUES (?, ?, ?, ?)", (name, self.machine_vars["model"].get().strip(), self.machine_vars["notes"].get().strip(), now)); self.selected_machine_id = cursor.lastrowid
        except sqlite3.IntegrityError: messagebox.showwarning("名称重复", "已有同名机器，请修改后保存。", parent=self); return
        self.conn.commit(); self.refresh_machines(); self.refresh_machine_choices(); self.sync_to_cloud(); self.show_toast("机器构成已保存")

    def add_machine_part(self):
        if not self.selected_machine_id: messagebox.showwarning("先保存机器", "请先保存或选择一台机器。", parent=self); return
        part_name = self.machine_vars["part"].get()
        try: quantity = int(self.machine_vars["part_quantity"].get())
        except ValueError: quantity = 0
        part = self.conn.execute("SELECT id FROM parts WHERE name=?", (part_name,)).fetchone()
        if not part or quantity < 1: messagebox.showwarning("配件不正确", "请选择已创建的配件并输入至少 1 的需求数量。", parent=self); return
        self.conn.execute("INSERT INTO machine_parts(machine_id, part_id, quantity) VALUES (?, ?, ?) ON CONFLICT(machine_id, part_id) DO UPDATE SET quantity=excluded.quantity", (self.selected_machine_id, part[0], quantity)); self.conn.commit(); self.refresh_machine_parts(); self.sync_to_cloud()

    def refresh_machine_parts(self):
        if not hasattr(self, "machine_parts_tree"): return
        self.machine_parts_tree.delete(*self.machine_parts_tree.get_children())
        rows = self.conn.execute("SELECT mp.id, p.name, mp.quantity, p.stock_quantity FROM machine_parts mp JOIN parts p ON p.id=mp.part_id WHERE mp.machine_id=? ORDER BY p.name", (self.selected_machine_id,)).fetchall()
        counts = {"missing": 0, "low": 0, "ready": 0}
        for row in rows:
            stock = int(row["stock_quantity"])
            tag = "missing" if stock == 0 else ("low" if stock <= 10 else "ready")
            counts[tag] += 1
            self.machine_parts_tree.insert("", "end", iid=str(row["id"]), values=(row["name"], row["quantity"], stock), tags=(tag,))
        if hasattr(self, "machine_status_values"):
            for label, value in zip(self.machine_status_values, (counts["missing"], counts["low"], counts["ready"])):
                label.configure(text=label.cget("text").split()[0] + f"  {value}")
        if hasattr(self, "machines_page"):
            self._apply_surface_theme(self.machines_page)

    def remove_machine_part(self):
        selected = self.machine_parts_tree.selection()
        if not selected: return
        self.conn.execute("DELETE FROM machine_parts WHERE id=?", (int(selected[0]),)); self.conn.commit(); self.refresh_machine_parts(); self.sync_to_cloud()

    def open_stocking(self):
        self._set_active_nav("stock")
        if hasattr(self, "stocking_page") and self.stocking_page.winfo_exists():
            self._show_page(self.stocking_page)
            self.refresh_stocking_table()
            return
        page = tk.Frame(self.page_host, bg="#f5f7fb", padx=34, pady=28)
        self.stocking_page = page
        self._show_page(page)
        page.columnconfigure(0, weight=1); page.rowconfigure(3, weight=1)
        top = tk.Frame(page, bg="#f5f7fb")
        top.grid(row=0, column=0, sticky="ew")
        tk.Label(top, text="备货单", bg="#f5f7fb", fg="#172033", font=("Microsoft YaHei UI", 22, "bold")).pack(side="left")
        tk.Label(top, text="订单新增后自动生成，按交货期安排备货", bg="#f5f7fb", fg="#7b879b", font=("Microsoft YaHei UI", 10)).pack(side="left", padx=14, pady=(8, 0))
        modules = tk.Frame(page, bg="#f5f7fb")
        modules.grid(row=1, column=0, sticky="ew", pady=(20, 10))
        self.stock_filter = "全部"
        self.stock_module_buttons = {}
        for title, filter_value, color in (("全部订单", "全部", "#2563eb"), ("待备货", "待备货", "#d97706"), ("已备货", "已备货", "#059669"), ("待发货", "待发货", "#7c3aed")):
            module = tk.Button(modules, text=title + "\n0", command=lambda value=filter_value: self.set_stock_filter(value), bg="white", fg=color,
                               relief="flat", bd=0, activebackground="#eaf2ff", font=("Microsoft YaHei UI", 10, "bold"), padx=18, pady=10, cursor="hand2")
            module.pack(side="left", fill="x", expand=True, padx=(0, 10))
            self.stock_module_buttons[filter_value] = module
        self.stock_summary = tk.Label(page, text="", bg="#f5f7fb", fg="#64748b", font=("Microsoft YaHei UI", 9))
        self.stock_summary.grid(row=2, column=0, sticky="w", pady=(0, 9))
        card = tk.Frame(page, bg="white", padx=22, pady=18, highlightthickness=1, highlightbackground="#e5eaf1")
        card.grid(row=3, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1); card.rowconfigure(1, weight=1)
        actions = tk.Frame(card, bg="white"); actions.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        tk.Label(actions, text="待处理备货项目", bg="white", fg="#172033", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        tk.Button(actions, text="标记为已备货", command=self.mark_stocked, bg="#2563eb", fg="white", relief="flat", bd=0,
                  activebackground="#1d4ed8", activeforeground="white", font=("Microsoft YaHei UI", 9, "bold"), padx=14, pady=8, cursor="hand2").pack(side="right")
        self.stock_selected_id = None
        self.stock_canvas = tk.Canvas(card, bg="#f8fafc", highlightthickness=0, bd=0)
        scroll = ttk.Scrollbar(card, orient="vertical", command=self.stock_canvas.yview)
        self.stock_canvas.configure(yscrollcommand=scroll.set)
        self.stock_cards_frame = tk.Frame(self.stock_canvas, bg="#f8fafc")
        self.stock_canvas_window = self.stock_canvas.create_window((0, 0), window=self.stock_cards_frame, anchor="nw")
        self.stock_cards_frame.bind("<Configure>", lambda _event: self.stock_canvas.configure(scrollregion=self.stock_canvas.bbox("all")))
        self.stock_canvas.bind("<Configure>", lambda event: self.stock_canvas.itemconfigure(self.stock_canvas_window, width=event.width))
        self.stock_canvas.bind("<MouseWheel>", lambda event: self.stock_canvas.yview_scroll(int(-event.delta / 120), "units"))
        self.stock_canvas.grid(row=1, column=0, sticky="nsew"); scroll.grid(row=1, column=1, sticky="ns")
        self.refresh_stocking_table()
        self._apply_surface_theme(page)

    def refresh_stocking_table(self):
        if not hasattr(self, "stock_cards_frame"):
            return
        for item in self.stock_cards_frame.winfo_children():
            item.destroy()
        filter_value = getattr(self, "stock_filter", "全部")
        where = "WHERE r.status NOT IN ('已发货', '已取消', '已撤销')"
        params = []
        if filter_value == "待发货":
            where += " AND r.status='待发货'"
        elif filter_value != "全部":
            where += " AND s.stock_status=?"; params.append(filter_value)
        rows = self.conn.execute("SELECT s.*, r.contact, r.phone, r.status AS order_status FROM stocking_items s JOIN records r ON r.id=s.record_id " + where + " ORDER BY CASE s.stock_status WHEN '待备货' THEN 0 ELSE 1 END, s.delivery_date, s.id", params).fetchall()
        if self.stock_selected_id and not any(row["id"] == self.stock_selected_id for row in rows):
            self.stock_selected_id = None
        for index, row in enumerate(rows):
            self._create_stock_card(row, index)
        if not rows:
            tk.Label(self.stock_cards_frame, text="当前模块没有备货项目", bg="#f8fafc", fg="#94a3b8", font=("Microsoft YaHei UI", 11)).pack(pady=46)
        all_rows = self.conn.execute("SELECT s.stock_status, r.status FROM stocking_items s JOIN records r ON r.id=s.record_id WHERE r.status NOT IN ('已发货', '已取消', '已撤销')").fetchall()
        counts = {"全部": len(all_rows), "待备货": sum(row["stock_status"] == "待备货" for row in all_rows), "已备货": sum(row["stock_status"] == "已备货" for row in all_rows), "待发货": sum(row["status"] == "待发货" for row in all_rows)}
        for key, button in getattr(self, "stock_module_buttons", {}).items():
            label = {"全部": "全部订单", "待备货": "待备货", "已备货": "已备货", "待发货": "待发货"}[key]
            button.configure(text=f"{label}\n{counts[key]}", bg="#dbeafe" if key == filter_value else "white")
        self.stock_summary.config(text=f"当前查看“{filter_value}”模块 · 共 {len(rows)} 条项目")
        self._apply_surface_theme(self.stocking_page)

    def _create_stock_card(self, row, index):
        """备货客户信息卡；一张卡对应一条订单，可整卡点击选中。"""
        selected = row["id"] == self.stock_selected_id
        state_color = {"待备货": "#f59e0b", "已备货": "#10b981", "待发货": "#7c3aed"}.get(row["stock_status"], "#64748b")
        background = "#eff6ff" if selected else "#ffffff"
        border = "#60a5fa" if selected else "#e2e8f0"
        card = tk.Frame(self.stock_cards_frame, bg=background, highlightthickness=1, highlightbackground=border, padx=16, pady=13, cursor="hand2")
        card.grid(row=index // 2, column=index % 2, sticky="nsew", padx=(0, 12) if index % 2 == 0 else 0, pady=(0, 12))
        self.stock_cards_frame.columnconfigure(0, weight=1); self.stock_cards_frame.columnconfigure(1, weight=1)
        strip = tk.Frame(card, bg=state_color, width=5); strip.pack(side="left", fill="y", padx=(0, 12)); strip.pack_propagate(False)
        content = tk.Frame(card, bg=background); content.pack(side="left", fill="both", expand=True)
        head = tk.Frame(content, bg=background); head.pack(fill="x")
        tk.Label(head, text=row["customer"], bg=background, fg="#172033", font=("Microsoft YaHei UI", 12, "bold"), cursor="hand2").pack(side="left")
        tk.Label(head, text=row["stock_status"], bg=state_color, fg="white", font=("Microsoft YaHei UI", 8, "bold"), padx=8, pady=3, cursor="hand2").pack(side="right")
        contact = row["contact"] or "未填写联系人"
        phone = row["phone"] or "未填写电话"
        tk.Label(content, text=f"{contact}  ·  {phone}", bg=background, fg="#64748b", font=("Microsoft YaHei UI", 8), cursor="hand2").pack(anchor="w", pady=(5, 9))
        tk.Label(content, text=row["product"] or "未填写产品", bg=background, fg="#334155", font=("Microsoft YaHei UI", 10, "bold"), cursor="hand2").pack(anchor="w")
        tk.Label(content, text=f"型号 / 规格：{row['spec'] or '—'}", bg=background, fg="#64748b", font=("Microsoft YaHei UI", 8), cursor="hand2").pack(anchor="w", pady=(3, 9))
        foot = tk.Frame(content, bg=background); foot.pack(fill="x")
        tk.Label(foot, text=f"数量  {row['quantity'] or '—'}", bg=background, fg="#334155", font=("Microsoft YaHei UI", 9, "bold"), cursor="hand2").pack(side="left")
        tk.Label(foot, text=f"交期  {row['delivery_date']}", bg=background, fg="#c2410c" if row["stock_status"] == "待备货" else "#64748b", font=("Microsoft YaHei UI", 8, "bold"), cursor="hand2").pack(side="right")
        self._bind_stock_card_click(card, row["id"])

    def _bind_stock_card_click(self, widget, item_id):
        widget.bind("<Button-1>", lambda _event, value=item_id: self.select_stock_card(value))
        for child in widget.winfo_children():
            self._bind_stock_card_click(child, item_id)

    def select_stock_card(self, item_id):
        self.stock_selected_id = item_id
        self.refresh_stocking_table()
        self.show_stock_detail(item_id)

    def _order_parts_readiness(self, item_id):
        row = self.conn.execute("""SELECT s.*, r.product FROM stocking_items s JOIN records r ON r.id=s.record_id WHERE s.id=?""", (item_id,)).fetchone()
        if not row: return None, []
        machine = self.conn.execute("SELECT id, name FROM machines WHERE name=?", (row["product"],)).fetchone()
        if not machine: return row, []
        amount = max(1, int(float(row["quantity"] or 1)))
        parts = self.conn.execute("""SELECT p.name, p.unit, p.stock_quantity, mp.quantity AS per_machine
            FROM machine_parts mp JOIN parts p ON p.id=mp.part_id WHERE mp.machine_id=? ORDER BY p.name""", (machine["id"],)).fetchall()
        return row, [{"name": part["name"], "unit": part["unit"], "stock": int(part["stock_quantity"]), "need": int(part["per_machine"]) * amount} for part in parts]

    def show_stock_detail(self, item_id):
        row, parts = self._order_parts_readiness(item_id)
        if not row: return
        dialog = tk.Toplevel(self); dialog.title("备货详情"); dialog.transient(self); dialog.configure(bg="#f5f7fb")
        width, height = min(540, self.winfo_screenwidth() - 40), min(720, self.winfo_screenheight() - 80)
        x = self.winfo_rootx() + max(0, (self.winfo_width() - width) // 2); y = self.winfo_rooty() + max(0, (self.winfo_height() - height) // 2)
        x = max(20, min(x, self.winfo_screenwidth() - width - 20)); y = max(20, min(y, self.winfo_screenheight() - height - 40))
        dialog.geometry(f"{width}x{height}+{x}+{y}"); dialog.minsize(420, 560); dialog.grab_set()
        head = tk.Frame(dialog, bg="#172b4d", padx=24, pady=22); head.pack(fill="x")
        tk.Label(head, text="备货详情", bg="#172b4d", fg="white", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        tk.Label(head, text=f"{row['customer']}  ·  {row['product']}  ·  订单数量 {row['quantity'] or '1'}", bg="#172b4d", fg="#c8d7ef", font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(5, 0))
        box = tk.Frame(dialog, bg="white", padx=22, pady=18); box.pack(fill="both", expand=True, padx=18, pady=18)
        tk.Label(box, text="所需备件", bg="white", fg="#172033", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        if not parts:
            tk.Label(box, text="这条订单尚未选择已定义机器，或该机器还没有配置配件。\n请先在“机器构成”中创建对应机器与所需零件。", bg="white", fg="#64748b", justify="left", font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=30)
        else:
            ready = all(part["stock"] >= part["need"] for part in parts)
            tk.Label(box, text="✓ 库存满足，可进行备货" if ready else "✕ 有零件库存不足，请先补充库存", bg="white", fg="#059669" if ready else "#dc2626", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(7, 14))
            for part in parts:
                enough = part["stock"] >= part["need"]; missing = max(0, part["need"] - part["stock"])
                item = tk.Frame(box, bg="#f8fafc", padx=14, pady=12, highlightthickness=1, highlightbackground="#e2e8f0"); item.pack(fill="x", pady=(0, 8))
                tk.Label(item, text="✓" if enough else "✕", bg="#f8fafc", fg="#16a34a" if enough else "#dc2626", font=("Segoe UI", 16, "bold")).pack(side="left")
                tk.Label(item, text=part["name"], bg="#f8fafc", fg="#172033", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left", padx=9)
                text = f"需 {part['need']} {part['unit']} · 库存 {part['stock']} {part['unit']}" + (f" · 缺 {missing}" if not enough else "")
                tk.Label(item, text=text, bg="#f8fafc", fg="#64748b" if enough else "#dc2626", font=("Microsoft YaHei UI", 9)).pack(side="right")
        tk.Button(dialog, text="关闭", command=dialog.destroy, bg="#2563eb", fg="white", relief="flat", bd=0, padx=22, pady=9, cursor="hand2").pack(pady=(0, 18))

    def set_stock_filter(self, value):
        self.stock_filter = value
        self.refresh_stocking_table()

    def mark_stocked(self):
        item_id = getattr(self, "stock_selected_id", None)
        if not item_id:
            messagebox.showwarning("未选择项目", "请先选择一条备货项目。", parent=self)
            return
        row = self.conn.execute("SELECT stock_status, record_id FROM stocking_items WHERE id=?", (item_id,)).fetchone()
        next_status = "待备货" if row and row["stock_status"] == "已备货" else "已备货"
        if next_status == "已备货":
            _order, parts = self._order_parts_readiness(item_id)
            insufficient = [part["name"] for part in parts if part["stock"] < part["need"]]
            if insufficient:
                messagebox.showwarning("库存不足", "以下零件库存不足，暂不能标记为已备货：\n" + "、".join(insufficient), parent=self)
                return
        self.conn.execute("UPDATE stocking_items SET stock_status=?, updated_at=? WHERE id=?", (next_status, datetime.now().isoformat(timespec="seconds"), item_id))
        if next_status == "已备货":
            self.conn.execute("UPDATE records SET status='待发货' WHERE id=? AND status NOT IN ('已发货', '已取消', '已撤销')", (row["record_id"],))
        self.conn.commit(); self.refresh_all_data(); self.sync_to_cloud()
        self.show_toast("备货状态已更新为“" + next_status + "”")

    def ship_selected_order(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("未选择订单", "请先在订单列表选择需要发货的客户订单。", parent=self)
            return
        record_id = int(selected[0])
        row = self.conn.execute("SELECT customer, product, status FROM records WHERE id=?", (record_id,)).fetchone()
        if not row:
            return
        if row["status"] == "已发货":
            messagebox.showinfo("无需重复操作", "该订单已发货。", parent=self)
            return
        self.conn.execute("UPDATE records SET status='已发货', shipping_at=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), record_id))
        self.conn.commit()
        self.clear_form(); self.refresh_all_data(); self.sync_to_cloud()
        self.show_toast(f"已发货：{row['customer']} · {row['product']}")

    def revoke_selected_order(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("未选择订单", "请先在订单列表选择需要撤销的订单。", parent=self)
            return
        record_id = int(selected[0])
        row = self.conn.execute("SELECT customer, product, status FROM records WHERE id=?", (record_id,)).fetchone()
        if not row:
            return
        if row["status"] == "已发货":
            messagebox.showwarning("无法撤销", "订单已发货，请在业务记录中另行处理。", parent=self)
            return
        if not messagebox.askyesno("确认撤销", f"确定撤销“{row['customer']} · {row['product']}”吗？\n撤销后将不计入备货与数据统计。", parent=self):
            return
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("UPDATE records SET status='已撤销' WHERE id=?", (record_id,))
        self.conn.execute("UPDATE stocking_items SET stock_status='已撤销', updated_at=? WHERE record_id=?", (now, record_id))
        self.conn.commit()
        self.clear_form(); self.refresh_all_data(); self.sync_to_cloud()
        self.show_toast(f"订单已撤销：{row['customer']} · {row['product']}")

    def open_shipped(self):
        self._set_active_nav("shipped")
        if hasattr(self, "shipped_page") and self.shipped_page.winfo_exists():
            self._show_page(self.shipped_page)
            self.refresh_shipped_table(); return
        page = tk.Frame(self.page_host, bg="#f5f7fb", padx=34, pady=28)
        self.shipped_page = page
        self._show_page(page)
        page.columnconfigure(0, weight=1); page.rowconfigure(2, weight=1)
        tk.Label(page, text="已发货客户", bg="#f5f7fb", fg="#172033", font=("Microsoft YaHei UI", 22, "bold")).grid(row=0, column=0, sticky="w")
        self.shipped_summary = tk.Label(page, text="", bg="#f5f7fb", fg="#64748b", font=("Microsoft YaHei UI", 10))
        self.shipped_summary.grid(row=1, column=0, sticky="w", pady=(8, 18))
        card = tk.Frame(page, bg="white", padx=22, pady=18, highlightthickness=1, highlightbackground="#e5eaf1")
        card.grid(row=2, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1); card.rowconfigure(1, weight=1)
        tk.Label(card, text="客户发货档案", bg="white", fg="#172033", font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 14))
        columns = ("customer", "contact", "phone", "product", "spec", "quantity", "delivery_date", "shipping_at")
        headings = ("客户", "联系人", "联系电话", "产品", "型号 / 规格", "数量", "交货期限", "发货时间")
        widths = (150, 110, 135, 170, 175, 80, 115, 160)
        self.shipped_tree = ttk.Treeview(card, columns=columns, show="headings")
        for col, heading, width in zip(columns, headings, widths):
            self.shipped_tree.heading(col, text=heading); self.shipped_tree.column(col, width=width, anchor="center" if col in ("quantity", "delivery_date") else "w")
        scroll = ttk.Scrollbar(card, orient="vertical", command=self.shipped_tree.yview)
        self.shipped_tree.configure(yscrollcommand=scroll.set)
        self.shipped_tree.grid(row=1, column=0, sticky="nsew"); scroll.grid(row=1, column=1, sticky="ns")
        self.refresh_shipped_table()
        self._apply_surface_theme(page)

    def refresh_shipped_table(self):
        if not hasattr(self, "shipped_tree"): return
        for item in self.shipped_tree.get_children(): self.shipped_tree.delete(item)
        rows = self.conn.execute("SELECT * FROM records WHERE status='已发货' ORDER BY shipping_at DESC, id DESC").fetchall()
        for row in rows:
            shipped_at = (row["shipping_at"] or "").replace("T", " ")
            values = (row["customer"], row["contact"], row["phone"], row["product"], row["spec"], row["quantity"], row["delivery_date"], shipped_at)
            self.shipped_tree.insert("", "end", values=values)
        self.shipped_summary.config(text=f"已存档 {len(rows)} 条已发货客户订单，可随时查询客户、产品与发货时间。")
        if hasattr(self, "shipped_page"):
            self._apply_surface_theme(self.shipped_page)

    def show_orders(self):
        self._show_page(self.body)
        self._set_active_nav("order")

    def render_statistics(self, period):
        self.stat_period.set(period)
        today = date.today()
        if period == "week":
            start = today - timedelta(days=today.weekday()); end = start + timedelta(days=6)
            labels = [(start + timedelta(days=i), f"周{'一二三四五六日'[i]}") for i in range(7)]
            caption = f"{start:%m.%d} – {end:%m.%d}"
        elif period == "year":
            start = date(today.year, 1, 1); end = date(today.year, 12, 31)
            labels = [(date(today.year, month, 1), f"{month}月") for month in range(1, 13)]
            caption = f"{today.year} 年"
        else:
            start = date(today.year, today.month, 1); end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
            labels = [(date(today.year, today.month, day), str(day)) for day in range(1, end.day + 1)]
            caption = f"{today.year} 年 {today.month} 月"
        previous_start = start.replace(year=start.year - 1); previous_end = end.replace(year=end.year - 1)
        rows = self.conn.execute("SELECT quantity, price, created_at FROM records WHERE status NOT IN ('已撤销', '已取消')").fetchall()
        current_qty = current_amount = old_qty = old_amount = 0.0
        groups = {item[0]: [0.0, 0.0] for item in labels}
        for row in rows:
            try: order_day = datetime.fromisoformat(row["created_at"]).date()
            except (TypeError, ValueError): continue
            qty = self._number(row["quantity"]); amount = qty * self._number(row["price"])
            if start <= order_day <= end:
                current_qty += qty; current_amount += amount
                group_key = date(order_day.year, order_day.month, 1) if period == "year" else order_day
                if group_key in groups:
                    groups[group_key][0] += qty; groups[group_key][1] += amount
            if previous_start <= order_day <= previous_end:
                old_qty += qty; old_amount += amount
        def ratio(now, old): return "—" if old == 0 else f"{(now - old) / old:+.1%}"
        values = (f"{current_qty:g}", f"¥ {current_amount:,.2f}", ratio(current_amount, old_amount), ratio(current_qty, old_qty))
        for label, value in zip(self.analytics_values, values): label.config(text=value)
        self.period_caption.config(text=caption + " · 对比去年同期")
        for key, button in self.period_buttons.items():
            active = key == period
            button.configure(bg="#2563eb" if active else "#eaf0f8", fg="white" if active else "#526076", activebackground="#1d4ed8" if active else "#dce7f5")
        self.chart_data = ([item[1] for item in labels], [groups[item[0]][0] for item in labels], [groups[item[0]][1] for item in labels])
        self.draw_chart()

    def draw_chart(self):
        if not hasattr(self, "chart_canvas") or not hasattr(self, "chart_data"): return
        canvas = self.chart_canvas; canvas.delete("all")
        width, height = max(canvas.winfo_width(), 100), max(canvas.winfo_height(), 100)
        labels, quantities, amounts = self.chart_data
        left, right, top, bottom = 40, 20, 14, 34
        plot_w, plot_h = width - left - right, height - top - bottom
        for step in range(5):
            y = top + plot_h * step / 4
            canvas.create_line(left, y, width - right, y, fill="#edf1f6")
        count = max(len(labels), 2)
        def points(values):
            maximum = max(values) if max(values, default=0) > 0 else 1
            return [(left + index * plot_w / (count - 1), top + plot_h - value / maximum * plot_h) for index, value in enumerate(values)]
        for values, color in ((amounts, "#7c3aed"), (quantities, "#2563eb")):
            pts = points(values)
            if len(pts) > 1: canvas.create_line(*[coord for point in pts for coord in point], fill=color, width=2, smooth=True)
            for x, y in pts: canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=color, outline="white", width=1)
        step = max(1, len(labels) // 8)
        for index in range(0, len(labels), step):
            x = left + index * plot_w / (count - 1)
            canvas.create_text(x, height - 15, text=labels[index], fill="#8b98aa", font=("Microsoft YaHei UI", 8))

    def values_from_form(self):
        return {key: self.entries[key].get().strip() for _label, key, _required in self.FIELDS}

    def refresh_delivery_preview(self):
        """交期以“今天 + 天数”计算，避免手动填写日期造成误差。"""
        if not hasattr(self, "delivery_preview"):
            return
        raw = self.entries["delivery_days"].get().strip()
        try:
            days = int(raw)
            if days < 0:
                raise ValueError
            due = date.today() + timedelta(days=days)
            self.delivery_preview.configure(text=f"预计交期：{due:%Y-%m-%d}（今天 + {days} 天）", fg="#2563eb")
        except ValueError:
            self.delivery_preview.configure(text="预计交期：请输入不小于 0 的整数天数", fg="#dc2626" if raw else "#64748b")

    def set_delivery_days_from_date(self, delivery_date):
        try:
            due = datetime.strptime(delivery_date, "%Y-%m-%d").date()
            self.entries["delivery_days"].delete(0, "end")
            self.entries["delivery_days"].insert(0, str(max(0, (due - date.today()).days)))
        except (ValueError, TypeError):
            self.entries["delivery_days"].delete(0, "end")
        self.refresh_delivery_preview()

    def save_record(self):
        values = self.values_from_form()
        values["notes"] = self.notes.get("1.0", "end").strip()
        missing = [label for label, key, required in self.FIELDS if required and not values[key]]
        if missing:
            messagebox.showwarning("请补全信息", "请填写：" + "、".join(missing))
            return
        try:
            delivery_days = int(values["delivery_days"])
            if delivery_days < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("交货天数不正确", "请输入不小于 0 的整数，例如 3 表示今天起 3 天后交货。", parent=self)
            return
        values["delivery_date"] = (date.today() + timedelta(days=delivery_days)).isoformat()
        columns = "customer, contact, phone, product, spec, quantity, price, delivery_date, status, notes"
        params = tuple(values[key] for key in ("customer", "contact", "phone", "product", "spec", "quantity", "price", "delivery_date", "status", "notes"))
        is_new = not self.selected_id
        if self.selected_id:
            self.conn.execute(f"UPDATE records SET {', '.join(f'{c}=?' for c in columns.split(', '))} WHERE id=?", params + (self.selected_id,))
            self.conn.execute("UPDATE stocking_items SET customer=?, product=?, spec=?, quantity=?, delivery_date=?, updated_at=? WHERE record_id=?", (values["customer"], values["product"], values["spec"], values["quantity"], values["delivery_date"], datetime.now().isoformat(timespec="seconds"), self.selected_id))
            notice = "订单已更新。"
        else:
            now = datetime.now().isoformat(timespec="seconds")
            cursor = self.conn.execute(f"INSERT INTO records ({columns}, created_at) VALUES ({','.join('?' * 10)}, ?)", params + (now,))
            self.conn.execute("INSERT INTO stocking_items(record_id, customer, product, spec, quantity, delivery_date, stock_status, updated_at) VALUES (?, ?, ?, ?, ?, ?, '待备货', ?)", (cursor.lastrowid, values["customer"], values["product"], values["spec"], values["quantity"], values["delivery_date"], now))
            notice = "订单已保存。"
        self.conn.commit()
        self.refresh_table()
        self.sync_to_cloud()
        self.clear_form()
        self.show_toast(notice)
        if is_new:
            self.play_customer_fireworks()

    def refresh_table(self):
        query = self.search_var.get().strip() if hasattr(self, "search_var") else ""
        for item in self.tree.get_children():
            self.tree.delete(item)
        # 主订单页保留已发货记录以方便回看；已发货行以低对比度显示，撤销/取消订单则不参与当前列表。
        where = " WHERE status NOT IN ('已取消', '已撤销')"
        params = ()
        if query:
            where += " AND (customer LIKE ? OR contact LIKE ? OR phone LIKE ? OR product LIKE ? OR spec LIKE ? OR price LIKE ? OR delivery_date LIKE ?)"
            params = tuple(f"%{query}%" for _ in range(7))
        rows = self.conn.execute("SELECT * FROM records" + where + " ORDER BY delivery_date ASC, id DESC", params).fetchall()
        for index, row in enumerate(rows):
            tags = ("shipped",) if row["status"] == "已发货" else (("zebra",) if index % 2 else ())
            self.tree.insert("", "end", iid=str(row["id"]), values=tuple(row[key] for key in ("id", "customer", "contact", "phone", "product", "spec", "quantity", "price", "delivery_date", "status")), tags=tags)
        total = self.conn.execute("SELECT COUNT(*) FROM records WHERE status NOT IN ('已取消', '已撤销')").fetchone()[0]
        pending = self.conn.execute("SELECT COUNT(*) FROM records WHERE status NOT IN ('已发货', '已交付', '已取消', '已撤销')").fetchone()[0]
        due = self.conn.execute("SELECT COUNT(*) FROM records WHERE status NOT IN ('已发货', '已交付', '已取消', '已撤销') AND delivery_date < ?", (date.today().isoformat(),)).fetchone()[0]
        near = self.conn.execute("SELECT COUNT(*) FROM records WHERE status NOT IN ('已发货', '已交付', '已取消', '已撤销') AND delivery_date BETWEEN ? AND ?", (date.today().isoformat(), date.fromordinal(date.today().toordinal() + 3).isoformat())).fetchone()[0]
        self.summary.config(text=f"当前显示 {len(rows)} 条 · 已逾期 {due} 条")
        for label, value in zip(self.stat_values, (total, pending, near)):
            label.config(text=str(value))

    def refresh_all_data(self):
        """统一刷新订单、备货、已发货及当前统计，确保撤销后所有数值同步。"""
        self.refresh_table()
        self.refresh_stocking_table()
        self.refresh_shipped_table()
        if hasattr(self, "statistics_page") and self.statistics_page.winfo_exists():
            self.render_statistics(self.stat_period.get())

    def show_toast(self, message):
        if hasattr(self, "toast") and self.toast.winfo_exists():
            self.toast.destroy()
        palette = THEMES[self.theme_name]
        self.toast = tk.Label(self.body, text="✓  " + message, bg=palette["accent"], fg="white", padx=16, pady=10,
                              font=("Microsoft YaHei UI", 10, "bold"))
        self.toast.place(relx=1, rely=1, x=-8, y=-8, anchor="se")
        self.after(1800, lambda: self.toast.destroy() if self.toast.winfo_exists() else None)

    def play_customer_fireworks(self):
        """新增客户订单后，从左右下角向中心斜向汇聚并绽放的轻量烟花。"""
        overlay = tk.Toplevel(self)
        overlay.overrideredirect(True)
        overlay.attributes("-topmost", True)
        transparent = "#ff00ff"
        try:
            overlay.attributes("-transparentcolor", transparent)
        except tk.TclError:
            transparent = self.body.cget("bg")
        x, y = self.winfo_rootx(), self.winfo_rooty()
        width, height = self.winfo_width(), self.winfo_height()
        overlay.geometry(f"{width}x{height}+{x}+{y}")
        canvas = tk.Canvas(overlay, bg=transparent, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        particles, colors = [], ("#60a5fa", "#a78bfa", "#f472b6", "#fbbf24", "#34d399")
        center_x, center_y = width / 2, height * 0.43
        for side in (0, width):
            for index in range(18):
                start_x, start_y = side + (12 if side == 0 else -12), height - 24 - (index % 5) * 10
                target_x = center_x + ((index % 6) - 3) * 24
                target_y = center_y + (index % 4) * 18
                item = canvas.create_oval(start_x - 3, start_y - 3, start_x + 3, start_y + 3, fill=colors[index % len(colors)], outline="")
                particles.append([item, start_x, start_y, target_x, target_y, index])
        frame = 0
        def animate():
            nonlocal frame
            frame += 1
            progress = min(frame / 34, 1.0)
            eased = 1 - (1 - progress) ** 3
            for item, start_x, start_y, target_x, target_y, index in particles:
                if progress < 0.72:
                    px = start_x + (target_x - start_x) * (eased / 0.72)
                    py = start_y + (target_y - start_y) * (eased / 0.72)
                    radius = 3
                else:
                    burst = (progress - 0.72) / 0.28
                    angle = index * 1.71
                    px = target_x + math.cos(angle) * burst * 82
                    py = target_y + math.sin(angle) * burst * 62 + burst * burst * 28
                    radius = max(1, 4 * (1 - burst))
                canvas.coords(item, px - radius, py - radius, px + radius, py + radius)
            if frame < 46:
                overlay.after(16, animate)
            else:
                overlay.destroy()
        animate()

    def load_selected(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        self.selected_id = int(selected[0])
        row = self.conn.execute("SELECT * FROM records WHERE id=?", (self.selected_id,)).fetchone()
        for _label, key, _required in self.FIELDS:
            field = self.entries[key]
            if key == "delivery_days":
                self.set_delivery_days_from_date(row["delivery_date"])
                continue
            if isinstance(field, ttk.Combobox):
                field.set(row[key] or "待交付")
            else:
                field.delete(0, "end")
                field.insert(0, row[key] or "")
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", row["notes"] or "")

    def clear_form(self):
        self.selected_id = None
        for _label, key, _required in self.FIELDS:
            field = self.entries[key]
            if key == "status":
                field.set("待交付")
            else:
                field.delete(0, "end")
        self.refresh_delivery_preview()
        self.notes.delete("1.0", "end")
        for item in self.tree.selection():
            self.tree.selection_remove(item)

    def delete_record(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("未选择订单", "请先在列表中选择需要删除的订单。")
            return
        if not messagebox.askyesno("确认删除", "确定删除选中的订单吗？此操作无法撤销。"):
            return
        self.conn.execute("DELETE FROM records WHERE id=?", (int(selected[0]),))
        self.conn.commit()
        self.clear_form()
        self.refresh_all_data()
        self.sync_to_cloud()

    def export_csv(self):
        destination = filedialog.asksaveasfilename(title="导出订单", defaultextension=".csv",
                                                   filetypes=[("CSV 文件", "*.csv")], initialfile="订单列表.csv")
        if not destination:
            return
        rows = self.conn.execute("SELECT id, customer, contact, phone, product, spec, quantity, price, delivery_date, status, notes, created_at FROM records ORDER BY delivery_date, id").fetchall()
        with open(destination, "w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            writer.writerow(["编号", "客户", "联系人", "电话", "产品", "型号/规格", "数量", "单价", "交货期限", "状态", "备注", "录入时间"])
            writer.writerows([tuple(row) for row in rows])
        messagebox.showinfo("导出完成", f"已导出 {len(rows)} 条订单。")

    def check_delivery_reminders(self):
        """兼容入口：按当前时段执行一次交货提醒。"""
        self.send_scheduled_reminders(datetime.now().hour)

    def schedule_next_alarm(self):
        """精确安排下一个 09:00 或 17:00 的提醒，不做持续轮询。"""
        now = datetime.now()
        candidates = [now.replace(hour=9, minute=0, second=0, microsecond=0), now.replace(hour=17, minute=0, second=0, microsecond=0)]
        upcoming = next((moment for moment in candidates if moment > now), (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0))
        delay = max(1000, int((upcoming - now).total_seconds() * 1000) + 120)
        self.after(delay, lambda slot=upcoming.hour: self._run_scheduled_alarm(slot))

    def _run_scheduled_alarm(self, slot):
        self.send_scheduled_reminders(slot)
        self.schedule_next_alarm()

    def send_scheduled_reminders(self, slot):
        """在交货前 3、2、1 天的指定时段播报；日志确保重启后不重复。"""
        if slot not in (9, 17):
            return
        today = date.today()
        due_rows = self.conn.execute("SELECT id, customer, product, delivery_date FROM records WHERE status NOT IN ('已发货', '已交付', '已取消', '已撤销')").fetchall()
        speech_lines, shown = [], []
        for row in due_rows:
            try:
                remaining = (datetime.strptime(row["delivery_date"], "%Y-%m-%d").date() - today).days
            except (TypeError, ValueError):
                continue
            if remaining not in (1, 2, 3):
                continue
            log_key = f"{today.isoformat()}-{slot}-{row['id']}"
            if log_key in self.reminder_log:
                continue
            self.reminder_log.add(log_key)
            speech_lines.append(f"客户 {row['customer']} 的 {row['product']}，还有 {remaining} 天到期。")
            shown.append(f"{row['customer']}｜{row['product']}（{remaining} 天后）")
        if not speech_lines:
            return
        save_reminder_log(self.reminder_log)
        self.play_reminder_sound("交货提醒。" + "".join(speech_lines), repeats=3)
        self.show_desktop_reminder(shown, slot)
        self.show_toast(f"{slot:02d}:00 交货提醒已播报：" + "、".join(shown))

    def show_desktop_reminder(self, orders, slot):
        """在当前桌面前景显示高对比置顶提醒窗口。"""
        alert = tk.Toplevel(self)
        alert.title("⚠ 交货提醒")
        alert.configure(bg="#fff7ed")
        alert.attributes("-topmost", True)
        alert.resizable(False, False)
        width, height = 600, 330
        x = (alert.winfo_screenwidth() - width) // 2
        y = (alert.winfo_screenheight() - height) // 3
        alert.geometry(f"{width}x{height}+{x}+{y}")
        header = tk.Frame(alert, bg="#dc2626", height=88)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="⚠  交货期限提醒", bg="#dc2626", fg="white", font=("Microsoft YaHei UI", 19, "bold")).pack(anchor="w", padx=28, pady=(17, 0))
        tk.Label(header, text=f"{slot:02d}:00 · 已通过扬声器播放三声提醒", bg="#dc2626", fg="#fee2e2", font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=31, pady=(3, 0))
        content = tk.Frame(alert, bg="#fff7ed", padx=28, pady=18)
        content.pack(fill="both", expand=True)
        tk.Label(content, text="以下订单将在三天内到期，请及时安排：", bg="#fff7ed", fg="#7c2d12", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        order_text = "\n".join("•  " + item for item in orders)
        tk.Label(content, text=order_text, justify="left", anchor="w", bg="#fff7ed", fg="#431407", font=("Microsoft YaHei UI", 11), wraplength=530).pack(anchor="w", pady=(10, 14))
        tk.Button(content, text="我知道了", command=alert.destroy, bg="#dc2626", fg="white", relief="flat", bd=0,
                  activebackground="#b91c1c", activeforeground="white", font=("Microsoft YaHei UI", 10, "bold"), padx=24, pady=9, cursor="hand2").pack(anchor="e")
        alert.lift()
        alert.focus_force()

    def play_reminder_sound(self, text, repeats=1):
        """通过 Windows 自带语音引擎从设备扬声器播报，不依赖网络。"""
        if self.speech_lock.locked():
            return
        self.speech_lock.acquire()

        def speak():
            try:
                try:
                    import winsound
                    for index in range(repeats):
                        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                        if index < repeats - 1:
                            time.sleep(0.38)
                except (ImportError, RuntimeError):
                    pass
                safe_text = text.replace("'", "''")
                command = (
                    "Add-Type -AssemblyName System.Speech; "
                    "$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                    "try { $speaker.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female, "
                    "[System.Speech.Synthesis.VoiceAge]::Adult, 0, [System.Globalization.CultureInfo]::GetCultureInfo('zh-CN')) } catch {} ; "
                    "$speaker.Rate = -1; $speaker.Volume = 100; "
                    f"$speaker.Speak('{safe_text}');"
                )
                subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", command],
                               check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            finally:
                self.speech_lock.release()

        threading.Thread(target=speak, daemon=True).start()

    def test_reminder_sound(self):
        self.play_reminder_sound("这是一条交货提醒测试。客户演示公司的数控设备，还有三天到期。", repeats=3)
        self.show_toast("正在通过设备扬声器播放三声测试铃声")

    def logout(self):
        if not messagebox.askyesno("退出登录", "退出后下次打开软件需要重新登录云端账户。", parent=self):
            return
        SESSION_PATH.unlink(missing_ok=True)
        self.cloud_token = None
        self.withdraw()
        dialog = CloudLoginDialog(self)
        self.wait_window(dialog)
        if dialog.success:
            self.cloud_token = dialog.token
            self.deiconify()
            self.sync_from_cloud()
        else:
            self.on_close()

    def on_close(self):
        self.conn.close()
        self.destroy()


if __name__ == "__main__":
    app = DeliveryApp()
    if app.authenticate():
        app.mainloop()
    else:
        app.on_close()
