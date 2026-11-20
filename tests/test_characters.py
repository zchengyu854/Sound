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

    def send(self, conversation, text='我喜欢清淡的食物。'):
        response=self.client.post(f"/api/conversations/{conversation['id']}/messages",json={'content':text})
        self.assertEqual(response.status_code,202,response.text)
        self.wait(lambda:not self.service.gate.locked())
        return self.client.get('/api/messages/'+response.json()['id']).json()

    def memory(self, character):
        return self.client.get(f"/api/characters/{character['id']}/memories").json()

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
        self.send(self.conversation(first))
        self.assertIn('喜欢辣的食物',self.histories[-1][0]['content'])
        self.send(self.conversation(second))
        self.assertNotIn('喜欢辣的食物',self.histories[-1][0]['content'])
        item=self.memory(first)['items'][0]
        self.assertEqual(self.client.delete(f"/api/characters/{second['id']}/memories/{item['id']}").status_code,404)
        self.assertEqual(self.memory(second)['items'],[])

    def test_every_three_turns_and_audio_retry_does_not_count(self):
        character=self.character(); conversation=self.conversation(character)
        for _ in range(2): self.send(conversation)
        self.assertEqual(self.requests,[])
        self.engine.fail=True
        task=self.send(conversation)
        self.wait(lambda:not self.store.gate.locked())
        self.assertEqual(task['status'],'audio_failed')
        self.assertEqual(len(self.requests),1)
        self.assertEqual(self.memory(character)['items'][0]['origin'],'auto')
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
        self.assertEqual(len(self.refresh(character)['items']),1)
        count=len(self.requests); self.refresh(character)
        self.assertEqual(len(self.requests),count)

    def test_memory_edits_win_and_deleted_data_not_restored(self):
        character=self.character(); self.send(self.conversation(character))
        self.release.clear()
        self.client.post(f"/api/characters/{character['id']}/memories/refresh")
        self.assertTrue(self.entered.wait(2))
        self.manual(character)
        item=self.memory(character)['items'][0]
        self.client.delete(f"/api/characters/{character['id']}/memories/{item['id']}")
        self.release.set(); self.wait(lambda:not self.store.gate.locked())
        result=self.memory(character)
        self.assertEqual(result['items'],[])
        self.assertEqual(result['status'],'warning')
        self.assertGreater(self.store.get(character['id'])['memory_cursor'],0)
        count=len(self.requests);self.refresh(character)
        self.assertEqual(len(self.requests),count)

    def test_manual_conflicts_capacity_and_duplicates(self):
        character=self.character();self.send(self.conversation(character))
        self.manual(character)
        result=self.refresh(character)
        self.assertEqual(result['status'],'warning')
        self.assertEqual(result['items'][0]['content'],'喜欢辣的食物')
        self.assertEqual(self.manual(character).status_code,409)
        for i in range(29):self.assertEqual(self.manual(character,str(i),str(i)).status_code,201)
        self.assertEqual(self.manual(character,'overflow','overflow').status_code,409)
        self.send(self.conversation(character))
        turn=self.service.rows('SELECT source_id FROM memory_turns ORDER BY seq DESC')[0]
        self.response=json.dumps({'items':[{'key':'overflow','content':'overflow','source_id':turn['source_id']}]})
        result=self.refresh(character)
        self.assertEqual(result['status'],'warning')
        self.assertEqual(len(result['items']),30)

    def test_auto_updates_and_duplicate_content(self):
        character=self.character();conversation=self.conversation(character)
        self.send(conversation);first=self.refresh(character)['items'][0]
        self.send(conversation)
        source=self.service.rows('SELECT source_id FROM memory_turns ORDER BY seq DESC')[0]['source_id']
        self.response=json.dumps({'items':[{'key':'饮食偏好','content':'现在喜欢辣的食物','source_id':source},
                                         {'key':'重复主题','content':'现在喜欢辣的食物','source_id':source}]})
        result=self.refresh(character)
        self.assertEqual(len(result['items']),1)
        self.assertEqual(result['items'][0]['id'],first['id'])
        self.assertEqual(result['items'][0]['content'],'现在喜欢辣的食物')

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
        with patch.object(self.store,'trigger'):
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

    def test_character_migration_backup_and_rollback(self):
        old=Path(self.temp.name)/'old'
        shutil.copytree(self.directory,old)
        with closing(sqlite3.connect(old/'chat.sqlite3')) as db:
            db.execute('ALTER TABLE conversations DROP COLUMN character_snapshot')
            db.execute('ALTER TABLE conversations DROP COLUMN character_id')
            for table in ('memory_turns','memories','characters'):db.execute('DROP TABLE '+table)
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


if __name__=='__main__':unittest.main()
