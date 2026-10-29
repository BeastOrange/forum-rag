import importlib
import json
import sqlite3

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
