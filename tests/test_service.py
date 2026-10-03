import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "companion"))
import service  # noqa: E402


class ScanTests(unittest.TestCase):
    def test_first_scan_is_baseline_and_second_scan_dispatches(self):
        with tempfile.TemporaryDirectory() as data_dir:
            store = service.Store(data_dir, Fernet.generate_key().decode())
            store.set("source_cookie", "source", True)
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
            binding = store.create_binding(30)
            store.consume_binding(binding["code"], 123)
            with self.assertRaisesRegex(ValueError, "无效或已过期"):
                store.consume_binding(binding["code"], 456)
            row = store.db.execute("SELECT status,used_by FROM bindings WHERE id=?", (binding["id"],)).fetchone()
            self.assertEqual((row["status"], row["used_by"]), ("used", "123"))
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
            record = next(item for item in store.overview()["resources"] if item["id"] == "r")
            self.assertEqual((record["delivery_total"], record["delivery_done"], record["delivery_failed"]), (2, 1, 1))
            store.resource_action({"id": "r", "action": "delete-record"})
            self.assertFalse(any(item["id"] == "r" for item in store.overview()["resources"]))
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
            self.assertEqual(service.Application.folders_115("cookie", "0"), [{"name": "Movies", "cid": "10", "is_dir": True}])
        finally:
            service.P115.list_dir = original_list

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
