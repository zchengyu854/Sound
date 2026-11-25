"""Character snapshots, memory isolation and extraction lifecycle tests."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from test_voice_app import Engine, wav_bytes, create_app


class CharacterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)/"data"
        self.engine = Engine()
        self.histories = []
        def reply(history):
            self.histories.append(history)
            return "你好，这是测试。", ""
        self.reply = reply
        self.app = create_app(self.directory, self.engine, reply)
        self.service = self.app.state.service
        self.store = self.service.characters
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()
        self.wait(lambda: self.service.model_status == "ready")
        response = self.client.post('/api/voices', data={'name':'声音','transcript':'测试录音'},
            files={'recording':('test.wav',wav_bytes(),'audio/wav')})
        self.voice = response.json()
        self.service.base_url = 'https://test.invalid/v1'
        self.requests = []
        self.response = None
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        async def request(history, headers, control, max_tokens=256):
            self.requests.append(history)
            self.entered.set()
            while not self.release.is_set():
                control.check()
                await asyncio.sleep(.01)
            control.check()
            sources = json.loads(history[1]['content'])['sources']
            content = self.response if self.response is not None else json.dumps({'items':[
                {'key':'饮食偏好', 'content':'喜欢清淡的食物', 'source_id':sources[0]['source_id']}]},ensure_ascii=False)
            return httpx.Response(200, request=httpx.Request('POST','https://test.invalid/v1/chat/completions'),
                json={'choices':[{'message':{'content':content},'finish_reason':'stop'}]})
        self.patcher = patch.object(self.service, 'request_chat', request)
        self.patcher.start()

    def tearDown(self):
        self.release.set()
        self.client.__exit__(None,None,None)
        self.patcher.stop()
        self.temp.cleanup()

    def wait(self, predicate):
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            if predicate(): return
            time.sleep(.01)
        self.fail('timed out')

    def character(self, name='小林', **fields):
        body={'name':name,'voice_id':self.voice['id'],'personality':'温和但有主见',**fields}
        response=self.client.post('/api/characters',json=body)
        self.assertEqual(response.status_code,201,response.text)
        return response.json()

    def conversation(self, character):
        response=self.client.post('/api/conversations',json={'character_id':character['id']})
        self.assertEqual(response.status_code,201,response.text)
        return response.json()

    def send(self, conversation, text='我喜欢清淡的食物。', mode='standard'):
        response=self.client.post(f"/api/conversations/{conversation['id']}/messages",json={'content':text,'mode':mode})
        self.assertEqual(response.status_code,202,response.text)
        self.wait(lambda:not self.service.gate.locked())
        return self.client.get('/api/messages/'+response.json()['id']).json()

    def memory(self, character):
        return self.client.get(f"/api/characters/{character['id']}/memories").json()

    def candidates(self, character):
        response = self.client.get(f"/api/characters/{character['id']}/memory-candidates")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['items']

    def review(self, character, candidate, action='approve', **fields):
        if action == 'approve':
            fields.setdefault('review_token', candidate['review_token'])
        return self.client.post(f"/api/characters/{character['id']}/memory-candidates/{candidate['id']}",
                                json={'action': action, **fields})

    def manual(self, character, key='饮食偏好',content='喜欢辣的食物'):
        return self.client.post(f"/api/characters/{character['id']}/memories",json={'key':key,'content':content})

    def refresh(self, character):
        response=self.client.post(f"/api/characters/{character['id']}/memories/refresh")
        self.assertEqual(response.status_code,202,response.text)
        self.wait(lambda:not self.store.gate.locked())
        return self.memory(character)

    def test_profile_validation_and_legacy_conversation(self):
        self.assertEqual(self.client.post('/api/characters',json={'name':' ','voice_id':self.voice['id']}).status_code,422)
        self.assertEqual(self.client.post('/api/characters',json={'name':'test','voice_id':str(uuid4())}).status_code,404)
        self.assertEqual(self.client.post('/api/characters',json={'name':'test','voice_id':self.voice['id'],'examples':[{'user':'hi','assistant':'hi'}]*4}).status_code,422)
        self.assertEqual(self.client.post('/api/conversations',json={}).status_code,422)
        character=self.character()
        self.assertEqual(self.client.post('/api/conversations',json={'voice_id':self.voice['id'],'character_id':character['id']}).status_code,422)
        old=self.client.post('/api/conversations',json={'voice_id':self.voice['id']}).json()
        self.assertIsNone(old['character_id'])
        self.send(old)
        self.assertNotIn('character_data',self.histories[-1][0]['content'])

    def test_snapshot_and_voice_are_immutable(self):
        character=self.character(background='海边摄影师', examples=[{'user':'累了','assistant':'先坐下，慢慢说。'}])
        old=self.conversation(character)
        second_voice=self.client.post('/api/voices',data={'name':'新声音','transcript':'测试录音'},
            files={'recording':('second.wav',wav_bytes(),'audio/wav')}).json()
        self.client.put('/api/characters/'+character['id'],json={'name':'新的名字','voice_id':second_voice['id'],'personality':'直接果断'})
        new=self.conversation(character)
        self.assertEqual(old['voice_id'],self.voice['id'])
        self.assertEqual(new['voice_id'],second_voice['id'])
        self.assertEqual(json.loads(old['character_snapshot'])['name'],'小林')
        self.assertEqual(json.loads(new['character_snapshot'])['name'],'新的名字')
        self.send(old)
        self.assertIn('海边摄影师',self.histories[-1][0]['content'])
        self.assertNotIn('直接果断',self.histories[-1][0]['content'])
        self.assertEqual(self.engine.calls[-1][1].stem,old['voice_id'])

    def test_memory_isolation_and_cross_session(self):
        first,second=self.character(),self.character('另外一个人物')
        self.manual(first)
        self.send(self.conversation(first), mode='realtime')
        self.assertIn('喜欢辣的食物',self.histories[-1][0]['content'])
        self.send(self.conversation(second), mode='realtime')
        self.assertNotIn('喜欢辣的食物',self.histories[-1][0]['content'])
        voice_only = self.client.post('/api/conversations', json={'voice_id': self.voice['id']}).json()
        self.send(voice_only, mode='realtime')
        self.assertNotIn('<character_data>', self.histories[-1][0]['content'])
        self.assertEqual(len(self.service.rows('SELECT * FROM memory_turns')), 2)
        item=self.memory(first)['items'][0]
        self.assertEqual(self.client.delete(f"/api/characters/{second['id']}/memories/{item['id']}").status_code,404)
        self.assertEqual(self.memory(second)['items'],[])

    def test_every_three_turns_and_audio_retry_does_not_count(self):
        character=self.character(); conversation=self.conversation(character)
        self.send(conversation)
        self.send(self.conversation(character), mode='realtime')
        self.assertEqual(self.requests,[])
        self.engine.fail=True
        task=self.send(conversation, mode='realtime')
        self.wait(lambda:not self.store.gate.locked())
        self.assertEqual(task['status'],'audio_failed')
        self.assertEqual(len(self.requests),1)
        self.assertEqual(self.memory(character)['items'], [])
        candidate = self.candidates(character)[0]
        self.assertEqual(candidate['source_text'], '我喜欢清淡的食物。')
        self.assertTrue(candidate['source_created_at'])
        self.assertEqual(self.review(character, candidate).status_code, 200)
        item = self.memory(character)['items'][0]
        self.assertEqual((item['origin'], item['source_id']), ('auto', candidate['source_id']))
        self.engine.fail=False
        self.client.post('/api/messages/'+task['id']+'/retry')
        self.wait(lambda:not self.service.gate.locked())
        self.assertEqual(len(self.requests),1)
        self.assertEqual(len(self.service.rows('SELECT * FROM memory_turns')),3)
        self.assertNotIn('assistant',json.dumps(json.loads(self.requests[0][1]['content'])['sources']))

    def test_manual_refresh_invalid_json_source_and_recovery(self):
        character=self.character(); self.send(self.conversation(character))
        self.response='not json'
        self.assertEqual(self.refresh(character)['status'],'failed')
        self.assertEqual(self.store.get(character['id'])['memory_cursor'],0)
        self.response=json.dumps({'items':[{'key':'bad','content':'bad','source_id':str(uuid4())}]})
        self.assertEqual(self.refresh(character)['status'],'failed')
        self.response=None
        self.assertEqual(self.refresh(character)['items'], [])
        self.assertEqual(len(self.candidates(character)), 1)
        count=len(self.requests); self.refresh(character)
        self.assertEqual(len(self.requests),count)

    def test_candidate_batch_and_cursor_roll_back_together(self):
        character = self.character()
        task = self.send(self.conversation(character))
        self.response = json.dumps({'items': [
            {'key': '地点', 'content': '靠海', 'source_id': task['reply_to']},
            {'key': '时间', 'content': '下午', 'source_id': task['reply_to']},
        ]})
        with closing(self.service.connect()) as db, db:
            db.execute("""CREATE TRIGGER reject_candidate_for_test BEFORE INSERT ON memory_candidates
                WHEN NEW.key='时间' BEGIN SELECT RAISE(ABORT, 'test write failure'); END""")
        self.assertEqual(self.refresh(character)['status'], 'failed')
        self.assertEqual(self.store.get(character['id'])['memory_cursor'], 0)
        self.assertEqual(self.candidates(character), [])
        with closing(self.service.connect()) as db, db:
            db.execute('DROP TRIGGER reject_candidate_for_test')
        self.assertEqual(self.refresh(character)['status'], 'idle')
        self.assertEqual(len(self.candidates(character)), 2)

    def test_memory_edits_win_and_deleted_data_not_restored(self):
        character=self.character(); self.send(self.conversation(character))
        self.manual(character)
        self.release.clear()
        self.client.post(f"/api/characters/{character['id']}/memories/refresh")
        self.assertTrue(self.entered.wait(2))
        item=self.memory(character)['items'][0]
        self.client.delete(f"/api/characters/{character['id']}/memories/{item['id']}")
        self.release.set(); self.wait(lambda:not self.store.gate.locked())
        result=self.memory(character)
        self.assertEqual(result['items'],[])
        self.assertEqual(self.candidates(character), [])
        self.assertEqual(result['status'],'warning')
        self.assertGreater(self.store.get(character['id'])['memory_cursor'],0)
        count=len(self.requests);self.refresh(character)
        self.assertEqual(len(self.requests),count)
        self.send(self.conversation(character), '继续聊天', mode='realtime')
        self.assertNotIn(item['content'], self.histories[-1][0]['content'])

    def test_deleting_a_fact_also_removes_its_pending_update(self):
        character = self.character()
        self.manual(character)
        self.send(self.conversation(character))
        self.refresh(character)
        candidate, = self.candidates(character)
        item, = self.memory(character)['items']
        url = f"/api/characters/{character['id']}/memories/{item['id']}"
        self.assertEqual(self.client.put(url, json={'key': '新的主题', 'content': '新的内容'}).status_code, 200)
        self.assertEqual(self.client.delete(url).status_code, 200)
        self.assertEqual(self.candidates(character), [])
        self.assertEqual(self.review(character, candidate).status_code, 404)

    def test_manual_conflicts_capacity_and_duplicates(self):
        character=self.character();self.send(self.conversation(character))
        self.manual(character)
        result=self.refresh(character)
        self.assertEqual(result['status'],'idle')
        self.assertEqual(result['items'][0]['content'],'喜欢辣的食物')
        self.assertEqual(self.candidates(character)[0]['old_content'],'喜欢辣的食物')
        self.assertEqual(self.manual(character).status_code,409)
        for i in range(29):self.assertEqual(self.manual(character,str(i),str(i)).status_code,201)
        self.assertEqual(self.manual(character,'overflow','overflow').status_code,409)
        self.send(self.conversation(character))
        turn=self.service.rows('SELECT source_id FROM memory_turns ORDER BY seq DESC')[0]
        self.response=json.dumps({'items':[{'key':'overflow','content':'overflow','source_id':turn['source_id']}]})
        result=self.refresh(character)
        self.assertEqual(len(result['items']),30)
        candidate = next(item for item in self.candidates(character) if item['key'] == 'overflow')
        self.assertEqual(self.review(character, candidate).status_code, 409)
        self.assertEqual(self.review(character, candidate, 'reject').status_code, 200)
        self.assertNotIn('overflow', {item['key'] for item in self.candidates(character)})

    def test_correction_requires_review_and_candidates_stay_out_of_chat(self):
        character=self.character();conversation=self.conversation(character)
        other = self.character('另一个人物')
        self.manual(character)
        first = self.memory(character)['items'][0]
        correction = '我现在改吃清淡，不再吃辣。'
        self.send(conversation, correction)
        self.assertEqual(self.histories[-1][-1]['content'], correction)
        self.assertIn('本轮以用户的新说法为准', self.histories[-1][0]['content'])
        source=self.service.rows('SELECT source_id FROM memory_turns ORDER BY seq DESC')[0]['source_id']
        content = '改为偏爱清淡口味'
        self.response=json.dumps({'items':[{'key':'饮食偏好','content':content,'source_id':source},
                                         {'key':'重复主题','content':content,'source_id':source}]})
        self.refresh(character)
        candidate, = self.candidates(character)
        self.assertEqual(candidate['old_content'], first['content'])
        self.send(self.conversation(character), '聊聊晚饭', mode='realtime')
        self.assertNotIn(content, self.histories[-1][0]['content'])
        self.assertIn(first['content'], self.histories[-1][0]['content'])
        self.assertEqual(self.candidates(other), [])
        self.assertEqual(self.review(other, candidate).status_code, 404)
        url = f"/api/characters/{character['id']}/memories/{first['id']}"
        self.assertEqual(self.client.put(url, json={'key': first['key'], 'content': '暂时吃微辣'}).status_code, 200)
        self.assertEqual(self.review(character, candidate).status_code, 409)
        refreshed, = self.review(character, candidate, 'review').json()['items']
        self.assertEqual(refreshed['old_content'], '暂时吃微辣')
        self.assertEqual(self.review(character, candidate).status_code, 409)
        self.assertEqual(self.review(character, refreshed, content='偏好清淡').status_code, 200)
        item, = self.memory(character)['items']
        self.assertEqual((item['id'], item['content'], item['source_id']), (first['id'], '偏好清淡', source))
        self.send(self.conversation(character), '继续聊天')
        self.assertIn('偏好清淡', self.histories[-1][0]['content'])
        self.assertNotIn(first['content'], self.histories[-1][0]['content'])

    def test_cancelled_replies_never_register(self):
        character = self.character()
        conversation = self.conversation(character)
        entered, release = threading.Event(), threading.Event()
        original = self.engine.synthesize

        def blocked(*args, **kwargs):
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)

        for mode in ('standard', 'realtime'):
            entered.clear()
            release.clear()
            with patch.object(self.engine, 'synthesize', side_effect=blocked):
                task = self.client.post(f"/api/conversations/{conversation['id']}/messages",
                                        json={'content': '正在说话', 'mode': mode}).json()
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(self.client.post(f"/api/messages/{task['id']}/cancel").json()['cancel_requested'])
                finally:
                    release.set()
                self.wait(lambda: not self.service.gate.locked())
            saved = self.service.one('messages', task['id'])
            self.assertTrue(saved['text_finalized'])
            self.assertEqual(saved['error_code'], 'interrupted' if mode == 'realtime' else 'cancelled')
        self.assertEqual(self.service.rows('SELECT * FROM memory_turns'), [])

    def test_global_memory_gate_and_chat_does_not_wait(self):
        first,second=self.character(),self.character('second')
        self.send(self.conversation(first));self.send(self.conversation(second))
        self.release.clear()
        self.client.post(f"/api/characters/{first['id']}/memories/refresh")
        self.assertTrue(self.entered.wait(2))
        self.assertEqual(self.client.post(f"/api/characters/{second['id']}/memories/refresh").status_code,409)
        task=self.send(self.conversation(second))
        self.assertEqual(task['status'],'complete')
        self.assertTrue(self.store.gate.locked())
        self.release.set();self.wait(lambda:not self.store.gate.locked())

    def test_memory_cancellation_and_restart_cursor(self):
        character=self.character();self.send(self.conversation(character))
        self.release.clear()
        self.client.post(f"/api/characters/{character['id']}/memories/refresh")
        self.assertTrue(self.entered.wait(2))
        self.store.active.cancel.set()
        self.wait(lambda:not self.store.gate.locked())
        self.assertEqual(self.memory(character)['status'],'failed')
        self.release.set();self.refresh(character)
        self.assertEqual(self.review(character, self.candidates(character)[0]).status_code, 200)
        cursor=self.store.get(character['id'])['memory_cursor']
        self.client.__exit__(None,None,None)
        self.patcher.stop()
        self.app=create_app(self.directory,Engine(),self.reply)
        self.service=self.app.state.service;self.store=self.service.characters
        self.client=TestClient(self.app,base_url='http://127.0.0.1');self.client.__enter__()
        self.assertEqual(self.store.get(character['id'])['memory_cursor'],cursor)
        self.assertEqual(len(self.memory(character)['items']),1)

    def test_twenty_turn_batch_limit(self):
        character=self.character();conversation=self.conversation(character)
        with patch.object(self.store,'trigger'), patch.object(self.service.summaries, 'note'):
            for _ in range(23):self.send(conversation)
        self.refresh(character)
        self.assertEqual(len(json.loads(self.requests[-1][1]['content'])['sources']),20)
        self.refresh(character)
        self.assertEqual(len(json.loads(self.requests[-1][1]['content'])['sources']),3)

    def test_memory_http_timeout_and_shutdown(self):
        from voice_app import TaskControl
        character=self.character();self.send(self.conversation(character))
        self.patcher.stop()
        entered,cancelled=threading.Event(),threading.Event()
        async def handler(request):
            entered.set()
            payload=json.loads(request.content)
            self.assertFalse(payload['stream'])
            self.assertEqual(payload['max_tokens'],2000)
            try:await asyncio.sleep(10)
            finally:cancelled.set()
        client_class=httpx.AsyncClient
        with patch('voice_app.httpx.AsyncClient',side_effect=lambda **kw:client_class(transport=httpx.MockTransport(handler),**kw)):
            with patch.object(self.store,'control_type',lambda value,attempt,budget:TaskControl(value,attempt,.04)):
                self.assertEqual(self.refresh(character)['status'],'failed')
                self.assertTrue(cancelled.is_set())
            entered.clear();cancelled.clear()
            self.client.post(f"/api/characters/{character['id']}/memories/refresh")
            self.assertTrue(entered.wait(2))
            self.store.stop()
            self.assertTrue(cancelled.is_set())
            self.assertFalse(self.store.gate.locked())
        self.patcher.start()

    def test_memory_failure_cannot_fail_chat(self):
        character=self.character()
        with patch.object(self.store,'_note',side_effect=sqlite3.OperationalError('private-error')):
            task=self.send(self.conversation(character))
        self.assertEqual(task['status'],'complete')

    def test_retried_cancelled_sources_cannot_be_extracted_or_approved(self):
        character = self.character()
        task = self.send(self.conversation(character))
        self.refresh(character)
        candidate, = self.candidates(character)
        with closing(self.service.connect()) as db, db:
            db.execute("UPDATE messages SET status='interrupted',error_code='interrupted',attempt=attempt+1 WHERE id=?", (task['id'],))
            db.execute("UPDATE characters SET memory_cursor=0 WHERE id=?", (character['id'],))
        count = len(self.requests)
        self.refresh(character)
        self.assertEqual(len(self.requests), count)
        self.assertEqual(self.review(character, candidate).status_code, 409)
        self.assertEqual(self.review(character, candidate, 'review').status_code, 409)
        self.assertEqual(self.review(character, candidate, 'reject').status_code, 200)

    def test_source_retry_during_extraction_preserves_cursor(self):
        character = self.character()
        task = self.send(self.conversation(character))
        self.release.clear()
        self.client.post(f"/api/characters/{character['id']}/memories/refresh")
        self.assertTrue(self.entered.wait(2))
        with closing(self.service.connect()) as db, db:
            db.execute("UPDATE messages SET attempt=attempt+1 WHERE id=?", (task['id'],))
        self.release.set()
        self.wait(lambda: not self.store.gate.locked())
        self.assertEqual(self.candidates(character), [])
        self.assertEqual(self.store.get(character['id'])['memory_cursor'], 0)
        self.assertEqual(self.memory(character)['status'], 'failed')

    def test_character_migration_backup_and_rollback(self):
        old=Path(self.temp.name)/'old'
        shutil.copytree(self.directory,old)
        with closing(sqlite3.connect(old/'chat.sqlite3')) as db:
            db.execute('ALTER TABLE conversations DROP COLUMN character_snapshot')
            db.execute('ALTER TABLE conversations DROP COLUMN character_id')
            db.execute('ALTER TABLE conversations DROP COLUMN character_revision_id')
            db.execute('DROP TABLE character_profile_revisions')
            db.execute('DROP TABLE character_profile_candidates')
            for table in ('memory_candidates','memory_turns','memories','characters'):db.execute('DROP TABLE '+table)
            db.commit()
        for backup in old.glob('before-characters-*.sqlite3'):backup.unlink()
        app=create_app(old,Engine(),self.reply);service=app.state.service
        class Fault(sqlite3.Connection):
            def execute(self,sql,*args):
                if 'ADD COLUMN character_snapshot' in sql:raise sqlite3.OperationalError('migration fault')
                return super().execute(sql,*args)
        def connect():
            db=sqlite3.connect(service.db,factory=Fault);db.row_factory=sqlite3.Row;return db
        with patch.object(service,'connect',connect):
            with self.assertRaises(sqlite3.OperationalError):service.start()
        with closing(sqlite3.connect(service.db)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='characters'").fetchone())
            self.assertNotIn('character_id',{r[1] for r in db.execute('PRAGMA table_info(conversations)')})
        with TestClient(create_app(old,Engine(),self.reply),base_url='http://127.0.0.1') as client:
            self.assertEqual(len(client.get('/api/voices').json()),1)
        backups=list(old.glob('before-characters-*.sqlite3'))
        self.assertEqual(len(backups),2)
        for path in backups:
            with closing(sqlite3.connect(path)) as backup:
                self.assertEqual(backup.execute('SELECT count(*) FROM voices').fetchone()[0],1)

    def test_review_migration_preserves_confirmed_history_and_source(self):
        character = self.character()
        task = self.send(self.conversation(character))
        self.refresh(character)
        self.assertEqual(self.review(character, self.candidates(character)[0]).status_code, 200)
        old = Path(self.temp.name) / 'pre-review'
        old.mkdir()
        with closing(self.service.connect()) as live, closing(sqlite3.connect(old / 'chat.sqlite3')) as db:
            live.backup(db)
            db.execute('DROP TABLE memory_candidates')
            db.execute('ALTER TABLE memories DROP COLUMN revision')
            db.commit()
        app = create_app(old, Engine(), self.reply)
        with TestClient(app, base_url='http://127.0.0.1') as client:
            url = f"/api/characters/{character['id']}"
            item, = client.get(f'{url}/memories').json()['items']
            self.assertEqual(item['source_id'], task['reply_to'])
            self.assertEqual(item['content'], '喜欢清淡的食物')
            self.assertEqual(client.get(f'{url}/memory-candidates').json()['items'], [])
        backup, = old.glob('before-memory-review-*.sqlite3')
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute('SELECT source_id FROM memories').fetchone()[0], task['reply_to'])


if __name__=='__main__':unittest.main()
