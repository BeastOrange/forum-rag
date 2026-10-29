from __future__ import annotations

from contextlib import closing, contextmanager
import os
import sqlite3
import threading

import pytest

from wq_forum_rag import search_index as index
from wq_forum_rag.storage import ForumStore


@contextmanager
def connection(path):
    # SQLite's transaction context does not close its file handle.
    with closing(sqlite3.connect(path)) as conn, conn:
        yield conn


@pytest.fixture
def counted_queries(monkeypatch):
    calls = []
    original = index._query_fts

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(index, '_query_fts', counted)
    with index._candidate_lock:
        index._candidate_cache.clear()
    yield calls
    with index._candidate_lock:
        index._candidate_cache.clear()


def make_db(path, record_id='old'):
    with ForumStore(path) as store:
        assert index.init_search_schema(store.conn)
        set_record(store.conn, record_id)
        store.conn.commit()
    return path


def set_record(conn, record_id):
    conn.execute('DELETE FROM document_fts')
    conn.execute(
        'INSERT INTO document_fts(kind, record_id, topic_id, title, community, content) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        ('forum_chunk', record_id, 'topic', 'needle', '', 'needle'),
    )


def lookup(path):
    return index.fts_candidates(path, 'needle', kind='forum_chunk', limit=10)


def assert_warm(path, calls, expected):
    before = len(calls)
    assert lookup(path) == [expected]
    assert len(calls) == before + 1
    assert lookup(path) == [expected]
    assert len(calls) == before + 1


def test_wal_commit_and_checkpoint_truncate(tmp_path, counted_queries):
    db = make_db(tmp_path / 'forum.sqlite3')
    with connection(db) as writer:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        writer.execute('SELECT count(*) FROM document_fts').fetchone()
        assert_warm(db, counted_queries, 'old')
        set_record(writer, 'new')
        writer.commit()
        assert_warm(db, counted_queries, 'new')
        assert writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone() == (0, 0, 0)
        assert_warm(db, counted_queries, 'new')


def test_vacuum_and_atomic_replacement(tmp_path, counted_queries):
    db = make_db(tmp_path / 'forum.sqlite3')
    assert_warm(db, counted_queries, 'old')
    with connection(db) as conn:
        conn.execute('VACUUM')
    assert_warm(db, counted_queries, 'old')
    replacement = make_db(tmp_path / 'replacement.sqlite3', 'new')
    os.replace(replacement, db)
    assert_warm(db, counted_queries, 'new')


def test_writer_commits_between_cache_hit_and_state_recheck(tmp_path, counted_queries, monkeypatch):
    db = make_db(tmp_path / 'forum.sqlite3')
    with connection(db) as writer:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        writer.execute('SELECT count(*) FROM document_fts').fetchone()
        assert_warm(db, counted_queries, 'old')
        recheck = threading.Event()
        committed = threading.Event()
        original = index._candidate_state
        calls = 0

        def coordinated(conn):
            nonlocal calls
            calls += 1
            if calls == 2:
                recheck.set()
                assert committed.wait(5), 'writer did not commit before the state recheck'
            return original(conn)

        monkeypatch.setattr(index, '_candidate_state', coordinated)
        result = []
        errors = []

        def read():
            try:
                result.extend(lookup(db))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=read)
        thread.start()
        try:
            assert recheck.wait(5), 'reader did not reach cache-hit recheck'
            set_record(writer, 'new')
            writer.commit()
        finally:
            committed.set()
            thread.join(5)
        assert not thread.is_alive()
        assert not errors
        assert result == ['new']
        assert len(counted_queries) == 2


def test_explicit_wal_snapshot_bypasses_current_cache(tmp_path, counted_queries):
    db = make_db(tmp_path / 'forum.sqlite3')
    with connection(db) as writer, connection(db) as reader:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        reader.execute('BEGIN')
        assert index.candidate_ids(reader, 'needle', kind='forum_chunk', limit=10) == ['old']
        set_record(writer, 'new')
        writer.commit()
        assert_warm(db, counted_queries, 'new')
        before = len(counted_queries)
        assert index._cached_candidate_ids(reader, 'needle', kind='forum_chunk', limit=10) == ['old']
        assert index.candidate_ids(reader, 'needle', kind='forum_chunk', limit=10) == ['old']
        assert len(counted_queries) == before + 2
        reader.rollback()
        assert index.candidate_ids(reader, 'needle', kind='forum_chunk', limit=10) == ['new']


def test_rollback_journal_uncommitted_writer_keeps_reader_snapshot(tmp_path, counted_queries):
    db = make_db(tmp_path / 'forum.sqlite3')
    with connection(db) as writer:
        assert writer.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
        assert_warm(db, counted_queries, 'old')
        set_record(writer, 'new')
        assert (tmp_path / 'forum.sqlite3-journal').stat().st_size > 0
        before = len(counted_queries)
        assert lookup(db) == ['old']
        assert lookup(db) == ['old']
        assert len(counted_queries) == before + 2
        writer.rollback()
        assert lookup(db) == ['old']


def test_replacement_after_open_does_not_cache_old_rows_under_new_file(tmp_path, counted_queries, monkeypatch):
    db = make_db(tmp_path / 'forum.sqlite3')
    replacement = make_db(tmp_path / 'replacement.sqlite3', 'new')
    original_connect = sqlite3.connect
    replaced = False

    def replace_after_open(*args, **kwargs):
        nonlocal replaced
        conn = original_connect(*args, **kwargs)
        if not replaced:
            # Force SQLite to load A before B is installed at the same path.
            assert conn.execute('SELECT record_id FROM document_fts').fetchone()[0] == 'old'
            os.replace(replacement, db)
            replaced = True
        return conn

    monkeypatch.setattr(index.sqlite3, 'connect', replace_after_open)
    assert lookup(db) == ['old']
    assert lookup(db) == ['new']
    before = len(counted_queries)
    assert lookup(db) == ['new']
    assert len(counted_queries) == before  # the unchanged B entry really hits


def test_arbitrary_old_connection_without_open_anchor_never_uses_global_cache(tmp_path, counted_queries):
    db = make_db(tmp_path / 'forum.sqlite3')
    replacement = make_db(tmp_path / 'replacement.sqlite3', 'new')
    with connection(db) as old:
        assert old.execute('SELECT record_id FROM document_fts').fetchone()[0] == 'old'
        os.replace(replacement, db)
        assert index._cached_candidate_ids(old, 'needle', kind='forum_chunk', limit=10) == ['old']
        assert_warm(db, counted_queries, 'new')
        before = len(counted_queries)
        assert index._cached_candidate_ids(old, 'needle', kind='forum_chunk', limit=10) == ['old']
        assert len(counted_queries) == before + 1


def test_symlink_retarget_after_open_does_not_cache_old_connection(tmp_path, counted_queries, monkeypatch):
    first = make_db(tmp_path / 'a.sqlite3')
    second = make_db(tmp_path / 'b.sqlite3', 'new')
    link = tmp_path / 'current.sqlite3'
    link.symlink_to(first.name)
    replacement_link = tmp_path / 'next.sqlite3'
    replacement_link.symlink_to(second.name)
    original_connect = sqlite3.connect
    replaced = False

    def switch_after_open(*args, **kwargs):
        nonlocal replaced
        conn = original_connect(*args, **kwargs)
        if not replaced:
            assert conn.execute('SELECT record_id FROM document_fts').fetchone()[0] == 'old'
            os.replace(replacement_link, link)
            replaced = True
        return conn

    monkeypatch.setattr(index.sqlite3, 'connect', switch_after_open)
    assert lookup(link) == ['old']
    assert_warm(link, counted_queries, 'new')


def test_replacement_before_connection_open_does_not_cache_until_next_lookup(tmp_path, counted_queries, monkeypatch):
    db = make_db(tmp_path / 'forum.sqlite3')
    replacement = make_db(tmp_path / 'replacement.sqlite3', 'new')
    original_connect = sqlite3.connect
    replaced = False

    def replace_before_open(*args, **kwargs):
        nonlocal replaced
        if not replaced:
            os.replace(replacement, db)
            replaced = True
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(index.sqlite3, 'connect', replace_before_open)
    assert lookup(db) == ['new']
    assert_warm(db, counted_queries, 'new')


def test_wal_created_during_open_bypasses_cache_for_that_connection(tmp_path, counted_queries, monkeypatch):
    db = make_db(tmp_path / 'forum.sqlite3')
    original_connect = sqlite3.connect
    with closing(original_connect(db)) as writer:
        changed = False

        def enable_wal_before_open(*args, **kwargs):
            nonlocal changed
            if not changed:
                assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
                set_record(writer, 'new')
                writer.commit()
                changed = True
            return original_connect(*args, **kwargs)

        monkeypatch.setattr(index.sqlite3, 'connect', enable_wal_before_open)
        assert lookup(db) == ['new']
        assert_warm(db, counted_queries, 'new')
