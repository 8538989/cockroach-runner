#!/usr/bin/env python3
import argparse
import base64
import concurrent.futures
import hashlib
import hmac
import html
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

APP_NAME = "蟑影递送"
ACTIVATION_SHA256 = "e14a944c3a27cb5bc10b0ae4374846c3c6854c9defd5df853af39cb294396e79"
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".iso"}
QR_LOGIN_APPS = {
    "alipaymini": "115生活（支付宝小程序）",
    "web": "网页版",
    "android": "115生活（Android端）",
    "ios": "115生活（iOS端）",
    "115android": "115网盘（Android端）",
    "115ios": "115网盘（iOS端）",
    "115ipad": "115网盘（iPad端）",
    "tv": "115网盘（Android电视端）",
    "wechatmini": "115生活（微信小程序）",
    "harmony": "115生活（鸿蒙端）",
}


def now(): return int(time.time())
def dumps(value): return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
def activation_valid(value): return hmac.compare_digest(hashlib.sha256(str(value or "").encode()).hexdigest(),ACTIVATION_SHA256)
def redact_sensitive_text(value):
    return re.sub(r"(['\"]?(?:user_key|cookie|authorization|api_key|tmdb_api_key|token)['\"]?\s*[:=]\s*)('[^']*'|\"[^\"]*\"|[^,;}\s&]+)",r"\1'***'",str(value),flags=re.I)


class RateGate:
    def __init__(self):
        self.lock = threading.Lock()
        self.last = 0.0

    def wait(self, seconds):
        interval = max(0.0, min(3600.0, float(seconds or 0)))
        if interval <= 0: return
        with self.lock:
            delay = interval - (time.monotonic() - self.last)
            if delay > 0: time.sleep(delay)
            self.last = time.monotonic()


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
                ("deliveries","destination","TEXT NOT NULL DEFAULT ''"),
                ("sources","monitor_type","TEXT NOT NULL DEFAULT 'api_poll'"),
                ("sources","cd2_path","TEXT NOT NULL DEFAULT ''"),
                ("sources","age_delete_minutes","INTEGER NOT NULL DEFAULT -1"),
                ("sources","cookie","TEXT NOT NULL DEFAULT ''"),
                ("sources","cookie_mode","TEXT NOT NULL DEFAULT 'global'"),
                ("resources","hidden","INTEGER NOT NULL DEFAULT 0"),
                ("users","membership_expires","INTEGER NOT NULL DEFAULT 0"),
                ("users","membership_plan","TEXT NOT NULL DEFAULT ''"),
                ("users","account_ready","INTEGER NOT NULL DEFAULT 0"),
                ("users","tmdb_subscriptions","TEXT NOT NULL DEFAULT '[]'"),
                ("bindings","plan","TEXT NOT NULL DEFAULT 'month'"),
                ("bindings","grant_days","INTEGER NOT NULL DEFAULT 30"),
            ):
                columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                if column not in columns: self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.ensure_legacy_source()
        with self.lock, self.db:
            stamp = now()
            self.db.execute("""UPDATE users SET account_ready=1 WHERE account_ready=0 AND cookie<>'' AND uid<>''
              AND target_cid NOT IN ('','0') AND target_name NOT IN ('','根目录')""")
            self.db.execute("""UPDATE deliveries SET status='retry',next_attempt=0,updated=?,
              error=CASE WHEN error='' THEN '服务重启后自动恢复未完成任务' ELSE error END WHERE status='running'""", (stamp,))
            self.db.execute("""UPDATE resources SET cleanup='retained'
              WHERE cleanup='waiting' AND source_id IN (SELECT id FROM sources WHERE retention_minutes<0)""")
            for row in self.db.execute("SELECT id,error FROM deliveries WHERE error LIKE '%user_key%' OR error LIKE '%cookie%'").fetchall():
                self.db.execute("UPDATE deliveries SET error=? WHERE id=?",(redact_sensitive_text(row["error"]),row["id"]))
            for row in self.db.execute("SELECT id,message FROM events WHERE message LIKE '%user_key%' OR message LIKE '%cookie%'").fetchall():
                self.db.execute("UPDATE events SET message=? WHERE id=?",(redact_sensitive_text(row["message"]),row["id"]))

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
        with self.lock: independent_source_configured=bool(self.db.execute("SELECT 1 FROM sources WHERE cookie_mode='independent' AND cookie<>'' LIMIT 1").fetchone())
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
            "source_configured": bool(self.get("source_cookie", "")) or independent_source_configured,
            "cd2_mode": self.get("cd2_mode", "mount"),
            "cd2_host": self.get("cd2_host", "127.0.0.1"),
            "cd2_port": self.get("cd2_port", 19798),
            "cd2_root": self.get("cd2_root", "/cd2/miaochuang"),
            "cd2_api_root": self.get("cd2_api_root", "/"),
            "cd2_token_configured": bool(self.get("cd2_api_token", "")),
            "cd2_metadata_enabled": self.get("cd2_metadata_enabled", False),
            "p115_api_interval_seconds": self.get("p115_api_interval_seconds", 1),
            "cd2_api_interval_seconds": self.get("cd2_api_interval_seconds", 1),
            "transfer_interval_seconds": self.get("transfer_interval_seconds", 3),
            "transfer_timeout_seconds": self.get("transfer_timeout_seconds", 300),
            "distributed_transfer_enabled": self.get("distributed_transfer_enabled", False),
            "tmdb_enabled": self.get("tmdb_enabled", False),
            "tmdb_configured": bool(self.get("tmdb_api_key", "")),
        }
        if include_secrets:
            result["bot_token"] = self.get("bot_token", "")
            result["source_cookie"] = self.get("source_cookie", "")
            result["cd2_api_token"] = self.get("cd2_api_token", "")
            result["tmdb_api_key"] = self.get("tmdb_api_key", "")
        return result

    def save_config(self, value):
        if "cd2_mode" in value and value["cd2_mode"] not in {"mount", "api"}: raise ValueError("CD2 接入方式无效")
        if "cd2_port" in value: value["cd2_port"] = max(1, min(65535, int(value["cd2_port"])))
        for key in ("p115_api_interval_seconds", "cd2_api_interval_seconds", "transfer_interval_seconds"):
            if key in value: value[key] = max(0.0, min(3600.0, float(value[key])))
        if "transfer_timeout_seconds" in value: value["transfer_timeout_seconds"] = max(30, min(3600, int(value["transfer_timeout_seconds"])))
        allowed = {"public_url", "source_cid", "scan_seconds", "enabled", "default_category", "concurrency",
                   "check_hours", "retry_minutes", "max_attempts", "search_limit", "log_days", "mini_enabled", "theme",
                   "cd2_mode", "cd2_host", "cd2_port", "cd2_root", "cd2_api_root"}
        allowed.update({"p115_api_interval_seconds", "cd2_api_interval_seconds", "transfer_interval_seconds"})
        allowed.update({"transfer_timeout_seconds","distributed_transfer_enabled","tmdb_enabled","cd2_metadata_enabled"})
        for key in allowed:
            if key in value: self.set(key, value[key])
        if value.get("bot_token"): self.set("bot_token", str(value["bot_token"]).strip(), True)
        if value.get("source_cookie"): self.set("source_cookie", str(value["source_cookie"]).strip(), True)
        if value.get("cd2_api_token"): self.set("cd2_api_token", str(value["cd2_api_token"]).strip(), True)
        if value.get("tmdb_api_key"): self.set("tmdb_api_key", str(value["tmdb_api_key"]).strip(), True)

    def upsert_user(self, tg_id, username="", name=""):
        stamp = now()
        with self.lock, self.db:
            self.db.execute("""INSERT INTO users(tg_id,username,name,membership_expires,membership_plan,created,updated) VALUES(?,?,?,?,?,?,?)
              ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username,name=excluded.name,updated=excluded.updated""",
              (tg_id, username or "", name or "", stamp+365*86400, "year", stamp, stamp))
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
        value["tmdb_subscriptions"] = json.loads(value.get("tmdb_subscriptions") or "[]")
        value["enabled"] = bool(value["enabled"]); value["hierarchy"] = bool(value["hierarchy"]); value["account_ready"] = bool(value.get("account_ready"))
        if include_cookie:
            try: value["cookie"] = self.crypt.decrypt(value["cookie"].encode()).decode() if value["cookie"] else ""
            except InvalidToken: value["cookie"] = ""
        else: value.pop("cookie", None); value["account_bound"] = bool(row["cookie"] and row["uid"])
        return value

    def delete_user(self, tg_id):
        with self.lock, self.db:
            row = self.db.execute("SELECT name,username FROM users WHERE tg_id=?", (tg_id,)).fetchone()
            if not row: raise ValueError("用户不存在")
            active = self.db.execute("SELECT COUNT(*) FROM deliveries WHERE tg_id=? AND status IN ('waiting','running','retry')", (tg_id,)).fetchone()[0]
            if active: raise ValueError("该用户仍有未完成派送，请先取消任务")
            self.db.execute("DELETE FROM deliveries WHERE tg_id=?", (tg_id,))
            self.db.execute("DELETE FROM users WHERE tg_id=?", (tg_id,))
        self.event("用户管理", f"删除用户：{row['name'] or row['username'] or tg_id}", str(tg_id))

    def save_user(self, tg_id, value):
        current = self.user(tg_id, True)
        if not current: raise ValueError("用户不存在")
        fields = {k: value[k] for k in ("target_cid","target_name","enabled","include_terms","exclude_terms","hierarchy","status","note","search_limit","account_ready") if k in value}
        if "mode" in value:
            mode=str(value.get("mode") or "all")
            if mode not in {"all","subscription","category","both","either","tmdb","off"}: raise ValueError("接收模式无效")
            fields["mode"]=mode
        if "categories" in value: fields["categories"] = dumps(value["categories"] if isinstance(value["categories"], list) else [])
        if "tmdb_subscriptions" in value: fields["tmdb_subscriptions"] = dumps(value["tmdb_subscriptions"] if isinstance(value["tmdb_subscriptions"], list) else [])
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

    def sources(self, include_cookie=False):
        with self.lock: rows=[dict(row) for row in self.db.execute("SELECT * FROM sources ORDER BY created")]
        for source in rows:
            encrypted=source.get("cookie") or ""
            source["cookie_configured"]=bool(encrypted)
            if include_cookie:
                try: source["cookie"]=self.crypt.decrypt(encrypted.encode()).decode() if encrypted else ""
                except InvalidToken: source["cookie"]=""
            else: source.pop("cookie",None)
        return rows

    def source_cookie(self, source_id, fallback=""):
        with self.lock: row=self.db.execute("SELECT cookie_mode,cookie FROM sources WHERE id=?",(str(source_id),)).fetchone()
        if not row or row["cookie_mode"] != "independent": return fallback
        try: return self.crypt.decrypt(row["cookie"].encode()).decode() if row["cookie"] else ""
        except InvalidToken: return ""

    def save_source(self, value):
        sid = str(value.get("id") or secrets.token_hex(10)); stamp = now()
        name = str(value.get("name") or "监听目录").strip()[:100]
        cid = str(value.get("cid") or "0").strip()
        poll = max(15, min(3600, int(value.get("poll_seconds") or 60)))
        stable = max(0, min(86400, int(value.get("stable_seconds") or 30)))
        retention = max(-1, min(525600, int(value.get("retention_minutes", -1))))
        age_delete = max(-1, min(525600, int(value.get("age_delete_minutes", -1))))
        monitor_type = str(value.get("monitor_type") or "api_poll")
        if monitor_type not in {"api_poll", "cd2_realtime", "cd2_poll", "cd2_api"}: raise ValueError("监听方式无效")
        cookie_mode=str(value.get("cookie_mode") or "global")
        if cookie_mode not in {"global","independent"}: raise ValueError("115 CK 使用方式无效")
        raw_cookie=str(value.get("cookie") or "").strip()
        with self.lock: current=self.db.execute("SELECT cookie FROM sources WHERE id=?",(sid,)).fetchone()
        encrypted_cookie=str(current["cookie"] or "") if current else ""
        if cookie_mode == "independent":
            if raw_cookie:
                profile=P115.profile(raw_cookie)
                if not profile.get("uid"): raise ValueError("115 CK 验证失败")
                encrypted_cookie=self.crypt.encrypt(raw_cookie.encode()).decode()
            if not encrypted_cookie: raise ValueError("独立 CK 模式请先填写有效的 115 CK")
        else: encrypted_cookie=""
        cd2_path = str(value.get("cd2_path") or "").strip()
        if monitor_type in {"cd2_realtime", "cd2_poll"}:
            resolved = Path(cd2_path).resolve()
            cd2_root = Path(str(self.get("cd2_root", "/cd2/miaochuang"))).resolve()
            if resolved != cd2_root and cd2_root not in resolved.parents: raise ValueError(f"CD2 路径必须位于 {cd2_root} 内")
            if not resolved.is_dir(): raise ValueError("CD2 监听目录不存在或容器无权访问")
            cd2_path = str(resolved)
        elif monitor_type == "cd2_api":
            if not cd2_path.startswith("/"): raise ValueError("CD2 API 目录必须是以 / 开头的网盘路径")
        with self.lock, self.db:
            self.db.execute("""INSERT INTO sources(id,name,cid,enabled,poll_seconds,stable_seconds,retention_minutes,monitor_type,cd2_path,age_delete_minutes,cookie,cookie_mode,created,updated)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,cid=excluded.cid,
              enabled=excluded.enabled,poll_seconds=excluded.poll_seconds,stable_seconds=excluded.stable_seconds,
              retention_minutes=excluded.retention_minutes,monitor_type=excluded.monitor_type,cd2_path=excluded.cd2_path,
              age_delete_minutes=excluded.age_delete_minutes,cookie=excluded.cookie,cookie_mode=excluded.cookie_mode,updated=excluded.updated""",
              (sid,name,cid,int(bool(value.get("enabled",True))),poll,stable,retention,monitor_type,cd2_path,age_delete,encrypted_cookie,cookie_mode,stamp,stamp))
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

    def create_binding(self, plan="month"):
        plans = {"month": (30, "月度"), "quarter": (90, "季度"), "year": (365, "年度")}
        if plan not in plans: raise ValueError("会员时长无效")
        grant_days, plan_name = plans[plan]
        code = "-".join(secrets.token_hex(3).upper()[i:i+3] for i in (0,3))
        stamp=now(); bid=secrets.token_hex(12); digest=hashlib.sha256(code.encode()).hexdigest()
        with self.lock, self.db:
            self.db.execute("INSERT INTO bindings(id,code_hash,code_hint,expires,plan,grant_days,created) VALUES(?,?,?,?,?,?,?)", (bid,digest,code[-3:],stamp+86400,plan,grant_days,stamp))
        self.event("绑定", f"创建{plan_name}会员码", bid); return {"id":bid,"code":code,"expires":stamp+86400,"plan":plan,"grant_days":grant_days}

    def consume_binding(self, code, tg_id):
        digest=hashlib.sha256(str(code).strip().upper().encode()).hexdigest(); stamp=now()
        with self.lock, self.db:
            row=self.db.execute("SELECT * FROM bindings WHERE code_hash=?",(digest,)).fetchone()
            if not row or row["status"] != "available" or row["expires"] < stamp: raise ValueError("绑定码无效或已过期")
            current=self.db.execute("SELECT membership_expires FROM users WHERE tg_id=?",(tg_id,)).fetchone()
            membership_expires=max(stamp,int(current[0] or 0) if current else 0)+int(row["grant_days"])*86400
            self.db.execute("""INSERT INTO users(tg_id,membership_expires,membership_plan,created,updated) VALUES(?,?,?,?,?)
              ON CONFLICT(tg_id) DO UPDATE SET membership_expires=excluded.membership_expires,
              membership_plan=excluded.membership_plan,updated=excluded.updated""",
              (tg_id,membership_expires,row["plan"],stamp,stamp))
            self.db.execute("UPDATE bindings SET status='used',used_by=? WHERE id=?",(str(tg_id),row["id"]))
        return {"id":row["id"],"membership_expires":membership_expires,"plan":row["plan"]}

    def binding_action(self, value):
        bid=str(value.get("id") or ""); action=str(value.get("action") or "")
        if action != "revoke": raise ValueError("未知绑定码操作")
        with self.lock, self.db: self.db.execute("UPDATE bindings SET status='revoked' WHERE id=? AND status='available'",(bid,))
        self.event("绑定", "撤销绑定码", bid)

    def delivery_action(self, value):
        did=str(value.get("id") or ""); action=str(value.get("action") or ""); stamp=now()
        with self.lock, self.db:
            if action == "retry": changed=self.db.execute("UPDATE deliveries SET status='retry',attempts=0,error='',next_attempt=0,updated=? WHERE id=? AND status NOT IN ('running','delivered')",(stamp,did)).rowcount
            elif action == "cancel": changed=self.db.execute("UPDATE deliveries SET status='cancelled',error='管理员已取消派送',next_attempt=0,updated=? WHERE id=? AND status NOT IN ('delivered','cancelled')",(stamp,did)).rowcount
            elif action == "retry-all": changed=self.db.execute("UPDATE deliveries SET status='retry',attempts=0,error='',next_attempt=0,updated=? WHERE status IN ('waiting','retry','cancelled','failed')",(stamp,)).rowcount
            elif action == "cancel-all": changed=self.db.execute("UPDATE deliveries SET status='cancelled',error='管理员已取消全部派送',next_attempt=0,updated=? WHERE status IN ('waiting','retry','running')",(stamp,)).rowcount
            elif action == "delete-all": changed=self.db.execute("DELETE FROM deliveries").rowcount
            else: raise ValueError("未知派送操作")
        descriptions={"retry":"手动重派","cancel":"取消派送","retry-all":"全部重新派送","cancel-all":"取消全部派送","delete-all":"删除全部派送任务记录"}
        self.event("派送",f"{descriptions[action]}：{changed} 条",did)
        return changed

    def service_action(self,action):
        stamp=now()
        if action not in {"resume","pause","restart"}: raise ValueError("未知服务操作")
        if action == "resume":
            self.set("enabled",True); self.event("服务控制","管理员启动自动扫描与派送"); return "服务已开始运行"
        if action == "pause":
            self.set("enabled",False)
            with self.lock,self.db:
                changed=self.db.execute("UPDATE deliveries SET status='retry',error='服务已暂停，等待恢复',next_attempt=0,updated=? WHERE status='running'",(stamp,)).rowcount
            self.event("服务控制",f"管理员暂停自动扫描与派送；暂停运行中任务 {changed} 条")
            return "服务已暂停"
        self.event("服务控制","管理员请求重启服务")
        return "服务正在重启"

    def relay_candidates(self, resource_id, target_tg_id, sha1="", size=0):
        with self.lock: rows=self.db.execute("""SELECT u.tg_id,u.name,u.username,u.note,u.cookie,MAX(d.updated) updated
          FROM deliveries d JOIN users u ON u.tg_id=d.tg_id JOIN resources r ON r.id=d.resource_id
          WHERE (d.resource_id=? OR (?<>'' AND UPPER(r.sha1)=UPPER(?) AND r.size=?))
            AND d.status='delivered' AND d.tg_id<>? AND u.enabled=1 AND u.status='active' AND u.cookie<>''
          GROUP BY u.tg_id,u.name,u.username,u.note,u.cookie ORDER BY updated DESC""",
          (resource_id,str(sha1 or ""),str(sha1 or ""),int(size or 0),target_tg_id)).fetchall()
        result=[]
        for row in rows:
            item=dict(row)
            try: item["cookie"]=self.crypt.decrypt(item["cookie"].encode()).decode()
            except InvalidToken: continue
            result.append(item)
        return result

    def wake_relay_retries(self,resource_id,successful_delivery_id):
        stamp=now()
        with self.lock,self.db:
            changed=self.db.execute("""UPDATE deliveries SET status='retry',next_attempt=0,updated=?,
              attempts=CASE WHEN status='failed' THEN 0 ELSE attempts END,
              error=CASE WHEN error='' THEN '已有其他账号接收成功，立即尝试接力秒传'
                ELSE error||'；已有其他账号接收成功，立即尝试接力秒传' END
              WHERE resource_id IN (SELECT candidate.id FROM resources candidate JOIN resources source
                ON source.id=? AND (candidate.id=source.id OR (source.sha1<>'' AND UPPER(candidate.sha1)=UPPER(source.sha1) AND candidate.size=source.size)))
                AND id<>? AND status IN ('retry','failed')""",(stamp,resource_id,successful_delivery_id)).rowcount
        return changed

    def resource_action(self, value):
        rid=str(value.get("id") or ""); action=str(value.get("action") or "")
        if action not in ("delete-record","delete-all-records"): raise ValueError("未知秒传记录操作")
        with self.lock,self.db:
            if action == "delete-all-records":
                active=self.db.execute("""SELECT COUNT(*) FROM deliveries d JOIN resources r ON r.id=d.resource_id
                  WHERE r.hidden=0 AND d.status IN ('waiting','running','retry')""").fetchone()[0]
                if active: raise ValueError("仍有未完成派送，请先点击“全部取消”再删除全部秒传记录")
                changed=self.db.execute("UPDATE resources SET hidden=1 WHERE hidden=0").rowcount
                self.event("秒传记录",f"删除全部记录：{changed} 条")
                return changed
            row=self.db.execute("SELECT name FROM resources WHERE id=?",(rid,)).fetchone()
            if not row: raise ValueError("秒传记录不存在")
            active=self.db.execute("SELECT COUNT(*) FROM deliveries WHERE resource_id=? AND status IN ('waiting','running','retry')",(rid,)).fetchone()[0]
            if active: raise ValueError("该资源仍有未完成派送，暂时不能删除记录")
            self.db.execute("UPDATE resources SET hidden=1 WHERE id=?",(rid,))
        self.event("秒传记录",f"删除记录：{row['name']}",rid)
        return 1

    def records_page(self, kind, page=1, page_size=100):
        page=max(1,int(page or 1)); page_size=max(1,min(100,int(page_size or 100))); offset=(page-1)*page_size
        with self.lock:
            if kind == "deliveries":
                total=self.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
                items=[dict(row) for row in self.db.execute("""SELECT d.*,r.name,u.name user_name,u.username
                  FROM deliveries d JOIN resources r ON r.id=d.resource_id JOIN users u ON u.tg_id=d.tg_id
                  ORDER BY d.created DESC,d.id LIMIT ? OFFSET ?""",(page_size,offset))]
            elif kind == "resources":
                total=self.db.execute("SELECT COUNT(*) FROM resources WHERE hidden=0").fetchone()[0]
                items=[dict(row) for row in self.db.execute("""SELECT r.*,
                  COUNT(d.id) delivery_total,
                  SUM(CASE WHEN d.status='delivered' THEN 1 ELSE 0 END) delivery_done,
                  SUM(CASE WHEN d.status IN ('waiting','running','retry') THEN 1 ELSE 0 END) delivery_active,
                  SUM(CASE WHEN d.status IN ('cancelled','failed') THEN 1 ELSE 0 END) delivery_failed
                  FROM resources r LEFT JOIN deliveries d ON d.resource_id=r.id
                  WHERE r.hidden=0 GROUP BY r.id ORDER BY r.first_seen DESC,r.id LIMIT ? OFFSET ?""",(page_size,offset))]
            else: raise ValueError("未知记录类型")
        pages=max(1,(total+page_size-1)//page_size)
        if page>pages: return self.records_page(kind,pages,page_size)
        return {"items":items,"page":page,"page_size":page_size,"total":total,"pages":pages}

    def overview(self):
        stamp=now()
        with self.lock, self.db:
            self.db.execute("UPDATE bindings SET status='expired' WHERE status='available' AND expires<?",(stamp,))
            users=[self.user(row[0], True) for row in self.db.execute("SELECT tg_id FROM users ORDER BY created DESC")]
            for user in users: user["account_bound"] = bool(user.get("cookie") and user.get("uid"))
            deliveries=[]
            resources=[]
            bindings=[dict(row) for row in self.db.execute("SELECT id,code_hint,expires,status,used_by,created,plan,grant_days FROM bindings ORDER BY created DESC LIMIT 100")]
            events=[dict(row) for row in self.db.execute("""SELECT e.*,
              COALESCE(NULLIF(u.name,''),CASE WHEN u.username<>'' THEN '@'||u.username END,
                CASE WHEN u.tg_id IS NOT NULL THEN '用户 '||u.tg_id END,NULLIF(du.name,''),
                CASE WHEN du.username<>'' THEN '@'||du.username END,CASE WHEN du.tg_id IS NOT NULL THEN '用户 '||du.tg_id END,
                NULLIF(r.name,''),NULLIF(s.name,''),'—') object_label
              FROM events e LEFT JOIN deliveries d ON d.id=e.object_id LEFT JOIN users u ON u.tg_id=d.tg_id
              LEFT JOIN users du ON du.tg_id=CAST(e.object_id AS INTEGER)
              LEFT JOIN resources r ON r.id=e.object_id LEFT JOIN sources s ON s.id=e.object_id
              ORDER BY e.created DESC LIMIT 200""")]
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
        heartbeat=int(self.get("monitor_heartbeat",0) or 0)
        return {"ok": True, "users": users, "pending": pending, "delivered": delivered,
                "last_error": self.get("last_error", ""), "last_scan": self.get("last_scan", 0),
                "monitor_heartbeat": heartbeat, "monitor_healthy": not self.get("enabled",False) or now()-heartbeat<180,
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

    def send_photo(self, chat_id, photo, caption):
        return self.call("sendPhoto", {"chat_id": str(chat_id), "photo": photo, "caption": caption, "parse_mode": "HTML"})

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
                membership=self.app.store.consume_binding(parts[1],tg_id)
                user=self.app.store.upsert_user(tg_id,sender.get("username",""),name)
                self.app.store.event("绑定",f"Telegram 用户完成绑定：{tg_id}",str(tg_id))
            except ValueError as exc: return self.send(tg_id,str(exc),False)
            expires=time.strftime("%Y-%m-%d %H:%M",time.localtime(membership["membership_expires"]))
            self.send(sender["id"], f"<b>蟑影递送已连接</b>\n会员有效期至：{expires}\n点击下方按钮绑定115并设置接收规则。")
        elif text.startswith("/start"):
            if not user: return self.send(tg_id,"尚未绑定。请向管理员索取绑定码，然后发送 <code>/bind 绑定码</code>。",False)
            self.app.store.upsert_user(tg_id,sender.get("username",""),name)
            self.send(tg_id,"<b>蟑影递送已连接</b>\n点击下方按钮管理115账号和接收规则。")
        elif not user:
            self.send(tg_id,"尚未绑定。请发送 <code>/bind 绑定码</code>。",False)
        elif text.startswith("/status"):
            receiving=bool(user["enabled"] and user.get("account_ready"))
            self.send(sender["id"], f"状态：{'接收中' if receiving else '已暂停'}\n模式：{user['mode']}\n115：{'配置完整' if user.get('account_ready') else '待完成 CK 与接收文件夹配置'}")
        elif text.startswith("/all"):
            self.app.store.save_user(sender["id"], {"mode": "all", "enabled": True})
            self.send(sender["id"], "已开启全部接收。")
        elif text.startswith("/pause"):
            self.app.store.save_user(sender["id"], {"enabled": False})
            self.send(sender["id"], "已暂停自动接收。")
        else:
            self.send(sender["id"], "可用命令：/start /status /all /pause")


class TMDB:
    API = "https://api.themoviedb.org/3"
    WEB = "https://www.themoviedb.org"
    IMAGE = "https://image.tmdb.org/t/p/w780"

    @staticmethod
    def request(api_key, path, params=None, timeout=15):
        query = {"api_key": api_key, "language": "zh-CN", **(params or {})}
        url = f"{TMDB.API}/{path.lstrip('/')}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "CockroachRunner/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())

    @staticmethod
    def parse_name(filename, category=""):
        stem = Path(str(filename)).stem
        explicit = re.search(r"\{tmdb[-_: ]?(\d+)\}", stem, re.I)
        episode = None
        for pattern in (
            r"(?<![A-Za-z0-9])S(?:eason)?[ ._-]?(\d{1,2})[ ._-]*E(?:p(?:isode)?)?[ ._-]?(\d{1,3})(?:[ ._-]*(?:E|EP|-)[ ._-]?(\d{1,3}))?(?!\d)",
            r"第\s*(\d{1,2})\s*季[ ._-]*第?\s*(\d{1,3})\s*集(?:\s*[-至到]\s*第?\s*(\d{1,3})\s*集)?",
            r"(?<![A-Za-z0-9])Season[ ._-]?(\d{1,2})[ ._-]+Episode[ ._-]?(\d{1,3})(?:[ ._-]*(?:-|to)[ ._-]?(\d{1,3}))?(?!\d)",
        ):
            episode = re.search(pattern, stem, re.I)
            if episode: break
        episode_only = None
        if not episode and str(category) in {"剧集", "电视剧", "综艺", "动漫"}:
            episode_only = re.search(r"(?<![A-Za-z0-9])(?:EP?|第)[ ._-]?(\d{1,3})(?:[ ._-]*(?:集|END))?(?!\d)", stem, re.I)
        year_match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", stem)
        media_type = "tv" if episode or episode_only or str(category) in {"剧集", "电视剧", "综艺", "动漫"} else "movie"
        marker = episode or episode_only
        title = stem[:marker.start()] if marker else stem
        title = re.sub(r"\{(?:tmdb|imdb)[^}]*\}", " ", title, flags=re.I)
        title = re.sub(r"[\[(（]?(?:19|20)\d{2}[\])）]?", " ", title)
        title = re.split(r"(?i)(?:\b(?:2160p|1080p|720p|4k|8k|web[- .]?dl|bluray|blu[- .]?ray|remux|hdtv)\b)", title, maxsplit=1)[0]
        title = re.sub(r"[._]+", " ", title)
        title = re.sub(r"\s*[-–—]+\s*$", "", title)
        title = re.sub(r"\s+", " ", title).strip(" -–—[]()（）")
        season = int(episode.group(1)) if episode else (1 if episode_only else 0)
        episode_number = int(episode.group(2)) if episode else (int(episode_only.group(1)) if episode_only else 0)
        episode_end = int(episode.group(3)) if episode and episode.lastindex and episode.lastindex >= 3 and episode.group(3) else 0
        return {"title": title or stem, "year": int(year_match.group(1)) if year_match else 0,
                "media_type": media_type, "tmdb_id": int(explicit.group(1)) if explicit else 0,
                "season": season, "episode": episode_number, "episode_end": episode_end}

    @staticmethod
    def episode_text(parsed):
        season=int(parsed.get("season") or 0); episode=int(parsed.get("episode") or 0); episode_end=int(parsed.get("episode_end") or 0)
        if not episode: return ""
        episode_part=f"第{episode}集" if not episode_end or episode_end == episode else f"第{episode}-{episode_end}集"
        return f"第{season}季 · {episode_part}" if season else episode_part

    @staticmethod
    def detail(api_key, media_type, tmdb_id):
        data = TMDB.request(api_key, f"{media_type}/{int(tmdb_id)}", {"append_to_response": "credits,external_ids"})
        data["media_type"] = media_type
        return data

    @staticmethod
    def search(api_key, query):
        text=str(query or "").strip()[:100]
        if not text: return []
        result=TMDB.request(api_key,"search/multi",{"query":text,"include_adult":"false"})
        items=[]
        for item in result.get("results") or []:
            media_type=str(item.get("media_type") or "")
            if media_type not in {"movie","tv"} or not item.get("id"): continue
            title=str(item.get("title") or item.get("name") or item.get("original_title") or item.get("original_name") or "")
            date=str(item.get("release_date") or item.get("first_air_date") or "")
            poster=str(item.get("poster_path") or "")
            items.append({"id":int(item["id"]),"media_type":media_type,"title":title,"year":date[:4] if re.match(r"\d{4}",date) else "",
                          "poster":TMDB.IMAGE+poster if poster.startswith("/") else "","overview":str(item.get("overview") or "")[:240],
                          "rating":round(float(item.get("vote_average") or 0),1)})
            if len(items)>=12: break
        return items

    @staticmethod
    def lookup(api_key, item):
        parsed = TMDB.parse_name(item.get("name", ""), item.get("category", ""))
        media_types = [parsed["media_type"], "movie" if parsed["media_type"] == "tv" else "tv"]
        if parsed["tmdb_id"]:
            for media_type in media_types:
                try: return TMDB.detail(api_key, media_type, parsed["tmdb_id"])
                except urllib.error.HTTPError as exc:
                    if exc.code != 404: raise
            return None
        params = {"query": parsed["title"], "include_adult": "false"}
        if parsed["year"]:
            params["first_air_date_year" if parsed["media_type"] == "tv" else "year"] = parsed["year"]
        result = TMDB.request(api_key, f"search/{parsed['media_type']}", params)
        matches = result.get("results") or []
        if not matches: return None
        return TMDB.detail(api_key, parsed["media_type"], matches[0]["id"])

    @staticmethod
    def size_text(size):
        value = max(0, int(size or 0))
        units = ("B", "KB", "MB", "GB", "TB")
        amount = float(value)
        for unit in units:
            if amount < 1024 or unit == units[-1]: return f"{amount:.2f}{unit}" if unit != "B" else f"{int(amount)}B"
            amount /= 1024

    @staticmethod
    def specifications(filename):
        text = str(filename); upper = text.upper(); result=[]
        if re.search(r"(?:2160P|\b4K\b)", upper): result.append("4K")
        elif "1080P" in upper: result.append("1080P")
        elif "720P" in upper: result.append("720P")
        if re.search(r"DOLBY[ ._-]?VISION|\bDOVI\b|\bDV\b", upper): result.append("杜比视界")
        elif "HDR10+" in upper: result.append("HDR10+")
        elif "HDR10" in upper: result.append("HDR10")
        elif "HDR" in upper: result.append("HDR")
        elif "SDR" in upper: result.append("SDR")
        if re.search(r"\b(?:HEVC|H[ .]?265|X265)\b", upper): result.append("HEVC")
        elif re.search(r"\b(?:AVC|H[ .]?264|X264)\b", upper): result.append("AVC")
        elif re.search(r"\bAV1\b", upper): result.append("AV1")
        suffix=Path(text).suffix.lstrip(".").upper()
        if suffix: result.append(suffix)
        return result

    @staticmethod
    def region_category(metadata, kind):
        countries = {str(item.get("iso_3166_1") or "") for item in metadata.get("production_countries") or []}
        countries.update(str(code) for code in metadata.get("origin_country") or [])
        if countries & {"CN","HK","TW","MO"}: region="华语"
        elif countries & {"JP","KR"}: region="日韩"
        elif countries & {"US","GB","CA","FR","DE","IT","ES","AU","NZ","IE"}: region="欧美"
        else: region=""
        return f"{region}{kind}"

    @staticmethod
    def caption(item, metadata):
        media_type=metadata.get("media_type") or "movie"; kind="剧集" if media_type == "tv" else "电影"
        title=str(metadata.get("name") or metadata.get("title") or TMDB.parse_name(item.get("name", ""))["title"])
        date=str(metadata.get("first_air_date") or metadata.get("release_date") or "")
        year=date[:4] if re.match(r"\d{4}",date) else str(TMDB.parse_name(item.get("name", ""))["year"] or "")
        tmdb_id=int(metadata.get("id") or 0); tmdb_url=f"{TMDB.WEB}/{media_type}/{tmdb_id}?language=zh-CN"
        rating=float(metadata.get("vote_average") or 0); category=TMDB.region_category(metadata,kind)
        cast=(metadata.get("credits") or {}).get("cast") or []
        actors=" / ".join(f'<a href="{TMDB.WEB}/person/{int(person["id"])}?language=zh-CN">{html.escape(str(person.get("name") or ""))}</a>' for person in cast[:4] if person.get("id") and person.get("name")) or "暂无资料"
        specs=TMDB.specifications(item.get("name", "")); spec_text=" / ".join(specs) or "待识别"
        short_name=str(item.get("name") or ""); short_name=short_name if len(short_name)<=90 else short_name[:87]+"…"
        genres=[str(value.get("name") or "") for value in metadata.get("genres") or [] if value.get("name")][:3]
        tag_title=re.sub(r"[^\w\u4e00-\u9fff]+","_",title).strip("_")[:30]
        tags=[kind,tag_title,*specs[:3],*genres]
        tag_line=" ".join("#"+re.sub(r"[^\w\u4e00-\u9fff]+","_",value).strip("_") for value in tags if value)
        imdb=(metadata.get("external_ids") or {}).get("imdb_id") or metadata.get("imdb_id") or ""
        parsed=TMDB.parse_name(item.get("name", ""),item.get("category", ""))
        episode_text=TMDB.episode_text(parsed)
        episode_line=f" · {episode_text}" if episode_text else ""
        lines=["🥳 <b>派送成功</b>",f"🎬 <b>{html.escape(title)}{(' · '+year) if year else ''}{episode_line}</b>","", "🎞 <b>影片资料</b>",
               f"├ 类型 {kind}",*([f"├ 季集 {episode_text}"] if episode_text else []),f'├ TMDB ID <a href="{tmdb_url}">{tmdb_id}</a>',f"├ 分类 {html.escape(category)}",
               f"├ 评分 {rating:.1f} / 10",f"├ 主演 {actors}","├ 接收方式 蟑影派送",
               f"├ 落盘位置 <code>{html.escape(str(item.get('delivery_path') or item.get('destination') or '/'+str(item.get('target_name') or '接收目录').strip('/')))}</code>",
               f"└ 大小 {TMDB.size_text(item.get('size',0))}","",
               "🎥 <b>影音规格</b>",html.escape(spec_text),"", "📂 <b>文件列表 · 1 项</b>",f"1. <code>{html.escape(short_name)}</code>","",f"🏷 {html.escape(tag_line)}"]
        if imdb: lines.extend(["",f'🎬 <a href="https://www.imdb.com/title/{urllib.parse.quote(str(imdb))}/">IMDb {html.escape(str(imdb))}</a>'])
        overview=re.sub(r"\s+"," ",str(metadata.get("overview") or "")).strip()
        if overview:
            overview=overview if len(overview)<=260 else overview[:257]+"…"
            lines.extend(["","📖 <b>剧情简介</b>",html.escape(overview)])
        return "\n".join(lines)

    @staticmethod
    def poster(metadata):
        path=str(metadata.get("poster_path") or "")
        return TMDB.IMAGE+path if path.startswith("/") else ""


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
    def qr_start(app):
        from p115client import P115Client
        if app not in QR_LOGIN_APPS: raise ValueError("不支持的115登录端")
        response=P115Client.login_qrcode_token(app,timeout=20)
        data=response.get("data") if isinstance(response,dict) else None
        if not isinstance(data,dict) or not data.get("uid"): raise RuntimeError((response or {}).get("message") or "生成115二维码失败")
        image=P115Client.login_qrcode(str(data["uid"]),app=app,timeout=20)
        return {"uid":str(data["uid"]),"time":data.get("time"),"sign":data.get("sign")}, bytes(image)

    @staticmethod
    def qr_poll(token,app):
        from p115client import P115Client
        if app not in QR_LOGIN_APPS: raise ValueError("不支持的115登录端")
        response=P115Client.login_qrcode_scan_status(token,timeout=35)
        data=response.get("data") if isinstance(response,dict) else None
        if not isinstance(data,dict):
            raise RuntimeError((response or {}).get("message") or (response or {}).get("error") or "查询扫码状态失败")
        status=int(data.get("status",0))
        if status == 0: return "waiting", ""
        if status == 1: return "scanned", ""
        if status != 2: return "expired", ""
        result=P115Client.login_qrcode_scan_result(str(token["uid"]),app=app,timeout=20)
        result_data=result.get("data") if isinstance(result,dict) else None
        cookie=(result_data or {}).get("cookie") if isinstance(result_data,dict) else None
        if isinstance(cookie,dict): cookie="; ".join(f"{key}={value}" for key,value in cookie.items() if value is not None)
        if not isinstance(cookie,str) or not cookie.strip(): raise RuntimeError((result or {}).get("message") or "扫码成功但未取得115 Cookie")
        return "confirmed", cookie.strip()

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
    def _file_entry(item, expected):
        if not isinstance(item,dict): return None
        sha1=str(item.get("sha") or item.get("sha1") or expected.get("sha1") or "").upper()
        size=int(item.get("s") or item.get("size") or item.get("file_size") or 0)
        node_id=str(item.get("fid") or item.get("file_id") or item.get("id") or "")
        pickcode=str(item.get("pc") or item.get("pick_code") or item.get("pickcode") or "")
        expected_size=int(expected.get("size") or 0)
        if not node_id or not pickcode or sha1 != str(expected.get("sha1") or "").upper(): return None
        if size and expected_size and size != expected_size: return None
        return {"node_id":node_id,"name":str(item.get("n") or item.get("fn") or item.get("file_name") or item.get("name") or expected.get("name") or ""),
                "is_dir":False,"pickcode":pickcode,"sha1":sha1,"size":size or expected_size}

    @staticmethod
    def _find_file_client(client, entry):
        sha1=str(entry.get("sha1") or "").upper()
        if sha1:
            last_error=None
            for attempt in range(3):
                try:
                    result=client.fs_shasearch(sha1,timeout=6)
                    if not isinstance(result,dict) or result.get("state") is False: break
                    found=P115._file_entry(result.get("data") or result,entry)
                    if found: return found
                    break
                except Exception as exc:
                    last_error=exc
                    if attempt<2: time.sleep(0.4*(attempt+1))
            if last_error and attempt == 2: raise last_error
        title=re.split(r"\.(?:19|20)\d{2}\b|\.S\d{1,2}E\d{1,3}\b",entry["name"],maxsplit=1,flags=re.I)[0]
        queries=list(dict.fromkeys(filter(None,(entry["name"],title.strip(" ._-"),Path(entry["name"]).stem))))
        for query in queries:
            result=client.fs_search({"search_value":query,"limit":115,"offset":0,"show_dir":0},timeout=8)
            data=result.get("data") if isinstance(result,dict) else None
            items=(data.get("data") or data.get("list") or []) if isinstance(data,dict) else data if isinstance(data,list) else []
            for item in items:
                found=P115._file_entry(item,entry)
                if found: return found
        return None

    @staticmethod
    def find_file(cookie, entry):
        from p115client import P115Client
        client=P115Client(P115.cookie_mapping(cookie))
        return P115._find_file_client(client,entry)

    @staticmethod
    def ensure_folder(cookie, parent_cid, name):
        from p115client import P115Client
        parent_cid=str(parent_cid or "0"); name=str(name or "").strip()
        if not name or "/" in name or "\\" in name: raise ValueError("115 分类目录名称无效")
        def find():
            return next((item["node_id"] for item in P115.list_dir(cookie,parent_cid)
                         if item["is_dir"] and item["name"]==name),"")
        existing=find()
        if existing: return existing
        client=P115Client(P115.cookie_mapping(cookie))
        result=client.fs_mkdir(name,pid=parent_cid)
        if not isinstance(result,dict) or result.get("state") is False:
            raise RuntimeError((result or {}).get("error") or "创建115分类目录失败")
        for _ in range(6):
            time.sleep(1)
            existing=find()
            if existing: return existing
        raise RuntimeError("创建115分类目录后未找到目录")

    @staticmethod
    def transfer(source_cookie, user_cookie, entry, target_cid):
        from p115client import P115Client
        if entry["is_dir"]:
            raise RuntimeError("目录资源必须先展开为视频文件，不能作为一个整体派送")
        source = P115Client(P115.cookie_mapping(source_cookie))
        target = P115Client(P115.cookie_mapping(user_cookie))

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

        existing=P115._find_file_client(target,entry)
        if existing:
            before = {item["node_id"] for item in target_entries()}
            result = target.fs_copy(existing["node_id"], pid=target_cid)
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

        class RangeReader:
            def __init__(self, source_client, pickcode, name, size):
                self.source = source_client
                self.pickcode = pickcode
                self.url = ""
                self.headers = {}
                self.name = name
                self.size = int(size)
                self.position = 0

            def refresh(self):
                source_url=self.source.download_url(self.pickcode,timeout=8)
                self.url=str(source_url)
                self.headers=dict(getattr(source_url,"headers",None) or {})

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
                last_error=None
                for attempt in range(3):
                    try:
                        if not self.url or attempt: self.refresh()
                        headers = {**self.headers, "Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
                        request = urllib.request.Request(self.url, headers=headers)
                        with urllib.request.urlopen(request, timeout=8) as response:
                            data = response.read(end - start + 1)
                        if len(data) != end-start+1: raise IOError(f"115 验证片段长度异常：期望 {end-start+1}，实际 {len(data)}")
                        break
                    except Exception as exc:
                        last_error=exc; self.url=""
                        if attempt<2: time.sleep(0.5*(attempt+1))
                else:
                    raise RuntimeError(f"115 验证片段连续读取失败：{last_error}") from last_error
                self.position += len(data)
                return data

        reader = RangeReader(source,entry["pickcode"], entry["name"], entry["size"])
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


class CD2:
    @staticmethod
    def _api_client(cfg):
        try:
            import grpc
            from clouddrive2_client import CloudDriveClient
        except ImportError as exc:
            raise RuntimeError("当前镜像缺少 CloudDrive2 API 客户端") from exc
        host = str(cfg.get("cd2_host") or "127.0.0.1").strip()
        port = max(1, min(65535, int(cfg.get("cd2_port") or 19798)))
        client = CloudDriveClient(f"{host}:{port}")
        token = str(cfg.get("cd2_api_token") or "").strip()
        if not token:
            client.close()
            raise RuntimeError("尚未配置 CD2 API Token")
        client.jwt_token = token
        try:
            grpc.channel_ready_future(client.channel).result(timeout=8)
        except Exception as exc:
            client.close()
            raise RuntimeError(f"无法连接 CD2 API：{host}:{port}") from exc
        return client

    @staticmethod
    def _api_name(item):
        full_path = str(getattr(item, "fullPathName", "") or "")
        return str(getattr(item, "name", "") or getattr(item, "fileName", "") or Path(full_path.rstrip("/")).name)

    @staticmethod
    def list_api(cfg, path):
        path = str(path or cfg.get("cd2_api_root") or "/").strip() or "/"
        if not path.startswith("/"): raise ValueError("CD2 API 路径必须以 / 开头")
        client = CD2._api_client(cfg)
        try:
            from clouddrive2_client.proto import clouddrive_pb2
            request = clouddrive_pb2.ListSubFileRequest(path=path, forceRefresh=True)
            metadata = client._create_authorized_metadata()
            result = []
            for response in client.stub.GetSubFiles(request, metadata=metadata, timeout=20):
                for item in response.subFiles:
                    name = CD2._api_name(item)
                    full_path = str(getattr(item, "fullPathName", "") or "") or (path.rstrip("/") + "/" + name)
                    is_dir = bool(getattr(item, "isDirectory", False))
                    hashes = getattr(item, "fileHashes", None)
                    try:
                        sha1 = str(hashes.get(2, "") or "").upper() if hashes is not None else ""
                    except (AttributeError, TypeError):
                        sha1 = ""
                    if name:
                        result.append({"name": name, "path": full_path, "is_dir": is_dir,
                                       "node_id": str(getattr(item, "id", "") or ""),
                                       "sha1": sha1,
                                       "size": int(getattr(item, "size", 0) or 0)})
            return result
        except Exception as exc:
            raise RuntimeError(f"读取 CD2 API 目录失败：{exc}") from exc
        finally:
            client.close()

    @staticmethod
    def list_mount(cfg, path=""):
        root = Path(str(cfg.get("cd2_root") or "/cd2/miaochuang")).resolve()
        selected = Path(str(path or root)).resolve()
        if selected != root and root not in selected.parents: raise ValueError("CD2 目录超出已配置的挂载根目录")
        if not selected.is_dir(): raise RuntimeError("CD2 挂载目录不存在或容器无权访问")
        return [{"name": item.name, "path": str(Path(item.path).resolve()), "is_dir": item.is_dir(follow_symlinks=False)} for item in os.scandir(selected)]

    @staticmethod
    def list_dir(cfg, path="", mode=None):
        selected_mode = mode or str(cfg.get("cd2_mode") or "mount")
        return CD2.list_api(cfg, path) if selected_mode == "api" else CD2.list_mount(cfg, path)

    @staticmethod
    def test(cfg):
        mode = str(cfg.get("cd2_mode") or "mount")
        path = cfg.get("cd2_api_root") if mode == "api" else cfg.get("cd2_root")
        items = CD2.list_dir(cfg, path, mode)
        return {"mode": mode, "path": path, "count": len(items)}


def category_for(name, default):
    text = name.lower()
    if any(word in text for word in ("anime", "动漫", "动画", "ova")): return "动漫"
    if any(word in text for word in ("documentary", "纪录片", "docu")): return "纪录片"
    if re.search(r"(?:\b(?:s\d{1,2}e\d{1,3}|ep?\d{1,3})\b|第.{1,4}集)", text, re.I): return "剧集"
    return default or "电影"


class Application:
    def __init__(self, store):
        self.store, self.stop = store, threading.Event()
        self.telegram = Telegram(self)
        self.scan_lock = threading.Lock()
        self.cd2_snapshots = {}
        self.qr_sessions = {}
        self.qr_lock = threading.Lock()
        self.tmdb_cache = {}
        self.tmdb_lock = threading.Lock()
        self.delivery_lock = threading.Lock()
        self.cleanup_lock = threading.Lock()
        self.loop_errors = {}
        self.relay_failures = {}
        self.destination_cache = {}
        self.destination_lock = threading.Lock()
        self.p115_gate = RateGate()
        self.cd2_gate = RateGate()
        self.transfer_gate = RateGate()

    def tmdb_metadata(self, api_key, item):
        cache_key=str(item.get("resource_id") or item.get("id") or item.get("name") or "")
        with self.tmdb_lock:
            cached=self.tmdb_cache.get(cache_key)
            if cached and cached[0]>now(): return cached[1]
        metadata=TMDB.lookup(api_key,item)
        with self.tmdb_lock: self.tmdb_cache[cache_key]=(now()+21600,metadata)
        return metadata

    def notify_delivery_success(self,cfg,item):
        parsed=TMDB.parse_name(item.get("name",""),item.get("category","")); episode_text=TMDB.episode_text(parsed)
        destination=html.escape(str(item.get("delivery_path") or item.get("destination") or "/"+str(item.get("target_name") or "接收目录").strip("/")))
        fallback=f"🥳 <b>派送成功</b>\n🎬 {html.escape(str(item['name']))}"+(f"\n📺 {episode_text}" if episode_text else "")+f"\n📂 落盘位置：<code>{destination}</code>"
        api_key=str(cfg.get("tmdb_api_key") or "")
        if not cfg.get("tmdb_enabled") or not api_key:
            return self.telegram.send(item["tg_id"],fallback,False)
        try:
            metadata=self.tmdb_metadata(api_key,item)
        except Exception as exc:
            self.store.event("TMDB",f"{item['name']}：资料读取失败，已改用普通成功通知；{redact_sensitive_text(exc)}",item["id"])
            return self.telegram.send(item["tg_id"],fallback,False)
        if not metadata:
            self.store.event("TMDB",f"{item['name']}：未匹配到影视资料，已改用普通成功通知",item["id"])
            return self.telegram.send(item["tg_id"],fallback,False)
        caption=TMDB.caption(item,metadata); poster=TMDB.poster(metadata)
        if poster:
            try: return self.telegram.send_photo(item["tg_id"],poster,caption)
            except Exception as exc:
                self.store.event("TMDB海报",f"{item['name']}：海报发送失败，已改为文字资料；{redact_sensitive_text(exc)}",item["id"])
        return self.telegram.send(item["tg_id"],caption,False)

    @staticmethod
    def user_label(value):
        return str(value.get("user_name") or value.get("name") or ("@"+value["username"] if value.get("username") else "") or value.get("note") or f"用户 {value.get('tg_id','未知')}")

    @staticmethod
    def transfer_error(exc):
        if isinstance(exc,TimeoutError): return str(exc)
        payload=exc.args[0] if getattr(exc,"args",None) and isinstance(exc.args[0],dict) else None
        if payload and {"pid","filename","filesha1","user_id"}.issubset(payload):
            return f"115 秒传初始化失败（目标账号 UID {payload.get('user_id')}，可能触发风控、秒传验证未通过或接口暂时异常）"
        text=redact_sensitive_text(exc)
        if "[Errno 61]" in text: return "115 接口暂时拒绝请求（Errno 61）"
        return text[:500]

    @staticmethod
    def timed_call(callback, timeout_seconds):
        result={}; finished=threading.Event()
        def worker():
            try: result["value"]=callback()
            except BaseException as exc: result["error"]=exc
            finally: finished.set()
        threading.Thread(target=worker,daemon=True,name="transfer-call").start()
        timeout=max(0.1,min(3600.0,float(timeout_seconds or 300)))
        if not finished.wait(timeout): raise TimeoutError(f"秒传超过 {timeout:.0f} 秒，任务已暂停并等待稍后重试")
        if "error" in result: raise result["error"]
        return result.get("value")

    def transfer_delivery(self,cfg,item,target_cookie):
        timeout=max(30,min(3600,int(cfg.get("transfer_timeout_seconds") or 300)))
        deadline=time.monotonic()+timeout
        def remaining_timeout():
            remaining=deadline-time.monotonic()
            if remaining<=0: raise TimeoutError(f"秒传超过 {timeout} 秒，任务已暂停并等待稍后重试")
            return remaining
        target_label=self.user_label(item)
        relay_errors=[]
        if cfg.get("distributed_transfer_enabled"):
            for relay in self.store.relay_candidates(item["resource_id"],item["tg_id"],item.get("sha1"),item.get("size")):
                relay_label=self.user_label(relay)
                failure_key=(str(item.get("sha1") or item["resource_id"]).upper(),int(relay["tg_id"]))
                if self.relay_failures.get(failure_key,0)>now():
                    continue
                try:
                    def relay_action(relay=relay):
                        relay_entry=P115.find_file(relay["cookie"],item)
                        if not relay_entry: raise FileNotFoundError("成功账号中未找到文件，可能已删除或改名")
                        relay_entry["name"]=item["name"]
                        return P115.transfer(relay["cookie"],target_cookie,relay_entry,item["target_cid"])
                    self.timed_call(relay_action,remaining_timeout())
                    self.store.event("接力秒传",f"{relay_label} → {target_label}：{item['name']} 成功",item["id"])
                    self.relay_failures.pop(failure_key,None)
                    return f"接力：{relay_label}"
                except TimeoutError:
                    raise
                except Exception as exc:
                    self.relay_failures[failure_key]=now()+1800
                    error=self.transfer_error(exc); relay_errors.append(f"{relay_label}：{error}")
                    self.store.event("接力跳过",f"{relay_label} → {target_label}：{item['name']}；{error}",item["id"])
        source_cookie=self.store.source_cookie(item["source_id"],cfg["source_cookie"])
        if not source_cookie: raise RuntimeError("资源所属监听目录没有可用的 115 CK")
        if relay_errors:
            self.store.event("接力回退",f"{target_label}：接力账号均不可用，改用源账号；{item['name']}",item["id"])
        try:
            self.timed_call(lambda:P115.transfer(source_cookie,target_cookie,item,item["target_cid"]),remaining_timeout())
            return "主源账号" if not relay_errors else "主源回退"
        except Exception as exc:
            error=self.transfer_error(exc)
            if relay_errors: error=f"接力账号均失败（{'；'.join(relay_errors)}）；主源回退失败：{error}"
            raise RuntimeError(error) from exc

    def prepare_destination(self,cfg,item,target_cookie):
        base_cid=str(item.get("target_cid") or "0")
        base_name=str(item.get("target_name") or "接收目录").strip("/") or "接收目录"
        if not bool(item.get("hierarchy")):
            return base_cid,"/"+base_name
        category=str(item.get("category") or "其他").strip() or "其他"
        cache_key=(int(item["tg_id"]),base_cid,category)
        with self.destination_lock:
            cached=self.destination_cache.get(cache_key)
            if cached:
                return cached,"/"+base_name+"/"+category
            # Keep folder discovery/creation single-flight so concurrent workers
            # cannot create duplicate category folders for the same account.
            self.p115_gate.wait(cfg.get("p115_api_interval_seconds",1))
            folder_cid=P115.ensure_folder(target_cookie,base_cid,category)
            self.destination_cache[cache_key]=folder_cid
        return folder_cid,"/"+base_name+"/"+category

    def reconcile_user_deliveries(self,user):
        stamp=now(); cancelled=0
        with self.store.lock,self.store.db:
            rows=self.store.db.execute("""SELECT d.id,r.name,r.category FROM deliveries d
              JOIN resources r ON r.id=d.resource_id WHERE d.tg_id=? AND d.status IN ('waiting','retry')""",(user["tg_id"],)).fetchall()
            for row in rows:
                if self.matches(user,row["name"],row["category"]): continue
                cancelled+=self.store.db.execute("""UPDATE deliveries SET status='cancelled',error='接收规则已变更，未开始任务已取消',
                  next_attempt=0,updated=? WHERE id=? AND status IN ('waiting','retry')""",(stamp,row["id"])).rowcount
        if cancelled:
            self.store.event("接收规则",f"{self.user_label(user)}：取消 {cancelled} 个不再匹配的待派送任务",str(user["tg_id"]))
        return cancelled

    def mini_preferences(self,tg_id,value):
        mode=str(value.get("mode") or "all")
        categories=[str(item) for item in (value.get("categories") or []) if str(item) in {"电影","剧集","动漫","纪录片","其他"}]
        include_terms=str(value.get("include_terms") or "").strip()
        if mode=="category" and not categories: raise ValueError("按分类接收时请至少选择一个分类")
        if mode=="subscription" and not include_terms: raise ValueError("按订阅关键词接收时请至少填写一个关键词")
        if mode=="both" and (not categories or not include_terms): raise ValueError("订阅和分类模式需要同时选择分类并填写关键词")
        current=self.store.user(tg_id)
        if mode=="tmdb" and not (current or {}).get("tmdb_subscriptions"): raise ValueError("请先在首页添加至少一个 TMDB 订阅")
        update={"mode":mode,"enabled":mode!="off","categories":categories,
                "include_terms":include_terms,"exclude_terms":str(value.get("exclude_terms") or "").strip(),
                "hierarchy":bool(value.get("hierarchy",False))}
        saved=self.store.save_user(tg_id,update)
        cancelled=self.reconcile_user_deliveries(saved)
        self.store.event("接收规则",f"{self.user_label(saved)}：保存 {mode} 模式，取消 {cancelled} 个待派送任务",str(tg_id))
        return {"user":saved,"cancelled":cancelled}

    def admin_user_save(self,value):
        tg_id=int(value.get("tg_id") or 0)
        result=self.mini_preferences(tg_id,value)
        extra={key:value[key] for key in ("status","search_limit","target_cid","target_name","note","enabled","account_ready") if key in value}
        if "target_cid" in extra or "target_name" in extra:
            current=self.store.user(tg_id,True) or {}
            cid=str(extra.get("target_cid",current.get("target_cid") or "0"))
            name=str(extra.get("target_name",current.get("target_name") or "根目录"))
            extra["account_ready"]=bool(current.get("cookie") and cid not in {"","0"} and name not in {"","根目录"})
        saved=self.store.save_user(tg_id,extra)
        result["cancelled"]+=self.reconcile_user_deliveries(saved)
        result["user"]=saved
        return result
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

    def qr_start(self,tg_id,value):
        login_app=str(value.get("app") or "alipaymini")
        if login_app not in QR_LOGIN_APPS: raise ValueError("不支持的115登录端")
        token,image=P115.qr_start(login_app); sid=secrets.token_urlsafe(24); expires=now()+300
        with self.qr_lock:
            self.qr_sessions={key:value for key,value in self.qr_sessions.items() if value["expires"]>now()}
            self.qr_sessions[sid]={"tg_id":tg_id,"token":token,"app":login_app,"expires":expires,"image":image}
        return {"session":sid,"app":login_app,"app_name":QR_LOGIN_APPS[login_app],"expires":expires,"image_url":"/api/mini/qr/image?session="+urllib.parse.quote(sid),"image":"data:image/png;base64,"+base64.b64encode(image).decode()}

    def qr_image(self,sid):
        with self.qr_lock: session=self.qr_sessions.get(str(sid or ""))
        if not session or session["expires"]<=now(): raise ValueError("二维码已失效，请重新生成")
        return session["image"]

    def qr_poll(self,tg_id,value):
        sid=str(value.get("session") or "")
        with self.qr_lock: session=self.qr_sessions.get(sid)
        if not session or session["tg_id"]!=tg_id: raise ValueError("二维码会话不存在")
        if session["expires"]<=now():
            with self.qr_lock: self.qr_sessions.pop(sid,None)
            return {"status":"expired"}
        status,cookie=P115.qr_poll(session["token"],session["app"])
        if status != "confirmed": return {"status":status,"expires":session["expires"]}
        profile=P115.profile(cookie)
        target_cid=str(value.get("target_cid") or "0"); target_name=str(value.get("target_name") or "根目录")
        ready=bool(target_cid not in {"","0"} and target_name not in {"","根目录"})
        user=self.store.save_user(tg_id,{"cookie":cookie,"uid":profile["uid"],"target_cid":target_cid,"target_name":target_name,"account_ready":ready})
        with self.qr_lock: self.qr_sessions.pop(sid,None)
        self.store.event("用户管理",f"扫码绑定115：{tg_id}",str(tg_id))
        return {"status":"confirmed","user":user,"account":profile}

    def folders_115(self, cookie, cid="0"):
        if not cookie: raise RuntimeError("尚未配置可用的 115 Cookie")
        self.p115_gate.wait(self.store.get("p115_api_interval_seconds", 1))
        folders = [entry for entry in P115.list_dir(cookie, str(cid or "0")) if entry["is_dir"]]
        return [{"name": item["name"], "cid": item["node_id"], "is_dir": True} for item in folders]

    def admin_browse(self, query):
        provider = str(query.get("provider", ["115"])[0] or "115")
        if provider == "115":
            return {"provider": "115", "items": self.folders_115(self.store.get("source_cookie", ""), query.get("cid", ["0"])[0])}
        if provider == "user115":
            tg_id = int(query.get("tg_id", ["0"])[0] or 0)
            user = self.store.user(tg_id, True)
            if not user: raise ValueError("用户不存在")
            return {"provider": "115", "items": self.folders_115(user.get("cookie", ""), query.get("cid", ["0"])[0])}
        if provider == "cd2":
            cfg = self.store.config(True)
            mode = str(query.get("mode", [cfg.get("cd2_mode", "mount")])[0] or cfg.get("cd2_mode", "mount"))
            path = str(query.get("path", [""])[0] or (cfg.get("cd2_api_root") if mode == "api" else cfg.get("cd2_root")))
            self.cd2_gate.wait(cfg.get("cd2_api_interval_seconds", 1))
            return {"provider": "cd2", "mode": mode, "path": path, "items": [item for item in CD2.list_dir(cfg, path, mode) if item["is_dir"]]}
        raise ValueError("不支持的目录来源")

    def source_browse(self,value):
        mode=str(value.get("cookie_mode") or "global")
        if mode == "independent":
            cookie=str(value.get("cookie") or "").strip()
            if not cookie: cookie=self.store.source_cookie(str(value.get("source_id") or ""),"")
        else: cookie=str(self.store.get("source_cookie","") or "")
        return {"provider":"115","items":self.folders_115(cookie,str(value.get("cid") or "0"))}

    def mini_browse(self, tg_id, query):
        user = self.store.user(tg_id, True)
        if not user: raise ValueError("用户不存在")
        return {"provider": "115", "items": self.folders_115(user.get("cookie", ""), query.get("cid", ["0"])[0])}

    def mini_account_validate(self,tg_id,value):
        cookie=str(value.get("cookie") or "").strip()
        if not cookie: raise ValueError("请先填写 115 CK")
        profile=P115.profile(cookie)
        user=self.store.save_user(tg_id,{"cookie":cookie,"uid":profile["uid"],"account_ready":False})
        return {"user":user,"account":profile,"message":"CK 已验证，请继续选择接收文件夹并完成保存"}

    def mini_account_save(self,tg_id,value):
        current=self.store.user(tg_id,True)
        if not current: raise ValueError("用户不存在")
        cookie=str(value.get("cookie") or current.get("cookie") or "").strip()
        if not cookie: raise ValueError("请先填写并验证 115 CK")
        target_cid=str(value.get("target_cid") or "").strip()
        target_name=str(value.get("target_name") or "").strip()
        if not target_cid or target_cid == "0" or not target_name or target_name == "根目录":
            raise ValueError("请先从 115 网盘选择一个非根目录作为接收文件夹")
        profile=P115.profile(cookie) if value.get("cookie") else {"uid":current.get("uid"),"name":""}
        self.folders_115(cookie,target_cid)
        user=self.store.save_user(tg_id,{"cookie":cookie,"uid":profile["uid"],"target_cid":target_cid,"target_name":target_name,"account_ready":True})
        return {"user":user,"account":profile,"message":"115 CK 与接收文件夹已保存"}

    def mini_tmdb_search(self,query):
        api_key=str(self.store.get("tmdb_api_key","") or "")
        if not self.store.get("tmdb_enabled",False) or not api_key: raise RuntimeError("管理员尚未启用 TMDB 搜索")
        return TMDB.search(api_key,str(query or ""))

    def mini_tmdb_subscription(self,tg_id,value):
        user=self.store.user(tg_id)
        if not user: raise ValueError("用户不存在")
        action=str(value.get("action") or "add"); media_type=str(value.get("media_type") or ""); tmdb_id=int(value.get("id") or 0)
        if action not in {"add","remove"} or media_type not in {"movie","tv"} or tmdb_id<=0: raise ValueError("TMDB 订阅参数无效")
        subscriptions=list(user.get("tmdb_subscriptions") or [])
        subscriptions=[item for item in subscriptions if not (int(item.get("id") or 0)==tmdb_id and item.get("media_type")==media_type)]
        update={"tmdb_subscriptions":subscriptions}
        if action == "add":
            if len(subscriptions)>=100: raise ValueError("每个用户最多订阅 100 个 TMDB 条目")
            api_key=str(self.store.get("tmdb_api_key","") or "")
            if not self.store.get("tmdb_enabled",False) or not api_key: raise RuntimeError("管理员尚未启用 TMDB 搜索")
            detail=TMDB.detail(api_key,media_type,tmdb_id)
            title=str(detail.get("title") or detail.get("name") or "")
            date=str(detail.get("release_date") or detail.get("first_air_date") or "")
            poster=str(detail.get("poster_path") or "")
            subscriptions.append({"id":tmdb_id,"media_type":media_type,"title":title,"year":date[:4] if re.match(r"\d{4}",date) else "",
                                  "poster":TMDB.IMAGE+poster if poster.startswith("/") else ""})
            update={"tmdb_subscriptions":subscriptions,"mode":"tmdb","enabled":True}
        elif not subscriptions and user.get("mode")=="tmdb": update.update({"mode":"off","enabled":False})
        saved=self.store.save_user(tg_id,update)
        self.reconcile_user_deliveries(saved)
        self.store.event("TMDB订阅",f"{saved.get('name') or saved.get('username') or tg_id}：{'添加' if action=='add' else '取消'} {tmdb_id}",str(tg_id))
        return {"user":saved}

    def scan(self, force=True, process_tasks=True):
        if not self.scan_lock.acquire(False): return
        try:
            cfg = self.store.config(True)
            sources=self.store.sources(True)
            if not cfg["source_configured"]: raise RuntimeError("尚未配置源115 Cookie或监听目录独立 CK")
            stamp=now(); scanned=0; errors=[]
            for source in sources:
                if not source["enabled"] or (not force and int(source["next_scan"] or 0)>stamp): continue
                error=self.scan_source(cfg,source,stamp,force); scanned+=1
                if error: errors.append(f"{source['name']}：{error}")
            if scanned:
                self.store.set("last_scan", stamp)
                sources=self.store.sources()
                self.store.set("baseline_ready",all(bool(x["baseline_ready"]) for x in sources))
                self.store.set("baseline_count",sum(int(x["historical"]) for x in sources))
            self.store.set("last_error", "；".join(errors)[:2000])
            if process_tasks:
                self.deliver(cfg)
                self.cleanup(cfg)
        except Exception as exc: self.store.set("last_error", f"扫描：{exc}")
        finally: self.scan_lock.release()

    def cd2_entries(self, cfg, source, force=False):
        mode = "api" if source.get("monitor_type") == "cd2_api" else "mount"
        self.cd2_gate.wait(cfg.get("cd2_api_interval_seconds", 1))
        root_items = CD2.list_dir(cfg, source["cd2_path"], mode)
        entries = [item["name"] for item in root_items]
        signatures = []
        if mode == "mount":
            root = Path(source["cd2_path"])
            for current, dirs, files in os.walk(root):
                relative = str(Path(current).relative_to(root))
                signatures.extend(f"d:{relative}/{name}" for name in dirs)
                for name in files:
                    path = Path(current) / name
                    try:
                        stat = path.stat(); signatures.append(f"f:{relative}/{name}:{stat.st_size}:{stat.st_mtime_ns}")
                    except OSError:
                        signatures.append(f"f:{relative}/{name}")
        else:
            queue = [item["path"] for item in root_items if item["is_dir"]]
            signatures.extend(("d:" if item["is_dir"] else "f:") + item["path"] for item in root_items)
            visited = 0
            while queue and visited < 1000:
                path = queue.pop(0); visited += 1
                self.cd2_gate.wait(cfg.get("cd2_api_interval_seconds", 1))
                children = CD2.list_dir(cfg, path, mode)
                for item in children:
                    signatures.append(("d:" if item["is_dir"] else "f:") + item["path"])
                    if item["is_dir"]: queue.append(item["path"])
            if queue: raise RuntimeError("CD2 目录层级项目超过 1000，已停止递归以避免过量请求")
        snapshot = hashlib.sha256(dumps(sorted(signatures)).encode()).hexdigest()
        previous = self.cd2_snapshots.get(source["id"])
        self.cd2_snapshots[source["id"]] = snapshot
        return entries, force or previous is None or snapshot != previous

    @staticmethod
    def cd2_api_path(cfg, source):
        if source.get("monitor_type") == "cd2_api":
            path = str(source.get("cd2_path") or cfg.get("cd2_api_root") or "/").strip()
            return "/" + path.lstrip("/")
        mount_root = str(cfg.get("cd2_root") or "/cd2/miaochuang").replace("\\", "/").rstrip("/")
        selected = str(source.get("cd2_path") or mount_root).replace("\\", "/").rstrip("/")
        if selected != mount_root and not selected.startswith(mount_root + "/"):
            raise ValueError("监听目录不在 CD2 挂载根目录内，无法映射到 API 路径")
        relative = selected[len(mount_root):].strip("/")
        api_root = "/" + str(cfg.get("cd2_api_root") or "/").strip("/")
        return api_root.rstrip("/") + (("/" + relative) if relative else "") or "/"

    def cd2_metadata_entries(self, cfg, source, source_cookie, force=False):
        """Use CD2's cached IDs/hashes instead of querying the source 115 directory API."""
        from p115client import P115Client
        api_path = self.cd2_api_path(cfg, source)
        queue = [api_path]
        videos = []
        signatures = []
        visited_dirs = 0
        source_client = P115Client(P115.cookie_mapping(source_cookie))
        while queue:
            path = queue.pop(0)
            visited_dirs += 1
            if visited_dirs > 1000:
                raise RuntimeError("CD2 目录数量超过 1000，已停止递归以避免过量请求")
            self.cd2_gate.wait(cfg.get("cd2_api_interval_seconds", 1))
            for item in CD2.list_api(cfg, path):
                signature = ("d:" if item["is_dir"] else "f:") + item["path"]
                signature += f":{item.get('node_id','')}:{item.get('size',0)}:{item.get('sha1','')}"
                signatures.append(signature)
                if item["is_dir"]:
                    queue.append(item["path"])
                    continue
                if Path(item["name"]).suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                node_id = str(item.get("node_id") or "")
                sha1 = str(item.get("sha1") or "").upper()
                if not node_id or not sha1:
                    raise RuntimeError(f"CD2 未返回文件 ID 或 SHA1：{item['path']}")
                videos.append({"node_id": node_id, "name": item["name"], "is_dir": False,
                               "pickcode": str(source_client.to_pickcode(node_id)),
                               "sha1": sha1, "size": int(item.get("size") or 0)})
        snapshot = hashlib.sha256(dumps(sorted(signatures)).encode()).hexdigest()
        snapshot_key = source["id"] + ":metadata"
        previous = self.cd2_snapshots.get(snapshot_key)
        self.cd2_snapshots[snapshot_key] = snapshot
        return videos, force or previous is None or snapshot != previous

    def expand_source_videos(self, cfg, entries, source_cookie):
        videos = []
        queue = list(entries)
        visited_dirs = 0
        while queue:
            entry = queue.pop(0)
            if not entry["is_dir"]:
                if Path(entry["name"]).suffix.lower() in VIDEO_EXTENSIONS: videos.append(entry)
                continue
            visited_dirs += 1
            if visited_dirs > 1000: raise RuntimeError("115 子目录数量超过 1000，已停止递归以避免 API 风控")
            self.p115_gate.wait(cfg.get("p115_api_interval_seconds", 1))
            queue.extend(P115.list_dir(source_cookie, entry["node_id"]))
        return videos

    def scan_source(self,cfg,source,stamp,force=False):
        try:
            source_cookie=source.get("cookie") if source.get("cookie_mode")=="independent" else cfg["source_cookie"]
            if not source_cookie: raise RuntimeError("该监听目录尚未配置可用的 115 CK")
            monitor_type = source.get("monitor_type") or "api_poll"
            local_names = None
            entries = None
            cd2_metadata_used = False
            if monitor_type.startswith("cd2_"):
                if cfg.get("cd2_metadata_enabled") and cfg.get("cd2_api_token"):
                    try:
                        entries, changed = self.cd2_metadata_entries(cfg, source, source_cookie, force)
                        cd2_metadata_used = True
                    except Exception as exc:
                        self.store.event("CD2旁路回退",f"{source['name']}：{exc}，改用 115 API",source["id"])
                if entries is None:
                    local, changed = self.cd2_entries(cfg, source, force)
                    local_names = set(local)
                if not changed:
                    stable_cutoff = stamp - int(source["stable_seconds"])
                    with self.store.lock:
                        due = self.store.db.execute("SELECT 1 FROM resources WHERE source_id=? AND status='stabilizing' AND first_seen<=? LIMIT 1", (source["id"], stable_cutoff)).fetchone()
                    if not due:
                        interval = 2 if monitor_type == "cd2_realtime" else int(source["poll_seconds"])
                        with self.store.lock,self.store.db:
                            self.store.db.execute("UPDATE sources SET last_scan=?,next_scan=?,error='',updated=? WHERE id=?",(stamp,stamp+interval,stamp,source["id"]))
                        return ""
            if entries is None:
                self.p115_gate.wait(cfg.get("p115_api_interval_seconds", 1))
                entries=P115.list_dir(source_cookie,source["cid"])
                if local_names is not None: entries = [entry for entry in entries if entry["name"] in local_names]
                entries=self.expand_source_videos(cfg,entries,source_cookie)
            discovered=0
            with self.store.lock,self.store.db:
                baseline=bool(source["baseline_ready"])
                for entry in entries:
                    identity=entry["node_id"]+":"+entry["name"]+":"+str(entry["size"])
                    # Preserve IDs created by versions before multi-source support,
                    # otherwise an upgrade would enqueue every existing file again.
                    rid=hashlib.sha256((identity if source["id"]=="legacy" else source["id"]+":"+identity).encode()).hexdigest()
                    row=self.store.db.execute("""SELECT * FROM resources WHERE source_id=? AND node_id=?
                      ORDER BY first_seen DESC LIMIT 1""",(source["id"],entry["node_id"])).fetchone()
                    if not row: row=self.store.db.execute("SELECT * FROM resources WHERE id=?",(rid,)).fetchone()
                    if row:
                        row_id=row["id"]
                        changed=(row["name"]!=entry["name"] or int(row["size"] or 0)!=int(entry["size"] or 0)
                                 or str(row["sha1"] or "").upper()!=str(entry["sha1"] or "").upper())
                        if row["status"]=="stabilizing" and changed:
                            cat=category_for(entry["name"],cfg["default_category"])
                            self.store.db.execute("""UPDATE resources SET name=?,pickcode=?,sha1=?,size=?,category=?,first_seen=? WHERE id=?""",
                              (entry["name"],entry["pickcode"],entry["sha1"],entry["size"],cat,stamp,row_id))
                        elif row["status"]=="stabilizing" and stamp-int(row["first_seen"])>=int(source["stable_seconds"]):
                            self.store.db.execute("UPDATE resources SET status='waiting' WHERE id=?",(row_id,)); self.queue_resource(row_id,entry["name"],row["category"],stamp)
                        continue
                    discovered+=1; cat=category_for(entry["name"],cfg["default_category"])
                    status="historical" if not baseline else ("stabilizing" if int(source["stable_seconds"])>0 else "waiting")
                    age_delete=int(source.get("age_delete_minutes",-1))
                    cleanup="scheduled" if age_delete>=0 else ("retained" if int(source["retention_minutes"])<0 else "waiting")
                    delete_due=stamp+age_delete*60 if age_delete>=0 else 0
                    self.store.db.execute("""INSERT INTO resources(id,node_id,name,pickcode,sha1,size,is_dir,category,first_seen,status,source_id,cleanup,delete_due)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(rid,entry["node_id"],entry["name"],entry["pickcode"],entry["sha1"],entry["size"],int(entry["is_dir"]),cat,stamp,status,source["id"],cleanup,delete_due))
                    if status=="waiting": self.queue_resource(rid,entry["name"],cat,stamp)
                interval=2 if monitor_type=="cd2_realtime" else int(source["poll_seconds"])
                self.store.db.execute("""UPDATE sources SET baseline_ready=1,historical=historical+?,added=added+?,last_scan=?,next_scan=?,error='',updated=? WHERE id=?""",
                  (discovered if not baseline else 0,discovered if baseline else 0,stamp,stamp+interval,stamp,source["id"]))
            scan_mode = "（CD2 元数据旁路）" if cd2_metadata_used else ""
            self.store.event("来源",f"扫描 {source['name']}：发现 {discovered} 项{scan_mode}",source["id"])
        except Exception as exc:
            with self.store.lock,self.store.db: self.store.db.execute("UPDATE sources SET error=?,last_scan=?,next_scan=?,updated=? WHERE id=?",(str(exc)[:500],stamp,stamp+int(source["poll_seconds"]),stamp,source["id"]))
            self.store.event("来源失败",f"扫描 {source['name']}：{exc}",source["id"])
            return str(exc)
        return ""

    def queue_resource(self,rid,name,category,stamp):
        users=self.store.db.execute("SELECT * FROM users WHERE enabled=1 AND account_ready=1 AND target_cid NOT IN ('','0') AND status='active' AND cookie<>'' AND membership_expires>?",(stamp,)).fetchall()
        for user in users:
            if self.matches(dict(user),name,category):
                did=hashlib.sha256(f"{rid}:{user['tg_id']}".encode()).hexdigest()
                self.store.db.execute("INSERT OR IGNORE INTO deliveries(id,resource_id,tg_id,created,updated) VALUES(?,?,?,?,?)",(did,rid,user["tg_id"],stamp,stamp))

    @staticmethod
    def matches(user, name, category):
        mode = user.get("mode") or "all"; lower = name.lower()
        if mode == "off" or not bool(user.get("enabled",True)) or user.get("status","active")!="active": return False
        excluded = [x.strip().lower() for x in re.split(r"[,，;；\n]+",str(user.get("exclude_terms") or "")) if x.strip()]
        if any(x in lower for x in excluded): return False
        if mode == "all": return True
        if mode == "tmdb":
            parsed=TMDB.parse_name(name,category); tmdb_id=parsed["tmdb_id"]
            if not tmdb_id: return False
            raw_subscriptions=user.get("tmdb_subscriptions") or []
            subscriptions=raw_subscriptions if isinstance(raw_subscriptions,list) else json.loads(raw_subscriptions)
            return any(int(item.get("id") or 0)==tmdb_id and item.get("media_type")==parsed["media_type"] for item in subscriptions)
        raw_categories=user.get("categories") or []
        categories=raw_categories if isinstance(raw_categories,list) else json.loads(raw_categories)
        cat_ok = category in categories
        terms = [x.strip().lower() for x in re.split(r"[,，;；\n]+",str(user.get("include_terms") or "")) if x.strip()]
        term_ok = bool(terms) and any(x in lower for x in terms)
        if mode == "category": return cat_ok
        if mode == "subscription": return term_ok
        if mode == "both": return cat_ok and term_ok
        if mode == "either": return cat_ok or term_ok
        return False

    def deliver(self, cfg):
        if not self.delivery_lock.acquire(False): return
        try: self._deliver(cfg)
        finally: self.delivery_lock.release()

    def _deliver(self, cfg):
        max_attempts=max(1,min(50,int(cfg.get("max_attempts") or 5))); stamp=now()
        with self.store.lock:
            rows = self.store.db.execute("""SELECT d.id,d.resource_id,d.tg_id,d.attempts,r.*,u.cookie,u.target_cid,u.target_name,u.hierarchy,u.name user_name,u.username,u.note
              FROM deliveries d JOIN resources r ON r.id=d.resource_id JOIN users u ON u.tg_id=d.tg_id
              WHERE d.status IN ('waiting','retry') AND d.attempts<? AND d.next_attempt<=? AND u.status='active' AND u.enabled=1 AND u.account_ready=1 AND u.target_cid NOT IN ('','0') AND u.membership_expires>?
              ORDER BY d.created LIMIT 20""",(max_attempts,stamp,stamp)).fetchall()
        workers=max(1,min(8,int(cfg.get("concurrency") or 1)))
        if workers==1:
            for row in rows: self.deliver_one(cfg,dict(row))
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers,thread_name_prefix="delivery") as pool:
                list(pool.map(lambda row:self.deliver_one(cfg,dict(row)),rows))

    def deliver_one(self,cfg,item):
        try:
            if not self.store.get("enabled",False): return
            self.transfer_gate.wait(cfg.get("transfer_interval_seconds", 3))
            cookie = self.store.crypt.decrypt(item["cookie"].encode()).decode()
            with self.store.lock, self.store.db:
                started=self.store.db.execute("UPDATE deliveries SET status='running',attempts=attempts+1,updated=? WHERE id=? AND status IN ('waiting','retry')", (now(), item["id"])).rowcount
            if not started: return
            destination_cid,delivery_path=self.prepare_destination(cfg,item,cookie)
            item["target_cid"]=destination_cid; item["delivery_path"]=delivery_path
            transfer_source=self.transfer_delivery(cfg,item,cookie)
            done=now()
            with self.store.lock, self.store.db:
                current=self.store.db.execute("SELECT status FROM deliveries WHERE id=?",(item["id"],)).fetchone()
                if not current or current["status"] != "running": return
                self.store.db.execute("UPDATE deliveries SET status='delivered',error='',next_attempt=0,source=?,destination=?,updated=? WHERE id=?", (transfer_source,delivery_path,done,item["id"]))
                source=self.store.db.execute("SELECT retention_minutes,age_delete_minutes FROM sources WHERE id=?",(item["source_id"],)).fetchone()
                retention=int(source[0]) if source else -1; age_delete=int(source[1]) if source else -1
                due=done+retention*60 if retention>=0 else 0
                self.store.db.execute("""UPDATE resources SET first_success=CASE WHEN first_success=0 THEN ? ELSE first_success END,
                  delete_due=CASE WHEN delete_due=0 THEN ? ELSE delete_due END,
                  cleanup=CASE WHEN ? >= 0 OR ? >= 0 THEN 'scheduled' ELSE 'retained' END WHERE id=?""",
                  (done,due,age_delete,retention,item["resource_id"]))
            target_label=self.user_label(item)
            self.store.event("派送",f"{target_label}：{item['name']}；来源 {transfer_source}；落盘 {delivery_path}",item["id"])
            if cfg.get("distributed_transfer_enabled"):
                woken=self.store.wake_relay_retries(item["resource_id"],item["id"])
                if woken:
                    self.store.event("接力唤醒",f"{target_label} 已接收成功，立即唤醒 {woken} 个错误任务：{item['name']}",item["id"])
            try: self.notify_delivery_success(cfg,item)
            except Exception as notify_exc:
                self.store.event("通知失败",f"{target_label}：{item['name']}；{redact_sensitive_text(notify_exc)}",item["id"])
        except Exception as exc:
            failed=now(); attempts=int(item.get("attempts") or 0)+1; max_attempts=max(1,min(50,int(cfg.get("max_attempts") or 5)))
            delay=max(1,int(cfg.get("retry_minutes") or 15))*60*min(16,2**max(0,attempts-1))
            status="failed" if attempts>=max_attempts else "retry"; error=self.transfer_error(exc)
            with self.store.lock, self.store.db: self.store.db.execute("UPDATE deliveries SET status=?,error=?,next_attempt=?,updated=? WHERE id=? AND status='running'", (status,error,failed+delay if status=="retry" else 0,failed,item["id"]))
            target_label=self.user_label(item)
            wait_text=f"，{delay//60} 分钟后重试" if status=="retry" else "，已达到最大尝试次数"
            self.store.event("派送失败",f"{target_label}：{item['name']}；{error}{wait_text}",item["id"])

    def cleanup(self,cfg):
        if not self.cleanup_lock.acquire(False): return
        try: self._cleanup(cfg)
        finally: self.cleanup_lock.release()

    def _cleanup(self,cfg):
        stamp=now()
        with self.store.lock: rows=self.store.db.execute("""SELECT r.* FROM resources r JOIN sources s ON s.id=r.source_id
          WHERE r.cleanup='scheduled' AND r.delete_due>0 AND r.delete_due<=? AND (s.retention_minutes>=0 OR s.age_delete_minutes>=0)
          AND NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.resource_id=r.id AND d.status NOT IN ('delivered','cancelled')) LIMIT 20""",(stamp,)).fetchall()
        for row in rows:
            item=dict(row)
            try:
                self.p115_gate.wait(cfg.get("p115_api_interval_seconds", 1))
                source_cookie=self.store.source_cookie(item["source_id"],cfg["source_cookie"])
                if not source_cookie: raise RuntimeError("资源所属监听目录没有可用的 115 CK")
                P115.delete(source_cookie,item["node_id"])
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

    def loop_error(self,kind,exc):
        message=redact_sensitive_text(exc)[:500]; stamp=now(); previous=self.loop_errors.get(kind)
        self.store.set("last_error",f"{kind}：{message}")
        if not previous or previous[0]!=message or stamp-previous[1]>=300:
            self.loop_errors[kind]=(message,stamp)
            self.store.event("后台自恢复",f"{kind}发生异常，循环将自动继续：{message}")

    def scheduler(self):
        while not self.stop.is_set():
            try:
                self.store.set("monitor_heartbeat",now())
                cfg = self.store.config(False)
                if cfg["enabled"]: self.scan(False,False)
                self.store.set("monitor_heartbeat",now())
            except Exception as exc: self.loop_error("监控循环",exc)
            self.stop.wait(2)

    def work_scheduler(self):
        while not self.stop.is_set():
            try:
                cfg=self.store.config(True)
                if cfg["enabled"]:
                    self.deliver(cfg)
                    self.cleanup(cfg)
                self.maintenance(cfg)
            except Exception as exc: self.loop_error("任务循环",exc)
            self.stop.wait(2)

    def watchdog(self):
        while not self.stop.wait(30):
            try:
                if not self.store.get("enabled",False): continue
                heartbeat=int(self.store.get("monitor_heartbeat",0) or 0)
                if heartbeat and now()-heartbeat>300:
                    self.store.event("监控看门狗",f"监控心跳已停止 {now()-heartbeat} 秒，服务将自动重启恢复")
                    os._exit(75)
            except Exception as exc: self.loop_error("监控看门狗",exc)


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
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "): raise PermissionError("请先登录管理中心")
        try:
            username, password = base64.b64decode(header[6:]).decode("utf-8").split(":", 1)
        except (ValueError, UnicodeDecodeError):
            raise PermissionError("登录信息无效")
        if not (hmac.compare_digest(username, self.server.admin_username) and hmac.compare_digest(password, self.server.admin_password)):
            raise PermissionError("用户名或密码错误")
    def mini(self): return self.app.mini_user(self.headers.get("Authorization", "").removeprefix("tma "))
    def do_OPTIONS(self): self.send_response(204); self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Access-Control-Allow-Headers", "content-type,authorization"); self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS"); self.end_headers()
    def do_GET(self): self.route("GET")
    def do_POST(self): self.route("POST")
    def do_PUT(self): self.route("PUT")
    def do_DELETE(self): self.route("DELETE")
    def route(self, method):
        try:
            parsed = urllib.parse.urlsplit(self.path); path = parsed.path; query = urllib.parse.parse_qs(parsed.query)
            if path == "/healthz": return self.send_json(200, {"ok": True, "name": APP_NAME})
            if path == "/" or path == "/index.html": return self.send_file("index.html", "text/html; charset=utf-8")
            if path == "/admin" or path == "/admin.html": return self.send_file("admin.html", "text/html; charset=utf-8")
            if path == "/app.js": return self.send_file("app.js", "text/javascript; charset=utf-8")
            if path == "/admin.js": return self.send_file("admin.js", "text/javascript; charset=utf-8")
            if path == "/admin.css": return self.send_file("admin.css", "text/css; charset=utf-8")
            if path == "/style.css": return self.send_file("style.css", "text/css; charset=utf-8")
            if path == "/qr.css": return self.send_file("qr.css", "text/css; charset=utf-8")
            if path == "/brand-icon.mp4": return self.send_file("brand-icon.mp4", "video/mp4")
            if path == "/api/mini/qr/image" and method == "GET": return self.send_bytes(200,self.app.qr_image(query.get("session",[""])[0]),"image/png")
            if path == "/api/admin/status" and method == "GET": self.admin(); return self.send_json(200, self.app.store.status())
            if path == "/api/admin/overview" and method == "GET": self.admin(); return self.send_json(200, self.app.store.overview())
            if path == "/api/admin/records" and method == "GET":
                self.admin(); return self.send_json(200,self.app.store.records_page(str(query.get("type",[""])[0]),query.get("page",[1])[0],query.get("page_size",[100])[0]))
            if path == "/api/admin/config" and method == "PUT": self.admin(); self.app.store.save_config(self.body()); return self.send_json(200, {**self.app.store.status(), "message": "配置已保存"})
            if path == "/api/admin/service" and method == "POST":
                self.admin(); action=str(self.body().get("action") or ""); message=self.app.store.service_action(action)
                if action=="restart":
                    timer=threading.Timer(0.8,lambda:os._exit(0)); timer.daemon=True; timer.start()
                    return self.send_json(202,{"ok":True,"message":message})
                return self.send_json(200,{**self.app.store.status(),"message":message})
            if path == "/api/admin/browse" and method == "GET": self.admin(); return self.send_json(200, self.app.admin_browse(query))
            if path == "/api/admin/source-browse" and method == "POST": self.admin(); return self.send_json(200,self.app.source_browse(self.body()))
            if path == "/api/admin/test-cd2" and method == "POST": self.admin(); result=CD2.test(self.app.store.config(True)); return self.send_json(200,{"message":f"CD2 连接正常，读取到 {result['count']} 个项目",**result})
            if path == "/api/admin/users" and method == "PUT":
                self.admin(); value=self.body(); result=self.app.admin_user_save(value); user=result["user"]; self.app.store.event("用户管理",f"更新用户：{user.get('name') or user.get('tg_id')}",str(user["tg_id"])); return self.send_json(200,{"message":"用户设置已保存",**result})
            if path == "/api/admin/users" and method == "DELETE":
                self.admin(); value=self.body(); self.app.store.delete_user(int(value.get("tg_id") or 0)); return self.send_json(200,{"message":"用户已删除"})
            if path == "/api/admin/deliveries" and method == "POST": self.admin(); self.app.store.delivery_action(self.body()); return self.send_json(200,{"message":"派送任务已更新"})
            if path == "/api/admin/resources" and method == "POST": self.admin(); self.app.store.resource_action(self.body()); return self.send_json(200,{"message":"秒传记录已删除"})
            if path == "/api/admin/sources" and method == "PUT": self.admin(); sid=self.app.store.save_source(self.body()); return self.send_json(200,{"message":"监听目录已保存","id":sid})
            if path == "/api/admin/sources" and method == "POST": self.admin(); self.app.store.source_action(self.body()); return self.send_json(200,{"message":"监听目录操作完成"})
            if path == "/api/admin/bindings" and method == "POST":
                self.admin(); value=self.body()
                if value.get("action")=="revoke": self.app.store.binding_action(value); return self.send_json(200,{"message":"绑定码已撤销"})
                result=self.app.store.create_binding(str(value.get("plan") or "month")); return self.send_json(200,{"message":"会员绑定码已创建",**result})
            if path == "/api/admin/test-bot" and method == "POST": self.admin(); info=self.app.telegram.call("getMe"); return self.send_json(200, {**self.app.store.status(), "message": f"Bot 连接正常：@{info.get('username','')}"})
            if path == "/api/admin/test-tmdb" and method == "POST":
                self.admin(); key=self.app.store.get("tmdb_api_key","")
                if not key: raise ValueError("请先保存 TMDB API Key")
                info=TMDB.detail(key,"movie",155)
                return self.send_json(200,{"message":f"TMDB 连接正常：{info.get('title') or info.get('original_title') or info.get('id')}"})
            if path == "/api/admin/scan" and method == "POST": self.admin(); threading.Thread(target=self.app.scan,daemon=True).start(); return self.send_json(202, {**self.app.store.status(), "message": "扫描已启动"})
            if path == "/api/mini/me" and method == "GET": uid=self.mini(); return self.send_json(200, {"user": self.app.store.user(uid), "categories": ["电影","剧集","动漫","纪录片","其他"], "qr_apps": QR_LOGIN_APPS})
            if path == "/api/mini/overview" and method == "GET": uid=self.mini(); return self.send_json(200, self.app.store.mini_overview(uid))
            if path == "/api/mini/browse" and method == "GET": uid=self.mini(); return self.send_json(200, self.app.mini_browse(uid,query))
            if path == "/api/mini/tmdb/search" and method == "GET": self.mini(); return self.send_json(200,{"items":self.app.mini_tmdb_search(query.get("q",[""])[0])})
            if path == "/api/mini/tmdb/subscriptions" and method == "POST": uid=self.mini(); return self.send_json(200,self.app.mini_tmdb_subscription(uid,self.body()))
            if path == "/api/mini/preferences" and method == "PUT": uid=self.mini(); return self.send_json(200,self.app.mini_preferences(uid,self.body()))
            if path == "/api/mini/account/validate" and method == "POST": uid=self.mini(); return self.send_json(200,self.app.mini_account_validate(uid,self.body()))
            if path == "/api/mini/account" and method == "PUT":
                uid=self.mini(); return self.send_json(200,self.app.mini_account_save(uid,self.body()))
            if path == "/api/mini/qr/start" and method == "POST": uid=self.mini(); return self.send_json(200,self.app.qr_start(uid,self.body()))
            if path == "/api/mini/qr/status" and method == "POST": uid=self.mini(); return self.send_json(200,self.app.qr_poll(uid,self.body()))
            return self.send_json(404, {"error": "接口不存在"})
        except PermissionError as exc: self.send_json(401, {"error": str(exc)})
        except (ValueError, RuntimeError, urllib.error.URLError) as exc: self.send_json(400, {"error": str(exc)})
        except Exception as exc: self.send_json(500, {"error": f"服务错误：{exc}"})
    def send_file(self, name, content_type):
        path = Path(self.server.static_dir) / name
        data = path.read_bytes(); self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Cache-Control", "no-cache"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def send_bytes(self, status, data, content_type):
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--host",default="127.0.0.1"); parser.add_argument("--port",type=int,default=8790); parser.add_argument("--data",default="/var/lib/cockroach-runner"); parser.add_argument("--static",default=str(Path(__file__).with_name("static"))); args=parser.parse_args()
    if not activation_valid(os.environ.get("COCKROACH_ACTIVATION_CODE")):
        raise SystemExit("蟑影递送激活失败：请在环境变量 COCKROACH_ACTIVATION_CODE 中填写有效激活码")
    admin_username=os.environ["COCKROACH_ADMIN_USERNAME"]; admin_password=os.environ["COCKROACH_ADMIN_PASSWORD"]; encryption_key=os.environ["COCKROACH_ENCRYPTION_KEY"]
    app=Application(Store(args.data,encryption_key)); server=ThreadingHTTPServer((args.host,args.port),Handler); server.app=app; server.admin_username=admin_username; server.admin_password=admin_password; server.static_dir=args.static
    threading.Thread(target=app.telegram.loop,daemon=True,name="telegram").start()
    threading.Thread(target=app.scheduler,daemon=True,name="monitor").start()
    threading.Thread(target=app.work_scheduler,daemon=True,name="worker").start()
    threading.Thread(target=app.watchdog,daemon=True,name="watchdog").start()
    try: server.serve_forever()
    finally: app.stop.set(); server.server_close()

if __name__ == "__main__": main()
