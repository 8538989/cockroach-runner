#!/usr/bin/env python3
import argparse
import base64
import concurrent.futures
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

APP_NAME = "蟑螂快跑"
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".iso"}


def now(): return int(time.time())
def dumps(value): return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, data_dir: str, encryption_key: str):
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "cockroach-runner.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.crypt = Fernet(encryption_key.encode())
        with self.db:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,secret INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS users(
              tg_id INTEGER PRIMARY KEY,username TEXT NOT NULL DEFAULT '',name TEXT NOT NULL DEFAULT '',
              cookie TEXT NOT NULL DEFAULT '',uid TEXT NOT NULL DEFAULT '',target_cid TEXT NOT NULL DEFAULT '0',
              target_name TEXT NOT NULL DEFAULT '根目录',enabled INTEGER NOT NULL DEFAULT 1,
              mode TEXT NOT NULL DEFAULT 'all',categories TEXT NOT NULL DEFAULT '[]',include_terms TEXT NOT NULL DEFAULT '',
              exclude_terms TEXT NOT NULL DEFAULT '',hierarchy INTEGER NOT NULL DEFAULT 0,
              created INTEGER NOT NULL,updated INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS resources(
              id TEXT PRIMARY KEY,node_id TEXT NOT NULL,name TEXT NOT NULL,pickcode TEXT NOT NULL DEFAULT '',sha1 TEXT NOT NULL DEFAULT '',
              size INTEGER NOT NULL DEFAULT 0,is_dir INTEGER NOT NULL DEFAULT 0,category TEXT NOT NULL DEFAULT '其他',
              first_seen INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'waiting',error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS deliveries(
              id TEXT PRIMARY KEY,resource_id TEXT NOT NULL,tg_id INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'waiting',
              attempts INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',created INTEGER NOT NULL,updated INTEGER NOT NULL,
              UNIQUE(resource_id,tg_id));
            CREATE TABLE IF NOT EXISTS bot_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources(
              id TEXT PRIMARY KEY,name TEXT NOT NULL,cid TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,
              poll_seconds INTEGER NOT NULL DEFAULT 60,stable_seconds INTEGER NOT NULL DEFAULT 30,
              retention_minutes INTEGER NOT NULL DEFAULT -1,baseline_ready INTEGER NOT NULL DEFAULT 0,
              historical INTEGER NOT NULL DEFAULT 0,added INTEGER NOT NULL DEFAULT 0,last_scan INTEGER NOT NULL DEFAULT 0,
              next_scan INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',created INTEGER NOT NULL,updated INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS bindings(
              id TEXT PRIMARY KEY,code_hash TEXT NOT NULL UNIQUE,code_hint TEXT NOT NULL,expires INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'available',used_by TEXT NOT NULL DEFAULT '',created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
              id TEXT PRIMARY KEY,kind TEXT NOT NULL,object_id TEXT NOT NULL DEFAULT '',message TEXT NOT NULL,created INTEGER NOT NULL);
            """)
            for table, column, definition in (
                ("users","status","TEXT NOT NULL DEFAULT 'active'"), ("users","note","TEXT NOT NULL DEFAULT ''"),
                ("users","search_limit","INTEGER NOT NULL DEFAULT 20"), ("users","ck_status","TEXT NOT NULL DEFAULT 'missing'"),
                ("users","checked_at","INTEGER NOT NULL DEFAULT 0"), ("resources","source_id","TEXT NOT NULL DEFAULT 'legacy'"),
                ("resources","first_success","INTEGER NOT NULL DEFAULT 0"), ("resources","delete_due","INTEGER NOT NULL DEFAULT 0"),
                ("resources","cleanup","TEXT NOT NULL DEFAULT 'waiting'"), ("resources","cleanup_error","TEXT NOT NULL DEFAULT ''"),
                ("deliveries","next_attempt","INTEGER NOT NULL DEFAULT 0"), ("deliveries","source","TEXT NOT NULL DEFAULT ''"),
            ):
                columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                if column not in columns: self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.ensure_legacy_source()
        with self.lock, self.db:
            self.db.execute("""UPDATE resources SET cleanup='retained'
              WHERE cleanup='waiting' AND source_id IN (SELECT id FROM sources WHERE retention_minutes<0)""")

    def set(self, key, value, secret=False):
        text = dumps(value)
        if secret: text = self.crypt.encrypt(text.encode()).decode()
        with self.lock, self.db:
            self.db.execute("INSERT INTO settings(key,value,secret) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=excluded.secret", (key, text, int(secret)))

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value,secret FROM settings WHERE key=?", (key,)).fetchone()
        if not row: return default
        text = row["value"]
        try:
            if row["secret"]: text = self.crypt.decrypt(text.encode()).decode()
            return json.loads(text)
        except (InvalidToken, ValueError, json.JSONDecodeError): return default

    def config(self, include_secrets=False):
        result = {
            "public_url": self.get("public_url", ""),
            "source_cid": self.get("source_cid", "0"),
            "scan_seconds": self.get("scan_seconds", 60),
            "enabled": self.get("enabled", False),
            "default_category": self.get("default_category", "其他"),
            "concurrency": self.get("concurrency", 1),
            "check_hours": self.get("check_hours", 6),
            "retry_minutes": self.get("retry_minutes", 15),
            "max_attempts": self.get("max_attempts", 5),
            "search_limit": self.get("search_limit", 20),
            "log_days": self.get("log_days", 30),
            "mini_enabled": self.get("mini_enabled", True),
            "theme": self.get("theme", "dark"),
            "bot_configured": bool(self.get("bot_token", "")),
            "source_configured": bool(self.get("source_cookie", "")),
        }
        if include_secrets:
            result["bot_token"] = self.get("bot_token", "")
            result["source_cookie"] = self.get("source_cookie", "")
        return result

    def save_config(self, value):
        allowed = {"public_url", "source_cid", "scan_seconds", "enabled", "default_category", "concurrency",
                   "check_hours", "retry_minutes", "max_attempts", "search_limit", "log_days", "mini_enabled", "theme"}
        for key in allowed:
            if key in value: self.set(key, value[key])
        if value.get("bot_token"): self.set("bot_token", str(value["bot_token"]).strip(), True)
        if value.get("source_cookie"): self.set("source_cookie", str(value["source_cookie"]).strip(), True)

    def upsert_user(self, tg_id, username="", name=""):
        stamp = now()
        with self.lock, self.db:
            self.db.execute("""INSERT INTO users(tg_id,username,name,created,updated) VALUES(?,?,?,?,?)
              ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username,name=excluded.name,updated=excluded.updated""",
              (tg_id, username or "", name or "", stamp, stamp))
        return self.user(tg_id)

    def ensure_legacy_source(self):
        with self.lock, self.db:
            if self.db.execute("SELECT 1 FROM sources LIMIT 1").fetchone(): return
            cid = str(self.get("source_cid", "0"))
            stamp = now()
            self.db.execute("""INSERT INTO sources(id,name,cid,poll_seconds,stable_seconds,baseline_ready,historical,created,updated)
              VALUES('legacy','默认监听目录',?,?,?,?,?,?,?)""",
              (cid, int(self.get("scan_seconds", 60)), 0, int(bool(self.get("baseline_ready", False))), int(self.get("baseline_count", 0)), stamp, stamp))

    def event(self, kind, message, object_id=""):
        stamp = now(); eid = secrets.token_hex(12)
        with self.lock, self.db:
            self.db.execute("INSERT INTO events(id,kind,object_id,message,created) VALUES(?,?,?,?,?)", (eid, kind, str(object_id), str(message)[:1000], stamp))
        return eid

    def user(self, tg_id, include_cookie=False):
        with self.lock: row = self.db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if not row: return None
        value = dict(row); value["categories"] = json.loads(value["categories"] or "[]")
        value["enabled"] = bool(value["enabled"]); value["hierarchy"] = bool(value["hierarchy"])
        if include_cookie:
            try: value["cookie"] = self.crypt.decrypt(value["cookie"].encode()).decode() if value["cookie"] else ""
            except InvalidToken: value["cookie"] = ""
        else: value.pop("cookie", None); value["account_bound"] = bool(row["cookie"] and row["uid"])
        return value

    def save_user(self, tg_id, value):
        current = self.user(tg_id, True)
        if not current: raise ValueError("用户不存在")
        fields = {k: value[k] for k in ("target_cid","target_name","enabled","mode","include_terms","exclude_terms","hierarchy","status","note","search_limit") if k in value}
        if "categories" in value: fields["categories"] = dumps(value["categories"] if isinstance(value["categories"], list) else [])
        if value.get("cookie"):
            fields["cookie"] = self.crypt.encrypt(str(value["cookie"]).strip().encode()).decode()
            fields["uid"] = str(value.get("uid") or "")
            fields["ck_status"] = "valid"
            fields["checked_at"] = now()
        if not fields: return self.user(tg_id)
        fields["updated"] = now()
        sql = "UPDATE users SET " + ",".join(f"{k}=?" for k in fields) + " WHERE tg_id=?"
        with self.lock, self.db: self.db.execute(sql, (*fields.values(), tg_id))
        return self.user(tg_id)

    def sources(self):
        with self.lock: return [dict(row) for row in self.db.execute("SELECT * FROM sources ORDER BY created")]

    def save_source(self, value):
        sid = str(value.get("id") or secrets.token_hex(10)); stamp = now()
        name = str(value.get("name") or "监听目录").strip()[:100]
        cid = str(value.get("cid") or "0").strip()
        poll = max(15, min(3600, int(value.get("poll_seconds") or 60)))
        stable = max(0, min(86400, int(value.get("stable_seconds") or 30)))
        retention = max(-1, min(525600, int(value.get("retention_minutes", -1))))
        with self.lock, self.db:
            self.db.execute("""INSERT INTO sources(id,name,cid,enabled,poll_seconds,stable_seconds,retention_minutes,created,updated)
              VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,cid=excluded.cid,
              enabled=excluded.enabled,poll_seconds=excluded.poll_seconds,stable_seconds=excluded.stable_seconds,
              retention_minutes=excluded.retention_minutes,updated=excluded.updated""",
              (sid,name,cid,int(bool(value.get("enabled",True))),poll,stable,retention,stamp,stamp))
        self.event("监听目录", f"保存监听目录：{name}", sid); return sid

    def source_action(self, value):
        sid = str(value.get("id") or ""); action = str(value.get("action") or "")
        if not sid: raise ValueError("监听目录编号不能为空")
        with self.lock, self.db:
            if action == "delete":
                active = self.db.execute("SELECT COUNT(*) FROM resources WHERE source_id=? AND status NOT IN ('historical','deleted')", (sid,)).fetchone()[0]
                if active: raise ValueError("该目录仍有关联资源，不能删除；可先停用")
                self.db.execute("DELETE FROM sources WHERE id=?", (sid,))
            elif action == "reset-baseline":
                self.db.execute("UPDATE sources SET baseline_ready=0,historical=0,added=0,error='',updated=? WHERE id=?", (now(),sid))
            else: raise ValueError("未知监听目录操作")
        self.event("监听目录", "删除监听目录" if action == "delete" else "重建历史基线", sid)

    def create_binding(self, minutes=30):
        code = "-".join(secrets.token_hex(3).upper()[i:i+3] for i in (0,3))
        stamp=now(); bid=secrets.token_hex(12); digest=hashlib.sha256(code.encode()).hexdigest()
        with self.lock, self.db:
            self.db.execute("INSERT INTO bindings(id,code_hash,code_hint,expires,created) VALUES(?,?,?,?,?)", (bid,digest,code[-3:],stamp+max(5,min(1440,int(minutes)))*60,stamp))
        self.event("绑定", "创建绑定码", bid); return {"id":bid,"code":code,"expires":stamp+max(5,min(1440,int(minutes)))*60}

    def consume_binding(self, code, tg_id):
        digest=hashlib.sha256(str(code).strip().upper().encode()).hexdigest(); stamp=now()
        with self.lock, self.db:
            row=self.db.execute("SELECT * FROM bindings WHERE code_hash=?",(digest,)).fetchone()
            if not row or row["status"] != "available" or row["expires"] < stamp: raise ValueError("绑定码无效或已过期")
            self.db.execute("UPDATE bindings SET status='used',used_by=? WHERE id=?",(str(tg_id),row["id"]))
        return row["id"]

    def binding_action(self, value):
        bid=str(value.get("id") or ""); action=str(value.get("action") or "")
        if action != "revoke": raise ValueError("未知绑定码操作")
        with self.lock, self.db: self.db.execute("UPDATE bindings SET status='revoked' WHERE id=? AND status='available'",(bid,))
        self.event("绑定", "撤销绑定码", bid)

    def delivery_action(self, value):
        did=str(value.get("id") or ""); action=str(value.get("action") or ""); stamp=now()
        with self.lock, self.db:
            if action == "retry": self.db.execute("UPDATE deliveries SET status='retry',error='',next_attempt=0,updated=? WHERE id=? AND status NOT IN ('running','delivered')",(stamp,did))
            elif action == "cancel": self.db.execute("UPDATE deliveries SET status='cancelled',updated=? WHERE id=? AND status NOT IN ('running','delivered')",(stamp,did))
            else: raise ValueError("未知派送操作")
        self.event("派送", "手动重派" if action == "retry" else "取消派送", did)

    def overview(self):
        stamp=now()
        with self.lock, self.db:
            self.db.execute("UPDATE bindings SET status='expired' WHERE status='available' AND expires<?",(stamp,))
            users=[self.user(row[0]) for row in self.db.execute("SELECT tg_id FROM users ORDER BY created DESC")]
            deliveries=[dict(row) for row in self.db.execute("""SELECT d.*,r.name,u.name user_name,u.username
              FROM deliveries d JOIN resources r ON r.id=d.resource_id JOIN users u ON u.tg_id=d.tg_id ORDER BY d.created DESC LIMIT 300""")]
            resources=[dict(row) for row in self.db.execute("SELECT * FROM resources ORDER BY first_seen DESC LIMIT 200")]
            bindings=[dict(row) for row in self.db.execute("SELECT id,code_hint,expires,status,used_by,created FROM bindings ORDER BY created DESC LIMIT 100")]
            events=[dict(row) for row in self.db.execute("SELECT * FROM events ORDER BY created DESC LIMIT 200")]
        return {**self.status(),"users_list":users,"deliveries":deliveries,"sources":self.sources(),"resources":resources,"bindings":bindings,"events":events}

    def mini_overview(self, tg_id):
        with self.lock:
            deliveries=[dict(row) for row in self.db.execute("""SELECT d.*,r.name,r.category,r.size,r.cleanup,r.cleanup_error
              FROM deliveries d JOIN resources r ON r.id=d.resource_id WHERE d.tg_id=?
              ORDER BY d.created DESC LIMIT 50""",(tg_id,))]
            events=[dict(row) for row in self.db.execute("""SELECT * FROM events
              WHERE object_id IN (SELECT id FROM deliveries WHERE tg_id=?) OR message LIKE ?
              ORDER BY created DESC LIMIT 50""",(tg_id,f"%{tg_id}%"))]
            stats=dict(self.db.execute("""SELECT
              SUM(CASE WHEN status='delivered' THEN 1 ELSE 0 END) delivered,
              SUM(CASE WHEN status IN ('waiting','running','retry') THEN 1 ELSE 0 END) pending,
              SUM(CASE WHEN status IN ('failed','cancelled') THEN 1 ELSE 0 END) failed
              FROM deliveries WHERE tg_id=?""",(tg_id,)).fetchone())
        return {"deliveries":deliveries,"events":events,"stats":{k:int(v or 0) for k,v in stats.items()}}

    def status(self):
        with self.lock:
            users = self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            pending = self.db.execute("SELECT COUNT(*) FROM deliveries WHERE status IN ('waiting','running','retry')").fetchone()[0]
            delivered = self.db.execute("SELECT COUNT(*) FROM deliveries WHERE status='delivered'").fetchone()[0]
        return {"ok": True, "users": users, "pending": pending, "delivered": delivered,
                "last_error": self.get("last_error", ""), "last_scan": self.get("last_scan", 0),
                "baseline_ready": all(bool(x["baseline_ready"]) for x in self.sources()),
                "public_url": self.get("public_url", ""), "config": self.config(False)}


class Telegram:
    def __init__(self, app): self.app, self.offset = app, 0
    def call(self, method, payload=None, timeout=35):
        token = self.app.store.get("bot_token", "")
        if not token: raise RuntimeError("尚未配置 Bot Token")
        body = urllib.parse.urlencode(payload or {}).encode()
        request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body)
        with urllib.request.urlopen(request, timeout=timeout) as response: result = json.loads(response.read())
        if not result.get("ok"): raise RuntimeError(result.get("description", "Telegram 请求失败"))
        return result.get("result")

    def send(self, chat_id, text, webapp=True):
        payload = {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML"}
        url = self.app.store.get("public_url", "")
        if webapp and url:
            payload["reply_markup"] = dumps({"inline_keyboard": [[{"text": "打开订阅中心", "web_app": {"url": url}}]]})
        return self.call("sendMessage", payload)

    def loop(self):
        while not self.app.stop.is_set():
            try:
                if not self.app.store.get("enabled", False) or not self.app.store.get("bot_token", ""):
                    self.app.stop.wait(5)
                    continue
                updates = self.call("getUpdates", {"offset": self.offset, "timeout": 25, "allowed_updates": dumps(["message"])}, 35)
                if str(self.app.store.get("last_error", "")).startswith("Bot:"):
                    self.app.store.set("last_error", "")
                for update in updates or []:
                    self.offset = max(self.offset, int(update["update_id"]) + 1)
                    self.handle(update.get("message") or {})
            except Exception as exc:
                self.app.store.set("last_error", f"Bot: {exc}"); self.app.stop.wait(5)

    def handle(self, message):
        chat = message.get("chat") or {}; sender = message.get("from") or {}; text = str(message.get("text") or "").strip()
        if chat.get("type") != "private" or not sender.get("id"): return
        tg_id=int(sender["id"]); name=" ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))
        user=self.app.store.user(tg_id)
        if text.startswith("/bind"):
            parts=text.split(maxsplit=1)
            if len(parts)<2: return self.send(tg_id,"请发送 <code>/bind 绑定码</code>。绑定码由管理员创建。",False)
            try:
                self.app.store.consume_binding(parts[1],tg_id)
                user=self.app.store.upsert_user(tg_id,sender.get("username",""),name)
                self.app.store.event("绑定",f"Telegram 用户完成绑定：{tg_id}",str(tg_id))
            except ValueError as exc: return self.send(tg_id,str(exc),False)
            self.send(sender["id"], "<b>蟑螂快跑已连接</b>\n无需门户或 Emby 认证。点击下方按钮绑定115并设置接收规则。")
        elif text.startswith("/start"):
            if not user: return self.send(tg_id,"尚未绑定。请向管理员索取绑定码，然后发送 <code>/bind 绑定码</code>。",False)
            self.app.store.upsert_user(tg_id,sender.get("username",""),name)
            self.send(tg_id,"<b>蟑螂快跑已连接</b>\n点击下方按钮管理115账号和接收规则。")
        elif not user:
            self.send(tg_id,"尚未绑定。请发送 <code>/bind 绑定码</code>。",False)
        elif text.startswith("/status"):
            self.send(sender["id"], f"状态：{'接收中' if user['enabled'] else '已暂停'}\n模式：{user['mode']}\n115：{'已绑定' if user['account_bound'] else '未绑定'}")
        elif text.startswith("/all"):
            self.app.store.save_user(sender["id"], {"mode": "all", "enabled": True})
            self.send(sender["id"], "已开启全部接收。")
        elif text.startswith("/pause"):
            self.app.store.save_user(sender["id"], {"enabled": False})
            self.send(sender["id"], "已暂停自动接收。")
        else:
            self.send(sender["id"], "可用命令：/start /status /all /pause")


class P115:
    @staticmethod
    def cookie_mapping(cookie):
        values = {}
        for part in str(cookie or "").split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key:
                values[key] = value
        if not values:
            raise RuntimeError("115 Cookie 为空或格式无效")
        return values

    @staticmethod
    def profile(cookie):
        req = urllib.request.Request("https://my.115.com/?ct=ajax&ac=nav", headers={"Cookie": cookie, "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r: data = json.loads(r.read())
        user = data.get("user") or data.get("data") or data
        uid = str(user.get("user_id") or user.get("uid") or "") if isinstance(user, dict) else ""
        name = str(user.get("user_name") or user.get("name") or "") if isinstance(user, dict) else ""
        if not uid: raise RuntimeError("115 Cookie 无效或已过期")
        return {"uid": uid, "name": name}

    @staticmethod
    def list_dir(cookie, cid):
        query = urllib.parse.urlencode({"aid": 1, "cid": cid, "o": "user_ptime", "asc": 0, "offset": 0, "show_dir": 1, "limit": 1150, "format": "json"})
        req = urllib.request.Request("https://webapi.115.com/files?" + query, headers={"Cookie": cookie, "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r: result = json.loads(r.read())
        if result.get("state") is False: raise RuntimeError(result.get("error") or "读取115目录失败")
        entries = []
        for item in result.get("data") or []:
            is_dir = "cid" in item and not item.get("fid")
            node_id = str(item.get("cid") if is_dir else item.get("fid") or item.get("file_id") or "")
            name = str(item.get("n") or item.get("fn") or item.get("file_name") or "")
            if not node_id or not name: continue
            entries.append({"node_id": node_id, "name": name, "is_dir": is_dir,
                            "pickcode": str(item.get("pc") or item.get("pick_code") or ""),
                            "sha1": str(item.get("sha") or item.get("sha1") or ""),
                            "size": int(item.get("s") or item.get("size") or 0)})
        return entries

    @staticmethod
    def transfer(source_cookie, user_cookie, entry, target_cid):
        from p115client import P115Client
        from p115client.tool.upload import iter_115_to_115
        source = P115Client(P115.cookie_mapping(source_cookie))
        target = P115Client(P115.cookie_mapping(user_cookie))
        if entry["is_dir"]:
            results = list(iter_115_to_115(source, target, from_cid=entry["node_id"], to_pid=target_cid, with_root=True, max_workers=2))
            failed = [x for x in results if isinstance(x, dict) and x.get("type") == "fail"]
            if failed: raise RuntimeError(f"115转存部分失败：{len(failed)} 项")
            return

        def target_entries():
            return P115.list_dir(user_cookie, target_cid)

        def verified():
            return any(
                item["name"] == entry["name"] and item["sha1"].upper() == entry["sha1"].upper()
                and int(item["size"]) == int(entry["size"])
                for item in target_entries()
            )

        if verified():
            return

        title = re.split(r"\.(?:19|20)\d{2}\b|\.S\d{1,2}E\d{1,3}\b", entry["name"], maxsplit=1, flags=re.I)[0]
        queries = list(dict.fromkeys(filter(None, (title.strip(" ._-"), Path(entry["name"]).stem))))
        existing = None
        for query in queries:
            result = target.fs_search({"search_value": query, "limit": 115, "offset": 0, "show_dir": 0})
            data = result.get("data") if isinstance(result, dict) else None
            items = (data.get("data") or data.get("list") or []) if isinstance(data, dict) else data if isinstance(data, list) else []
            for item in items:
                sha1 = str(item.get("sha") or item.get("sha1") or "").upper()
                size = int(item.get("s") or item.get("size") or 0)
                file_id = str(item.get("fid") or item.get("file_id") or "")
                if file_id and sha1 == entry["sha1"].upper() and size == int(entry["size"]):
                    existing = file_id
                    break
            if existing:
                break
        if existing:
            before = {item["node_id"] for item in target_entries()}
            result = target.fs_copy(existing, pid=target_cid)
            if not isinstance(result, dict) or result.get("state") is False:
                raise RuntimeError((result or {}).get("error") or "115 账号内复制失败")
            copied = None
            for _ in range(6):
                time.sleep(1)
                copied = next((item for item in target_entries() if item["node_id"] not in before and item["sha1"].upper() == entry["sha1"].upper()), None)
                if copied:
                    break
            if not copied:
                raise RuntimeError("115 账号内复制后未找到目标文件")
            if copied["name"] != entry["name"]:
                result = target.fs_rename((copied["node_id"], entry["name"]))
                if not isinstance(result, dict) or result.get("state") is False:
                    raise RuntimeError((result or {}).get("error") or "115 目标文件重命名失败")
            for _ in range(6):
                time.sleep(1)
                if verified():
                    return
            raise RuntimeError("115 账号内复制结果校验失败")

        url = source.download_url(entry["pickcode"])

        class RangeReader:
            def __init__(self, source_url, name, size):
                self.url = str(source_url)
                self.headers = dict(getattr(source_url, "headers", None) or {})
                self.name = name
                self.size = int(size)
                self.position = 0

            def seek(self, offset, whence=0):
                if whence == 0:
                    position = offset
                elif whence == 1:
                    position = self.position + offset
                elif whence == 2:
                    position = self.size + offset
                else:
                    raise ValueError(f"unsupported whence: {whence}")
                self.position = max(0, int(position))
                return self.position

            def read(self, length=-1):
                if length is None or length < 0:
                    length = self.size - self.position
                if length <= 0:
                    return b""
                start = self.position
                end = min(self.size - 1, start + int(length) - 1)
                headers = {**self.headers, "Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
                request = urllib.request.Request(self.url, headers=headers)
                with urllib.request.urlopen(request, timeout=30) as response:
                    data = response.read(end - start + 1)
                self.position += len(data)
                return data

        reader = RangeReader(url, entry["name"], entry["size"])
        result = target.upload_file(reader, pid=target_cid, filesha1=entry["sha1"], filesize=entry["size"], filename=entry["name"])
        if isinstance(result, dict) and result.get("state") is False:
            raise RuntimeError(result.get("error") or "115 传输失败")
        for _ in range(10):
            time.sleep(1)
            if verified():
                return
        raise RuntimeError("115 传输返回成功，但目标目录未找到文件")

    @staticmethod
    def delete(source_cookie, node_id):
        from p115client import P115Client
        client=P115Client(P115.cookie_mapping(source_cookie))
        result=client.fs_delete(str(node_id))
        if not isinstance(result,dict) or result.get("state") is False:
            raise RuntimeError((result or {}).get("error") or "删除源文件失败")


def category_for(name, default):
    text = name.lower()
    if re.search(r"\b(s\d{1,2}e\d{1,3}|ep?\d{1,3}|第.{1,4}集)\b", text, re.I): return "剧集"
    if any(word in text for word in ("anime", "动漫", "动画", "ova")): return "动漫"
    if any(word in text for word in ("documentary", "纪录片", "docu")): return "纪录片"
    return default or "电影"


class Application:
    def __init__(self, store):
        self.store, self.stop = store, threading.Event()
        self.telegram = Telegram(self)
        self.scan_lock = threading.Lock()

    def mini_user(self, init_data):
        if not self.store.get("mini_enabled",True): raise PermissionError("小程序当前已停用")
        token = self.store.get("bot_token", "")
        if not token: raise PermissionError("Bot 尚未配置")
        params = urllib.parse.parse_qs(init_data, keep_blank_values=True)
        received = (params.pop("hash", [""])[0] or "").lower()
        auth_date = int(params.get("auth_date", ["0"])[0] or 0)
        if not received or abs(now() - auth_date) > 86400: raise PermissionError("Telegram 登录信息已过期")
        check = "\n".join(f"{key}={values[0]}" for key, values in sorted(params.items()))
        secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret_key, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received): raise PermissionError("Telegram 签名无效")
        user = json.loads(params.get("user", ["{}"]) [0])
        tg_id = int(user["id"])
        current=self.store.user(tg_id)
        if not current or current.get("status") != "active": raise PermissionError("请先在 Bot 中使用管理员提供的绑定码")
        self.store.upsert_user(tg_id, user.get("username", ""), " ".join(filter(None, [user.get("first_name"), user.get("last_name")])))
        return tg_id

    def scan(self, force=True):
        if not self.scan_lock.acquire(False): return
        try:
            cfg = self.store.config(True)
            if not cfg["source_configured"]: raise RuntimeError("尚未配置源115 Cookie")
            stamp=now(); scanned=0; errors=[]
            for source in self.store.sources():
                if not source["enabled"] or (not force and int(source["next_scan"] or 0)>stamp): continue
                error=self.scan_source(cfg,source,stamp); scanned+=1
                if error: errors.append(f"{source['name']}：{error}")
            if scanned:
                self.store.set("last_scan", stamp)
                sources=self.store.sources()
                self.store.set("baseline_ready",all(bool(x["baseline_ready"]) for x in sources))
                self.store.set("baseline_count",sum(int(x["historical"]) for x in sources))
            self.store.set("last_error", "；".join(errors)[:2000])
            self.deliver(cfg)
            self.cleanup(cfg)
        except Exception as exc: self.store.set("last_error", f"扫描：{exc}")
        finally: self.scan_lock.release()

    def scan_source(self,cfg,source,stamp):
        try:
            entries=P115.list_dir(cfg["source_cookie"],source["cid"]); discovered=0
            with self.store.lock,self.store.db:
                baseline=bool(source["baseline_ready"])
                for entry in entries:
                    if not entry["is_dir"] and Path(entry["name"]).suffix.lower() not in VIDEO_EXTENSIONS: continue
                    identity=entry["node_id"]+":"+entry["name"]+":"+str(entry["size"])
                    # Preserve IDs created by versions before multi-source support,
                    # otherwise an upgrade would enqueue every existing file again.
                    rid=hashlib.sha256((identity if source["id"]=="legacy" else source["id"]+":"+identity).encode()).hexdigest()
                    row=self.store.db.execute("SELECT * FROM resources WHERE id=?",(rid,)).fetchone()
                    if row:
                        if row["status"]=="stabilizing" and stamp-int(row["first_seen"])>=int(source["stable_seconds"]):
                            self.store.db.execute("UPDATE resources SET status='waiting' WHERE id=?",(rid,)); self.queue_resource(rid,entry["name"],row["category"],stamp)
                        continue
                    discovered+=1; cat=category_for(entry["name"],cfg["default_category"])
                    status="historical" if not baseline else ("stabilizing" if int(source["stable_seconds"])>0 else "waiting")
                    cleanup="retained" if int(source["retention_minutes"])<0 else "waiting"
                    self.store.db.execute("""INSERT INTO resources(id,node_id,name,pickcode,sha1,size,is_dir,category,first_seen,status,source_id,cleanup)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(rid,entry["node_id"],entry["name"],entry["pickcode"],entry["sha1"],entry["size"],int(entry["is_dir"]),cat,stamp,status,source["id"],cleanup))
                    if status=="waiting": self.queue_resource(rid,entry["name"],cat,stamp)
                self.store.db.execute("""UPDATE sources SET baseline_ready=1,historical=historical+?,added=added+?,last_scan=?,next_scan=?,error='',updated=? WHERE id=?""",
                  (discovered if not baseline else 0,discovered if baseline else 0,stamp,stamp+int(source["poll_seconds"]),stamp,source["id"]))
            self.store.event("来源",f"扫描 {source['name']}：发现 {discovered} 项",source["id"])
        except Exception as exc:
            with self.store.lock,self.store.db: self.store.db.execute("UPDATE sources SET error=?,last_scan=?,next_scan=?,updated=? WHERE id=?",(str(exc)[:500],stamp,stamp+int(source["poll_seconds"]),stamp,source["id"]))
            self.store.event("来源失败",f"扫描 {source['name']}：{exc}",source["id"])
            return str(exc)
        return ""

    def queue_resource(self,rid,name,category,stamp):
        users=self.store.db.execute("SELECT * FROM users WHERE enabled=1 AND status='active' AND cookie<>''").fetchall()
        for user in users:
            if self.matches(dict(user),name,category):
                did=hashlib.sha256(f"{rid}:{user['tg_id']}".encode()).hexdigest()
                self.store.db.execute("INSERT OR IGNORE INTO deliveries(id,resource_id,tg_id,created,updated) VALUES(?,?,?,?,?)",(did,rid,user["tg_id"],stamp,stamp))

    @staticmethod
    def matches(user, name, category):
        mode = user.get("mode") or "all"; lower = name.lower()
        excluded = [x.strip().lower() for x in str(user.get("exclude_terms") or "").split(",") if x.strip()]
        if any(x in lower for x in excluded): return False
        if mode == "all": return True
        categories = json.loads(user.get("categories") or "[]")
        cat_ok = category in categories
        terms = [x.strip().lower() for x in str(user.get("include_terms") or "").split(",") if x.strip()]
        term_ok = bool(terms) and any(x in lower for x in terms)
        return cat_ok if mode == "category" else term_ok if mode == "subscription" else cat_ok or term_ok

    def deliver(self, cfg):
        max_attempts=max(1,min(50,int(cfg.get("max_attempts") or 5))); stamp=now()
        with self.store.lock:
            rows = self.store.db.execute("""SELECT d.id,d.resource_id,d.tg_id,d.attempts,r.*,u.cookie,u.target_cid
              FROM deliveries d JOIN resources r ON r.id=d.resource_id JOIN users u ON u.tg_id=d.tg_id
              WHERE d.status IN ('waiting','retry') AND d.attempts<? AND d.next_attempt<=? AND u.status='active'
              ORDER BY d.created LIMIT 20""",(max_attempts,stamp)).fetchall()
        workers=max(1,min(8,int(cfg.get("concurrency") or 1)))
        if workers==1:
            for row in rows: self.deliver_one(cfg,dict(row))
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers,thread_name_prefix="delivery") as pool:
                list(pool.map(lambda row:self.deliver_one(cfg,dict(row)),rows))

    def deliver_one(self,cfg,item):
        try:
            cookie = self.store.crypt.decrypt(item["cookie"].encode()).decode()
            with self.store.lock, self.store.db: self.store.db.execute("UPDATE deliveries SET status='running',attempts=attempts+1,updated=? WHERE id=?", (now(), item["id"]))
            P115.transfer(cfg["source_cookie"], cookie, item, item["target_cid"])
            done=now()
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE deliveries SET status='delivered',error='',next_attempt=0,source='source115',updated=? WHERE id=?", (done, item["id"]))
                source=self.store.db.execute("SELECT retention_minutes FROM sources WHERE id=?",(item["source_id"],)).fetchone()
                retention=int(source[0]) if source else -1
                due=done+retention*60 if retention>=0 else 0
                self.store.db.execute("UPDATE resources SET first_success=CASE WHEN first_success=0 THEN ? ELSE first_success END,delete_due=CASE WHEN delete_due=0 THEN ? ELSE delete_due END,cleanup=CASE WHEN ? >= 0 THEN 'scheduled' ELSE 'retained' END WHERE id=?",(done,due,retention,item["resource_id"]))
            self.store.event("派送",f"派送成功：{item['name']}",item["id"])
            self.telegram.send(item["tg_id"], f"✅ <b>派送完成</b>\n{item['name']}", False)
        except Exception as exc:
            failed=now(); delay=max(1,int(cfg.get("retry_minutes") or 15))*60
            with self.store.lock, self.store.db: self.store.db.execute("UPDATE deliveries SET status='retry',error=?,next_attempt=?,updated=? WHERE id=?", (str(exc)[:500],failed+delay,failed,item["id"]))
            self.store.event("派送失败",f"{item['name']}：{exc}",item["id"])

    def cleanup(self,cfg):
        stamp=now()
        with self.store.lock: rows=self.store.db.execute("""SELECT r.* FROM resources r JOIN sources s ON s.id=r.source_id
          WHERE r.cleanup='scheduled' AND r.delete_due>0 AND r.delete_due<=? AND s.retention_minutes>=0
          AND NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.resource_id=r.id AND d.status NOT IN ('delivered','cancelled')) LIMIT 20""",(stamp,)).fetchall()
        for row in rows:
            item=dict(row)
            try:
                P115.delete(cfg["source_cookie"],item["node_id"])
                with self.store.lock,self.store.db: self.store.db.execute("UPDATE resources SET cleanup='deleted',status='deleted',cleanup_error='' WHERE id=?",(item["id"],))
                self.store.event("清理",f"秒传后删除源文件：{item['name']}",item["id"])
            except Exception as exc:
                with self.store.lock,self.store.db: self.store.db.execute("UPDATE resources SET cleanup='blocked',cleanup_error=? WHERE id=?",(str(exc)[:500],item["id"]))
                self.store.event("清理失败",f"{item['name']}：{exc}",item["id"])

    def maintenance(self,cfg):
        stamp=now()
        if stamp-int(self.store.get("last_maintenance",0) or 0)<300: return
        self.store.set("last_maintenance",stamp)
        cutoff=stamp-max(1,int(cfg.get("log_days") or 30))*86400
        with self.store.lock,self.store.db:
            self.store.db.execute("DELETE FROM events WHERE created<?",(cutoff,))
            rows=self.store.db.execute("SELECT tg_id,uid,cookie FROM users WHERE cookie<>'' AND checked_at<=?",(stamp-max(1,int(cfg.get("check_hours") or 6))*3600,)).fetchall()
        for row in rows:
            try:
                cookie=self.store.crypt.decrypt(row["cookie"].encode()).decode(); profile=P115.profile(cookie)
                status="valid" if not row["uid"] or str(row["uid"])==profile["uid"] else "unavailable"
                with self.store.lock,self.store.db: self.store.db.execute("UPDATE users SET ck_status=?,checked_at=?,uid=CASE WHEN uid='' THEN ? ELSE uid END WHERE tg_id=?",(status,stamp,profile["uid"],row["tg_id"]))
            except Exception:
                with self.store.lock,self.store.db: self.store.db.execute("UPDATE users SET ck_status='unavailable',checked_at=? WHERE tg_id=?",(stamp,row["tg_id"]))

    def scheduler(self):
        while not self.stop.is_set():
            cfg = self.store.config(False)
            if cfg["enabled"]: self.scan(False)
            self.maintenance(cfg)
            self.stop.wait(15)


class Handler(BaseHTTPRequestHandler):
    server_version = "CockroachRunner/0.1"
    def log_message(self, fmt, *args): pass
    @property
    def app(self): return self.server.app
    def send_json(self, status, value):
        data = dumps(value).encode(); self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 1024 * 1024: raise ValueError("请求过大")
        return json.loads(self.rfile.read(length) or b"{}")
    def admin(self):
        if not hmac.compare_digest(self.headers.get("X-Admin-Key", ""), self.server.admin_key): raise PermissionError("管理密钥错误")
    def mini(self): return self.app.mini_user(self.headers.get("Authorization", "").removeprefix("tma "))
    def do_OPTIONS(self): self.send_response(204); self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Access-Control-Allow-Headers", "content-type,authorization,x-admin-key"); self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,OPTIONS"); self.end_headers()
    def do_GET(self): self.route("GET")
    def do_POST(self): self.route("POST")
    def do_PUT(self): self.route("PUT")
    def route(self, method):
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/healthz": return self.send_json(200, {"ok": True, "name": APP_NAME})
            if path == "/" or path == "/index.html": return self.send_file("index.html", "text/html; charset=utf-8")
            if path == "/admin" or path == "/admin.html": return self.send_file("admin.html", "text/html; charset=utf-8")
            if path == "/app.js": return self.send_file("app.js", "text/javascript; charset=utf-8")
            if path == "/admin.js": return self.send_file("admin.js", "text/javascript; charset=utf-8")
            if path == "/admin.css": return self.send_file("admin.css", "text/css; charset=utf-8")
            if path == "/style.css": return self.send_file("style.css", "text/css; charset=utf-8")
            if path == "/api/admin/status" and method == "GET": self.admin(); return self.send_json(200, self.app.store.status())
            if path == "/api/admin/overview" and method == "GET": self.admin(); return self.send_json(200, self.app.store.overview())
            if path == "/api/admin/config" and method == "PUT": self.admin(); self.app.store.save_config(self.body()); return self.send_json(200, {**self.app.store.status(), "message": "配置已保存"})
            if path == "/api/admin/users" and method == "PUT":
                self.admin(); value=self.body(); user=self.app.store.save_user(int(value.get("tg_id") or 0),value); self.app.store.event("用户管理",f"更新用户：{user.get('name') or user.get('tg_id')}",str(user["tg_id"])); return self.send_json(200,{"message":"用户设置已保存","user":user})
            if path == "/api/admin/deliveries" and method == "POST": self.admin(); self.app.store.delivery_action(self.body()); return self.send_json(200,{"message":"派送任务已更新"})
            if path == "/api/admin/sources" and method == "PUT": self.admin(); sid=self.app.store.save_source(self.body()); return self.send_json(200,{"message":"监听目录已保存","id":sid})
            if path == "/api/admin/sources" and method == "POST": self.admin(); self.app.store.source_action(self.body()); return self.send_json(200,{"message":"监听目录操作完成"})
            if path == "/api/admin/bindings" and method == "POST":
                self.admin(); value=self.body()
                if value.get("action")=="revoke": self.app.store.binding_action(value); return self.send_json(200,{"message":"绑定码已撤销"})
                result=self.app.store.create_binding(value.get("minutes",30)); return self.send_json(200,{"message":"绑定码已创建",**result})
            if path == "/api/admin/test-bot" and method == "POST": self.admin(); info=self.app.telegram.call("getMe"); return self.send_json(200, {**self.app.store.status(), "message": f"Bot 连接正常：@{info.get('username','')}"})
            if path == "/api/admin/scan" and method == "POST": self.admin(); threading.Thread(target=self.app.scan,daemon=True).start(); return self.send_json(202, {**self.app.store.status(), "message": "扫描已启动"})
            if path == "/api/mini/me" and method == "GET": uid=self.mini(); return self.send_json(200, {"user": self.app.store.user(uid), "categories": ["电影","剧集","动漫","纪录片","其他"]})
            if path == "/api/mini/overview" and method == "GET": uid=self.mini(); return self.send_json(200, self.app.store.mini_overview(uid))
            if path == "/api/mini/preferences" and method == "PUT": uid=self.mini(); return self.send_json(200, {"user": self.app.store.save_user(uid, self.body())})
            if path == "/api/mini/account" and method == "PUT":
                uid=self.mini(); value=self.body(); profile=P115.profile(str(value.get("cookie") or "")); value["uid"]=profile["uid"]
                return self.send_json(200, {"user": self.app.store.save_user(uid, value), "account": profile})
            return self.send_json(404, {"error": "接口不存在"})
        except PermissionError as exc: self.send_json(401, {"error": str(exc)})
        except (ValueError, RuntimeError, urllib.error.URLError) as exc: self.send_json(400, {"error": str(exc)})
        except Exception as exc: self.send_json(500, {"error": f"服务错误：{exc}"})
    def send_file(self, name, content_type):
        path = Path(self.server.static_dir) / name
        data = path.read_bytes(); self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Cache-Control", "no-cache"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--host",default="127.0.0.1"); parser.add_argument("--port",type=int,default=8790); parser.add_argument("--data",default="/var/lib/cockroach-runner"); parser.add_argument("--static",default=str(Path(__file__).with_name("static"))); args=parser.parse_args()
    admin_key=os.environ["COCKROACH_ADMIN_KEY"]; encryption_key=os.environ["COCKROACH_ENCRYPTION_KEY"]
    app=Application(Store(args.data,encryption_key)); server=ThreadingHTTPServer((args.host,args.port),Handler); server.app=app; server.admin_key=admin_key; server.static_dir=args.static
    threading.Thread(target=app.telegram.loop,daemon=True).start(); threading.Thread(target=app.scheduler,daemon=True).start()
    try: server.serve_forever()
    finally: app.stop.set(); server.server_close()

if __name__ == "__main__": main()
