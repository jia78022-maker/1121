"""交期管家账号服务器（Windows Server 2022 / Python 3 标准库可运行）。"""
import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "account_server.db"
HOST = "0.0.0.0"
PORT = 8080


def digest(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()


def connection():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS accounts (
        username TEXT PRIMARY KEY,
        salt TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        session_token TEXT,
        session_expires_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS cloud_orders (
        username TEXT PRIMARY KEY,
        records_json TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL,
        FOREIGN KEY(username) REFERENCES accounts(username)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS cloud_inventory (
        username TEXT PRIMARY KEY,
        inventory_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL,
        FOREIGN KEY(username) REFERENCES accounts(username)
    )""")
    return db


def issue_session(db, username):
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    db.execute("UPDATE accounts SET session_token=?, session_expires_at=? WHERE username=?", (token, expires, username))
    db.commit()
    return token


class Handler(BaseHTTPRequestHandler):
    server_version = "DeliveryAccountServer/1.0"

    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {self.address_string()} - {format % args}")

    def reply(self, status, payload):
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def authenticated_username(self, db):
        token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        account = db.execute("SELECT username, session_expires_at FROM accounts WHERE session_token=?", (token,)).fetchone()
        if not account or datetime.fromisoformat(account["session_expires_at"]) <= datetime.now(timezone.utc):
            return None
        return account["username"]

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self.reply(200, {"ok": True, "service": "交期管家账号服务器"})
        else:
            self.reply(404, {"ok": False, "message": "接口不存在。"})

    def do_POST(self):
        payload = self.read_json()
        if not isinstance(payload, dict):
            self.reply(400, {"ok": False, "message": "请求格式不正确。"})
            return
        db = connection()
        try:
            path = self.path.rstrip("/")
            if path in ("/api/sync/pull", "/api/sync/push"):
                username = self.authenticated_username(db)
                if not username:
                    self.reply(401, {"ok": False, "message": "登录状态已失效，请重新登录。"})
                    return
                if path == "/api/sync/pull":
                    row = db.execute("SELECT records_json, updated_at FROM cloud_orders WHERE username=?", (username,)).fetchone()
                    inventory = db.execute("SELECT inventory_json FROM cloud_inventory WHERE username=?", (username,)).fetchone()
                    self.reply(200, {"ok": True, "records": json.loads(row["records_json"]) if row else [], "inventory": json.loads(inventory["inventory_json"]) if inventory else {}, "updated_at": row["updated_at"] if row else None})
                    return
                records = payload.get("records")
                if not isinstance(records, list):
                    self.reply(400, {"ok": False, "message": "订单数据格式不正确。"})
                    return
                now = datetime.now(timezone.utc).isoformat()
                db.execute("INSERT INTO cloud_orders(username, records_json, updated_at) VALUES (?, ?, ?) ON CONFLICT(username) DO UPDATE SET records_json=excluded.records_json, updated_at=excluded.updated_at", (username, json.dumps(records, ensure_ascii=False), now))
                inventory = payload.get("inventory")
                if isinstance(inventory, dict):
                    db.execute("INSERT INTO cloud_inventory(username, inventory_json, updated_at) VALUES (?, ?, ?) ON CONFLICT(username) DO UPDATE SET inventory_json=excluded.inventory_json, updated_at=excluded.updated_at", (username, json.dumps(inventory, ensure_ascii=False), now))
                db.commit()
                self.reply(200, {"ok": True, "updated_at": now})
                return
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", ""))
            if len(username) < 6 or len(username) > 20 or not username.isdigit() or len(password) < 6:
                self.reply(400, {"ok": False, "message": "请输入 6 至 20 位手机号和至少 6 位密码。"})
                return
            if path == "/api/register":
                if db.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone():
                    self.reply(409, {"ok": False, "message": "该账号已存在，请直接登录。"})
                    return
                salt = secrets.token_hex(16)
                db.execute("INSERT INTO accounts (username, salt, password_hash, created_at) VALUES (?, ?, ?, ?)",
                           (username, salt, digest(password, salt), datetime.now(timezone.utc).isoformat()))
                token = issue_session(db, username)
                self.reply(201, {"ok": True, "username": username, "token": token})
            elif path == "/api/login":
                account = db.execute("SELECT salt, password_hash FROM accounts WHERE username=?", (username,)).fetchone()
                if not account or digest(password, account["salt"]) != account["password_hash"]:
                    self.reply(401, {"ok": False, "message": "账号或密码不正确。"})
                    return
                token = issue_session(db, username)
                self.reply(200, {"ok": True, "username": username, "token": token})
            else:
                self.reply(404, {"ok": False, "message": "接口不存在。"})
        finally:
            db.close()


if __name__ == "__main__":
    with ThreadingHTTPServer((HOST, PORT), Handler) as server:
        print(f"账号服务器已启动：http://{HOST}:{PORT}")
        server.serve_forever()
