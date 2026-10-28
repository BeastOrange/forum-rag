from __future__ import annotations

import hashlib
import copy
import threading
from collections import OrderedDict
from contextlib import closing
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

import numpy as np

from .embeddings import file_signature, get_semantic_model, model_identity
from .search import _chunk_text

SQL = """SELECT c.chunk_id, c.topic_id, c.community_id, c.content, t.title,
 t.community_title, t.author, t.created_at, t.url, t.vote_num, t.comment_count
 FROM chunks c JOIN topics t ON t.topic_id=c.topic_id
 WHERE c.chunk_level=1 ORDER BY c.chunk_id"""


def source_state(db_path):
    path = Path(db_path).resolve(strict=True)
    result = {"path": str(path)}
    for suffix in ("", "-wal"):
        p = Path(str(path) + suffix)
        try:
            s = p.stat()
            # SQLite may create and remove an empty WAL solely for a reader.
            result[suffix or "db"] = None if suffix and s.st_size == 0 else [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]
        except FileNotFoundError:
            if not suffix:
                raise
            result[suffix] = None
    return result


def readonly(db_path):
    path = Path(db_path).resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def source_fingerprint(rows):
    h = hashlib.sha256()
    for row in rows:
        h.update(json.dumps(list(row), ensure_ascii=False, separators=(',', ':')).encode())
        h.update(b'\n')
    return h.hexdigest()


def load_generation(index_dir, source, identity):
    root = Path(index_dir)
    pointer = (root / 'CURRENT').read_text().strip()
    if not pointer or Path(pointer).name != pointer:
        raise RuntimeError('invalid generation pointer')
    folder = root / pointer
    meta = json.loads((folder / 'manifest.json').read_text())
    if meta['source'] != source:
        raise RuntimeError('semantic index stale: source changed')
    if meta['model'] != identity:
        raise RuntimeError('semantic index model mismatch')
    for name, digest in meta['checksums'].items():
        if file_hash(folder / name) != digest:
            raise RuntimeError(f'semantic index checksum mismatch: {name}')
    if set(meta['checksums']) != {'vectors.npy', 'ids.json'}:
        raise RuntimeError('invalid index checksums')
    return folder, meta



_generation_cache = OrderedDict()
_GENERATION_CACHE_LIMIT = 4
_generation_lock = threading.RLock()


def generation_signatures(folder):
    return tuple(file_signature(folder / name) for name in ('manifest.json', 'vectors.npy', 'ids.json'))


def generation_snapshot(index_dir, source, identity):
    root = Path(index_dir).resolve(strict=True)
    with _generation_lock:
        current_path = root / 'CURRENT'
        pointer_signature = file_signature(current_path)
        pointer = current_path.read_text().strip()
        if not pointer or Path(pointer).name != pointer:
            raise RuntimeError('invalid generation pointer')
        folder = root / pointer
        signatures = generation_signatures(folder)
        key = (pointer, pointer_signature, signatures)
        cached = _generation_cache.get(root)
        if cached is None or cached[0] != key:
            checked_folder, meta = load_generation(root, source, identity)
            if checked_folder != folder:
                raise RuntimeError('generation changed during validation')
            ids = tuple(json.loads((folder / 'ids.json').read_text(encoding='utf-8')))
            matrix = np.load(folder / 'vectors.npy', mmap_mode='r', allow_pickle=False)
            if (matrix.shape != (meta['count'], identity['dimension']) or matrix.dtype != np.float32
                    or len(ids) != meta['count'] or len(set(ids)) != len(ids)):
                raise RuntimeError('invalid semantic index shape or IDs')
            if file_signature(current_path) != pointer_signature or generation_signatures(folder) != signatures:
                raise RuntimeError('generation changed during validation')
            cached = (key, meta, ids, matrix)
            _generation_cache[root] = cached
            while len(_generation_cache) > _GENERATION_CACHE_LIMIT:
                _generation_cache.popitem(last=False)
        _generation_cache.move_to_end(root)
        _, meta, ids, matrix = cached
        if meta['source'] != source:
            raise RuntimeError('semantic index stale: source changed')
        if meta['model'] != identity:
            raise RuntimeError('semantic index model mismatch')
        return folder, copy.deepcopy(meta), ids, matrix, signatures

def build_semantic_index(db_path, *, model_dir, index_dir, batch_size=32, threads=1):
    if batch_size <= 0 or threads <= 0:
        raise ValueError('batch_size and threads must be positive')
    initial_state = source_state(db_path)
    with closing(readonly(db_path)) as conn:
        conn.execute("BEGIN")
        rows = conn.execute(SQL).fetchall()
        state = source_state(db_path)
        if state != initial_state:
            raise RuntimeError("source changed during snapshot")
    if source_state(db_path) != state:
        raise RuntimeError("source changed after snapshot")
    fingerprint = source_fingerprint(rows)
    identity = model_identity(model_dir)
    root = Path(index_dir)
    root.mkdir(parents=True, exist_ok=True)
    try:
        folder, meta = load_generation(root, state, identity)
        if fingerprint != meta['source_fingerprint'] or source_state(db_path) != state:
            raise RuntimeError('source changed since index build')
        return {'generation': folder.name, 'count': meta['count'], 'status': 'existing'}
    except (FileNotFoundError, RuntimeError, KeyError, ValueError, json.JSONDecodeError):
        pass
    temp = Path(tempfile.mkdtemp(prefix='.building-', dir=root))
    try:
        dimension = identity['dimension']
        vectors = np.lib.format.open_memmap(temp / 'vectors.npy', mode='w+', dtype=np.float32, shape=(len(rows), dimension))
        model = get_semantic_model(model_dir, identity, threads=threads, batch_size=batch_size)
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            values = np.asarray(model.embed_documents([_chunk_text(dict(row)) for row in batch]), dtype=np.float32)
            if values.shape != (len(batch), dimension) or not np.isfinite(values).all():
                raise RuntimeError('invalid model vectors')
            norms = np.linalg.norm(values, axis=1)
            if np.any(norms == 0):
                raise RuntimeError('zero model vector')
            vectors[start:start + len(batch)] = values / norms[:, None]
            print(f'encoded {start + len(batch)}/{len(rows)}', file=sys.stderr, flush=True)
        vectors.flush()
        del vectors
        (temp / 'ids.json').write_text(json.dumps([row['chunk_id'] for row in rows], ensure_ascii=False), encoding='utf-8')
        if source_state(db_path) != state:
            raise RuntimeError('source changed during index build')
        if model_identity(model_dir) != identity:
            raise RuntimeError('model changed during index build')
        meta = {'version': 1, 'source': state, 'source_fingerprint': fingerprint,
                'model': identity, 'count': len(rows),
                'checksums': {name: file_hash(temp / name) for name in ('vectors.npy', 'ids.json')}}
        (temp / 'manifest.json').write_text(json.dumps(meta, ensure_ascii=False, sort_keys=True), encoding='utf-8')
        generation = 'generation-' + uuid.uuid4().hex
        os.rename(temp, root / generation)
        if source_state(db_path) != state:
            raise RuntimeError('source changed before index publication')
        fd, pointer = tempfile.mkstemp(prefix='.CURRENT-', dir=root)
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(generation + '\n')
                f.flush()
                os.fsync(f.fileno())
            os.replace(pointer, root / 'CURRENT')
            if source_state(db_path) != state:
                raise RuntimeError('source changed during index publication')
        finally:
            if os.path.exists(pointer):
                os.unlink(pointer)
        return {'generation': generation, 'count': len(rows), 'status': 'built'}
    finally:
        shutil.rmtree(temp, ignore_errors=True)
