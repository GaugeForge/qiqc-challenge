"""Read-only synthetic code examples; no submitted program is executed."""
import io
import json
import zipfile
from qiqc.archive import inspect_archive, reviewer_snippet


def test_snippet_redacts_before_cutting():
    text = 'API_KEY=synthetic-secret Bearer synthetic-token\n-----BEGIN PRIVATE KEY-----\nprivate-blob\n-----END PRIVATE KEY-----\nprint("public")'
    got = reviewer_snippet(text)
    for secret in ('synthetic-secret', 'synthetic-token', 'private-blob'):
        assert secret not in got
    assert 'print("public")' in got


def test_utf8_and_global_snippet_caps():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('harness.json', json.dumps({'schema':'qiqc-harness/v1-demo','name':'fixture','entrypoint':'agent0.py'}))
        for n in range(20):
            archive.writestr(f'agent{n}.py', '科学\n' * 3000)
    report = inspect_archive(stream.getvalue())
    assert len(report['snippets']) <= 15
    assert all(len(item['text'].encode()) <= 2048 and item['untrusted']=='true' for item in report['snippets'])
    assert sum(len(item['text'].encode()) for item in report['snippets']) <= 16384
