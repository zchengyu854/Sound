"""Source ownership, bounded summaries and optimistic versioning without models."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError

from conversation_summary import SummaryBase, SummaryEdit, SummaryItems, SummaryRollback, SummaryStore


class Service:
    def __init__(self, directory):
        self.directory = directory
        self.db = directory / "chat.sqlite3"
        self.configured = True
        self.base_url, self.key = "https://test.invalid/v1", ""
        self.memory_version = 0
        self.characters = SimpleNamespace(lock=threading.RLock(), get=lambda value: {"memory_version": self.memory_version})
        self.requests = []
        self.output = None
        self.finish_reason = "stop"
        self.before_response = lambda: None

    def connect(self):
        db = sqlite3.connect(self.db)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def rows(self, query, values=()):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(query, values)]

    def one(self, table, value):
        try:
            UUID(str(value))
        except ValueError:
            raise HTTPException(422, "invalid id") from None
        rows = self.rows(f"SELECT * FROM {table} WHERE id=?", (value,))
        if not rows:
            raise HTTPException(404, "not found")
        return rows[0]

    async def request_chat(self, history, headers, control, max_tokens=2400):
        payload = json.loads(history[1]["content"])
        self.requests.append(payload)
        self.before_response()
        source = next((source for source in payload["sources"] if source["role"] == "user"), None)
        output = self.output
        if output is None:
            output = json.dumps({"items": [{"kind": "open_thread", "text": "下次继续讨论通勤问题",
                                           "source_message_ids": [source["id"]]}] if source else payload["previous"]})
        return httpx.Response(200, request=httpx.Request("POST", "https://test.invalid/v1/chat/completions"),
                              json={"choices": [{"message": {"content": output}, "finish_reason": self.finish_reason}]})


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.service = Service(self.directory)
        with closing(self.service.connect()) as db, db:
            db.executescript("""CREATE TABLE conversations(id TEXT PRIMARY KEY, character_id TEXT);
                CREATE TABLE messages(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    role TEXT NOT NULL,content TEXT NOT NULL,status TEXT NOT NULL,reply_to TEXT REFERENCES messages(id),
                    text_finalized INTEGER NOT NULL DEFAULT 0,attempt INTEGER NOT NULL DEFAULT 1,error_code TEXT,
                    finished_at TEXT,created_at TEXT NOT NULL DEFAULT '2026-11-23T10:00:00Z');""")
        self.store = SummaryStore(self.service)
        with closing(self.service.connect()) as db:
            self.store.migrate(db)
        self.conversation = self.new_conversation()
        self.control = SimpleNamespace(check=lambda: None)

    def tearDown(self):
        self.temp.cleanup()

    def new_conversation(self, character=None):
        value = str(uuid4())
        with closing(self.service.connect()) as db, db:
            db.execute("INSERT INTO conversations VALUES(?,?)", (value, character))
        return value

    def add_pairs(self, count, conversation=None, question="我在考虑换工作，下次聊通勤。", status="complete", **fields):
        conversation = conversation or self.conversation
        pairs = []
        with closing(self.service.connect()) as db, db:
            for _ in range(count):
                user, assistant = str(uuid4()), str(uuid4())
                db.execute("INSERT INTO messages(id,conversation_id,role,content,status) VALUES(?,?,'user',?,'complete')",
                           (user, conversation, question))
                db.execute("""INSERT INTO messages(id,conversation_id,role,content,status,reply_to,text_finalized,
                    error_code,finished_at) VALUES(?,?,'assistant','先比较一下通勤。',?,?,?,?,'2026-11-23T10:00:01Z')""",
                           (assistant, conversation, status, user, fields.get("text_finalized", 1), fields.get("error_code")))
                pairs.append((user, assistant))
        return pairs

    def task(self, conversation=None):
        conversation = conversation or self.conversation
        user = str(uuid4())
        with closing(self.service.connect()) as db, db:
            db.execute("INSERT INTO messages(id,conversation_id,role,content,status) VALUES(?,?,'user','接着聊','complete')",
                       (user, conversation))
        return {"conversation_id": conversation, "reply_to": user}

    def generate(self, conversation=None):
        value = conversation or self.conversation
        self.store.refresh(value)
        while self.store.next_pending():
            self.store.run(self.store.next_pending(), self.control)
        return self.store.get(value)

    def assert_http(self, status, callback):
        with self.assertRaises(HTTPException) as caught:
            callback()
        self.assertEqual(caught.exception.status_code, status)

    def test_first_twenty_and_disjoint_recent_context(self):
        pairs = self.add_pairs(19)
        self.store.note(pairs[-1][1])
        self.assertIsNone(self.store.next_pending())
        self.assertEqual(len(self.store.context(self.task())[1]), 10)
        pairs += self.add_pairs(1)
        self.store.note(pairs[-1][1])
        self.assertEqual(self.store.next_pending(), self.conversation)
        self.store.run(self.conversation, self.control)
        summary = self.store.get(self.conversation)
        self.assertEqual(summary["covered_until_assistant_id"], pairs[9][1])
        self.assertEqual(summary["covered_pair_count"], 10)
        self.assertEqual(summary["items"][0]["sources"][0]["role"], "user")
        items, recent = self.store.context(self.task())
        self.assertEqual(items[0]["source_message_ids"], [pairs[0][0]])
        self.assertEqual([pair["assistant_id"] for pair in recent], [pair[1] for pair in pairs[10:]])
        self.assertEqual(len(self.service.requests[0]["sources"]), 20)
        more = self.add_pairs(10)
        self.store.note(more[-1][1])
        self.store.run(self.conversation, self.control)
        self.assertEqual(self.store.get(self.conversation)["covered_pair_count"], 20)
        self.assertEqual(self.service.requests[-1]["previous"], items)

    def test_failures_preserve_revision_and_cursor_then_retry(self):
        pairs = self.add_pairs(30)
        first = self.generate()
        self.add_pairs(10)
        self.service.output = "not JSON"
        self.store.refresh(self.conversation)
        with self.assertRaises(ValidationError) as caught:
            self.store.run(self.conversation, self.control)
        self.store.failed(self.conversation, caught.exception)
        failed = self.store.get(self.conversation)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["current_revision_id"], first["current_revision_id"])
        self.assertEqual(failed["covered_until_assistant_id"], first["covered_until_assistant_id"])
        self.assertEqual(len(self.store.context(self.task())[1]), 20)
        self.store.note(pairs[-1][1])
        self.assertIsNone(self.store.next_pending())
        self.service.output = None
        self.assertEqual(self.generate()["covered_pair_count"], 30)

    def test_input_budget_catches_up_without_skipping_pairs(self):
        self.add_pairs(40, question="通" * 3800)
        summary = self.generate()
        self.assertEqual(summary["covered_pair_count"], 30)
        self.assertGreater(len(self.service.requests), 2)
        seen = [source["id"] for payload in self.service.requests for source in payload["sources"]]
        self.assertEqual(len(seen), 60)
        self.assertEqual(len(set(seen)), 60)
        self.assertTrue(all(len(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) <= 12000
                            for payload in self.service.requests))

    def test_oversized_whole_pair_fails_without_advancing(self):
        self.add_pairs(20, question="字" * 12000)
        self.store.refresh(self.conversation)
        with self.assertRaisesRegex(ValueError, "input budget"):
            self.store.run(self.conversation, self.control)
        self.assertIsNone(self.store.get(self.conversation)["current_revision_id"])
        self.assertEqual(self.service.requests, [])

    def test_manual_versions_conflicts_clear_and_rollback(self):
        self.add_pairs(20)
        first = self.generate()
        item = {key: first["items"][0][key] for key in ("kind", "text", "source_message_ids")}
        item["text"] = "用户想继续比较通勤时间"
        edit = SummaryEdit(base_revision_id=first["current_revision_id"], items=[item])
        second = self.store.edit(self.conversation, edit)
        self.assertEqual(second["items"][0]["text"], item["text"])
        self.assert_http(409, lambda: self.store.edit(self.conversation, edit))
        clear = self.store.clear(self.conversation, SummaryBase(base_revision_id=second["current_revision_id"]))
        self.assertEqual(clear["items"], [])
        more = self.add_pairs(10)
        self.store.note(more[-1][1])
        self.assertIsNone(self.store.next_pending())
        restored = self.store.rollback(self.conversation, SummaryRollback(base_revision_id=clear["current_revision_id"],
                                                                          target_revision_id=first["current_revision_id"]))
        self.assertEqual(restored["items"], first["items"])
        revisions = self.store.revisions(self.conversation)["items"]
        self.assertEqual([revision["origin"] for revision in revisions], ["rollback", "clear", "manual", "auto"])
        self.assertEqual(revisions[0]["parent_id"], clear["current_revision_id"])

    def test_late_result_cannot_overwrite_manual_clear(self):
        self.add_pairs(20)
        self.store.refresh(self.conversation)
        self.service.before_response = lambda: self.store.clear(self.conversation, SummaryBase())
        self.store.run(self.conversation, self.control)
        result = self.store.get(self.conversation)
        self.assertEqual(result["origin"], "clear")
        self.assertEqual(result["status"], "idle")
        self.assertEqual(len(self.store.revisions(self.conversation)["items"]), 1)

    def test_cross_conversation_model_sources_and_revisions_rejected(self):
        self.add_pairs(20)
        other = self.new_conversation()
        other_pairs = self.add_pairs(20, other)
        other_summary = self.generate(other)
        self.service.output = json.dumps({"items": [{"kind": "user_statement", "text": "伪造", "source_message_ids": [other_pairs[0][0]]}]})
        self.store.refresh(self.conversation)
        with self.assertRaisesRegex(ValueError, "invalid summary source"):
            self.store.run(self.conversation, self.control)
        self.assertIsNone(self.store.get(self.conversation)["current_revision_id"])
        self.assert_http(404, lambda: self.store.rollback(self.conversation, SummaryRollback(target_revision_id=other_summary["current_revision_id"])))
        self.assert_http(409, lambda: self.store.edit(self.conversation, SummaryEdit(items=[{"kind": "user_statement", "text": "伪造", "source_message_ids": [other_pairs[0][0]]}])))

    def test_speaker_misattribution_and_truncation_rejected(self):
        pairs = self.add_pairs(20)
        self.store.refresh(self.conversation)
        self.service.output = json.dumps({"items": [{"kind": "user_statement", "text": "把人物的建议当事实", "source_message_ids": [pairs[0][1]]}]})
        with self.assertRaisesRegex(ValueError, "speaker mismatch"):
            self.store.run(self.conversation, self.control)
        self.store.failed(self.conversation, ValueError())
        self.service.output, self.service.finish_reason = None, "length"
        self.store.refresh(self.conversation)
        with self.assertRaisesRegex(ValueError, "truncated"):
            self.store.run(self.conversation, self.control)
        self.assertIsNone(self.store.get(self.conversation)["current_revision_id"])

    def test_cancelled_incomplete_and_audio_retry_are_not_new_pairs(self):
        self.add_pairs(3, status="interrupted", error_code="interrupted")
        self.add_pairs(3, status="audio_failed", error_code="cancelled")
        self.add_pairs(3, status="audio_failed", text_finalized=0)
        eligible = self.add_pairs(20, status="audio_failed")
        self.store.note(eligible[-1][1])
        first = self.generate()
        self.assertEqual(first["total_pair_count"], 20)
        with closing(self.service.connect()) as db, db:
            db.execute("UPDATE messages SET attempt=attempt+1 WHERE id=?", (eligible[-1][1],))
        self.store.note(eligible[-1][1])
        self.assertEqual(self.store.get(self.conversation)["total_pair_count"], 20)
        self.assertEqual(len(self.service.requests), 1)

    def test_changed_source_invalidates_summary_and_inflight_result(self):
        pairs = self.add_pairs(20)
        first = self.generate()
        with closing(self.service.connect()) as db, db:
            db.execute("UPDATE messages SET content='我已经不考虑换工作了' WHERE id=?", (pairs[0][0],))
        invalid = self.store.get(self.conversation)
        self.assertFalse(invalid["valid"])
        self.assertEqual(invalid["items"], [])
        self.assertEqual(len(self.store.context(self.task())[1]), 20)
        self.assert_http(409, lambda: self.store.rollback(self.conversation, SummaryRollback(base_revision_id=first["current_revision_id"], target_revision_id=first["current_revision_id"])))
        self.store.refresh(self.conversation)
        def change_source():
            with closing(self.service.connect()) as db, db:
                db.execute("UPDATE messages SET content='再次纠正' WHERE id=?", (pairs[0][0],))
        self.service.before_response = change_source
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.store.run(self.conversation, self.control)
        self.assertEqual(self.store.get(self.conversation)["current_revision_id"], first["current_revision_id"])

    def test_deleted_memory_source_never_returns_through_rollback_or_generation(self):
        pairs = self.add_pairs(20)
        first = self.generate()
        with self.store.lock, closing(self.service.connect()) as db, db:
            self.store.exclude_source(db, pairs[0][0])
        filtered = self.store.get(self.conversation)
        self.assertEqual(filtered["items"], [])
        self.assertIn(pairs[0][0], json.loads(filtered["excluded_source_ids"]))
        self.assertIn(pairs[0][1], json.loads(filtered["excluded_source_ids"]))
        self.assert_http(409, lambda: self.store.rollback(self.conversation, SummaryRollback(base_revision_id=filtered["current_revision_id"], target_revision_id=first["current_revision_id"])))
        self.store.clear(self.conversation, SummaryBase(base_revision_id=filtered["current_revision_id"]))
        self.generate()
        source_ids = {source["id"] for source in self.service.requests[-1]["sources"]}
        self.assertNotIn(pairs[0][0], source_ids)
        self.assertNotIn(pairs[0][1], source_ids)

    def test_unreferenced_covered_correction_invalidates_summary_and_late_result(self):
        pairs = self.add_pairs(20)
        first = self.generate()
        self.assertEqual(first["items"][0]["source_message_ids"], [pairs[0][0]])
        with closing(self.service.connect()) as db, db:
            db.execute("UPDATE messages SET content='我改主意了，不再讨论通勤' WHERE id=?", (pairs[4][0],))
        self.assertFalse(self.store.get(self.conversation)["valid"])
        self.assertEqual(self.store.context(self.task())[0], [])
        self.assert_http(409, lambda: self.store.rollback(self.conversation, SummaryRollback(
            base_revision_id=first["current_revision_id"], target_revision_id=first["current_revision_id"])))
        self.service.before_response = lambda: None
        second = self.generate()
        self.add_pairs(10)
        self.store.refresh(self.conversation)
        def earlier_correction():
            with closing(self.service.connect()) as db, db:
                db.execute("UPDATE messages SET content='已经离职' WHERE id=?", (pairs[5][0],))
        self.service.before_response = earlier_correction
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.store.run(self.conversation, self.control)
        self.assertEqual(self.store.get(self.conversation)["current_revision_id"], second["current_revision_id"])

    def test_excluding_memory_source_during_generation_discards_late_result(self):
        pairs = self.add_pairs(20)
        self.store.refresh(self.conversation)
        def exclude_during_request():
            with self.store.lock, closing(self.service.connect()) as db, db:
                self.store.exclude_source(db, pairs[0][0])
        self.service.before_response = exclude_during_request
        self.store.run(self.conversation, self.control)
        result = self.store.get(self.conversation)
        self.assertEqual(result["items"], [])
        self.assertIsNone(result["current_revision_id"])
        self.assertEqual(result["status"], "idle")

    def test_memory_version_change_discards_inflight_without_cursor_advance(self):
        character_conversation = self.new_conversation(str(uuid4()))
        self.add_pairs(20, character_conversation)
        self.service.before_response = lambda: setattr(self.service, "memory_version", 1)
        self.store.refresh(character_conversation)
        self.store.run(character_conversation, self.control)
        result = self.store.get(character_conversation)
        self.assertIsNone(result["current_revision_id"])
        self.assertEqual(result["status"], "failed")

    def test_migration_backup_and_restart_preserve_data(self):
        self.add_pairs(20)
        first = self.generate()
        with closing(self.service.connect()) as db, db:
            db.execute("UPDATE conversation_summary_state SET status='updating' WHERE conversation_id=?", (self.conversation,))
        with closing(self.service.connect()) as db:
            self.store.migrate(db)
        result = self.store.get(self.conversation)
        self.assertEqual(result["current_revision_id"], first["current_revision_id"])
        self.assertEqual(result["items"], first["items"])
        self.assertEqual(result["status"], "failed")
        backups = list(self.directory.glob("before-conversation-summary-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='messages'").fetchone())
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='conversation_summary_state'").fetchone())

    def test_summary_validation_budgets_and_revision_pagination(self):
        source = str(uuid4())
        with self.assertRaises(ValidationError):
            SummaryItems(items=[{"kind": "user_statement", "text": "长" * 160, "source_message_ids": [source]}] * 8)
        self.add_pairs(20)
        summary = self.generate()
        for _ in range(21):
            summary = self.store.clear(self.conversation, SummaryBase(base_revision_id=summary["current_revision_id"]))
        first = self.store.revisions(self.conversation)
        second = self.store.revisions(self.conversation, first["next_offset"])
        self.assertEqual(len(first["items"]), 20)
        self.assertEqual(len(second["items"]), 2)
        self.assertIsNone(second["next_offset"])


class SummaryAPITests(unittest.TestCase):
    add_pairs = SummaryTests.add_pairs

    def setUp(self):
        from test_voice_app import Engine, create_app, wav_bytes
        self.temp = tempfile.TemporaryDirectory()
        self.histories = []
        def reply(history):
            self.histories.append(history)
            return "我们继续比较通勤。", ""
        self.app = create_app(Path(self.temp.name) / "data", Engine(), reply)
        self.service = self.app.state.service
        self.store = self.service.summaries
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()
        self.wait(lambda: self.service.model_status == "ready")
        voice = self.client.post("/api/voices", data={"name": "测试声音", "transcript": "这是测试录音"},
                                 files={"recording": ("test.wav", wav_bytes(), "audio/wav")}).json()
        self.conversation = self.client.post("/api/conversations", json={"voice_id": voice["id"]}).json()["id"]
        self.endpoint = f"/api/conversations/{self.conversation}/summary"
        self.service.base_url = "https://test.invalid/v1"
        async def request(history, headers, control, max_tokens=2400):
            payload = json.loads(history[1]["content"])
            source = next(source for source in payload["sources"] if source["role"] == "user")
            return httpx.Response(200, request=httpx.Request("POST", "https://test.invalid/v1/chat/completions"),
                json={"choices": [{"message": {"content": json.dumps({"items": [{"kind": "open_thread", "text": "用户还想聊通勤问题", "source_message_ids": [source["id"]]}]})}, "finish_reason": "stop"}]})
        self.patcher = patch.object(self.service, "request_chat", request)
        self.patcher.start()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.patcher.stop()
        self.temp.cleanup()

    def wait(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail("timed out")

    def test_summary_routes_shared_worker_and_chat_context(self):
        self.add_pairs(20)
        self.assertEqual(self.client.post(self.endpoint + "/refresh").status_code, 202)
        self.wait(lambda: not self.service.characters.gate.locked())
        response = self.client.get(self.endpoint)
        self.assertEqual(response.status_code, 200, response.text)
        summary = response.json()
        self.assertEqual(summary["covered_pair_count"], 10)
        item = {key: summary["items"][0][key] for key in ("kind", "text", "source_message_ids")}
        item["text"] = "用户希望下次比较实际通勤时间"
        edited = self.client.put(self.endpoint, json={"base_revision_id": summary["current_revision_id"], "items": [item]})
        self.assertEqual(edited.status_code, 200, edited.text)
        result = self.client.post(f"/api/conversations/{self.conversation}/messages", json={"content": "继续讨论通勤", "mode": "realtime"})
        self.assertEqual(result.status_code, 202, result.text)
        self.wait(lambda: not self.service.gate.locked())
        self.assertIn(item["text"], self.histories[-1][0]["content"])
        self.assertEqual(len(self.histories[-1]), 22)
        self.assertEqual(self.client.request("DELETE", self.endpoint, json={"base_revision_id": summary["current_revision_id"]}).status_code, 409)
        cleared = self.client.request("DELETE", self.endpoint, json={"base_revision_id": edited.json()["current_revision_id"]})
        self.assertEqual(cleared.status_code, 200, cleared.text)
        rollback = self.client.post(self.endpoint + "/rollback", json={"base_revision_id": cleared.json()["current_revision_id"], "target_revision_id": summary["current_revision_id"]})
        self.assertEqual(rollback.status_code, 200, rollback.text)
        self.assertEqual(len(self.client.get(self.endpoint + "/revisions").json()["items"]), 4)
        self.assertEqual(self.client.get("/api/conversations/not-a-uuid/summary").status_code, 422)

    def test_reply_finish_queues_summary_for_pure_voice_conversation(self):
        self.add_pairs(19)
        result = self.client.post(f"/api/conversations/{self.conversation}/messages", json={"content": "接着聊通勤"})
        self.assertEqual(result.status_code, 202, result.text)
        self.wait(lambda: not self.service.gate.locked())
        self.wait(lambda: not self.service.characters.gate.locked())
        summary = self.client.get(self.endpoint).json()
        self.assertEqual(summary["covered_pair_count"], 10)
        self.assertEqual(summary["total_pair_count"], 20)
        self.assertEqual(self.service.rows("SELECT * FROM memory_turns"), [])


if __name__ == "__main__":
    unittest.main()
