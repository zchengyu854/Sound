"""SQLite persistence and immutable manifests for local voice training."""
import hashlib
import json
import math
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from training_exchange import canonical


SCHEMAS = {
    "training_datasets": "id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'new', approved_duration REAL NOT NULL DEFAULT 0, speaker_confirmed INTEGER NOT NULL DEFAULT 0, preferred_reference_id TEXT",
    "training_sources": "id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES training_datasets(id), extension TEXT NOT NULL, size INTEGER NOT NULL, duration REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'uploaded', metadata TEXT NOT NULL DEFAULT '{}'",
    "training_segments": "id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES training_datasets(id), source_id TEXT NOT NULL REFERENCES training_sources(id), start REAL NOT NULL, end REAL NOT NULL, audio_id TEXT NOT NULL, transcript TEXT NOT NULL DEFAULT '', review_status TEXT NOT NULL DEFAULT 'pending', quality_hints TEXT NOT NULL DEFAULT '[]', quality_stats TEXT NOT NULL DEFAULT '{}'",
    "training_jobs": "id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES training_datasets(id), kind TEXT NOT NULL CHECK(kind IN ('prepare','train')), snapshot TEXT, digest TEXT, config TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempt INTEGER NOT NULL DEFAULT 1, stage TEXT NOT NULL DEFAULT 'queued', segments_done INTEGER NOT NULL DEFAULT 0, segments_total INTEGER NOT NULL DEFAULT 0, steps_done INTEGER NOT NULL DEFAULT 0, steps_total INTEGER NOT NULL DEFAULT 0, checkpoint TEXT, error_code TEXT, request_key TEXT, parent_job_id TEXT, created_at TEXT NOT NULL, UNIQUE(dataset_id, request_key)",
    "voice_models": "id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES training_jobs(id), base_model TEXT NOT NULL, config TEXT NOT NULL, reference_segment_id TEXT NOT NULL REFERENCES training_segments(id), created_at TEXT NOT NULL",
}
JSON_FIELDS = {"metadata", "quality_hints", "quality_stats", "snapshot", "config"}
FIELDS = {table: {part.strip().split()[0] for part in schema.split(', ') if not part.startswith('UNIQUE')} for table, schema in SCHEMAS.items()}
IMMUTABLE = {"id", "dataset_id", "source_id", "audio_id", "snapshot", "digest", "config", "request_key",
             "created_at", "kind", "job_id", "parent_job_id", "preferred_reference_id"}


def reference_rank(segment):
    """Sort key for choosing a synthesis reference: lower is better.

    A reference is prompt material, so prefer clean, clearly voiced, longer
    clips with text. The id breaks ties so the same snapshot always selects the
    same reference.
    """
    hints = set(segment.get('quality_hints') or [])
    stats = segment.get('quality_stats') or {}
    penalty = 0
    if 'low_volume_or_silence' in hints:
        penalty += 6
    if 'possible_clipping' in hints:
        penalty += 4
    if 'low_voiced_ratio' in hints:
        penalty += 3
    if 'short_segment' in hints:
        penalty += 2
    if not str(segment.get('transcript') or '').strip():
        penalty += 5
    effective = stats.get('effective_seconds')
    if not isinstance(effective, (int, float)):
        effective = max(0.0, float(segment.get('end', 0)) - float(segment.get('start', 0)))
    edges = (stats.get('trimmed_start_ms') or 0) + (stats.get('trimmed_end_ms') or 0)
    return (penalty, -round(float(effective), 3), round(float(edges), 1), str(segment['id']))


def select_reference(segments, indices, preferred=None):
    """Return (index, source) for the synthesis reference of a training split.

    `preferred` must be an approved segment inside the training subset; extra
    material such as a held-out validation clip is never silently substituted.
    """
    candidates = [index for index in indices if 0 <= index < len(segments)]
    if not candidates:
        return None, 'none'
    if preferred is not None:
        for index in candidates:
            if segments[index]['id'] == preferred:
                return index, 'preferred'
    best = min(candidates, key=lambda index: reference_rank(segments[index]))
    return best, 'auto'


class TrainingStore:
    def __init__(self, chat):
        self.chat = chat
        self.directory = Path(chat.directory)
        self.root = self.directory / 'training'

    @staticmethod
    def _id(value):
        from voice_app import identifier
        return identifier(value)

    @staticmethod
    def _text(value, limit, label):
        from voice_app import clean_text
        return clean_text(value, limit, label)

    @staticmethod
    def _table(table):
        if table not in SCHEMAS:
            raise HTTPException(422, '不支持的数据表。')
        return table

    @staticmethod
    def _decode(row):
        result = dict(row)
        for key in JSON_FIELDS & result.keys():
            if result[key] is not None:
                result[key] = json.loads(result[key])
        if 'speaker_confirmed' in result:
            result['speaker_confirmed'] = bool(result['speaker_confirmed'])
        return result

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with closing(self.chat.connect()) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {row[1] for row in db.execute('PRAGMA table_info(voices)')}
            job_columns = ({row[1] for row in db.execute('PRAGMA table_info(training_jobs)')}
                           if 'training_jobs' in tables else set())
            dataset_columns = ({row[1] for row in db.execute('PRAGMA table_info(training_datasets)')}
                               if 'training_datasets' in tables else set())
            segment_columns = ({row[1] for row in db.execute('PRAGMA table_info(training_segments)')}
                               if 'training_segments' in tables else set())
            migration = (not set(SCHEMAS).issubset(tables) or 'model_id' not in columns
                         or ('training_jobs' in tables and 'parent_job_id' not in job_columns)
                         or ('training_datasets' in tables and 'preferred_reference_id' not in dataset_columns)
                         or ('training_segments' in tables and 'quality_stats' not in segment_columns))
            if migration:
                with closing(sqlite3.connect(self.directory / f'before-training-{uuid4()}.sqlite3')) as backup:
                    db.backup(backup)
            try:
                db.execute('BEGIN IMMEDIATE')
                for table, schema in SCHEMAS.items():
                    db.execute(f'CREATE TABLE IF NOT EXISTS {table} ({schema})')
                if 'model_id' not in columns:
                    db.execute('ALTER TABLE voices ADD COLUMN model_id TEXT REFERENCES voice_models(id)')
                if 'training_jobs' in tables and 'parent_job_id' not in job_columns:
                    db.execute('ALTER TABLE training_jobs ADD COLUMN parent_job_id TEXT')
                if 'training_datasets' in tables and 'preferred_reference_id' not in dataset_columns:
                    db.execute('ALTER TABLE training_datasets ADD COLUMN preferred_reference_id TEXT')
                if 'training_segments' in tables and 'quality_stats' not in segment_columns:
                    db.execute("ALTER TABLE training_segments ADD COLUMN quality_stats TEXT NOT NULL DEFAULT '{}'")
                db.execute("UPDATE training_jobs SET status='failed', stage='interrupted', error_code='interrupted' WHERE status IN ('queued','preparing','transcribing','training','validating','cancelling')")
                db.commit()
            except Exception:
                db.rollback()
                raise

    def _one(self, db, table, value):
        self._table(table)
        row = db.execute(f'SELECT * FROM {table} WHERE id=?', (self._id(value),)).fetchone()
        if row is None:
            raise HTTPException(404, '找不到训练记录。')
        return self._decode(row)

    def one(self, table, id):
        with closing(self.chat.connect()) as db:
            return self._one(db, table, id)

    def rows(self, table, where='', parameters=()):
        self._table(table)
        # Only a small, parameterized predicate grammar is accepted, never SQL fragments.
        if where:
            predicate = where.strip()
            if predicate.upper().startswith('WHERE '):
                predicate = predicate[6:]
            terms = re.split(r'\s+AND\s+', predicate, flags=re.I)
            for term in terms:
                match = re.fullmatch(r'([a-z_]+)\s*(=|!=|<>|>=|<=|>|<)\s*\?', term.strip())
                if not match or match[1] not in FIELDS[table]:
                    raise HTTPException(422, '无效的查询条件。')
            where = ' WHERE ' + predicate
        with closing(self.chat.connect()) as db:
            return [self._decode(row) for row in db.execute(f'SELECT * FROM {table}{where}', parameters)]

    def datasets(self):
        return self.rows('training_datasets')

    def update(self, table, id, **fields):
        self._table(table)
        if not fields or set(fields) - (FIELDS[table] - IMMUTABLE):
            raise HTTPException(422, '不允许修改这些字段。')
        # Review changes must update the parent dataset atomically.
        if table == 'training_segments':
            if set(fields) <= {'transcript', 'review_status'}:
                return self.edit_segment(id, **fields)
            raise HTTPException(422, '请使用片段审核接口。')
        values = [canonical(value) if key in JSON_FIELDS else value for key, value in fields.items()]
        with closing(self.chat.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            record = self._one(db, table, id)
            if table == 'training_sources' and 'duration' in fields:
                duration = float(fields['duration'])
                if not math.isfinite(duration) or duration < 0:
                    raise HTTPException(422, '素材时长无效。')
                source = self._one(db, table, id)
                total = db.execute('SELECT COALESCE(SUM(duration),0) FROM training_sources WHERE dataset_id=? AND id!=?', (source['dataset_id'], id)).fetchone()[0]
                if total + duration > 7200:
                    raise HTTPException(422, '数据集不能超过两小时。')
            db.execute(f"UPDATE {table} SET {', '.join(key + '=?' for key in fields)} WHERE id=?", (*values, self._id(id)))
            if table == 'training_sources':
                db.execute('UPDATE training_datasets SET version=version+1 WHERE id=?', (record['dataset_id'],))
            elif table == 'training_datasets':
                db.execute('UPDATE training_datasets SET version=version+1 WHERE id=?', (id,))
            return self._one(db, table, id)

    def create_dataset(self, name, speaker_confirmed=False):
        name = self._text(name, 200, '数据集名称')
        value = str(uuid4())
        with closing(self.chat.connect()) as db, db:
            db.execute('INSERT INTO training_datasets(id,name,speaker_confirmed) VALUES(?,?,?)', (value, name, bool(speaker_confirmed)))
            return self._one(db, 'training_datasets', value)

    def dataset_dir(self, id):
        value = self._id(id)
        root = self.root.resolve()
        if not root.is_relative_to(self.directory.resolve()):
            raise HTTPException(422, '训练目录不安全。')
        path = (root / value).resolve()
        if not path.is_relative_to(root):
            raise HTTPException(422, '训练目录不安全。')
        return path

    def segment_path(self, segment):
        root = self.dataset_dir(segment['dataset_id'])
        path = (root / 'segments' / (self._id(segment['audio_id']) + '.wav')).resolve()
        if not path.is_relative_to(root):
            raise HTTPException(422, '片段路径不安全。')
        return path

    def add_source(self, dataset_id, source_id, extension, size, metadata):
        self._id(source_id)
        extension = extension.lower().lstrip('.')
        if extension not in {'mp3', 'mp4', 'wav'} or not isinstance(size, int) or size < 0:
            raise HTTPException(422, '素材格式或大小无效。')
        metadata = dict(metadata or {})
        metadata.setdefault('audio', 0)
        duration = float(metadata.get('duration', 0))
        if not math.isfinite(duration) or duration < 0:
            raise HTTPException(422, '素材时长无效。')
        with closing(self.chat.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._one(db, 'training_datasets', dataset_id)
            total = db.execute('SELECT COALESCE(SUM(duration),0) FROM training_sources WHERE dataset_id=?', (dataset_id,)).fetchone()[0]
            if total + duration > 7200:
                raise HTTPException(422, '数据集不能超过两小时。')
            db.execute('INSERT INTO training_sources(id,dataset_id,extension,size,duration,metadata) VALUES(?,?,?,?,?,?)', (source_id, dataset_id, extension, size, duration, canonical(metadata)))
            db.execute('UPDATE training_datasets SET version=version+1 WHERE id=?', (dataset_id,))
            return self._one(db, 'training_sources', source_id)

    def add_segment(self, dataset_id, source_id, segment_id, start, end, transcript='', quality_hints=None,
                    quality_stats=None):
        self._id(segment_id)
        start, end = float(start), float(end)
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise HTTPException(422, '片段时间无效。')
        if quality_stats is not None and not isinstance(quality_stats, dict):
            raise HTTPException(422, '片段质量统计无效。')
        transcript = self._text(transcript, 20000, '转写') if transcript.strip() else ''
        with closing(self.chat.connect()) as db, db:
            source = self._one(db, 'training_sources', source_id)
            if source['dataset_id'] != self._id(dataset_id):
                raise HTTPException(422, '素材不属于此数据集。')
            db.execute('INSERT INTO training_segments(id,dataset_id,source_id,start,end,audio_id,transcript,quality_hints,quality_stats) VALUES(?,?,?,?,?,?,?,?,?)', (segment_id, dataset_id, source_id, start, end, segment_id, transcript, canonical(quality_hints or []), canonical(quality_stats or {})))
            db.execute('UPDATE training_datasets SET version=version+1 WHERE id=?', (dataset_id,))
            return self._one(db, 'training_segments', segment_id)

    def segments(self, dataset_id, offset=0, limit=50):
        self._id(dataset_id)
        if not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or not 1 <= limit <= 500:
            raise HTTPException(422, '分页参数无效。')
        with closing(self.chat.connect()) as db:
            self._one(db, 'training_datasets', dataset_id)
            return [self._decode(row) for row in db.execute('SELECT * FROM training_segments WHERE dataset_id=? ORDER BY source_id,start,id LIMIT ? OFFSET ?', (dataset_id, limit, offset))]

    def edit_segment(self, id, transcript=None, review_status=None):
        with closing(self.chat.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            segment = self._one(db, 'training_segments', id)
            text = segment['transcript'] if transcript is None else (self._text(transcript, 20000, '转写') if transcript.strip() else '')
            status = review_status if review_status is not None else segment['review_status']
            if status not in {'pending', 'approved', 'excluded'} or (status == 'approved' and not text):
                raise HTTPException(422, '审核通过的片段必须有原文。')
            db.execute('UPDATE training_segments SET transcript=?,review_status=? WHERE id=?', (text, status, id))
            # An excluded clip can no longer be the synthesis reference.
            if status == 'excluded':
                db.execute('UPDATE training_datasets SET preferred_reference_id=NULL WHERE id=? AND preferred_reference_id=?',
                           (segment['dataset_id'], id))
            db.execute("UPDATE training_datasets SET version=version+1, approved_duration=(SELECT COALESCE(SUM(end-start),0) FROM training_segments WHERE dataset_id=? AND review_status='approved') WHERE id=?", (segment['dataset_id'], segment['dataset_id']))
            return self._one(db, 'training_segments', id)

    def set_preferred_reference(self, dataset_id, segment_id=None):
        """Record the human choice of synthesis reference for a dataset.

        Only an approved clip of this dataset may be selected; `None` returns to
        the automatic quality-based choice at the next snapshot.
        """
        self._id(dataset_id)
        with closing(self.chat.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._one(db, 'training_datasets', dataset_id)
            if segment_id is not None:
                segment = self._one(db, 'training_segments', segment_id)
                if segment['dataset_id'] != self._id(dataset_id):
                    raise HTTPException(422, '片段不属于此数据集。')
                if segment['review_status'] != 'approved':
                    raise HTTPException(422, '只有已确认的片段可以作为试听参考。')
                segment_id = segment['id']
            db.execute('UPDATE training_datasets SET preferred_reference_id=?, version=version+1 WHERE id=?',
                       (segment_id, dataset_id))
            return self._one(db, 'training_datasets', dataset_id)

    def create_job(self, dataset_id, kind, config, request_key=None):
        if kind not in {'prepare', 'train'} or not isinstance(config, dict):
            raise HTTPException(422, '任务类型或配置无效。')
        encoded_config = canonical(config)
        if request_key is not None:
            self._id(request_key)
        with closing(self.chat.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            dataset = self._one(db, 'training_datasets', dataset_id)
            if request_key is not None:
                old = db.execute('SELECT * FROM training_jobs WHERE dataset_id=? AND request_key=?', (dataset_id, request_key)).fetchone()
                if old:
                    old = self._decode(old)
                    if old['kind'] != kind or canonical(old['config']) != encoded_config:
                        raise HTTPException(409, '幂等键已用于其他配置。')
                    return old
            snapshot = None
            digest = None
            if kind == 'train':
                segments = [self._decode(row) for row in db.execute("SELECT * FROM training_segments WHERE dataset_id=? AND review_status='approved' ORDER BY source_id,start,id", (dataset_id,))]
                if not dataset['speaker_confirmed'] or not segments:
                    raise HTTPException(422, '请确认单一说话者并审核至少一个片段。')
                for segment in segments:
                    if not segment['transcript'].strip():
                        raise HTTPException(422, '审核片段缺少原文。')
                    path = self.segment_path(segment)
                    if not path.is_file():
                        raise HTTPException(422, '片段音频不存在，请重新处理素材。')
                    with path.open('rb') as audio:
                        segment['audio_sha256'] = hashlib.file_digest(audio, 'sha256').hexdigest()
                sources = {segment['source_id'] for segment in segments}
                validation = []
                training = list(range(len(segments)))
                if len(sources) > 1:
                    validation = [i for i, segment in enumerate(segments) if segment['source_id'] == segments[-1]['source_id']]
                    training = [i for i in training if i not in validation]
                elif len(segments) > 1:
                    candidate = len(segments) - 1
                    training = [i for i, segment in enumerate(segments[:-1]) if segment['end'] + 3 <= segments[candidate]['start']]
                    if training:
                        validation = [candidate]
                    else:
                        training = list(range(len(segments)))
                # A human-chosen reference must train the model: reporting it as
                # the reference while training on a different prompt would make
                # the preview describe a condition the job never used.
                preferred = dataset.get('preferred_reference_id')
                if preferred:
                    for index, segment in enumerate(segments):
                        if segment['id'] == preferred and index in validation:
                            validation = [item for item in validation if item != index]
                            training = sorted(training + [index])
                            break
                reference_index, reference_source = select_reference(segments, training, preferred)
                reference = segments[reference_index] if reference_index is not None else None
                if reference is None:
                    raise HTTPException(422, '没有可用于试听的参考片段。')
                validation_kind = None
                if validation:
                    validation_kind = ('independent_source' if len(sources) > 1
                                       else 'same_recording')
                snapshot = {'dataset_id': dataset_id, 'version': dataset['version'], 'segments': segments,
                            'train_indices': training, 'validation_indices': validation,
                            'validation_kind': validation_kind,
                            'reference_segment_id': reference['id'] if reference else None,
                            'reference_source': reference_source,
                            'validation_warning': None if validation else 'insufficient_independent_validation'}
                digest = hashlib.sha256(canonical(snapshot).encode()).hexdigest()
            value = str(uuid4())
            db.execute('INSERT INTO training_jobs(id,dataset_id,kind,snapshot,digest,config,request_key,created_at,segments_total) VALUES(?,?,?,?,?,?,?,?,?)', (value, dataset_id, kind, canonical(snapshot) if snapshot is not None else None, digest, encoded_config, request_key, datetime.now(timezone.utc).isoformat(), len(snapshot['segments']) if snapshot else 0))
            return self._one(db, 'training_jobs', value)

    def create_variant_job(self, parent_id, steps, request_key=None):
        from voice_app import identifier
        parent_id = identifier(parent_id)
        if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= 1000:
            raise HTTPException(422, '训练步数无效。')
        if request_key is not None:
            request_key = identifier(request_key)
        with closing(self.chat.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            parent = self._one(db, 'training_jobs', parent_id)
            if parent['kind'] != 'train' or not parent.get('snapshot') or not parent.get('digest'):
                raise HTTPException(422, '只能从已完成数据快照的训练任务创建对照实验。')
            config = dict(parent['config'])
            config['steps'] = steps
            encoded = canonical(config)
            if request_key is not None:
                old = db.execute('SELECT * FROM training_jobs WHERE dataset_id=? AND request_key=?', (parent['dataset_id'], request_key)).fetchone()
                if old:
                    old = self._decode(old)
                    if (old.get('parent_job_id') != parent_id or canonical(old['config']) != encoded
                            or old['snapshot'] != parent['snapshot'] or old['digest'] != parent['digest']):
                        raise HTTPException(409, '幂等键已用于其他对照配置。')
                    return old
            value = str(uuid4())
            db.execute('INSERT INTO training_jobs(id,dataset_id,kind,snapshot,digest,config,request_key,parent_job_id,created_at,segments_total) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (value, parent['dataset_id'], 'train', canonical(parent['snapshot']), parent['digest'], encoded,
                        request_key, parent_id, datetime.now(timezone.utc).isoformat(), len(parent['snapshot'].get('segments', []))))
            return self._one(db, 'training_jobs', value)
