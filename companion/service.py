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

APP_NAME = "蟑影递送"
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
                ("sources","monitor_type","TEXT NOT NULL DEFAULT 'api_poll'"),
                ("sources","cd2_path","TEXT NOT NULL DEFAULT ''"),
                ("sources","age_delete_minutes","INTEGER NOT NULL DEFAULT -1"),
                ("sources","cookie","TEXT NOT NULL DEFAULT ''"),
                ("sources","cookie_mode","TEXT NOT NULL DEFAULT 'global'"),
                ("resources","hidden","INTEGER NOT NULL DEFAULT 0"),
                ("users","membership_expires","INTEGER NOT NULL DEFAULT 0"),
                ("users","membership_plan","TEXT NOT NULL DEFAULT ''"),
                ("bindings","plan","TEXT NOT NULL DEFAULT 'month'"),
                ("bindings","grant_days","INTEGER NOT NULL DEFAULT 30"),
            ):
                columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                if column not in columns: self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.ensure_legacy_source()
        with self.lock, self.db:
            stamp = now()
            self.db.execute("""UPDATE deliveries SET status='retry',next_attempt=0,updated=?,
              error=CASE WHEN error='' THEN '服务重启后自动恢复未完成任务' ELSE error END WHERE status='running'""", (stamp,))
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
            "p115_api_interval_seconds": self.get("p115_api_interval_seconds", 1),
            "cd2_api_interval_seconds": self.get("cd2_api_interval_seconds", 1),
            "transfer_interval_seconds": self.get("transfer_interval_seconds", 3),
            "transfer_timeout_seconds": self.get("transfer_timeout_seconds", 300),
            "distributed_transfer_enabled": self.get("distributed_transfer_enabled", False),
        }
        if include_secrets:
            result["bot_token"] = self.get("bot_token", "")
            result["source_cookie"] = self.get("source_cookie", "")
            result["cd2_api_token"] = self.get("cd2_api_token", "")
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
        allowed.update({"transfer_timeout_seconds","distributed_transfer_enabled"})
        for key in allowed:
            if key in value: self.set(key, value[key])
        if value.get("bot_token"): self.set("bot_token", str(value["bot_token"]).strip(), True)
        if value.get("source_cookie"): self.set("source_cookie", str(value["source_cookie"]).strip(), True)
        if value.get("cd2_api_token"): self.set("cd2_api_token", str(value["cd2_api_token"]).strip(), True)

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
        value["enabled"] = bool(value["enabled"]); value["hierarchy"] = bool(value["hierarchy"])
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

    def relay_candidates(self, resource_id, target_tg_id):
        with self.lock: rows=self.db.execute("""SELECT u.tg_id,u.name,u.username,u.note,u.cookie,d.updated
          FROM deliveries d JOIN users u ON u.tg_id=d.tg_id
          WHERE d.resource_id=? AND d.status='delivered' AND d.tg_id<>? AND u.enabled=1 AND u.status='active' AND u.cookie<>''
          ORDER BY d.updated DESC""",(resource_id,target_tg_id)).fetchall()
        result=[]
        for row in rows:
            item=dict(row)
            try: item["cookie"]=self.crypt.decrypt(item["cookie"].encode()).decode()
            except InvalidToken: continue
            result.append(item)
        return result

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
    def find_file(cookie, entry):
        from p115client import P115Client
        client=P115Client(P115.cookie_mapping(cookie))
        title=re.split(r"\.(?:19|20)\d{2}\b|\.S\d{1,2}E\d{1,3}\b",entry["name"],maxsplit=1,flags=re.I)[0]
        queries=list(dict.fromkeys(filter(None,(entry["name"],title.strip(" ._-"),Path(entry["name"]).stem))))
        for query in queries:
            result=client.fs_search({"search_value":query,"limit":115,"offset":0,"show_dir":0})
            data=result.get("data") if isinstance(result,dict) else None
            items=(data.get("data") or data.get("list") or []) if isinstance(data,dict) else data if isinstance(data,list) else []
            for item in items:
                sha1=str(item.get("sha") or item.get("sha1") or "").upper()
                size=int(item.get("s") or item.get("size") or 0)
                node_id=str(item.get("fid") or item.get("file_id") or "")
                pickcode=str(item.get("pc") or item.get("pick_code") or "")
                if node_id and pickcode and sha1==str(entry["sha1"]).upper() and size==int(entry["size"]):
                    return {"node_id":node_id,"name":str(item.get("n") or item.get("fn") or item.get("file_name") or entry["name"]),
                            "is_dir":False,"pickcode":pickcode,"sha1":sha1,"size":size}
        return None

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
                    if name: result.append({"name": name, "path": full_path, "is_dir": is_dir})
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
    if re.search(r"\b(s\d{1,2}e\d{1,3}|ep?\d{1,3}|第.{1,4}集)\b", text, re.I): return "剧集"
    if any(word in text for word in ("anime", "动漫", "动画", "ova")): return "动漫"
    if any(word in text for word in ("documentary", "纪录片", "docu")): return "纪录片"
    return default or "电影"


class Application:
    def __init__(self, store):
        self.store, self.stop = store, threading.Event()
        self.telegram = Telegram(self)
        self.scan_lock = threading.Lock()
        self.cd2_snapshots = {}
        self.qr_sessions = {}
        self.qr_lock = threading.Lock()
        self.p115_gate = RateGate()
        self.cd2_gate = RateGate()
        self.transfer_gate = RateGate()

    @staticmethod
    def user_label(value):
        return str(value.get("name") or value.get("user_name") or ("@"+value["username"] if value.get("username") else "") or value.get("note") or f"用户 {value.get('tg_id','未知')}")

    @staticmethod
    def transfer_error(exc):
        if isinstance(exc,TimeoutError): return str(exc)
        payload=exc.args[0] if getattr(exc,"args",None) and isinstance(exc.args[0],dict) else None
        if payload and {"pid","filename","filesha1","user_id"}.issubset(payload):
            return f"115 秒传初始化失败（目标账号 UID {payload.get('user_id')}，可能触发风控、秒传验证未通过或接口暂时异常）"
        text=str(exc)
        text=re.sub(r"(['\"]?(?:user_key|cookie|authorization)['\"]?\s*[:=]\s*)[^,;}]+",r"\1***",text,flags=re.I)
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
        timeout=max(30,min(3600,int(timeout_seconds or 300)))
        if not finished.wait(timeout): raise TimeoutError(f"秒传超过 {timeout} 秒，任务已暂停并等待稍后重试")
        if "error" in result: raise result["error"]
        return result.get("value")

    def transfer_delivery(self,cfg,item,target_cookie):
        timeout=cfg.get("transfer_timeout_seconds",300)
        target_label=self.user_label(item)
        relay_errors=[]
        if cfg.get("distributed_transfer_enabled"):
            for relay in self.store.relay_candidates(item["resource_id"],item["tg_id"]):
                relay_label=self.user_label(relay)
                try:
                    def relay_action(relay=relay):
                        relay_entry=P115.find_file(relay["cookie"],item)
                        if not relay_entry: raise FileNotFoundError("成功账号中未找到文件，可能已删除或改名")
                        relay_entry["name"]=item["name"]
                        return P115.transfer(relay["cookie"],target_cookie,relay_entry,item["target_cid"])
                    self.timed_call(relay_action,timeout)
                    self.store.event("接力秒传",f"{relay_label} → {target_label}：{item['name']} 成功",item["id"])
                    return f"接力：{relay_label}"
                except TimeoutError:
                    raise
                except Exception as exc:
                    error=self.transfer_error(exc); relay_errors.append(f"{relay_label}：{error}")
                    self.store.event("接力跳过",f"{relay_label} → {target_label}：{item['name']}；{error}",item["id"])
        source_cookie=self.store.source_cookie(item["source_id"],cfg["source_cookie"])
        if not source_cookie: raise RuntimeError("资源所属监听目录没有可用的 115 CK")
        try:
            self.timed_call(lambda:P115.transfer(source_cookie,target_cookie,item,item["target_cid"]),timeout)
            return "主源账号" if not relay_errors else "主源回退"
        except Exception as exc:
            error=self.transfer_error(exc)
            if relay_errors: error=f"接力账号均失败（{'；'.join(relay_errors)}）；主源回退失败：{error}"
            raise RuntimeError(error) from exc
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
            self.qr_sessions[sid]={"tg_id":tg_id,"token":token,"app":login_app,"expires":expires}
        return {"session":sid,"app":login_app,"app_name":QR_LOGIN_APPS[login_app],"expires":expires,"image":"data:image/png;base64,"+base64.b64encode(image).decode()}

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
        user=self.store.save_user(tg_id,{"cookie":cookie,"uid":profile["uid"],"target_cid":str(value.get("target_cid") or "0"),"target_name":str(value.get("target_name") or "根目录")})
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

    def scan(self, force=True):
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
            if monitor_type.startswith("cd2_"):
                local, changed = self.cd2_entries(cfg, source, force)
                if not changed:
                    stable_cutoff = stamp - int(source["stable_seconds"])
                    with self.store.lock:
                        due = self.store.db.execute("SELECT 1 FROM resources WHERE source_id=? AND status='stabilizing' AND first_seen<=? LIMIT 1", (source["id"], stable_cutoff)).fetchone()
                    if not due:
                        interval = 2 if monitor_type == "cd2_realtime" else int(source["poll_seconds"])
                        with self.store.lock,self.store.db:
                            self.store.db.execute("UPDATE sources SET last_scan=?,next_scan=?,error='',updated=? WHERE id=?",(stamp,stamp+interval,stamp,source["id"]))
                        return ""
                local_names = set(local)
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
                    row=self.store.db.execute("SELECT * FROM resources WHERE id=?",(rid,)).fetchone()
                    if row:
                        if row["status"]=="stabilizing" and stamp-int(row["first_seen"])>=int(source["stable_seconds"]):
                            self.store.db.execute("UPDATE resources SET status='waiting' WHERE id=?",(rid,)); self.queue_resource(rid,entry["name"],row["category"],stamp)
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
            self.store.event("来源",f"扫描 {source['name']}：发现 {discovered} 项",source["id"])
        except Exception as exc:
            with self.store.lock,self.store.db: self.store.db.execute("UPDATE sources SET error=?,last_scan=?,next_scan=?,updated=? WHERE id=?",(str(exc)[:500],stamp,stamp+int(source["poll_seconds"]),stamp,source["id"]))
            self.store.event("来源失败",f"扫描 {source['name']}：{exc}",source["id"])
            return str(exc)
        return ""

    def queue_resource(self,rid,name,category,stamp):
        users=self.store.db.execute("SELECT * FROM users WHERE enabled=1 AND status='active' AND cookie<>'' AND membership_expires>?",(stamp,)).fetchall()
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
            rows = self.store.db.execute("""SELECT d.id,d.resource_id,d.tg_id,d.attempts,r.*,u.cookie,u.target_cid,u.name user_name,u.username,u.note
              FROM deliveries d JOIN resources r ON r.id=d.resource_id JOIN users u ON u.tg_id=d.tg_id
              WHERE d.status IN ('waiting','retry') AND d.attempts<? AND d.next_attempt<=? AND u.status='active' AND u.enabled=1 AND u.membership_expires>?
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
            transfer_source=self.transfer_delivery(cfg,item,cookie)
            done=now()
            with self.store.lock, self.store.db:
                current=self.store.db.execute("SELECT status FROM deliveries WHERE id=?",(item["id"],)).fetchone()
                if not current or current["status"] != "running": return
                self.store.db.execute("UPDATE deliveries SET status='delivered',error='',next_attempt=0,source=?,updated=? WHERE id=?", (transfer_source,done,item["id"]))
                source=self.store.db.execute("SELECT retention_minutes,age_delete_minutes FROM sources WHERE id=?",(item["source_id"],)).fetchone()
                retention=int(source[0]) if source else -1; age_delete=int(source[1]) if source else -1
                due=done+retention*60 if retention>=0 else 0
                self.store.db.execute("""UPDATE resources SET first_success=CASE WHEN first_success=0 THEN ? ELSE first_success END,
                  delete_due=CASE WHEN delete_due=0 THEN ? ELSE delete_due END,
                  cleanup=CASE WHEN ? >= 0 OR ? >= 0 THEN 'scheduled' ELSE 'retained' END WHERE id=?""",
                  (done,due,age_delete,retention,item["resource_id"]))
            target_label=self.user_label(item)
            self.store.event("派送",f"{target_label}：{item['name']}；来源 {transfer_source}",item["id"])
            self.telegram.send(item["tg_id"], f"✅ <b>派送完成</b>\n{item['name']}", False)
        except Exception as exc:
            failed=now(); attempts=int(item.get("attempts") or 0)+1; max_attempts=max(1,min(50,int(cfg.get("max_attempts") or 5)))
            delay=max(1,int(cfg.get("retry_minutes") or 15))*60*min(16,2**max(0,attempts-1))
            status="failed" if attempts>=max_attempts else "retry"; error=self.transfer_error(exc)
            with self.store.lock, self.store.db: self.store.db.execute("UPDATE deliveries SET status=?,error=?,next_attempt=?,updated=? WHERE id=? AND status='running'", (status,error,failed+delay if status=="retry" else 0,failed,item["id"]))
            target_label=self.user_label(item)
            wait_text=f"，{delay//60} 分钟后重试" if status=="retry" else "，已达到最大尝试次数"
            self.store.event("派送失败",f"{target_label}：{item['name']}；{error}{wait_text}",item["id"])

    def cleanup(self,cfg):
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

    def scheduler(self):
        while not self.stop.is_set():
            cfg = self.store.config(False)
            if cfg["enabled"]: self.scan(False)
            self.maintenance(cfg)
            self.stop.wait(2)


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
                self.admin(); value=self.body(); user=self.app.store.save_user(int(value.get("tg_id") or 0),value); self.app.store.event("用户管理",f"更新用户：{user.get('name') or user.get('tg_id')}",str(user["tg_id"])); return self.send_json(200,{"message":"用户设置已保存","user":user})
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
            if path == "/api/admin/scan" and method == "POST": self.admin(); threading.Thread(target=self.app.scan,daemon=True).start(); return self.send_json(202, {**self.app.store.status(), "message": "扫描已启动"})
            if path == "/api/mini/me" and method == "GET": uid=self.mini(); return self.send_json(200, {"user": self.app.store.user(uid), "categories": ["电影","剧集","动漫","纪录片","其他"], "qr_apps": QR_LOGIN_APPS})
            if path == "/api/mini/overview" and method == "GET": uid=self.mini(); return self.send_json(200, self.app.store.mini_overview(uid))
            if path == "/api/mini/browse" and method == "GET": uid=self.mini(); return self.send_json(200, self.app.mini_browse(uid,query))
            if path == "/api/mini/preferences" and method == "PUT": uid=self.mini(); return self.send_json(200, {"user": self.app.store.save_user(uid, self.body())})
            if path == "/api/mini/account" and method == "PUT":
                uid=self.mini(); value=self.body(); profile=P115.profile(str(value.get("cookie") or "")); value["uid"]=profile["uid"]
                return self.send_json(200, {"user": self.app.store.save_user(uid, value), "account": profile})
            if path == "/api/mini/qr/start" and method == "POST": uid=self.mini(); return self.send_json(200,self.app.qr_start(uid,self.body()))
            if path == "/api/mini/qr/status" and method == "POST": uid=self.mini(); return self.send_json(200,self.app.qr_poll(uid,self.body()))
            return self.send_json(404, {"error": "接口不存在"})
        except PermissionError as exc: self.send_json(401, {"error": str(exc)})
        except (ValueError, RuntimeError, urllib.error.URLError) as exc: self.send_json(400, {"error": str(exc)})
        except Exception as exc: self.send_json(500, {"error": f"服务错误：{exc}"})
    def send_file(self, name, content_type):
        path = Path(self.server.static_dir) / name
        data = path.read_bytes(); self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Cache-Control", "no-cache"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--host",default="127.0.0.1"); parser.add_argument("--port",type=int,default=8790); parser.add_argument("--data",default="/var/lib/cockroach-runner"); parser.add_argument("--static",default=str(Path(__file__).with_name("static"))); args=parser.parse_args()
    admin_username=os.environ["COCKROACH_ADMIN_USERNAME"]; admin_password=os.environ["COCKROACH_ADMIN_PASSWORD"]; encryption_key=os.environ["COCKROACH_ENCRYPTION_KEY"]
    app=Application(Store(args.data,encryption_key)); server=ThreadingHTTPServer((args.host,args.port),Handler); server.app=app; server.admin_username=admin_username; server.admin_password=admin_password; server.static_dir=args.static
    threading.Thread(target=app.telegram.loop,daemon=True).start(); threading.Thread(target=app.scheduler,daemon=True).start()
    try: server.serve_forever()
    finally: app.stop.set(); server.server_close()

if __name__ == "__main__": main()
