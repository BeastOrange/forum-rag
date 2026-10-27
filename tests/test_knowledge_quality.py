from __future__ import annotations

import sqlite3

from wq_forum_rag.evolution import EvolutionService
from test_evolution import _index_fixture


def propose(service, slug, links=None):
    return service.propose_knowledge_page(
        slug=slug, title='Evidence guide', summary='A sufficiently detailed summary for this page.',
        body='A sufficiently detailed body explaining the evidence and its implications.',
        source_topic_ids=['t1'], confidence=0.99, links=links,
        source_evidence=[{'topic_id': 't1', 'claim': 'Test decay', 'excerpt': 'Alpha decay should be tested'}],
    )


def test_review_conflicts_and_changed_sources(tmp_path):
    from wq_forum_rag.knowledge_quality import eligible_knowledge_slugs
    db = _index_fixture(tmp_path)
    service = EvolutionService(db)
    assert not propose(service, 'alpha/one')['auto_published']
    assert not service.publish_knowledge_page('alpha/one')['published']
    service.review_knowledge_page('alpha/one', 'reviewed', note='Evidence checked')
    assert service.publish_knowledge_page('alpha/one')['published']
    with sqlite3.connect(db) as conn:
        assert eligible_knowledge_slugs(conn) == {'alpha/one'}
        assert conn.execute("SELECT count(*) FROM document_fts WHERE kind='knowledge_page' AND record_id='alpha/one'").fetchone()[0] == 1
        assert conn.execute("SELECT record_id FROM document_fts WHERE document_fts MATCH 'Evidence' AND kind='knowledge_page'").fetchone()[0] == 'alpha/one'
    propose(service, 'alpha/two')
    service.link_knowledge_pages('alpha/two', 'alpha/one', 'conflicts_with')
    with sqlite3.connect(db) as conn:
        assert not eligible_knowledge_slugs(conn)
    assert not service.publish_knowledge_page('alpha/one')['published']
    assert not propose(service, 'alpha/one')['auto_published']
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE topics SET content_hash='changed' WHERE topic_id='t1'")
    assert 'source_changed' in {i['code'] for i in service.lint_knowledge('alpha/one')['issues']}


def test_dependencies_propagate_and_legacy_is_read_only(tmp_path):
    from wq_forum_rag.knowledge_quality import eligible_knowledge_slugs
    db = _index_fixture(tmp_path)
    service = EvolutionService(db)
    for slug in ('alpha/base', 'alpha/child', 'alpha/related'):
        propose(service, slug)
        service.review_knowledge_page(slug, 'reviewed', note='Checked')
        assert service.publish_knowledge_page(slug)['published']
    service.link_knowledge_pages('alpha/child', 'alpha/base', 'depends_on')
    service.link_knowledge_pages('alpha/related', 'alpha/base', 'related_to')
    service.review_knowledge_page('alpha/base', 'archived', note='Superseded', replacement_slug='alpha/related')
    with sqlite3.connect(db) as conn:
        assert eligible_knowledge_slugs(conn) == {'alpha/related'}
    assert any(e['event_type'] == 'status:archived' and 'Superseded' in e['note'] for e in service.get_knowledge_page('alpha/base')['events'])
    legacy = sqlite3.connect(':memory:')
    legacy.execute('CREATE TABLE knowledge_pages(slug TEXT, status TEXT)')
    legacy.execute("INSERT INTO knowledge_pages VALUES ('old', 'published')")
    before = legacy.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    assert eligible_knowledge_slugs(legacy) == set()
    assert legacy.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == before


def test_missing_evidence_missing_topic_and_cycles(tmp_path):
    from wq_forum_rag.knowledge_quality import eligible_knowledge_slugs
    import pytest

    db = _index_fixture(tmp_path)
    service = EvolutionService(db)
    service.propose_knowledge_page(slug='alpha/no-evidence', title='Guide',
        summary='This summary contains sufficient context.', body='This body contains sufficient supporting discussion.',
        source_topic_ids=['t1'], confidence=1.0)
    with pytest.raises(ValueError, match='missing_support'):
        service.review_knowledge_page('alpha/no-evidence', 'reviewed', note='Checked')
    for slug in ('alpha/cycle-a', 'alpha/cycle-b'):
        propose(service, slug)
        service.review_knowledge_page(slug, 'reviewed', note='Checked')
        assert service.publish_knowledge_page(slug)['published']
    service.link_knowledge_pages('alpha/cycle-a', 'alpha/cycle-b', 'depends_on')
    service.link_knowledge_pages('alpha/cycle-b', 'alpha/cycle-a', 'depends_on')
    with sqlite3.connect(db) as conn:
        assert not eligible_knowledge_slugs(conn)
        conn.execute("DELETE FROM topics WHERE topic_id='t1'")
    assert 'source_missing' in {i['code'] for i in service.lint_knowledge('alpha/cycle-a')['issues']}


def test_legacy_schema_migrates_only_on_write_and_failed_proposal_rolls_back(tmp_path):
    from wq_forum_rag.knowledge_quality import eligible_knowledge_slugs
    import pytest

    db = _index_fixture(tmp_path)
    service = EvolutionService(db)
    propose(service, 'alpha/legacy')
    with sqlite3.connect(db) as conn:
        conn.execute('ALTER TABLE knowledge_pages DROP COLUMN review_status')
        for column in ('source_content_hash', 'claim', 'excerpt'):
            conn.execute(f'ALTER TABLE knowledge_sources DROP COLUMN {column}')
        before = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        assert eligible_knowledge_slugs(conn) == set()
        assert conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == before
    assert 'review_required' in {issue['code'] for issue in service.lint_knowledge('alpha/legacy')['issues']}
    propose(service, 'alpha/legacy')
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT source_content_hash FROM knowledge_sources WHERE slug='alpha/legacy'").fetchone()[0]
    with pytest.raises(ValueError, match='both source_slug'):
        propose(service, 'alpha/legacy', links=[{'target_slug': 'alpha/missing'}])
    assert len(service.get_knowledge_page('alpha/legacy')['sources']) == 1


def _quality_db():
    conn = sqlite3.connect(':memory:')
    conn.executescript('''
        CREATE TABLE knowledge_pages(slug TEXT, status TEXT, review_status TEXT);
        CREATE TABLE knowledge_sources(slug TEXT, topic_id TEXT, source_content_hash TEXT, claim TEXT, excerpt TEXT);
        CREATE TABLE knowledge_links(source_slug TEXT, target_slug TEXT, relation_type TEXT);
        CREATE TABLE topics(topic_id TEXT, content_hash TEXT, title TEXT, body_text TEXT, comments_json TEXT);
    ''')
    return conn


def test_comment_evidence_uses_decoded_content_only():
    import json
    from wq_forum_rag.knowledge_quality import quality_issues

    conn = _quality_db()
    content = '中文第一行\n他说 "quoted" and \\path'
    comments = json.dumps([{'content_text': content, 'author': 'author-secret',
                            'id': 'comment-secret', 'datetime': 'time-secret'}])
    conn.execute('INSERT INTO topics VALUES (?, ?, ?, ?, ?)', ('t', 'hash', '', '', comments))
    for slug, excerpt in [('body', content), ('author', 'author-secret'),
                          ('id', 'comment-secret'), ('time', 'time-secret')]:
        conn.execute('INSERT INTO knowledge_pages VALUES (?, ?, ?)', (slug, 'published', 'reviewed'))
        conn.execute('INSERT INTO knowledge_sources VALUES (?, ?, ?, ?, ?)', (slug, 't', 'hash', 'claim', excerpt))
    issues = quality_issues(conn)
    assert 'missing_support' not in issues['body']
    assert all('missing_support' in issues[slug] for slug in ('author', 'id', 'time'))
    conn.execute("UPDATE topics SET comments_json = '{broken'")
    assert 'missing_support' in quality_issues(conn)['body']


def test_long_dependency_chain_is_read_only_and_propagates():
    from wq_forum_rag.knowledge_quality import eligible_knowledge_slugs

    conn = _quality_db()
    count = 1200
    conn.execute("INSERT INTO topics VALUES ('t', 'hash', 'support', '', '[]')")
    conn.executemany('INSERT INTO knowledge_pages VALUES (?, ?, ?)',
                     [(f'p{i:04}', 'published', 'reviewed') for i in range(count)])
    conn.executemany('INSERT INTO knowledge_sources VALUES (?, ?, ?, ?, ?)',
                     [(f'p{i:04}', 't', 'hash', 'claim', 'support') for i in range(count)])
    conn.executemany('INSERT INTO knowledge_links VALUES (?, ?, ?)',
                     [(f'p{i:04}', f'p{i+1:04}', 'depends_on') for i in range(count - 1)])
    before = conn.total_changes
    assert len(eligible_knowledge_slugs(conn)) == count
    assert conn.total_changes == before
    conn.execute("UPDATE knowledge_pages SET review_status='needs_review' WHERE slug='p1199'")
    assert eligible_knowledge_slugs(conn) == set()
    conn.execute("UPDATE knowledge_pages SET review_status='reviewed' WHERE slug='p1199'")
    conn.execute("INSERT INTO knowledge_links VALUES ('p1199', 'p0000', 'depends_on')")
    assert eligible_knowledge_slugs(conn) == set()
