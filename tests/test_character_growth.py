"""Profile suggestions, review races, snapshots and migration with fake providers."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from test_voice_app import Engine, create_app, wav_bytes


class GrowthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "data"
        self.histories, self.requests = [], []
        def reply(history):
            self.histories.append(history)
            return "先说结论，稍后再解释。", ""
        self.app = create_app(self.directory, Engine(), reply)
        self.service = self.app.state.service
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()
        self.wait(lambda: self.service.model_status == "ready")
        self.voice = self.client.post("/api/voices", data={"name": "测试声音", "transcript": "测试录音"},
            files={"recording": ("test.wav", wav_bytes(), "audio/wav")}).json()
        self.character = self.client.post("/api/characters", json={"name": "小林", "voice_id": self.voice["id"],
            "personality": "温和", "speech_style": "自然交谈"}).json()
        self.path = "/api/characters/" + self.character["id"]
        self.service.base_url = "https://test.invalid/v1"
        self.output, self.finish_reason = None, "stop"
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        async def request(history, headers, control, max_tokens=2000):
            payload = json.loads(history[1]["content"])
            self.requests.append(payload)
            self.entered.set()
            while not self.release.is_set():
                control.check()
                await asyncio.sleep(.005)
            output = self.output
            if output is None:
                first = payload["sources"][0]
                second = next(source for source in payload["sources"] if source["conversation_id"] != first["conversation_id"])
                output = json.dumps({"items": [{"field": "speech_style", "new_value": "先给结论，少追问，必要时再解释。",
                    "reason": "用户在多次对话中要求先说结论。", "source_message_ids": [first["source_id"], second["source_id"]]}]}, ensure_ascii=False)
            return httpx.Response(200, request=httpx.Request("POST", self.service.base_url),
                json={"choices": [{"message": {"content": output}, "finish_reason": self.finish_reason}]})
        self.patcher = patch.object(self.service, "request_chat", request)
        self.patcher.start()

    def tearDown(self):
        self.release.set()
        self.client.__exit__(None, None, None)
        self.patcher.stop()
        self.temp.cleanup()

    def wait(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.005)
        self.fail("timed out")

    def conversation(self):
        return self.client.post("/api/conversations", json={"character_id": self.character["id"]}).json()

    def seed(self, count=20, first_count=10):
        conversations = [self.conversation(), self.conversation()]
        sources = []
        with closing(self.service.connect()) as db, db:
            for index in range(count):
                conversation = conversations[0 if index < first_count else 1]["id"]
                user, assistant = str(uuid4()), str(uuid4())
                db.execute("INSERT INTO messages(id,conversation_id,role,content,status) VALUES(?,?,'user',?,'complete')",
                           (user, conversation, "请先给结论，别每次都追问。"))
                db.execute("""INSERT INTO messages(id,conversation_id,role,content,status,reply_to,text_finalized,
                    attempt,finished_at) VALUES(?,?,'assistant','好的，先说重点。','complete',?,1,1,'2026-11-23T10:00:00Z')""",
                           (assistant, conversation, user))
                db.execute("INSERT INTO memory_turns(character_id,assistant_id,source_id) VALUES(?,?,?)",
                           (self.character["id"], assistant, user))
                sources.append(user)
            db.execute("UPDATE characters SET memory_cursor=(SELECT max(seq) FROM memory_turns) WHERE id=?", (self.character["id"],))
        return conversations, sources

    def refresh(self):
        response = self.client.post(self.path + "/profile-candidates/refresh")
        self.assertEqual(response.status_code, 202, response.text)
        self.wait(lambda: not self.service.characters.gate.locked())
        return self.client.get(self.path + "/profile-candidates").json()

    def approve(self, candidate, **fields):
        return self.client.post(self.path + "/profile-candidates/" + candidate["id"], json={"action": "approve",
            "review_token": candidate["review_token"], "base_revision_id": candidate["base_revision_id"], **fields})

    def test_reviewed_growth_versions_snapshots_and_idempotency(self):
        old, _ = self.seed()
        item, = self.refresh()["items"]
        self.assertEqual(self.client.get(self.path).json()["speech_style"], "自然交谈")
        task = self.client.post("/api/conversations/" + old[0]["id"] + "/messages", json={"content": "继续", "mode": "realtime"}).json()
        self.wait(lambda: not self.service.gate.locked())
        self.assertNotIn(item["new_value"], self.histories[-1][0]["content"])
        self.assertEqual(self.approve(item, new_value="先给结论，再按需展开。").status_code, 200)
        grown = self.client.get(self.path).json()
        self.assertEqual(grown["speech_style"], "先给结论，再按需展开。")
        self.assertNotEqual(grown["profile_revision_id"], self.character["profile_revision_id"])
        new = self.conversation()
        self.assertEqual(new["character_revision_id"], grown["profile_revision_id"])
        self.assertEqual(old[0]["character_revision_id"], self.character["profile_revision_id"])
        history = self.client.get(self.path + "/profile-revisions").json()["items"]
        self.assertEqual([version["origin"] for version in history], ["growth", "manual"])
        self.assertEqual({source["source_id"] for source in history[0]["sources"]},
                         {source["source_id"] for source in item["sources"]})
        self.assertEqual(self.approve(item).status_code, 200)
        self.assertEqual(len(self.client.get(self.path + "/profile-revisions").json()["items"]), 2)
        rollback = self.client.post(self.path + "/profile-rollback", json={"base_revision_id": grown["profile_revision_id"],
            "target_revision_id": self.character["profile_revision_id"]})
        self.assertEqual(rollback.status_code, 200, rollback.text)
        self.assertEqual(rollback.json()["speech_style"], "自然交谈")
        self.assertEqual(json.loads(new["character_snapshot"])["speech_style"], grown["speech_style"])
        self.assertTrue(self.service.one("messages", task["id"])["text_finalized"])

    def test_conflict_review_rotates_token_preserves_new_value(self):
        self.seed()
        item, = self.refresh()["items"]
        body = {key: self.character[key] for key in ("name", "voice_id", "personality", "speech_style")}
        body.update(speech_style="更直接", base_revision_id=self.character["profile_revision_id"])
        self.assertEqual(self.client.put(self.path, json=body).status_code, 200)
        self.assertEqual(self.client.put(self.path, json=body).status_code, 409)
        self.assertEqual(self.approve(item).status_code, 409)
        refreshed = self.client.post(self.path + "/profile-candidates/" + item["id"], json={"action": "review"})
        reviewed, = refreshed.json()["items"]
        self.assertEqual(reviewed["old_value"], "更直接")
        self.assertEqual(reviewed["new_value"], item["new_value"])
        self.assertNotEqual(reviewed["review_token"], item["review_token"])
        self.assertEqual(self.approve(item).status_code, 409)
        self.assertEqual(self.approve(reviewed).status_code, 200)

    def test_requires_twenty_turns_two_sessions_and_handles_long_first_session(self):
        self.seed(20, 20)
        self.assertEqual(self.refresh()["items"], [])
        self.assertEqual(self.requests, [])
        # A later session must unblock a long initial conversation.
        self.seed(1, 0)
        item, = self.refresh()["items"]
        self.assertEqual(len(self.requests[0]["sources"]), 20)
        self.assertEqual(len({source["conversation_id"] for source in item["sources"]}), 2)

    def test_sources_whitelist_truncation_and_failure_cursor_retry(self):
        self.seed()
        invalid = {"field": "background", "new_value": "凭空改身份", "reason": "无依据",
                   "source_message_ids": [str(uuid4()), str(uuid4())]}
        for item in (invalid, {**invalid, "field": "personality"}):
            self.output = json.dumps({"items": [item]})
            self.assertEqual(self.refresh()["status"], "failed")
            self.assertEqual(self.client.get(self.path).json()["growth_cursor"], 0)
        self.output = None
        self.finish_reason = "length"
        self.assertEqual(self.refresh()["status"], "failed")
        self.finish_reason = "stop"
        candidate, = self.refresh()["items"]
        self.assertGreater(self.client.get(self.path).json()["growth_cursor"], 0)
        other = self.client.post("/api/characters", json={"name": "另一个", "voice_id": self.voice["id"]}).json()
        self.assertEqual(self.client.post(f"/api/characters/{other['id']}/profile-candidates/{candidate['id']}",
            json={"action": "reject"}).status_code, 404)
        self.assertEqual(self.client.post(f"/api/characters/{other['id']}/profile-rollback", json={
            "base_revision_id": other["profile_revision_id"], "target_revision_id": self.character["profile_revision_id"]}).status_code, 404)

    def test_profile_or_memory_edit_during_generation_discards_result(self):
        self.seed()
        self.release.clear()
        self.client.post(self.path + "/profile-candidates/refresh")
        self.assertTrue(self.entered.wait(2))
        self.client.post(self.path + "/memories", json={"key": "交流偏好", "content": "简短回答"})
        self.release.set()
        self.wait(lambda: not self.service.characters.gate.locked())
        state = self.client.get(self.path + "/profile-candidates").json()
        self.assertEqual(state["status"], "warning")
        self.assertEqual(state["items"], [])
        self.assertEqual(self.client.get(self.path).json()["growth_cursor"], 0)
        self.assertEqual(len(self.refresh()["items"]), 1)

    def test_changed_sources_and_rejected_candidate_do_not_apply(self):
        self.seed()
        candidate, = self.refresh()["items"]
        source = candidate["sources"][0]
        with closing(self.service.connect()) as db, db:
            db.execute("UPDATE messages SET attempt=attempt+1 WHERE id=?", (source["assistant_id"],))
        self.assertEqual(self.approve(candidate).status_code, 409)
        url = self.path + "/profile-candidates/" + candidate["id"]
        self.assertEqual(self.client.post(url, json={"action": "review"}).status_code, 409)
        self.assertEqual(self.client.post(url, json={"action": "reject"}).status_code, 200)
        self.assertEqual(self.approve(candidate).status_code, 409)
        self.assertEqual(self.client.get(self.path).json()["speech_style"], "自然交谈")

    def test_candidate_and_cursor_commit_atomically(self):
        self.seed()
        with closing(self.service.connect()) as db, db:
            db.execute("""CREATE TRIGGER fail_profile_write BEFORE INSERT ON character_profile_candidates
                BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        self.assertEqual(self.refresh()["status"], "failed")
        self.assertEqual(self.client.get(self.path).json()["growth_cursor"], 0)
        with closing(self.service.connect()) as db, db:
            db.execute("DROP TRIGGER fail_profile_write")
        self.assertEqual(len(self.refresh()["items"]), 1)

    def test_stale_cancel_attempt_does_not_cancel_new_reply(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service.engine.synthesize
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)
        conversation = self.conversation()
        with patch.object(self.service.engine, "synthesize", blocked):
            task = self.client.post("/api/conversations/" + conversation["id"] + "/messages", json={"content": "你好", "mode": "realtime"}).json()
            try:
                self.assertTrue(entered.wait(2))
                response = self.client.post(f"/api/messages/{task['id']}/cancel?attempt={task['attempt'] - 1}")
                self.assertEqual(response.status_code, 409)
                self.assertFalse(self.service.active.cancel.is_set())
                self.assertEqual(self.client.post(f"/api/messages/{task['id']}/cancel?attempt={task['attempt']}").status_code, 202)
            finally:
                release.set()
            self.wait(lambda: not self.service.gate.locked())
        self.assertEqual(self.service.one("messages", task["id"])["status"], "interrupted")

    def test_migration_preserves_legacy_snapshots_and_rolls_back_on_failure(self):
        old_conversation = self.conversation()
        old = Path(self.temp.name) / "legacy"
        old.mkdir()
        with closing(self.service.connect()) as live, closing(sqlite3.connect(old / "chat.sqlite3")) as db:
            live.backup(db)
            db.execute("ALTER TABLE conversations DROP COLUMN character_revision_id")
            for column in ("profile_revision_id", "growth_cursor", "growth_status", "growth_error"):
                db.execute("ALTER TABLE characters DROP COLUMN " + column)
            db.execute("DROP TABLE character_profile_revisions")
            db.execute("DROP TABLE character_profile_candidates")
            db.commit()
        failed_app = create_app(old, Engine(), lambda history: ("测试", ""))
        failed_service = failed_app.state.service
        class Fault(sqlite3.Connection):
            def execute(self, sql, *args):
                if "ADD COLUMN growth_cursor" in sql:
                    raise sqlite3.OperationalError("test migration failure")
                return super().execute(sql, *args)
        def connect():
            db = sqlite3.connect(failed_service.db, factory=Fault)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            return db
        with patch.object(failed_service, "connect", connect), self.assertRaises(sqlite3.OperationalError):
            failed_service.start()
        with closing(sqlite3.connect(failed_service.db)) as db:
            self.assertNotIn("profile_revision_id", {row[1] for row in db.execute("PRAGMA table_info(characters)")})
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='character_profile_revisions'").fetchone())
        with TestClient(create_app(old, Engine(), lambda history: ("测试", "")), base_url="http://127.0.0.1") as client:
            character = client.get(self.path).json()
            conversation, = client.get("/api/conversations").json()
            self.assertEqual(conversation["character_snapshot"], old_conversation["character_snapshot"])
            self.assertIsNone(conversation["character_revision_id"])
            self.assertTrue(character["profile_revision_id"])
            self.assertEqual(client.get(self.path + "/profile-revisions").json()["items"][0]["origin"], "migration")
        self.assertEqual(len(list(old.glob("before-profile-history-*.sqlite3"))), 2)

    def test_background_jobs_share_one_worker_and_coalesce_requests(self):
        conversations, _ = self.seed(40, 20)
        kinds, concurrent = [], []
        active = 0
        entered, release = threading.Event(), threading.Event()
        async def request(history, headers, control, max_tokens=2000):
            nonlocal active
            payload = json.loads(history[1]["content"])
            kind = "profile" if "profile" in payload else "summary" if "previous" in payload else "memory"
            active += 1
            concurrent.append(active)
            kinds.append(kind)
            try:
                if kind == "memory":
                    entered.set()
                    while not release.is_set():
                        control.check()
                        await asyncio.sleep(.005)
                return httpx.Response(200, request=httpx.Request("POST", self.service.base_url),
                    json={"choices": [{"message": {"content": '{"items":[]}'}, "finish_reason": "stop"}]})
            finally:
                active -= 1
        with closing(self.service.connect()) as db, db:
            db.execute("UPDATE characters SET memory_cursor=0 WHERE id=?", (self.character["id"],))
        with patch.object(self.service, "request_chat", request):
            self.client.post(self.path + "/memories/refresh")
            try:
                self.assertTrue(entered.wait(2))
                for _ in range(10):
                    self.assertEqual(self.client.post("/api/conversations/" + conversations[0]["id"] + "/summary/refresh").status_code, 202)
                    self.assertEqual(self.client.post(self.path + "/profile-candidates/refresh").status_code, 202)
                self.assertEqual(self.client.get(self.path + "/profile-candidates").json()["status"], "queued")
            finally:
                release.set()
            self.wait(lambda: not self.service.characters.gate.locked())
        self.assertEqual(kinds, ["memory", "summary", "profile"])
        self.assertEqual(max(concurrent), 1)
        summary = self.client.get("/api/conversations/" + conversations[0]["id"] + "/summary").json()
        self.assertEqual(summary["covered_pair_count"], 10)
        self.assertGreater(self.client.get(self.path).json()["growth_cursor"], 0)


if __name__ == "__main__":
    unittest.main()
