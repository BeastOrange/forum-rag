import importlib
import json
import sqlite3

import pytest

from typer.testing import CliRunner
from wq_forum_rag.cli import app
from wq_forum_rag.storage import ForumStore


def diagnose(path):
    return importlib.import_module('wq_forum_rag.diagnostics').diagnose(path)


def test_missing_database_is_reported_without_creation(tmp_path):
    path = tmp_path / 'missing.sqlite3'
    report = diagnose(path)
    assert report['status'] == 'unavailable'
    assert not path.exists()
    assert report['client_connection'] == 'unverified'


def test_existing_index_is_read_only_and_optional_tables_are_not_required(tmp_path):
    path = tmp_path / 'index.sqlite3'
    with ForumStore(path):
        pass
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE VIRTUAL TABLE document_fts USING fts5(kind UNINDEXED, record_id UNINDEXED, topic_id UNINDEXED, title, community, content)')
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    report = diagnose(path)
    assert report['status'] == 'ready'
    assert report['forum_tables'] == {'topics': True, 'chunks': True}
    assert report['fts_available'] is True
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_missing_fts_is_degraded_and_partial_schema_unavailable(tmp_path):
    path = tmp_path / 'old.sqlite3'
    with ForumStore(path):
        pass
    assert diagnose(path)['status'] == 'degraded'
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE chunks')
    assert diagnose(path)['status'] == 'unavailable'


def test_invalid_database_returns_actionable_error(tmp_path):
    path = tmp_path / 'bad.sqlite3'
    path.write_bytes(b'not a database')
    report = diagnose(path)
    assert report['status'] == 'error'
    assert report['reason']
    assert path.read_bytes() == b'not a database'


def test_doctor_cli_is_json_and_does_not_create_database(tmp_path):
    path = tmp_path / 'missing.sqlite3'
    result = CliRunner().invoke(app, ['doctor', '--db', str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['status'] == 'unavailable'
    assert not path.exists()


def test_incomplete_columns_do_not_claim_ready(tmp_path):
    path = tmp_path / 'partial.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE topics(topic_id TEXT)')
        conn.execute('CREATE TABLE chunks(chunk_id TEXT)')
    report = diagnose(path)
    assert report['status'] == 'unavailable'
    assert 'content' in report['missing_columns']['chunks']


def test_content_only_fts_does_not_claim_ready(tmp_path):
    path = tmp_path / 'fts.sqlite3'
    with ForumStore(path):
        pass
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE VIRTUAL TABLE document_fts USING fts5(content)')
    report = diagnose(path)
    assert report['status'] == 'unavailable'
    assert 'record_id' in report['missing_columns']['document_fts']


def make_document_index(tmp_path):
    from wq_forum_rag.mcp_server import ingest_docs
    directory = tmp_path / 'Documents'
    directory.mkdir()
    (directory / 'Operators.md').write_text('# Operators\nA small local operator reference.\n')
    path = tmp_path / 'documents.sqlite3'
    result = ingest_docs(str(directory), db=str(path))
    assert result['indexed_documents'] == 1
    return path


def test_documents_only_installation_is_ready_and_read_only(tmp_path):
    path = make_document_index(tmp_path)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    report = diagnose(path)
    assert report['status'] == 'ready'
    assert report['forum_tables'] == {'topics': False, 'chunks': False}
    assert report['source_status']['forum']['status'] == 'absent'
    assert report['source_status']['documents']['status'] == 'available'
    assert report['missing_columns'] == {}
    assert report['fts_available'] is True
    assert report['actions'] == []
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_usable_documents_and_partial_forum_are_degraded(tmp_path):
    path = make_document_index(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE topics(topic_id TEXT)')
    report = diagnose(path)
    assert report['status'] == 'degraded'
    assert report['source_status']['documents']['status'] == 'available'
    assert report['source_status']['forum']['status'] == 'incomplete'
    assert 'chunks' in report['missing_columns']
    assert report['actions']


def test_document_columns_required_by_actual_search_are_checked(tmp_path):
    path = make_document_index(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute('ALTER TABLE doc_chunks DROP COLUMN chunk_index')
    report = diagnose(path)
    assert report['status'] == 'unavailable'
    assert 'chunk_index' in report['missing_columns']['doc_chunks']
    assert report['source_status']['documents']['status'] == 'incomplete'


def test_documents_missing_fts_or_sdk_are_degraded(tmp_path, monkeypatch):
    path = make_document_index(tmp_path)
    module = importlib.import_module('wq_forum_rag.diagnostics')
    monkeypatch.setattr(module, 'version', lambda name: '1.0.0')
    report = diagnose(path)
    assert report['status'] == 'degraded'
    assert not report['mcp_compatible']
    monkeypatch.setattr(module, 'version', lambda name: '2.3.0')
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE document_fts')
    report = diagnose(path)
    assert report['status'] == 'degraded'
    assert report['fts_available'] is False


def test_no_configured_source_does_not_recommend_reindex(tmp_path):
    path = tmp_path / 'empty.sqlite3'
    with sqlite3.connect(path):
        pass
    report = diagnose(path)
    assert report['status'] == 'unavailable'
    assert all(x['status'] == 'absent' for x in report['source_status'].values())
    assert not any('reindex' in action.lower() for action in report['actions'])


@pytest.mark.parametrize(('table', 'column'), [
    ('documents', 'slug'), ('documents', 'title'), ('documents', 'source_path'),
    ('doc_chunks', 'chunk_id'), ('doc_chunks', 'slug'),
    ('doc_chunks', 'chunk_index'), ('doc_chunks', 'content'),
])
def test_every_document_search_column_is_required(tmp_path, table, column):
    path = tmp_path / 'partial-docs.sqlite3'
    definitions = {'documents': ['slug', 'title', 'source_path'],
                   'doc_chunks': ['chunk_id', 'slug', 'chunk_index', 'content']}
    definitions[table].remove(column)
    with sqlite3.connect(path) as conn:
        for name, columns in definitions.items():
            conn.execute(f"CREATE TABLE {name} ({', '.join(c + ' TEXT' for c in columns)})")
    report = diagnose(path)
    assert report['status'] == 'unavailable'
    assert column in report['missing_columns'][table]


def test_minimal_document_search_schema_does_not_require_unused_columns(tmp_path):
    path = tmp_path / 'minimal-docs.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.executescript("""CREATE TABLE documents(slug TEXT,title TEXT,source_path TEXT);
            CREATE TABLE doc_chunks(chunk_id TEXT,slug TEXT,chunk_index INTEGER,content TEXT);
            CREATE VIRTUAL TABLE document_fts USING fts5(kind UNINDEXED,record_id UNINDEXED,topic_id UNINDEXED,title,community,content);""")
    assert diagnose(path)['status'] == 'ready'


def test_forum_with_partial_documents_is_degraded(tmp_path):
    path = tmp_path / 'forum.sqlite3'
    with ForumStore(path):
        pass
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE documents(slug TEXT)')
        conn.execute('CREATE VIRTUAL TABLE document_fts USING fts5(kind UNINDEXED,record_id UNINDEXED,topic_id UNINDEXED,title,community,content)')
    report = diagnose(path)
    assert report['status'] == 'degraded'
    assert report['source_status']['forum']['status'] == 'available'
    assert report['source_status']['documents']['status'] == 'incomplete'


def test_documents_without_sdk_and_with_invalid_fts(tmp_path, monkeypatch):
    path = make_document_index(tmp_path)
    module = importlib.import_module('wq_forum_rag.diagnostics')
    def absent(name):
        raise module.PackageNotFoundError(name)
    monkeypatch.setattr(module, 'version', absent)
    report = diagnose(path)
    assert report['status'] == 'degraded'
    assert report['mcp_version'] is None
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE document_fts')
        conn.execute('CREATE TABLE document_fts(kind TEXT,record_id TEXT,topic_id TEXT,title TEXT,community TEXT,content TEXT)')
    report = diagnose(path)
    assert report['status'] == 'unavailable'
    assert report['fts_available'] is False
    assert 'FTS' in report['reason']
