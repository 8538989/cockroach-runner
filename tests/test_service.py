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


if __name__ == "__main__":
    unittest.main()
