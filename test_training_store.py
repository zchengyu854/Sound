"""Training persistence regression tests; no inference or private data required."""
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from training_store import SCHEMAS, TrainingStore, canonical


class Chat:
    def __init__(self, directory):
        self.directory = Path(directory)
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE voices(id TEXT PRIMARY KEY)')

    def connect(self):
        db = sqlite3.connect(self.directory / 'chat.sqlite3')
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db


class TrainingStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.chat = Chat(self.temp.name)
        self.store = TrainingStore(self.chat)
        self.store.start()
        self.dataset = self.store.create_dataset('角色', True)
        self.did = self.dataset['id']

    def source(self, duration=10):
        return self.store.add_source(self.did, str(uuid4()), 'mp3', 100, {'duration': duration})

    def segment(self, source=None, start=0, end=3, text='原文', approved=True):
        source = source or self.source()
        segment = self.store.add_segment(self.did, source['id'], str(uuid4()), start, end, text)
        path = self.store.segment_path(segment)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'test audio')
        if approved:
            segment = self.store.edit_segment(segment['id'], review_status='approved')
        return segment

    def test_backup_is_before_migration_and_restart_does_not_repeat(self):
        backups = list(self.chat.directory.glob('before-training-*.sqlite3'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertNotIn('model_id', [row[1] for row in db.execute('PRAGMA table_info(voices)')])
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='training_jobs'").fetchone())
        self.store.start()
        self.assertEqual(list(self.chat.directory.glob('before-training-*.sqlite3')), backups)
        with closing(self.chat.connect()) as db:
            self.assertIn('model_id', [row[1] for row in db.execute('PRAGMA table_info(voices)')])

    def test_migration_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            chat = Chat(directory)
            store = TrainingStore(chat)
            with patch.dict(SCHEMAS, {'voice_models': 'invalid SQL ('}):
                with self.assertRaises(sqlite3.OperationalError):
                    store.start()
            with closing(chat.connect()) as db:
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='training_datasets'").fetchone())
            self.assertEqual(len(list(Path(directory).glob('before-training-*.sqlite3'))), 1)

    def test_review_duration_versions_and_json(self):
        source = self.source()
        self.assertEqual(source['metadata']['audio'], 0)
        segment = self.segment(source, approved=False, text='')
        with self.assertRaises(HTTPException):
            self.store.edit_segment(segment['id'], review_status='approved')
        version = self.store.one('training_datasets', self.did)['version']
        self.store.edit_segment(segment['id'], transcript=' 新原文 ', review_status='approved')
        dataset = self.store.one('training_datasets', self.did)
        self.assertEqual(dataset['version'], version + 1)
        self.assertEqual(dataset['approved_duration'], 3)
        self.assertIs(dataset['speaker_confirmed'], True)
        self.store.edit_segment(segment['id'], review_status='excluded')
        self.assertEqual(self.store.one('training_datasets', self.did)['approved_duration'], 0)
        self.assertEqual(self.store.segments(self.did)[0]['quality_hints'], [])
        self.assertEqual(self.store.segments(self.did, offset=1), [])

    def test_source_limit_rollback_and_ownership(self):
        self.source(7200)
        version = self.store.one('training_datasets', self.did)['version']
        with self.assertRaises(HTTPException):
            self.source(1)
        self.assertEqual(self.store.one('training_datasets', self.did)['version'], version)
        self.assertEqual(len(self.store.rows('training_sources')), 1)
        other = self.store.create_dataset('其他')
        source = self.store.rows('training_sources')[0]
        with self.assertRaises(HTTPException):
            self.store.add_segment(other['id'], source['id'], str(uuid4()), 0, 3)
        self.assertEqual(self.store.segments(self.did), [])

    def test_snapshot_freeze_digest_idempotency(self):
        segment = self.segment()
        key = str(uuid4())
        job = self.store.create_job(self.did, 'train', {'rank': 4}, key)
        snapshot = job['snapshot']
        self.assertEqual(snapshot['segments'][0]['audio_sha256'], hashlib.sha256(b'test audio').hexdigest())
        self.assertEqual(job['digest'], hashlib.sha256(canonical(snapshot).encode()).hexdigest())
        self.assertTrue(snapshot['validation_warning'])
        self.store.edit_segment(segment['id'], transcript='更改')
        self.store.segment_path(segment).write_bytes(b'changed')
        self.assertEqual(self.store.one('training_jobs', job['id'])['snapshot'], snapshot)
        self.assertEqual(self.store.create_job(self.did, 'train', {'rank': 4}, key)['id'], job['id'])
        with self.assertRaises(HTTPException) as error:
            self.store.create_job(self.did, 'train', {'rank': 8}, key)
        self.assertEqual(error.exception.status_code, 409)
        new = self.store.create_job(self.did, 'train', {'rank': 4})
        self.assertNotEqual(job['digest'], new['digest'])
        with self.assertRaises(HTTPException):
            self.store.update('training_jobs', job['id'], snapshot={})

    def test_train_gates_and_prepare(self):
        with self.assertRaises(HTTPException):
            self.store.create_job(self.did, 'train', {})
        segment = self.segment()
        self.store.update('training_datasets', self.did, speaker_confirmed=False)
        with self.assertRaises(HTTPException):
            self.store.create_job(self.did, 'train', {})
        self.store.update('training_datasets', self.did, speaker_confirmed=True)
        self.store.segment_path(segment).unlink()
        with self.assertRaises(HTTPException):
            self.store.create_job(self.did, 'train', {})
        self.assertEqual(self.store.rows('training_jobs'), [])
        job = self.store.create_job(self.did, 'prepare', {})
        self.assertIsNone(job['snapshot'])
        self.assertEqual((job['status'], job['attempt']), ('queued', 1))

    def test_validation_source_and_time_splits(self):
        source = self.source(30)
        self.segment(source, 0, 3)
        self.segment(source, 3, 6)
        adjacent = self.store.create_job(self.did, 'train', {})['snapshot']
        self.assertEqual(adjacent['validation_indices'], [])
        self.segment(source, 12, 15)
        split = self.store.create_job(self.did, 'train', {})['snapshot']
        self.assertTrue(split['validation_indices'])
        self.assertIsNone(split['validation_warning'])
        self.segment(self.source())
        split = self.store.create_job(self.did, 'train', {})['snapshot']
        train_sources = {split['segments'][i]['source_id'] for i in split['train_indices']}
        validation_sources = {split['segments'][i]['source_id'] for i in split['validation_indices']}
        self.assertFalse(train_sources & validation_sources)

    def test_restart_marks_active_only_no_replay(self):
        active = self.store.create_job(self.did, 'prepare', {})
        completed = self.store.create_job(self.did, 'prepare', {})
        self.store.update('training_jobs', completed['id'], status='completed')
        self.store.start()
        self.assertEqual(self.store.one('training_jobs', active['id'])['error_code'], 'interrupted')
        self.assertEqual(self.store.one('training_jobs', active['id'])['status'], 'failed')
        self.assertEqual(self.store.one('training_jobs', completed['id'])['status'], 'completed')
        self.assertEqual(len(self.store.rows('training_jobs')), 2)

    def test_uuid_queries_and_field_injection(self):
        for bad in ('../escape', self.did.upper(), str(uuid4()).replace('-', '')):
            with self.assertRaises(HTTPException):
                self.store.dataset_dir(bad)
        with self.assertRaises(HTTPException):
            self.store.one('voices', self.did)
        with self.assertRaises(HTTPException):
            self.store.rows('training_datasets', 'id=?; DROP TABLE voices', (self.did,))
        with self.assertRaises(HTTPException):
            self.store.update('training_datasets', self.did, **{'name=?;--': 'bad'})
        with self.assertRaises(HTTPException):
            self.store.create_job(self.did, 'prepare', {}, 'not-uuid')
        rows = self.store.rows('training_datasets', 'WHERE id=?', (self.did,))
        self.assertEqual(rows[0]['id'], self.did)

    def test_symlink_escape_dataset_segment_and_root(self):
        segment = self.segment()
        with tempfile.TemporaryDirectory() as outside:
            path = self.store.segment_path(segment)
            path.unlink()
            path.symlink_to(Path(outside) / 'audio.wav')
            with self.assertRaises(HTTPException):
                self.store.segment_path(segment)
            escaped_id = str(uuid4())
            (self.store.root / escaped_id).symlink_to(outside, target_is_directory=True)
            with self.assertRaises(HTTPException):
                self.store.dataset_dir(escaped_id)
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            chat = Chat(directory)
            (Path(directory) / 'training').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(HTTPException):
                TrainingStore(chat).dataset_dir(str(uuid4()))


if __name__ == '__main__':
    unittest.main()
