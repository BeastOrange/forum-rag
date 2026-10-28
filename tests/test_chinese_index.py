from __future__ import annotations

from contextlib import closing, contextmanager
import sqlite3

import pytest
from typer.testing import CliRunner

from wq_forum_rag import search_index as index
from wq_forum_rag.cli import app
from wq_forum_rag.knowledge import KnowledgePage, KnowledgeStore
from wq_forum_rag.storage import ForumStore


@contextmanager
def connection(*args, **kwargs):
    with closing(sqlite3.connect(*args, **kwargs)) as conn, conn:
        yield conn


def test_repeated_chinese_conditions_do_not_exceed_sql_depth(tmp_path, monkeypatch):
    monkeypatch.setenv('FORUM_RAG_CHINESE_INDEX', '1')
    db = make_db(tmp_path / 'db')
    with connection(db) as conn:
        add_chunk(conn, 'match', '中性化')
        index.rebuild_search_index(conn, chinese=True)
        assert ids(conn, '中性化 ' * 1000) == ['match']


def make_db(path):
    with ForumStore(path) as store:
        c = store.conn
        c.execute("INSERT INTO topics VALUES ('t','c','社区','标题','', '', '', '', 0, 0, '[]', 'h')")
        c.execute("CREATE TABLE documents (slug TEXT PRIMARY KEY, title TEXT, source_path TEXT)")
        c.execute("CREATE TABLE doc_chunks (chunk_id TEXT PRIMARY KEY, slug TEXT, content TEXT, chunk_index INTEGER)")
        c.execute("INSERT INTO documents VALUES ('d', '文档', '')")
        c.execute("INSERT INTO doc_chunks VALUES ('doc', 'd', '中性化 因子 处理', 0)")
        c.commit()
    with KnowledgeStore(path) as store:
        store.upsert_page(KnowledgePage('page', '中性化', '因子', '处理', status='published'), source_topic_ids=[])
    return path


def add_chunk(conn, ident, content):
    conn.execute("INSERT INTO chunks VALUES (?, NULL, 't', 'c', '', '', 0, 1, 0, 0, ?, 'h')", (ident, content))


def ids(conn, query, kind='forum_chunk', limit=10, include_drafts=False):
    return index.candidate_ids(conn, query, kind=kind, limit=limit, include_drafts=include_drafts)


def test_chinese_literal_before_limit_and_segments(tmp_path, monkeypatch):
    monkeypatch.setenv('FORUM_RAG_CHINESE_INDEX', '1')
    db = make_db(tmp_path / 'db')
    with connection(db) as conn:
        for n in range(12):
            add_chunk(conn, f'false{n}', '中性 性化 处理')
        add_chunk(conn, 'yes', '中性化 因子 处理')
        conn.commit()
        index.rebuild_search_index(conn, chinese=True)
        assert ids(conn, '中性化 处理', limit=1) == ['yes']
        assert ids(conn, '中性化', limit=20) == ['yes']
        assert ids(conn, '中', limit=20) and 'yes' in ids(conn, '中', limit=20)
        assert ids(conn, '中性化 english', limit=20) == []
        assert ids(conn, '中性化 处理', 'doc_chunk') == ['doc']
        assert ids(conn, '中性化 处理', 'knowledge_page') == ['page']
        assert ids(conn, '中性化因子', 'knowledge_page') == []
        assert 'yes' in ids(conn, '标题 处理', limit=20)


def test_opt_in_missing_marker_and_read_only(tmp_path, monkeypatch):
    db = make_db(tmp_path / 'db')
    with connection(db) as conn:
        add_chunk(conn, 'x', '中性化 因子 处理')
        conn.commit()
        index.rebuild_search_index(conn)
        before = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        monkeypatch.delenv('FORUM_RAG_CHINESE_INDEX', raising=False)
        legacy = ids(conn, '中性化 处理')
        monkeypatch.setenv('FORUM_RAG_CHINESE_INDEX', '1')
        assert ids(conn, '中性化 处理') == legacy
        assert conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == before
        index.rebuild_search_index(conn, chinese=True)
        conn.execute('DELETE FROM chinese_index_meta')
        conn.commit()
    with connection(f'file:{db}?mode=ro', uri=True) as conn:
        before = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        assert ids(conn, '中性化 处理') == legacy
        assert conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == before
    with connection(db) as conn:
        index.rebuild_search_index(conn, chinese=True)
        monkeypatch.delenv('FORUM_RAG_CHINESE_INDEX')
        assert ids(conn, '中性化 处理') == legacy
        monkeypatch.setenv('FORUM_RAG_CHINESE_INDEX', '1')
        assert ids(conn, '中性化 处理') == ['x']


def test_sync_knowledge_and_regular_rebuild(tmp_path, monkeypatch):
    monkeypatch.setenv('FORUM_RAG_CHINESE_INDEX', '1')
    db = make_db(tmp_path / 'db')
    index.rebuild_search_index(db, chinese=True)
    with KnowledgeStore(db) as store:
        store.upsert_page(KnowledgePage('page', '新标题', '新内容', '处理', status='draft'), source_topic_ids=[])
        assert ids(store.conn, '中性化', 'knowledge_page', include_drafts=True) == []
        assert ids(store.conn, '新标题', 'knowledge_page') == []
        assert ids(store.conn, '新标题', 'knowledge_page', include_drafts=True) == ['page']
        store.set_status('page', 'published')
        assert ids(store.conn, '新标题', 'knowledge_page') == ['page']
    with connection(db) as conn:
        add_chunk(conn, 'added', '新标题 处理')
        conn.commit()
        index.rebuild_search_index(conn)
        assert ids(conn, '新标题 处理') == ['added']


def test_failed_rebuild_rolls_back_and_sql_errors_propagate(tmp_path, monkeypatch):
    monkeypatch.setenv('FORUM_RAG_CHINESE_INDEX', '1')
    db = make_db(tmp_path / 'db')
    with connection(db) as conn:
        add_chunk(conn, 'old', '中性化')
        conn.commit()
        index.rebuild_search_index(conn, chinese=True)
        from wq_forum_rag import chinese_index
        original = chinese_index.insert_rows
        def fail(conn, rows):
            original(conn, rows)
            raise RuntimeError('injected')
        monkeypatch.setattr(chinese_index, 'insert_rows', fail)
        with pytest.raises(RuntimeError, match='injected'):
            index.rebuild_search_index(conn, chinese=True)
        assert ids(conn, '中性化') == ['old']
        conn.execute('DROP TABLE chinese_fts_v2')
        with pytest.raises(sqlite3.OperationalError):
            ids(conn, '中性化')


def test_cli_explicit_chinese(tmp_path):
    db = make_db(tmp_path / 'db')
    result = CliRunner().invoke(app, ['search-reindex', '--db', str(db), '--chinese'])
    assert result.exit_code == 0, result.output
    with connection(db) as conn:
        assert conn.execute('SELECT version FROM chinese_index_meta').fetchone() == (2,)


def test_literal_filter_precedes_limit_even_for_fts_false_positives(tmp_path, monkeypatch):
    monkeypatch.setenv('FORUM_RAG_CHINESE_INDEX', '1')
    db = make_db(tmp_path / 'db')
    with connection(db) as conn:
        for n in range(12):
            add_chunk(conn, f'bad{n}', '中性化 处理')
        add_chunk(conn, 'correct', '中性化 处理')
        conn.commit()
        index.rebuild_search_index(conn, chinese=True)
        # Simulate a stale index: matching postings whose source no longer
        # satisfies the literal constraint. All rank before the correct row.
        conn.execute("UPDATE chunks SET content='中性 性化 处理' WHERE chunk_id LIKE 'bad%'")
        match = 'grams : "b4e2dx6027 b6027x5316" AND grams : "b5904x7406"'
        first = conn.execute("SELECT m.record_id FROM chinese_fts_v2 f JOIN chinese_index_rows m "
                             "ON m.rowid=f.rowid WHERE chinese_fts_v2 MATCH ? "
                             "AND m.kind='forum_chunk' ORDER BY rank LIMIT 1", (match,)).fetchone()
        assert first[0].startswith('bad')
        assert ids(conn, '中性化 处理', limit=1) == ['correct']


def test_private_cache_distinguishes_switch_and_old_readonly(tmp_path, monkeypatch):
    db = make_db(tmp_path / 'db')
    with connection(db) as conn:
        add_chunk(conn, 'partial', '中性')
        add_chunk(conn, 'both', '中性化 处理')
        conn.commit()
        index.rebuild_search_index(conn)
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='chinese_fts_v2'").fetchone()
    with connection(f'file:{db}?mode=ro', uri=True) as reader:
        before = reader.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall()
        monkeypatch.setenv('FORUM_RAG_CHINESE_INDEX', '1')
        ids(reader, '中性化 处理')
        assert reader.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall() == before
    index.rebuild_search_index(db, chinese=True)
    monkeypatch.delenv('FORUM_RAG_CHINESE_INDEX')
    old = index.fts_candidates(db, '中性化 缺失', limit=10)
    assert 'both' in old
    monkeypatch.setenv('FORUM_RAG_CHINESE_INDEX', '1')
    assert index.fts_candidates(db, '中性化 缺失', limit=10) == []
    monkeypatch.delenv('FORUM_RAG_CHINESE_INDEX')
    assert index.fts_candidates(db, '中性化 缺失', limit=10) == old


def test_build_failure_and_caller_transaction_roll_back(tmp_path, monkeypatch):
    from wq_forum_rag import chinese_index

    db = make_db(tmp_path / 'db')
    with connection(db) as conn:
        add_chunk(conn, 'pending', '中文')
        original = chinese_index.insert_rows

        def fail(conn, rows):
            original(conn, rows)
            raise RuntimeError('injected first build failure')

        with monkeypatch.context() as patch:
            patch.setattr(chinese_index, 'insert_rows', fail)
            with pytest.raises(RuntimeError, match='injected first build failure'):
                index.rebuild_search_index(conn, chinese=True)
        assert conn.execute("SELECT count(*) FROM chunks WHERE chunk_id='pending'").fetchone() == (1,)
        assert not chinese_index.table_exists(conn, 'chinese_fts_v2')
        assert not chinese_index.table_exists(conn, 'chinese_index_meta')
        conn.rollback()
        index.rebuild_search_index(conn, chinese=True)
        assert conn.in_transaction
        conn.rollback()
        assert not chinese_index.table_exists(conn, 'chinese_fts_v2')


def test_mixed_query_requires_ascii_terms_before_limit(tmp_path, monkeypatch):
    monkeypatch.setenv('FORUM_RAG_CHINESE_INDEX', '1')
    db = make_db(tmp_path / 'db')
    with connection(db) as conn:
        for n in range(12):
            add_chunk(conn, f'missing{n}', '中性化')
        add_chunk(conn, 'valid', '中性化 ENGLISH factor')
        add_chunk(conn, 'one_english', '中性化 english')
        conn.commit()
        index.rebuild_search_index(conn, chinese=True)
        assert ids(conn, '中性化 english factor', limit=1) == ['valid']
        assert set(ids(conn, '中性化 english', limit=20)) == {'valid', 'one_english'}
        assert ids(conn, '中性化 absent', limit=20) == []


def test_contentless_schema_and_unsupported_build(tmp_path, monkeypatch):
    from wq_forum_rag import chinese_index

    db = make_db(tmp_path / 'db')
    with connection(db) as conn:
        monkeypatch.setattr(chinese_index.sqlite3, 'sqlite_version_info', (3, 42, 0))
        with pytest.raises(sqlite3.OperationalError, match='3.43'):
            index.rebuild_search_index(conn, chinese=True)
        assert not chinese_index.table_exists(conn, 'chinese_fts_v2')
        index.rebuild_search_index(conn)
        assert not chinese_index.table_exists(conn, 'chinese_fts_v2')
        monkeypatch.setenv('FORUM_RAG_CHINESE_INDEX', '1')
        assert ids(conn, '不存在') == []
        conn.commit()
        result = CliRunner().invoke(app, ['search-reindex', '--db', str(db), '--chinese'])
        assert isinstance(result.exception, sqlite3.OperationalError)
        assert 'SQLite 3.43' in str(result.exception)
        monkeypatch.undo()
        index.rebuild_search_index(conn, chinese=True)
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='chinese_fts_v2'").fetchone()[0]
        assert "contentless_delete=1" in sql and "content=''" in sql
        assert not chinese_index.table_exists(conn, 'chinese_fts_v2_content')
        assert [row[1] for row in conn.execute('PRAGMA table_info(chinese_index_rows)')] == ['rowid', 'kind', 'record_id']


def test_knowledge_index_sync_rolls_back_with_page(tmp_path, monkeypatch):
    monkeypatch.setenv('FORUM_RAG_CHINESE_INDEX', '1')
    db = make_db(tmp_path / 'db')
    index.rebuild_search_index(db, chinese=True)
    with KnowledgeStore(db) as store:
        assert ids(store.conn, '中性化', 'knowledge_page') == ['page']
        store.set_status('page', 'draft')
        assert ids(store.conn, '中性化', 'knowledge_page') == []
        store.conn.rollback()
        assert ids(store.conn, '中性化', 'knowledge_page') == ['page']
        assert store.conn.execute("SELECT count(*) FROM chinese_index_rows WHERE kind='knowledge_page' AND record_id='page'").fetchone()[0] == 1
