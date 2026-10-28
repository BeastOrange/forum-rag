from __future__ import annotations

import sqlite3

import pytest

pytest.importorskip("numpy")

from wq_forum_rag.storage import ForumStore


class FakeModel:
    def embed_documents(self, texts):
        return [[1., 0.] if 'apple' in t else [0., 1.] for t in texts]

    def embed_query(self, text):
        return [1., 0.] if text == 'fruit' else [0., 1.]


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    from wq_forum_rag import semantic_index, semantic_retrieval
    db = tmp_path / 'forum.db'
    with ForumStore(db) as store:
        store.conn.execute("INSERT INTO topics (topic_id, community_id, community_title, title, body_text, url, created_at, author, vote_num, comment_count, comments_json, content_hash) VALUES ('1','c','community','Apple','apple','u','d','a',0,0,'[]','h')")
        store.conn.execute("INSERT INTO chunks (chunk_id, topic_id, community_id, url, created_at, chunk_index, chunk_level, start_offset, end_offset, content, content_hash) VALUES ('1#0','1','c','u','d',0,1,0,5,'apple','h')")
        store.conn.commit()
    model = tmp_path / 'model'
    model.mkdir()
    identity = {'model_name': 'fake', 'revision': 'test', 'files': {'fake': 'hash'}, 'dimension': 2, 'preprocessing': 'test'}
    monkeypatch.setattr(semantic_index, 'model_identity', lambda path: identity)
    monkeypatch.setattr(semantic_retrieval, 'model_identity', lambda path: identity)
    monkeypatch.setattr(semantic_index, 'get_semantic_model', lambda *args, **kwargs: FakeModel())
    monkeypatch.setattr(semantic_retrieval, 'get_semantic_model', lambda *args, **kwargs: FakeModel())
    index = tmp_path / 'index'
    semantic_index.build_semantic_index(db, model_dir=model, index_dir=index, batch_size=2, threads=1)
    return db, model, index


def test_independent_dense_and_hybrid(corpus):
    from wq_forum_rag.semantic_retrieval import search_semantic_forum
    db, model, index = corpus
    for mode in ('semantic', 'hybrid'):
        result = search_semantic_forum(db, 'fruit', index_dir=index, model_dir=model, mode=mode)
        assert result['results'][0]['topic_id'] == '1'
        assert result['results'][0]['semantic_score'] == pytest.approx(1)


def test_stale_deleted_and_invalid(corpus):
    from wq_forum_rag.semantic_retrieval import search_semantic_forum
    db, model, index = corpus
    kw = dict(index_dir=index, model_dir=model)
    assert search_semantic_forum(db, ' ', **kw)['results'] == []
    with pytest.raises(ValueError):
        search_semantic_forum(db, 'fruit', top_k=0, **kw)
    with sqlite3.connect(db) as conn:
        conn.execute('DELETE FROM chunks')
    with pytest.raises(RuntimeError, match='stale|changed'):
        search_semantic_forum(db, 'fruit', **kw)


def test_model_mismatch_and_corruption(corpus, monkeypatch):
    from wq_forum_rag import semantic_retrieval
    db, model, index = corpus
    monkeypatch.setattr(semantic_retrieval, 'model_identity', lambda _: {'revision': 'other'})
    with pytest.raises(RuntimeError, match='model'):
        semantic_retrieval.search_semantic_forum(db, 'fruit', index_dir=index, model_dir=model)


def test_checksum_rejected_and_existing_build_skipped(corpus):
    from wq_forum_rag.semantic_index import build_semantic_index
    from wq_forum_rag.semantic_retrieval import search_semantic_forum
    db, model, index = corpus
    assert build_semantic_index(db, model_dir=model, index_dir=index)['status'] == 'existing'
    generation = (index / 'CURRENT').read_text().strip()
    with (index / generation / 'vectors.npy').open('ab') as f:
        f.write(b'corrupt')
    with pytest.raises(RuntimeError, match='checksum'):
        search_semantic_forum(db, 'fruit', index_dir=index, model_dir=model)


def test_changed_source_during_build_does_not_publish(corpus, monkeypatch):
    from wq_forum_rag import semantic_index
    db, model, index = corpus
    previous = (index / 'CURRENT').read_text()
    class ChangingModel(FakeModel):
        def embed_documents(self, texts):
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE topics SET title='changed'")
            return super().embed_documents(texts)
    # WAL permits a writer while the builder holds its read snapshot.
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("UPDATE topics SET title='before build'")
    monkeypatch.setattr(semantic_index, 'get_semantic_model', lambda *args, **kwargs: ChangingModel())
    with pytest.raises(RuntimeError, match='changed'):
        semantic_index.build_semantic_index(db, model_dir=model, index_dir=index)
    assert (index / 'CURRENT').read_text() == previous


def test_query_pins_generation_and_never_encodes_documents(corpus, monkeypatch):
    from wq_forum_rag import semantic_retrieval
    db, model, index = corpus
    original = (index / 'CURRENT').read_text().strip()
    class SwitchingModel:
        def embed_query(self, text):
            (index / 'CURRENT').write_text('other-generation\n')
            return [1., 0.]
        def embed_documents(self, texts):
            raise AssertionError('query must never encode documents')
    monkeypatch.setattr(semantic_retrieval, 'get_semantic_model', lambda *args, **kwargs: SwitchingModel())
    result = semantic_retrieval.search_semantic_forum(db, 'fruit', index_dir=index, model_dir=model)
    assert result['generation'] == original
    assert result['results'][0]['topic_id'] == '1'


def test_concurrent_requests_are_consistent(corpus):
    from concurrent.futures import ThreadPoolExecutor
    from wq_forum_rag.semantic_retrieval import search_semantic_forum
    db, model, index = corpus
    def run(_):
        return search_semantic_forum(db, 'fruit', index_dir=index, model_dir=model)['results']
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(run, range(16)))
    assert all(result == results[0] for result in results)


def test_model_spec_checks_all_files(tmp_path):
    import hashlib
    import json
    from wq_forum_rag.embeddings import model_identity
    files = {}
    for name in ('model_optimized.onnx', 'config.json', 'tokenizer.json',
                 'tokenizer_config.json', 'special_tokens_map.json'):
        (tmp_path / name).write_bytes(name.encode())
        files[name] = hashlib.sha256(name.encode()).hexdigest()
    (tmp_path / 'model-spec.json').write_text(json.dumps({
        'repo': 'qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q',
        'revision': 'pinned', 'files': files,
    }))
    assert model_identity(tmp_path)['dimension'] == 384
    (tmp_path / 'tokenizer.json').write_bytes(b'changed')
    with pytest.raises(RuntimeError, match='checksum'):
        model_identity(tmp_path)


def test_warm_queries_do_not_rescan_or_rehash(corpus, monkeypatch):
    from wq_forum_rag import semantic_index, semantic_retrieval
    db, model, index = corpus
    calls = []
    original = semantic_index.file_hash
    def counted(path):
        calls.append(str(path))
        return original(path)
    monkeypatch.setattr(semantic_index, 'file_hash', counted)
    def no_fingerprint(*args):
        raise AssertionError('query scanned all source rows')
    monkeypatch.setattr(semantic_index, 'source_fingerprint', no_fingerprint)
    query = lambda: semantic_retrieval.search_semantic_forum(db, 'fruit', index_dir=index, model_dir=model)
    first = query()
    assert len(calls) == 2
    first['model']['files']['fake'] = 'mutated'
    calls.clear()
    assert query()['model']['files']['fake'] == 'hash'
    assert calls == []


def test_concurrent_first_generation_load_hashes_once(corpus, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from wq_forum_rag import semantic_index, semantic_retrieval
    db, model, index = corpus
    calls = []
    original = semantic_index.file_hash
    def counted(path):
        calls.append(str(path))
        return original(path)
    monkeypatch.setattr(semantic_index, 'file_hash', counted)
    def run(_):
        return semantic_retrieval.search_semantic_forum(db, 'fruit', index_dir=index, model_dir=model)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(run, range(8)))
    assert all(r['results'][0]['topic_id'] == '1' for r in results)
    assert len(calls) == 2


def test_cached_generation_detects_same_size_tamper(corpus):
    import os
    from wq_forum_rag.semantic_retrieval import search_semantic_forum
    db, model, index = corpus
    search_semantic_forum(db, 'fruit', index_dir=index, model_dir=model)
    path = index / (index / 'CURRENT').read_text().strip() / 'vectors.npy'
    stat = path.stat()
    with path.open('r+b') as f:
        f.seek(-1, 2)
        f.write(b'X')
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(RuntimeError, match='checksum'):
        search_semantic_forum(db, 'fruit', index_dir=index, model_dir=model)


def test_model_identity_cache_rechecks_changed_content(tmp_path, monkeypatch):
    import hashlib
    import json
    import os
    from wq_forum_rag import embeddings
    files = {}
    for name in ('model_optimized.onnx', 'config.json', 'tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json'):
        (tmp_path / name).write_bytes(name.encode())
        files[name] = hashlib.sha256(name.encode()).hexdigest()
    (tmp_path / 'model-spec.json').write_text(json.dumps({'repo': 'qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q', 'revision': 'pinned', 'files': files}))
    real_sha = embeddings.hashlib.sha256
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return real_sha(*args, **kwargs)
    monkeypatch.setattr(embeddings.hashlib, 'sha256', counted)
    identity = embeddings.model_identity(tmp_path)
    identity['files'].clear()
    assert len(calls) == len(files)
    calls.clear()
    assert len(embeddings.model_identity(tmp_path)['files']) == len(files)
    assert not calls
    path = tmp_path / 'tokenizer.json'
    stat = path.stat()
    path.write_bytes(b'x' * stat.st_size)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(RuntimeError, match='checksum'):
        embeddings.model_identity(tmp_path)


def test_concurrent_model_identity_first_load_hashes_once(tmp_path, monkeypatch):
    import hashlib
    import json
    from concurrent.futures import ThreadPoolExecutor
    from wq_forum_rag import embeddings
    files = {}
    for name in ('model_optimized.onnx', 'config.json', 'tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json'):
        (tmp_path / name).write_bytes(name.encode())
        files[name] = hashlib.sha256(name.encode()).hexdigest()
    (tmp_path / 'model-spec.json').write_text(json.dumps({'repo': 'qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q', 'revision': 'pinned', 'files': files}))
    real_sha = embeddings.hashlib.sha256
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return real_sha(*args, **kwargs)
    monkeypatch.setattr(embeddings.hashlib, 'sha256', counted)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(embeddings.model_identity, [tmp_path] * 8))
    assert len(calls) == len(files)
    assert all(item == results[0] for item in results)


def test_model_initialization_is_offline_and_never_falls_back(monkeypatch, tmp_path):
    import sys
    import types
    from wq_forum_rag.embeddings import LocalSemanticBackend
    captured = {}
    def fail(**kwargs):
        captured.update(kwargs)
        raise RuntimeError('offline model load failed')
    monkeypatch.setitem(sys.modules, 'fastembed', types.SimpleNamespace(TextEmbedding=fail))
    with pytest.raises(RuntimeError, match='offline model load failed'):
        LocalSemanticBackend(tmp_path)
    assert captured['specific_model_path'] == str(tmp_path.resolve())
    assert captured['local_files_only'] is True


def test_wal_change_invalidates_warm_generation(corpus):
    from wq_forum_rag.semantic_retrieval import search_semantic_forum
    db, model, index = corpus
    search_semantic_forum(db, 'fruit', index_dir=index, model_dir=model)
    with sqlite3.connect(db) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute("UPDATE topics SET title='changed'")
        conn.commit()
        with pytest.raises(RuntimeError, match='stale|changed'):
            search_semantic_forum(db, 'fruit', index_dir=index, model_dir=model)


@pytest.mark.parametrize('name', ['ids.json', 'manifest.json'])
def test_generation_metadata_changes_invalidate_cache(corpus, name):
    import os
    from wq_forum_rag.semantic_retrieval import search_semantic_forum
    db, model, index = corpus
    search_semantic_forum(db, 'fruit', index_dir=index, model_dir=model)
    path = index / (index / 'CURRENT').read_text().strip() / name
    stat = path.stat()
    with path.open('r+b') as stream:
        stream.seek(1)
        stream.write(b'!')
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises((RuntimeError, ValueError)):
        search_semantic_forum(db, 'fruit', index_dir=index, model_dir=model)


def test_atomic_source_replacement_invalidates_cache(corpus, tmp_path):
    import os
    import shutil
    from wq_forum_rag.semantic_retrieval import search_semantic_forum
    db, model, index = corpus
    search_semantic_forum(db, 'fruit', index_dir=index, model_dir=model)
    replacement = tmp_path / 'replacement.db'
    shutil.copy2(db, replacement)
    os.replace(replacement, db)
    with pytest.raises(RuntimeError, match='stale|changed'):
        search_semantic_forum(db, 'fruit', index_dir=index, model_dir=model)


def test_build_wal_database_without_wal_file(corpus, tmp_path):
    from wq_forum_rag.semantic_index import build_semantic_index
    db, model, _ = corpus
    with sqlite3.connect(db) as conn:
        assert conn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    assert not db.with_name(db.name + '-wal').exists()
    result = build_semantic_index(db, model_dir=model, index_dir=tmp_path / 'wal-index')
    assert result['status'] == 'built'
    assert build_semantic_index(db, model_dir=model, index_dir=tmp_path / 'wal-index')['status'] == 'existing'
    from wq_forum_rag.semantic_retrieval import search_semantic_forum
    assert search_semantic_forum(db, 'fruit', index_dir=tmp_path / 'wal-index', model_dir=model)['results']


def test_rollback_writer_commits_during_encoding_and_build_rejects(corpus, tmp_path, monkeypatch):
    from wq_forum_rag import semantic_index
    db, model, _ = corpus
    committed = []
    class WritingModel(FakeModel):
        def embed_documents(self, texts):
            with sqlite3.connect(db, timeout=0.1) as conn:
                conn.execute("UPDATE topics SET title='concurrent write'")
            committed.append(True)
            return super().embed_documents(texts)
    monkeypatch.setattr(semantic_index, 'get_semantic_model', lambda *a, **kw: WritingModel())
    index = tmp_path / 'concurrent-index'
    with pytest.raises(RuntimeError, match='changed'):
        semantic_index.build_semantic_index(db, model_dir=model, index_dir=index)
    assert committed == [True]
    assert not (index / 'CURRENT').exists()


def test_model_swap_during_load_does_not_poison_cache(tmp_path, monkeypatch):
    from wq_forum_rag import embeddings
    path = tmp_path / 'version'
    path.write_text('A')
    monkeypatch.setattr(embeddings, 'model_identity', lambda _: {'revision': path.read_text()})
    constructed = []
    class SwitchingModel:
        def __init__(self, *args, **kwargs):
            if not constructed:
                path.write_text('B')
            constructed.append(path.read_text())
    monkeypatch.setattr(embeddings, 'LocalSemanticBackend', SwitchingModel)
    with pytest.raises(RuntimeError, match='model.*changed'):
        embeddings.get_semantic_model(tmp_path, {'revision': 'A'})
    path.write_text('A')
    instance = embeddings.get_semantic_model(tmp_path, {'revision': 'A'})
    assert constructed == ['B', 'A']
    assert embeddings.get_semantic_model(tmp_path, {'revision': 'A'}) is instance


def test_model_and_generation_lru_are_bounded_and_pinned(corpus, tmp_path, monkeypatch):
    import shutil
    import weakref
    from wq_forum_rag import embeddings, semantic_index
    db, model, index = corpus
    identity = {'model_name': 'fake', 'revision': 'test', 'files': {'fake': 'hash'}, 'dimension': 2, 'preprocessing': 'test'}
    class Model:
        def __init__(self, *args, **kwargs):
            pass
    monkeypatch.setattr(embeddings, 'LocalSemanticBackend', Model)
    monkeypatch.setattr(embeddings, 'model_identity', lambda _: identity)
    monkeypatch.setattr(embeddings, 'file_signature', lambda _: (1,))
    embeddings._semantic_models.clear()
    pinned = embeddings.get_semantic_model(model, identity)
    reference = weakref.ref(pinned)
    for i in range(12):
        path = tmp_path / f'model-{i}'
        path.mkdir()
        embeddings.get_semantic_model(path, identity)
    assert len(embeddings._semantic_models) <= 4
    assert reference() is pinned
    del pinned
    assert reference() is None
    semantic_index._generation_cache.clear()
    state = semantic_index.source_state(db)
    first = semantic_index.generation_snapshot(index, state, identity)
    matrix_ref = weakref.ref(first[3])
    for i in range(12):
        path = tmp_path / f'index-{i}'
        shutil.copytree(index, path)
        semantic_index.generation_snapshot(path, state, identity)
    assert len(semantic_index._generation_cache) <= 4
    assert matrix_ref() is first[3]
    del first
    assert matrix_ref() is None


def test_model_changes_and_restores_during_initialization(tmp_path, monkeypatch):
    from wq_forum_rag import embeddings
    (tmp_path / 'model-spec.json').write_text('spec')
    file = tmp_path / 'model.bin'
    file.write_bytes(b'A')
    identity = {'revision': 'A', 'files': {'model.bin': 'hash'}}
    monkeypatch.setattr(embeddings, 'model_identity', lambda _: identity)
    class SwitchingModel:
        def __init__(self, *args, **kwargs):
            file.write_bytes(b'B')
            file.write_bytes(b'A')
    monkeypatch.setattr(embeddings, 'LocalSemanticBackend', SwitchingModel)
    with pytest.raises(RuntimeError, match='model.*changed'):
        embeddings.get_semantic_model(tmp_path, identity)
    assert not any(key[0] == str(tmp_path.resolve()) for key in embeddings._semantic_models)


def test_model_versions_evict_lru_without_invalidating_inflight(tmp_path, monkeypatch):
    import weakref
    from wq_forum_rag import embeddings
    version = tmp_path / 'version'
    version.write_text('0')
    monkeypatch.setattr(embeddings, 'model_identity', lambda _: {'revision': version.read_text()})
    class Model:
        def __init__(self, *args, **kwargs):
            self.revision = version.read_text()
    monkeypatch.setattr(embeddings, 'LocalSemanticBackend', Model)
    embeddings._semantic_models.clear()
    initial = embeddings.get_semantic_model(tmp_path, {'revision': '0'})
    ref = weakref.ref(initial)
    for i in range(1, 6):
        version.write_text(str(i))
        assert embeddings.get_semantic_model(tmp_path, {'revision': str(i)}).revision == str(i)
    assert len(embeddings._semantic_models) == 4
    assert ref() is initial and initial.revision == '0'
    del initial
    assert ref() is None
    version.write_text('0')
    assert embeddings.get_semantic_model(tmp_path, {'revision': '0'}).revision == '0'
