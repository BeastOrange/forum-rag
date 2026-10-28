from __future__ import annotations

import sqlite3
import pytest

from wq_forum_rag.batch_search import get_forum_evidence, search_forum_batch


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'forum.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('''CREATE TABLE topics (topic_id TEXT PRIMARY KEY, title TEXT, community_title TEXT, url TEXT, content_hash TEXT);
        CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, topic_id TEXT, chunk_index INTEGER, chunk_level INTEGER, start_offset INTEGER, end_offset INTEGER, content TEXT, content_hash TEXT);
        CREATE INDEX chunk_topic ON chunks(topic_id);''')
        for topic, title, content in [('1', 'Missing values', 'missing values ts_backfill for alpha101'), ('2', '缺失值', '缺失值 ts_backfill alpha101'), ('3', 'Unrelated', 'other content')]:
            conn.execute('INSERT INTO topics VALUES (?,?,?,?,?)', (topic, title, '社区', 'url/'+topic, 'topic-'+topic))
            conn.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)', (topic+'#0', topic, 0, 1, 10, 10+len(content), content, 'hash-'+topic))
    return path


def test_routes_and_verbatim_evidence(db):
    result = search_forum_batch(db, 'missing values', query_variants=['缺失值', ' MISSING VALUES '], top_k=6)
    assert result['executed_queries'] == ['missing values', '缺失值']
    assert {r['topic_id'] for r in result['results']} == {'1', '2'}
    assert result['language_groups']['en'] == ['1'] and result['language_groups']['zh'] == ['2']
    for hit in result['results']:
        evidence = hit['evidence'][0]
        with sqlite3.connect(db) as conn:
            original = conn.execute('SELECT content,start_offset FROM chunks WHERE chunk_id=?', (evidence['chunk_id'],)).fetchone()
        assert evidence['excerpt'] == original[0][evidence['excerpt_start']:evidence['excerpt_end']]
        assert evidence['document_start_offset'] == original[1] + evidence['excerpt_start']
        assert evidence['document_end_offset'] == original[1] + evidence['excerpt_end']
    assert search_forum_batch(db, 'missing values', query_variants=['缺失值'], top_k=1)['results'] == result['results'][:1]


def test_original_literals_apply_to_variants(db):
    result = search_forum_batch(db, 'missing values wrong_field', query_variants=['缺失值'])
    assert not result['results']
    result = search_forum_batch(db, 'missing values ts_backfill alpha101', query_variants=['缺失值'])
    assert {r['topic_id'] for r in result['results']} == {'1', '2'}
    assert not search_forum_batch(db, '928371', query_variants=['缺失值'])['results']
    assert search_forum_batch(db, '1')['results'][0]['topic_id'] == '1'


@pytest.mark.parametrize('kwargs', [dict(top_k=True), dict(top_k=21), dict(evidence_chars=99), dict(max_evidence_chars=199), dict(query_variants=['a']*4), dict(query_variants=['x'*513])])
def test_invalid_parameters(db, kwargs):
    with pytest.raises((TypeError, ValueError)):
        search_forum_batch(db, 'missing', **kwargs)


def test_evidence_anchor_priority_and_stale(db):
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO chunks VALUES ('1#1','1',1,1,50,250,?, 'anchor')", ('x'*200,))
        conn.execute("INSERT INTO chunks VALUES ('1#2','1',2,1,240,440,?, 'neighbor')", ('y'*200,))
    result = get_forum_evidence(db, '1#1', before=1, after=1, max_chars=100, expected_content_hash='anchor')
    assert result['anchor_complete'] is False
    assert sum(len(x['excerpt']) for x in result['evidence']) == 100
    assert result['evidence'][0]['chunk_id'] == '1#1'
    assert result['evidence'][0]['excerpt'] == 'x'*100
    assert get_forum_evidence(db, '1#1', expected_content_hash='old')['status'] == 'stale'
    assert get_forum_evidence(db, 'none')['status'] == 'not_found'


def test_routes_fair_and_shared_candidate_budget(db, monkeypatch):
    from wq_forum_rag import batch_search
    calls = []
    def candidates(conn, query, *, kind, limit):
        calls.append((query, limit))
        return ['1#0'] if query == 'missing values' else ['2#0']
    monkeypatch.setattr(batch_search, 'candidate_ids', candidates)
    result = search_forum_batch(db, 'missing values', query_variants=['缺失值'])
    assert calls == [('missing values', 100), ('缺失值', 100)]
    assert {hit['topic_id'] for hit in result['results']} == {'1', '2'}
    assert result['budget']['candidate_limit'] == 200


def test_fts_error_propagates_and_empty_result_no_python_fallback(db):
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE document_fts (kind TEXT, record_id TEXT)')
    with pytest.raises(sqlite3.OperationalError):
        search_forum_batch(db, 'missing')
    with sqlite3.connect(db) as conn:
        conn.execute('DROP TABLE document_fts')
        conn.execute('CREATE VIRTUAL TABLE document_fts USING fts5(kind UNINDEXED,record_id UNINDEXED,topic_id UNINDEXED,title,community,content)')
    assert search_forum_batch(db, 'missing')['results'] == []


def test_timeout_and_locked_database_are_explicit(db, monkeypatch):
    from wq_forum_rag import batch_search
    monkeypatch.setattr(batch_search, '_TIMEOUT_SECONDS', 0.001)
    def slow(conn, query, **kwargs):
        import time
        time.sleep(.005)
        return []
    monkeypatch.setattr(batch_search, 'candidate_ids', slow)
    with pytest.raises(TimeoutError):
        search_forum_batch(db, 'missing')
    with sqlite3.connect(db, timeout=.01) as conn:
        conn.execute('BEGIN EXCLUSIVE')
        conn.execute("UPDATE topics SET title='locked' WHERE topic_id='1'")
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            get_forum_evidence(db, '1#0')


def test_batch_import_does_not_load_optional_libraries():
    import os
    import subprocess
    import sys
    code = '''import builtins
orig = builtins.__import__
def guarded(name, *a, **kw):
    if name.split('.')[0] in ('numpy', 'fastembed'):
        raise AssertionError(name)
    return orig(name, *a, **kw)
builtins.__import__ = guarded
from wq_forum_rag.batch_search import search_forum_batch, get_forum_evidence
'''
    subprocess.run([sys.executable, '-c', code], env=os.environ.copy(), check=True)


def test_search_budget_and_evidence_neighbor_boundaries(db):
    with sqlite3.connect(db) as conn:
        for n in range(1, 5):
            content = f'missing values {n} ' + 'x' * 250
            conn.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)', (f'1#{n}', '1', n, 1, 10+n*200, 10+n*200+len(content), content, f'hash-{n}'))
    result = search_forum_batch(db, 'missing values', evidence_chars=100, max_evidence_chars=200)
    assert sum(len(e['excerpt']) for r in result['results'] for e in r['evidence']) <= 200
    detail = get_forum_evidence(db, '1#2', before=2, after=2, max_chars=2000)
    assert detail['anchor_complete'] and len(detail['evidence']) == 5
    assert detail['evidence'][0]['chunk_id'] == '1#2'
    assert {e['chunk_index'] for e in detail['evidence']} == {0, 1, 2, 3, 4}
    assert all(e['document_start_offset'] <= e['document_end_offset'] for e in detail['evidence'])


def test_numeric_first_code_and_query_length(db):
    assert not search_forum_batch(db, 'missing 123abc', query_variants=['缺失值'])['results']
    with pytest.raises(ValueError):
        search_forum_batch(db, ' ' * 513 + 'x')


def test_id_fast_path_reports_only_actual_route_and_read_only(db):
    before = db.read_bytes()
    result = search_forum_batch(db, '1', query_variants=['缺失值'])
    assert result['executed_queries'] == ['1']
    assert [r['topic_id'] for r in result['results']] == ['1']
    assert result['results'][0]['matched_queries'] == ['1']
    get_forum_evidence(db, '1#0')
    assert db.read_bytes() == before


def test_whitespace_and_validation_boundaries(db):
    assert search_forum_batch(db, '   ')['results'] == []
    assert search_forum_batch(db, 'missing values', query_variants=['', '缺失值', '缺失值'])['executed_queries'] == ['missing values', '缺失值']
    for args in (dict(before=True), dict(after=3), dict(max_chars=0)):
        with pytest.raises(ValueError):
            get_forum_evidence(db, '1#0', **args)
    with pytest.raises(TypeError):
        search_forum_batch(db, 'missing', query_variants='缺失值')


def test_language_excludes_code_adjacent_to_chinese(db):
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE topics SET title='缺失值' WHERE topic_id='2'")
        conn.execute("UPDATE chunks SET content='缺失值ts_backfill以及123abc' WHERE chunk_id='2#0'")
    result = search_forum_batch(db, '缺失值')
    assert result['results'][0]['language'] == 'zh'


def test_sparse_duplicate_indices_actual_neighbors_and_overlap(db):
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM chunks WHERE topic_id='1'")
        for item, idx, start, level in [('a', 2, 0, 1), ('b', 7, 30, 1), ('c', 7, 40, 1), ('d', 20, 200, 1), ('e', 90, 300, 1), ('parent', 7, 0, 0)]:
            text = f'Title: sample\nBody:\n{item}' + 'z' * 40
            conn.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)', (item, '1', idx, level, start, start + len(text), text, 'version-' + item))
    result = get_forum_evidence(db, 'c', before=2, after=2, expected_content_hash='version-c')
    assert [item['chunk_id'] for item in result['evidence']] == ['c', 'b', 'a', 'd', 'e']
    assert result['evidence'][1]['overlaps_anchor'] is True
    assert result['evidence'][3]['overlaps_anchor'] is False
    assert result['evidence'][0]['chunk_content_hash'] == 'version-c'
    assert result['evidence'][0]['excerpt'].startswith('Title: sample\nBody:\n')
    assert get_forum_evidence(db, 'parent')['status'] == 'invalid_level'


def test_missing_fts_and_nondivisible_route_budgets_are_reported(db, monkeypatch):
    from wq_forum_rag import batch_search
    calls = []
    def candidates(conn, query, *, kind, limit):
        calls.append((query, limit))
        return []
    monkeypatch.setattr(batch_search, 'candidate_ids', candidates)
    result = search_forum_batch(db, 'missing', query_variants=['缺失值', 'other'])
    assert calls == [('missing', 66), ('缺失值', 66), ('other', 66)]
    assert result['index_status'] == 'degraded'
    assert result['budget']['route_candidate_limits'] == [66, 66, 66]
    assert 'version' in result['hash_note'].lower()


def test_duplicate_routes_count_once_and_status_distinguishes_fts(db):
    one = search_forum_batch(db, 'missing values')
    duplicate = search_forum_batch(db, 'missing values', query_variants=[' MISSING VALUES '])
    assert duplicate['executed_queries'] == ['missing values']
    assert duplicate['results'] == one['results']
    assert duplicate['results'][0]['matched_queries'] == ['missing values']
    assert one['index_status'] == 'degraded'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE VIRTUAL TABLE document_fts USING fts5(kind UNINDEXED,record_id UNINDEXED,topic_id UNINDEXED,title,community,content)')
    no_matches = search_forum_batch(db, 'missing')
    assert no_matches['results'] == [] and no_matches['index_status'] == 'ready'
    assert search_forum_batch(db, '1')['index_status'] == 'not_used'
    assert search_forum_batch(db, '')['index_status'] == 'not_queried'


def test_literal_policy_and_hash_metadata_are_explicit(db):
    result = search_forum_batch(db, 'missing 2024', query_variants=['缺失值'])
    assert '2' in [hit['topic_id'] for hit in result['results']]
    assert 'five digits' in result['literal_note'] and 'same chunk' in result['literal_note']
    assert 'version' in result['hash_note'] and 'not hashes' in result['hash_note']
    evidence = get_forum_evidence(db, '1#0', expected_content_hash='wrong')
    assert evidence['status'] == 'stale' and 'version' in evidence['hash_note']


def test_sql_progress_handler_interrupts_long_statement(db, monkeypatch):
    from wq_forum_rag import batch_search
    monkeypatch.setattr(batch_search, '_TIMEOUT_SECONDS', 0.005)
    def long_query(conn, query, **kwargs):
        conn.execute('''WITH RECURSIVE numbers(n) AS (
            SELECT 0 UNION ALL SELECT n+1 FROM numbers WHERE n<10000000
        ) SELECT SUM(n) FROM numbers''').fetchone()
        return []
    monkeypatch.setattr(batch_search, 'candidate_ids', long_query)
    with pytest.raises(TimeoutError, match='time budget'):
        search_forum_batch(db, 'missing')


@pytest.mark.parametrize('identifier', ['123_abc', '_field'])
def test_all_underscore_identifiers_remain_literal_constraints(db, identifier):
    result = search_forum_batch(db, f'missing {identifier}', query_variants=['缺失值'])
    assert result['results'] == []


def test_long_numeric_topic_id_does_not_require_id_in_content(db):
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO topics VALUES ('928371','Long ID title','community','url/long','topic-version')")
        conn.execute("INSERT INTO chunks VALUES ('long#0','928371',0,1,0,16,'body without id','chunk-version')")
    result = search_forum_batch(db, '928371', query_variants=['缺失值'])
    assert result['executed_queries'] == ['928371']
    assert [hit['topic_id'] for hit in result['results']] == ['928371']
    assert result['results'][0]['evidence'][0]['chunk_id'] == 'long#0'
    assert search_forum_batch(db, '928372', query_variants=['缺失值'])['results'] == []


def test_evidence_assembly_only_processes_requested_hits(db, monkeypatch):
    from wq_forum_rag import batch_search
    excerpts, languages = [], []
    original_excerpt = batch_search._excerpt
    original_language = batch_search._language
    def counted_excerpt(row, *args):
        excerpts.append(row['chunk_id'])
        return original_excerpt(row, *args)
    def counted_language(*args):
        languages.append(1)
        return original_language(*args)
    monkeypatch.setattr(batch_search, '_excerpt', counted_excerpt)
    monkeypatch.setattr(batch_search, '_language', counted_language)
    one = search_forum_batch(db, 'missing values', query_variants=['缺失值'], top_k=1)
    assert len(excerpts) == len(languages) == 1
    assert one['budget']['returned_evidence_chars'] == len(one['results'][0]['evidence'][0]['excerpt'])
    excerpts.clear()
    languages.clear()
    two = search_forum_batch(db, 'missing values', query_variants=['缺失值'], top_k=2)
    assert len(excerpts) == len(languages) == 2
    assert one['results'] == two['results'][:1]


@pytest.mark.parametrize('missing_table', ['topics', 'chunks'])
@pytest.mark.parametrize('query', ['missing', '1', ''])
def test_missing_forum_tables_report_unavailable(db, missing_table, query):
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE VIRTUAL TABLE document_fts USING fts5(kind UNINDEXED,record_id UNINDEXED,topic_id UNINDEXED,title,community,content)')
        conn.execute(f'DROP TABLE {missing_table}')
    result = search_forum_batch(db, query)
    assert result['index_status'] == 'unavailable'
    assert result['results'] == []
    evidence = get_forum_evidence(db, '1#0')
    assert evidence['status'] == 'unavailable'
    assert evidence['evidence'] == []


def test_internal_whitespace_duplicate_routes_preserve_score_and_budget(db, monkeypatch):
    from wq_forum_rag import batch_search
    calls = []
    def candidates(conn, query, *, kind, limit):
        calls.append((query, limit))
        return ['1#0'] if query == 'missing values' else ['2#0']
    monkeypatch.setattr(batch_search, 'candidate_ids', candidates)
    baseline = search_forum_batch(db, 'missing values', query_variants=['缺失值'])
    calls.clear()
    duplicate = search_forum_batch(db, 'missing values', query_variants=[' MISSING  VALUES ', '缺失值'])
    assert calls == [('missing values', 100), ('缺失值', 100)]
    assert duplicate['executed_queries'] == baseline['executed_queries']
    assert duplicate['results'] == baseline['results']
    assert duplicate['budget'] == baseline['budget']


def test_no_results_without_fts_remains_degraded(db):
    result = search_forum_batch(db, 'nonexistentword')
    assert result['results'] == []
    assert result['index_status'] == 'degraded'
