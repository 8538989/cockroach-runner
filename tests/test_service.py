import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "companion"))
import service  # noqa: E402


class ScanTests(unittest.TestCase):
    def test_monitor_loop_is_decoupled_from_delivery_and_recovers_after_exception(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.set("enabled", True)
            app = service.Application(store)
            calls = []
            class FastStop:
                stopped = False
                def is_set(self): return self.stopped
                def wait(self, _seconds): return self.stopped
                def set(self): self.stopped = True
            app.stop = FastStop()
            def scan(force, process_tasks):
                calls.append((force, process_tasks))
                if len(calls) == 1: raise RuntimeError("temporary monitor error")
                app.stop.set()
            app.scan = scan
            app.scheduler()
            self.assertEqual(calls, [(False, False), (False, False)])
            self.assertGreater(store.get("monitor_heartbeat", 0), 0)
            event = store.db.execute("SELECT message FROM events WHERE kind='后台自恢复'").fetchone()
            self.assertIn("temporary monitor error", event["message"])
            store.db.close()

    def test_tmdb_filename_parser_and_rich_caption(self):
        item = {"name": "蝙蝠侠：黑暗骑士 (2008) - 2160p.HDR10.HEVC.mkv", "size": 68490000000, "category": "电影"}
        parsed = service.TMDB.parse_name(item["name"], item["category"])
        self.assertEqual((parsed["title"], parsed["year"], parsed["media_type"]), ("蝙蝠侠：黑暗骑士", 2008, "movie"))
        metadata = {"id": 155, "media_type": "movie", "title": "蝙蝠侠：黑暗骑士", "release_date": "2008-07-16",
                    "vote_average": 8.5, "poster_path": "/poster.jpg", "overview": "蝙蝠侠在打击犯罪的战争中加大了赌注。",
                    "production_countries": [{"iso_3166_1": "US"}], "genres": [{"name": "动作"}],
                    "credits": {"cast": [{"id": 3894, "name": "克里斯蒂安·贝尔"}]},
                    "external_ids": {"imdb_id": "tt0468569"}}
        caption = service.TMDB.caption(item, metadata)
        self.assertIn("🎬 <b>蝙蝠侠：黑暗骑士 · 2008</b>", caption)
        self.assertIn("欧美电影", caption)
        self.assertIn("TMDB ID", caption)
        self.assertIn("4K / HDR10 / HEVC / MKV", caption)
        self.assertIn("IMDb tt0468569", caption)
        self.assertLess(len(service.re.sub(r"<[^>]+>", "", caption)), 1024)
        self.assertEqual(service.TMDB.poster(metadata), "https://image.tmdb.org/t/p/w780/poster.jpg")

    def test_tmdb_api_key_is_encrypted_and_hidden_from_public_config(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.save_config({"tmdb_api_key": "TMDB-SECRET", "tmdb_enabled": True})
            public = store.config(False); private = store.config(True)
            raw = store.db.execute("SELECT value,secret FROM settings WHERE key='tmdb_api_key'").fetchone()
            self.assertNotIn("tmdb_api_key", public)
            self.assertTrue(public["tmdb_configured"])
            self.assertTrue(public["tmdb_enabled"])
            self.assertEqual(private["tmdb_api_key"], "TMDB-SECRET")
            self.assertNotIn("TMDB-SECRET", raw["value"])
            self.assertEqual(raw["secret"], 1)
            store.db.close()

    def test_rich_success_notification_uses_tmdb_poster(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            app = service.Application(store)
            item = {"id": "d", "resource_id": "r", "tg_id": 123, "name": "Movie.2024.2160p.mkv", "size": 100, "category": "电影"}
            metadata = {"id": 1, "media_type": "movie", "title": "电影", "release_date": "2024-01-01", "poster_path": "/p.jpg"}
            calls = []
            app.tmdb_metadata = lambda _key, _item: metadata
            app.telegram.send_photo = lambda chat_id, photo, caption: calls.append((chat_id, photo, caption))
            app.notify_delivery_success({"tmdb_enabled": True, "tmdb_api_key": "KEY"}, item)
            self.assertEqual(calls[0][0:2], (123, "https://image.tmdb.org/t/p/w780/p.jpg"))
            self.assertIn("🎞 <b>影片资料</b>", calls[0][2])
            store.db.close()

    def test_notification_failure_does_not_turn_delivered_task_into_failure(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.set("source_cookie", "source", True)
            store.set("enabled", True)
            store.upsert_user(123)
            store.save_user(123, {"cookie": "target", "uid": "u1"})
            stamp = service.now()
            with store.db:
                store.db.execute("INSERT INTO resources(id,node_id,name,pickcode,sha1,size,is_dir,category,first_seen,status,source_id) VALUES('r','1','Movie.2024.mkv','p','a',1,0,'电影',?,'waiting','legacy')", (stamp,))
                store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,created,updated) VALUES('d','r',123,?,?)", (stamp, stamp))
            app = service.Application(store)
            original_transfer = service.P115.transfer
            try:
                service.P115.transfer = staticmethod(lambda *_args: None)
                app.telegram.send = lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("Telegram 超时"))
                app.deliver(store.config(True))
                row = store.db.execute("SELECT status,error FROM deliveries WHERE id='d'").fetchone()
                self.assertEqual((row["status"], row["error"]), ("delivered", ""))
                event = store.db.execute("SELECT kind,message FROM events WHERE kind='通知失败'").fetchone()
                self.assertIn("Telegram 超时", event["message"])
            finally:
                service.P115.transfer = original_transfer
                store.db.close()

    def test_service_pause_recovers_running_tasks_and_resume_starts_service(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            stamp = service.now()
            with store.db:
                store.db.execute("INSERT INTO resources(id,node_id,name,first_seen) VALUES('r','1','Video.mkv',?)", (stamp,))
                store.db.execute("INSERT INTO users(tg_id,created,updated) VALUES(1,?,?)", (stamp, stamp))
                store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,status,created,updated) VALUES('d','r',1,'running',?,?)", (stamp, stamp))
            store.service_action("pause")
            row = store.db.execute("SELECT status,error FROM deliveries WHERE id='d'").fetchone()
            self.assertEqual(row["status"], "retry")
            self.assertIn("暂停", row["error"])
            self.assertFalse(store.get("enabled"))
            store.service_action("resume")
            self.assertTrue(store.get("enabled"))
            store.db.close()

    def test_distributed_transfer_uses_a_successful_recipient_as_relay(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.set("source_cookie", "MAIN-CK", True)
            store.upsert_user(1, "relay_user", "")
            store.save_user(1, {"cookie": "RELAY-CK", "uid": "relay"})
            store.upsert_user(2, "target_user", "")
            store.save_user(2, {"cookie": "TARGET-CK", "uid": "target", "target_cid": "99"})
            stamp = service.now()
            with store.db:
                store.db.execute("INSERT INTO resources(id,node_id,name,pickcode,sha1,size,first_seen,source_id) VALUES('r','10','Relay.mkv','pc','ABC',100,?,'legacy')", (stamp,))
                store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,status,created,updated) VALUES('done','r',1,'delivered',?,?)", (stamp, stamp))
                store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,status,created,updated) VALUES('target','r',2,'retry',?,?)", (stamp, stamp))
            app = service.Application(store)
            item = {"id": "target", "resource_id": "r", "tg_id": 2, "source_id": "legacy", "target_cid": "99",
                    "name": "Relay.mkv", "node_id": "10", "pickcode": "pc", "sha1": "ABC", "size": 100,
                    "is_dir": 0, "user_name": "", "username": "target_user", "note": ""}
            calls = []
            original_find, original_transfer = service.P115.find_file, service.P115.transfer
            try:
                service.P115.find_file = staticmethod(lambda cookie, entry: {**entry, "node_id": "relay-file", "pickcode": "relay-pc"})
                service.P115.transfer = staticmethod(lambda source, target, entry, cid: calls.append((source, target, entry["pickcode"], cid)))
                cfg = store.config(True); cfg.update({"distributed_transfer_enabled": True, "transfer_timeout_seconds": 30})
                self.assertEqual(app.transfer_delivery(cfg, item, "TARGET-CK"), "接力：@relay_user")
                self.assertEqual(calls, [("RELAY-CK", "TARGET-CK", "relay-pc", "99")])
            finally:
                service.P115.find_file, service.P115.transfer = original_find, original_transfer
                store.db.close()

    def test_distributed_transfer_falls_back_to_source_when_relay_file_is_missing(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.set("source_cookie", "MAIN-CK", True)
            store.upsert_user(1, "relay_user", "")
            store.save_user(1, {"cookie": "RELAY-CK", "uid": "relay"})
            store.upsert_user(2, "target_user", "")
            store.save_user(2, {"cookie": "TARGET-CK", "uid": "target", "target_cid": "99"})
            stamp = service.now()
            with store.db:
                store.db.execute("INSERT INTO resources(id,node_id,name,pickcode,sha1,size,first_seen,source_id) VALUES('r','10','Fallback.mkv','pc','ABC',100,?,'legacy')", (stamp,))
                store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,status,created,updated) VALUES('done','r',1,'delivered',?,?)", (stamp, stamp))
            app = service.Application(store)
            item = {"id": "target", "resource_id": "r", "tg_id": 2, "source_id": "legacy", "target_cid": "99",
                    "name": "Fallback.mkv", "node_id": "10", "pickcode": "pc", "sha1": "ABC", "size": 100,
                    "is_dir": 0, "user_name": "", "username": "target_user", "note": ""}
            calls = []
            timeouts = []
            original_find, original_transfer, original_monotonic = service.P115.find_file, service.P115.transfer, service.time.monotonic
            try:
                service.P115.find_file = staticmethod(lambda cookie, entry: None)
                service.P115.transfer = staticmethod(lambda source, target, entry, cid: calls.append((source, target, entry["pickcode"], cid)))
                moments = iter((100, 110, 125))
                service.time.monotonic = lambda: next(moments)
                app.timed_call = lambda callback, timeout: timeouts.append(timeout) or callback()
                cfg = store.config(True); cfg.update({"distributed_transfer_enabled": True, "transfer_timeout_seconds": 30})
                self.assertEqual(app.transfer_delivery(cfg, item, "TARGET-CK"), "主源回退")
                self.assertEqual(calls, [("MAIN-CK", "TARGET-CK", "pc", "99")])
                self.assertEqual(timeouts, [20, 5])
            finally:
                service.P115.find_file, service.P115.transfer, service.time.monotonic = original_find, original_transfer, original_monotonic
                store.db.close()

    def test_successful_recipient_wakes_retry_and_failed_peers_for_relay(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            stamp = service.now()
            with store.db:
                store.db.execute("INSERT INTO resources(id,node_id,name,first_seen) VALUES('r','1','Wake.mkv',?)", (stamp,))
                for tg_id in range(1, 5):
                    store.db.execute("INSERT INTO users(tg_id,created,updated) VALUES(?,?,?)", (tg_id, stamp, stamp))
                rows = (("done", 1, "delivered", 1), ("retry", 2, "retry", 2),
                        ("failed", 3, "failed", 5), ("cancelled", 4, "cancelled", 1))
                for delivery_id, tg_id, status, attempts in rows:
                    store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,status,attempts,next_attempt,created,updated) VALUES(?,?,?, ?,?,?,?,?)",
                                     (delivery_id, "r", tg_id, status, attempts, stamp + 3600, stamp, stamp))
            self.assertEqual(store.wake_relay_retries("r", "done"), 2)
            retry = store.db.execute("SELECT status,attempts,next_attempt,error FROM deliveries WHERE id='retry'").fetchone()
            failed = store.db.execute("SELECT status,attempts,next_attempt,error FROM deliveries WHERE id='failed'").fetchone()
            cancelled = store.db.execute("SELECT status FROM deliveries WHERE id='cancelled'").fetchone()
            self.assertEqual((retry["status"], retry["attempts"], retry["next_attempt"]), ("retry", 2, 0))
            self.assertEqual((failed["status"], failed["attempts"], failed["next_attempt"]), ("retry", 0, 0))
            self.assertIn("立即尝试接力秒传", failed["error"])
            self.assertEqual(cancelled["status"], "cancelled")
            store.db.close()

    def test_transfer_error_hides_115_user_key(self):
        error = RuntimeError({"pid": "1", "filename": "A.mkv", "filesha1": "ABC", "user_id": 9, "user_key": "SECRET"})
        text = service.Application.transfer_error(error)
        self.assertIn("UID 9", text)
        self.assertNotIn("SECRET", text)

    def test_source_can_use_its_own_encrypted_115_cookie(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            original_profile = service.P115.profile
            try:
                service.P115.profile = staticmethod(lambda cookie: {"uid": "source-user", "name": "Source"} if cookie == "SOURCE-CK" else {})
                sid = store.save_source({"name": "独立账号目录", "cid": "88", "cookie_mode": "independent", "cookie": "SOURCE-CK"})
                public = next(item for item in store.sources() if item["id"] == sid)
                self.assertNotIn("cookie", public)
                self.assertTrue(public["cookie_configured"])
                self.assertTrue(store.config()["source_configured"])
                self.assertEqual(store.source_cookie(sid, "GLOBAL-CK"), "SOURCE-CK")
                store.save_source({"id": sid, "name": "独立账号目录", "cid": "88", "cookie_mode": "independent"})
                self.assertEqual(store.source_cookie(sid, "GLOBAL-CK"), "SOURCE-CK")
            finally:
                service.P115.profile = original_profile
                store.db.close()

    def test_source_folder_browser_uses_typed_independent_cookie(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.set("p115_api_interval_seconds", 0)
            seen = []
            original_list = service.P115.list_dir
            try:
                service.P115.list_dir = staticmethod(lambda cookie, cid: seen.append((cookie, cid)) or [
                    {"node_id": "9", "name": "Movies", "is_dir": True}
                ])
                result = service.Application(store).source_browse({"cookie_mode": "independent", "cookie": "NEW-CK", "cid": "7"})
                self.assertEqual(seen, [("NEW-CK", "7")])
                self.assertEqual(result["items"], [{"name": "Movies", "cid": "9", "is_dir": True}])
            finally:
                service.P115.list_dir = original_list
                store.db.close()

    def test_scan_uses_monitor_directory_independent_cookie(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.set("source_cookie", "GLOBAL-CK", True)
            store.set("p115_api_interval_seconds", 0)
            encrypted = store.crypt.encrypt(b"DIRECTORY-CK").decode()
            with store.db:
                store.db.execute("UPDATE sources SET cookie_mode='independent',cookie=?,baseline_ready=1,stable_seconds=0 WHERE id='legacy'", (encrypted,))
            seen = []
            original_list = service.P115.list_dir
            try:
                service.P115.list_dir = staticmethod(lambda cookie, cid: seen.append((cookie, cid)) or [])
                source = store.sources(True)[0]
                service.Application(store).scan_source(store.config(True), source, service.now(), True)
                self.assertEqual(seen, [("DIRECTORY-CK", source["cid"])])
            finally:
                service.P115.list_dir = original_list
                store.db.close()

    def test_bulk_delivery_actions_and_record_deletion(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            stamp = service.now()
            with store.db:
                store.db.execute("INSERT INTO users(tg_id,created,updated) VALUES(1,?,?)", (stamp, stamp))
                for index, status in enumerate(("waiting", "cancelled", "delivered"), 1):
                    store.db.execute("INSERT INTO resources(id,node_id,name,first_seen) VALUES(?,?,?,?)", (f"r{index}", str(index), f"Video{index}.mkv", stamp))
                    store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,status,created,updated) VALUES(?,?,1,?,?,?)", (f"d{index}", f"r{index}", status, stamp, stamp))
            self.assertEqual(store.delivery_action({"action": "retry-all"}), 2)
            self.assertEqual(store.db.execute("SELECT status FROM deliveries WHERE id='d3'").fetchone()[0], "delivered")
            self.assertEqual(store.delivery_action({"action": "cancel-all"}), 2)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM deliveries WHERE status='cancelled'").fetchone()[0], 2)
            self.assertEqual(store.delivery_action({"action": "delete-all"}), 3)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
            store.db.close()

    def test_admin_records_are_paginated_at_one_hundred(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            stamp = service.now()
            with store.db:
                store.db.execute("INSERT INTO users(tg_id,name,created,updated) VALUES(1,'User',?,?)", (stamp, stamp))
                for index in range(205):
                    store.db.execute("INSERT INTO resources(id,node_id,name,first_seen) VALUES(?,?,?,?)", (f"r{index:03}", str(index), f"Video{index:03}.mkv", stamp + index))
                    store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,created,updated) VALUES(?,?,1,?,?)", (f"d{index:03}", f"r{index:03}", stamp + index, stamp + index))
            first = store.records_page("deliveries", 1, 100)
            third = store.records_page("resources", 3, 100)
            self.assertEqual((len(first["items"]), first["total"], first["pages"]), (100, 205, 3))
            self.assertEqual((len(third["items"]), third["page"], third["page_size"]), (5, 3, 100))
            store.db.close()

    def test_delete_all_resource_records_keeps_resources_for_deduplication(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            stamp = service.now()
            with store.db:
                store.db.execute("INSERT INTO resources(id,node_id,name,first_seen) VALUES('r1','1','One.mkv',?)", (stamp,))
                store.db.execute("INSERT INTO resources(id,node_id,name,first_seen) VALUES('r2','2','Two.mkv',?)", (stamp,))
            self.assertEqual(store.resource_action({"action": "delete-all-records"}), 2)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM resources WHERE hidden=1").fetchone()[0], 2)
            self.assertEqual(store.records_page("resources", 1)["total"], 0)
            store.db.close()

    def test_store_recovers_running_delivery_after_restart(self):
        with tempfile.TemporaryDirectory() as data_dir:
            key = Fernet.generate_key().decode()
            store = service.Store(data_dir, key)
            stamp = service.now()
            with store.db:
                store.db.execute("INSERT INTO resources(id,node_id,name,first_seen) VALUES('r','1','Stuck.mkv',?)", (stamp,))
                store.db.execute("INSERT INTO users(tg_id,created,updated) VALUES(123,?,?)", (stamp, stamp))
                store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,status,created,updated) VALUES('d','r',123,'running',?,?)", (stamp, stamp))
            store.db.close()

            reopened = service.Store(data_dir, key)
            row = reopened.db.execute("SELECT status,next_attempt,error FROM deliveries WHERE id='d'").fetchone()
            self.assertEqual((row["status"], row["next_attempt"]), ("retry", 0))
            self.assertIn("服务重启", row["error"])
            reopened.db.close()

    def test_nested_directories_expand_to_individual_video_resources(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.set("source_cookie", "source", True)
            store.set("enabled", True)
            store.set("p115_api_interval_seconds", 0)
            store.upsert_user(123)
            store.save_user(123, {"cookie": "target", "uid": "u1"})
            with store.db:
                store.db.execute("UPDATE sources SET baseline_ready=1,stable_seconds=0 WHERE id='legacy'")
            tree = {
                "0": [{"node_id": "dir1", "name": "Show", "is_dir": True, "pickcode": "", "sha1": "", "size": 0}],
                "dir1": [
                    {"node_id": "v1", "name": "Show.S01E01.mkv", "is_dir": False, "pickcode": "p1", "sha1": "a", "size": 10},
                    {"node_id": "sub", "name": "Season 2", "is_dir": True, "pickcode": "", "sha1": "", "size": 0},
                    {"node_id": "txt", "name": "readme.txt", "is_dir": False, "pickcode": "p3", "sha1": "c", "size": 1},
                ],
                "sub": [{"node_id": "v2", "name": "Show.S02E01.mp4", "is_dir": False, "pickcode": "p2", "sha1": "b", "size": 20}],
            }
            original_list = service.P115.list_dir
            try:
                service.P115.list_dir = staticmethod(lambda _cookie, cid: tree[str(cid)])
                app = service.Application(store)
                app.scan_source(store.config(True), store.sources()[0], service.now(), True)
                rows = store.db.execute("SELECT name,is_dir FROM resources ORDER BY name").fetchall()
                self.assertEqual([(row["name"], row["is_dir"]) for row in rows], [
                    ("Show.S01E01.mkv", 0), ("Show.S02E01.mp4", 0)
                ])
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 2)
            finally:
                service.P115.list_dir = original_list
                store.db.close()

    def test_cd2_mount_snapshot_detects_nested_file_change(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.set("cd2_api_interval_seconds", 0)
            store.set("cd2_root", data_dir)
            watched = Path(data_dir) / "watched"
            nested = watched / "Show"
            nested.mkdir(parents=True)
            video = nested / "Episode.mkv"
            video.write_bytes(b"one")
            source = store.sources()[0]
            source.update({"monitor_type": "cd2_realtime", "cd2_path": str(watched)})
            app = service.Application(store)
            _names, first_changed = app.cd2_entries(store.config(True), source)
            _names, unchanged = app.cd2_entries(store.config(True), source)
            video.write_bytes(b"changed")
            _names, nested_changed = app.cd2_entries(store.config(True), source)
            self.assertTrue(first_changed)
            self.assertFalse(unchanged)
            self.assertTrue(nested_changed)
            store.db.close()

    def test_first_scan_is_baseline_and_second_scan_dispatches(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.set("source_cookie", "source", True)
            store.set("enabled", True)
            store.set("p115_api_interval_seconds", 0)
            store.upsert_user(123)
            store.save_user(123, {"cookie": "target", "uid": "u1"})
            app = service.Application(store)
            rounds = [
                [{"node_id": "1", "name": "Old.mkv", "is_dir": False, "pickcode": "p1", "sha1": "a", "size": 1}],
                [
                    {"node_id": "1", "name": "Old.mkv", "is_dir": False, "pickcode": "p1", "sha1": "a", "size": 1},
                    {"node_id": "2", "name": "New.mkv", "is_dir": False, "pickcode": "p2", "sha1": "b", "size": 2},
                ],
            ]
            original_list, original_transfer = service.P115.list_dir, service.P115.transfer
            try:
                service.P115.list_dir = staticmethod(lambda _cookie, _cid: rounds.pop(0))
                service.P115.transfer = staticmethod(lambda *_args: None)
                app.telegram.send = lambda *_args, **_kwargs: None
                app.scan()
                self.assertTrue(store.get("baseline_ready"))
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
                app.scan()
                self.assertEqual(
                    store.db.execute("SELECT COUNT(*) FROM deliveries WHERE status='delivered'").fetchone()[0], 1
                )
            finally:
                service.P115.list_dir, service.P115.transfer = original_list, original_transfer
                store.db.close()

    def test_binding_code_is_single_use(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            binding = store.create_binding("quarter")
            result = store.consume_binding(binding["code"], 123)
            self.assertEqual((binding["grant_days"], result["plan"]), (90, "quarter"))
            self.assertGreaterEqual(store.user(123)["membership_expires"], service.now() + 90 * 86400 - 2)
            with self.assertRaisesRegex(ValueError, "无效或已过期"):
                store.consume_binding(binding["code"], 456)
            row = store.db.execute("SELECT status,used_by FROM bindings WHERE id=?", (binding["id"],)).fetchone()
            self.assertEqual((row["status"], row["used_by"]), ("used", "123"))
            store.db.close()

    def test_expired_member_is_not_queued_or_delivered(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.upsert_user(123)
            store.save_user(123, {"cookie": "target", "uid": "u1"})
            stamp = service.now()
            with store.db:
                store.db.execute("UPDATE users SET membership_expires=? WHERE tg_id=123", (stamp - 1,))
                store.db.execute("INSERT INTO resources(id,node_id,name,first_seen) VALUES('expired-resource','1','Expired.mkv',?)", (stamp,))
            app = service.Application(store)
            app.queue_resource("expired-resource", "Expired.mkv", "电影", stamp)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM deliveries WHERE tg_id=123").fetchone()[0], 0)
            store.db.close()

    def test_membership_renewal_extends_from_current_expiry(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.upsert_user(123)
            original = store.user(123)["membership_expires"]
            binding = store.create_binding("month")
            result = store.consume_binding(binding["code"], 123)
            self.assertEqual(result["membership_expires"], original + 30 * 86400)
            store.db.close()

    def test_mini_overview_is_scoped_to_current_user(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            stamp = service.now()
            with store.db:
                store.db.execute("INSERT INTO resources(id,node_id,name,pickcode,sha1,size,is_dir,category,first_seen,status,source_id) VALUES('r1','1','Mine.mkv','p','a',1,0,'电影',?,'waiting','legacy')", (stamp,))
                store.db.execute("INSERT INTO resources(id,node_id,name,pickcode,sha1,size,is_dir,category,first_seen,status,source_id) VALUES('r2','2','Other.mkv','p','b',1,0,'电影',?,'waiting','legacy')", (stamp,))
                store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,status,created,updated) VALUES('d1','r1',123,'delivered',?,?)", (stamp, stamp))
                store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,status,created,updated) VALUES('d2','r2',456,'waiting',?,?)", (stamp, stamp))
            overview = store.mini_overview(123)
            self.assertEqual([item["name"] for item in overview["deliveries"]], ["Mine.mkv"])
            self.assertEqual(overview["stats"], {"delivered": 1, "pending": 0, "failed": 0})
            store.db.close()

    def test_success_schedules_cleanup_only_when_source_rule_enabled(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.set("source_cookie", "source", True)
            store.set("enabled", True)
            store.set("p115_api_interval_seconds", 0)
            store.set("cd2_api_interval_seconds", 0)
            store.upsert_user(123)
            store.save_user(123, {"cookie": "target", "uid": "u1"})
            stamp = service.now()
            with store.db:
                store.db.execute("UPDATE sources SET retention_minutes=5 WHERE id='legacy'")
                store.db.execute("INSERT INTO resources(id,node_id,name,pickcode,sha1,size,is_dir,category,first_seen,status,source_id) VALUES('r','1','New.mkv','p','a',1,0,'电影',?,'waiting','legacy')", (stamp,))
                store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,created,updated) VALUES('d','r',123,?,?)", (stamp, stamp))
            app = service.Application(store)
            original_transfer = service.P115.transfer
            try:
                service.P115.transfer = staticmethod(lambda *_args: None)
                app.telegram.send = lambda *_args, **_kwargs: None
                app.deliver(store.config(True))
                row = store.db.execute("SELECT first_success,delete_due,cleanup FROM resources WHERE id='r'").fetchone()
                self.assertEqual(row["cleanup"], "scheduled")
                self.assertGreaterEqual(row["delete_due"], row["first_success"] + 300)
            finally:
                service.P115.transfer = original_transfer
                store.db.close()

    def test_cleanup_waits_for_all_deliveries(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.set("source_cookie", "source", True)
            stamp = service.now()
            with store.db:
                store.db.execute("UPDATE sources SET retention_minutes=0 WHERE id='legacy'")
                store.db.execute("INSERT INTO resources(id,node_id,name,pickcode,sha1,size,is_dir,category,first_seen,status,source_id,delete_due,cleanup) VALUES('r','1','New.mkv','p','a',1,0,'电影',?,'waiting','legacy',?,'scheduled')", (stamp, stamp - 1))
                store.db.execute("INSERT INTO users(tg_id,created,updated) VALUES(1,?,?)", (stamp, stamp))
                store.db.execute("INSERT INTO users(tg_id,created,updated) VALUES(2,?,?)", (stamp, stamp))
                store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,status,created,updated) VALUES('d1','r',1,'delivered',?,?)", (stamp, stamp))
                store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,status,created,updated) VALUES('d2','r',2,'retry',?,?)", (stamp, stamp))
            app = service.Application(store)
            deleted = []
            original_delete = service.P115.delete
            try:
                service.P115.delete = staticmethod(lambda *_args: deleted.append(True))
                app.cleanup(store.config(True))
                self.assertEqual(deleted, [])
                with store.db: store.db.execute("UPDATE deliveries SET status='cancelled' WHERE id='d2'")
                app.cleanup(store.config(True))
                self.assertEqual(deleted, [True])
                self.assertEqual(store.db.execute("SELECT cleanup FROM resources WHERE id='r'").fetchone()[0], "deleted")
            finally:
                service.P115.delete = original_delete
                store.db.close()

    def test_age_rule_schedules_cleanup_when_resource_enters_source(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.set("source_cookie", "source", True)
            with store.db:
                store.db.execute("UPDATE sources SET baseline_ready=1,stable_seconds=0,age_delete_minutes=60 WHERE id='legacy'")
            app = service.Application(store)
            original_list = service.P115.list_dir
            try:
                service.P115.list_dir = staticmethod(lambda *_args: [
                    {"node_id": "1", "name": "Timed.mkv", "is_dir": False, "pickcode": "p", "sha1": "a", "size": 1}
                ])
                app.scan_source(store.config(True), store.sources()[0], service.now(), True)
                row = store.db.execute("SELECT first_seen,delete_due,cleanup FROM resources WHERE name='Timed.mkv'").fetchone()
                self.assertEqual(row["cleanup"], "scheduled")
                self.assertGreaterEqual(row["delete_due"], row["first_seen"] + 3600)
            finally:
                service.P115.list_dir = original_list
                store.db.close()

    def test_delete_user_removes_completed_history(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.upsert_user(123, "tester", "Test User")
            stamp = service.now()
            with store.db:
                store.db.execute("INSERT INTO resources(id,node_id,name,first_seen) VALUES('r','1','Done.mkv',?)", (stamp,))
                store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,status,created,updated) VALUES('d','r',123,'delivered',?,?)", (stamp, stamp))
            store.delete_user(123)
            self.assertIsNone(store.user(123))
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM deliveries WHERE tg_id=123").fetchone()[0], 0)
            store.db.close()

    def test_cd2_unchanged_snapshot_releases_stabilized_resource(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.set("source_cookie", "source", True)
            store.set("p115_api_interval_seconds", 0)
            store.set("cd2_api_interval_seconds", 0)
            store.upsert_user(123)
            store.save_user(123, {"cookie": "target", "uid": "u1"})
            watched = Path(data_dir) / "watched"
            watched.mkdir()
            (watched / "New.mkv").touch()
            store.set("cd2_root", data_dir)
            with store.db:
                store.db.execute("UPDATE sources SET baseline_ready=1,monitor_type='cd2_realtime',cd2_path=?,stable_seconds=30 WHERE id='legacy'", (str(watched),))
            app = service.Application(store)
            entry = {"node_id": "1", "name": "New.mkv", "is_dir": False, "pickcode": "p", "sha1": "a", "size": 1}
            original_list = service.P115.list_dir
            try:
                service.P115.list_dir = staticmethod(lambda *_args: [entry])
                source = store.sources()[0]
                stamp = service.now()
                app.scan_source(store.config(True), source, stamp, False)
                self.assertEqual(store.db.execute("SELECT status FROM resources WHERE name='New.mkv'").fetchone()[0], "stabilizing")
                app.scan_source(store.config(True), store.sources()[0], stamp + 31, False)
                self.assertEqual(store.db.execute("SELECT status FROM resources WHERE name='New.mkv'").fetchone()[0], "waiting")
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM deliveries WHERE resource_id IN (SELECT id FROM resources WHERE name='New.mkv')").fetchone()[0], 1)
            finally:
                service.P115.list_dir = original_list
                store.db.close()

    def test_resource_overview_aggregates_deliveries_and_hides_deleted_record(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            stamp = service.now()
            with store.db:
                store.db.execute("INSERT INTO resources(id,node_id,name,first_seen) VALUES('r','1','Record.mkv',?)", (stamp,))
                store.db.execute("INSERT INTO users(tg_id,created,updated) VALUES(1,?,?)", (stamp, stamp))
                store.db.execute("INSERT INTO users(tg_id,created,updated) VALUES(2,?,?)", (stamp, stamp))
                store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,status,created,updated) VALUES('d1','r',1,'delivered',?,?)", (stamp, stamp))
                store.db.execute("INSERT INTO deliveries(id,resource_id,tg_id,status,created,updated) VALUES('d2','r',2,'cancelled',?,?)", (stamp, stamp))
            record = next(item for item in store.records_page("resources")["items"] if item["id"] == "r")
            self.assertEqual((record["delivery_total"], record["delivery_done"], record["delivery_failed"]), (2, 1, 1))
            store.resource_action({"id": "r", "action": "delete-record"})
            self.assertFalse(any(item["id"] == "r" for item in store.records_page("resources")["items"]))
            self.assertEqual(store.db.execute("SELECT hidden FROM resources WHERE id='r'").fetchone()[0], 1)
            store.db.close()

    def test_qr_confirmation_binds_cookie_to_current_user(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.upsert_user(123)
            app = service.Application(store)
            original_start, original_poll, original_profile = service.P115.qr_start, service.P115.qr_poll, service.P115.profile
            try:
                service.P115.qr_start = staticmethod(lambda app_name: ({"uid": "qr", "time": 1, "sign": "s", "selected": app_name}, b"png"))
                service.P115.qr_poll = staticmethod(lambda token, app_name: ("confirmed", "UID=1; CID=2; SEID=3") if token["selected"] == app_name else ("expired", ""))
                service.P115.profile = staticmethod(lambda _cookie: {"uid": "1", "name": "QR User"})
                started = app.qr_start(123, {"app": "115ios"})
                self.assertEqual((started["app"], started["app_name"]), ("115ios", "115网盘（iOS端）"))
                result = app.qr_poll(123, {"session": started["session"], "target_cid": "9", "target_name": "扫码目录"})
                self.assertEqual(result["status"], "confirmed")
                user = store.user(123, True)
                self.assertEqual((user["uid"], user["cookie"], user["target_cid"]), ("1", "UID=1; CID=2; SEID=3", "9"))
            finally:
                service.P115.qr_start, service.P115.qr_poll, service.P115.profile = original_start, original_poll, original_profile
                store.db.close()

    def test_qr_rejects_unknown_login_app(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.upsert_user(123)
            app = service.Application(store)
            with self.assertRaisesRegex(ValueError, "不支持"):
                app.qr_start(123, {"app": "unknown-device"})
            store.db.close()

    def test_folder_browser_only_returns_115_directories(self):
        entries = [
            {"node_id": "10", "name": "Movies", "is_dir": True},
            {"node_id": "11", "name": "readme.txt", "is_dir": False},
        ]
        original_list = service.P115.list_dir
        try:
            service.P115.list_dir = staticmethod(lambda cookie, cid: entries)
            with tempfile.TemporaryDirectory() as data_dir:
                store = service.Store(data_dir, Fernet.generate_key().decode())
                store.set("p115_api_interval_seconds", 0)
                self.assertEqual(service.Application(store).folders_115("cookie", "0"), [{"name": "Movies", "cid": "10", "is_dir": True}])
                store.db.close()
        finally:
            service.P115.list_dir = original_list

    def test_api_interval_settings_are_clamped_and_exposed(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.save_config({"p115_api_interval_seconds": 1.5, "cd2_api_interval_seconds": -1, "transfer_interval_seconds": 9999})
            cfg = store.config()
            self.assertEqual((cfg["p115_api_interval_seconds"], cfg["cd2_api_interval_seconds"], cfg["transfer_interval_seconds"]), (1.5, 0.0, 3600.0))
            store.db.close()

    def test_cd2_mount_browser_stays_inside_configured_root(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "folder").mkdir()
            items = service.CD2.list_mount({"cd2_root": root}, root)
            self.assertEqual([(item["name"], item["is_dir"]) for item in items], [("folder", True)])
            with self.assertRaisesRegex(ValueError, "超出"):
                service.CD2.list_mount({"cd2_root": root}, str(Path(root).parent))

    def test_cd2_api_source_accepts_remote_path_without_local_mount(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            sid = store.save_source({"name": "API", "cid": "123", "monitor_type": "cd2_api", "cd2_path": "/cloud/watch"})
            source = next(item for item in store.sources() if item["id"] == sid)
            self.assertEqual((source["monitor_type"], source["cd2_path"]), ("cd2_api", "/cloud/watch"))
            store.db.close()


if __name__ == "__main__":
    unittest.main()
