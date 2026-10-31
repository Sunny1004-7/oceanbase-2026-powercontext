"""Runtime self-check for group-relay using the real SDK + HTTP.

Verifies things the contract test does not cover:
  * the SPA and app.js are served and carry the new write controls
  * a second process on the SAME data dir is refused (exclusive lock)
  * the 500-event cap refuses writes while reads/reports still work

Each server is launched with subprocess.Popen; only the exact saved PID is
terminated. Run with the project's Python (dev venv or the app .venv).
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = sys.executable


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def start(data_dir, port):
    log = open(ROOT / 'evidence' / 'runtime-server.log', 'ab')
    p = subprocess.Popen(
        [PY, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
         '--port', str(port), '--app-dir', str(ROOT), '--workers', '1'],
        env={**os.environ, 'DATA_DIR': str(data_dir)},
        stdout=log, stderr=subprocess.STDOUT,
    )
    base = f'http://127.0.0.1:{port}'
    for _ in range(150):
        if p.poll() is not None:
            raise RuntimeError(f'server exited early (code {p.returncode}); see evidence/runtime-server.log')
        try:
            with httpx.Client(trust_env=False, timeout=1) as c:
                if c.get(base + '/api/projects').status_code == 200:
                    return p, base
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    p.terminate(); p.wait(timeout=20)
    raise RuntimeError('server startup timed out')


def main():
    (ROOT / 'evidence').mkdir(exist_ok=True)
    results = []

    def check(name, ok, detail=''):
        results.append({'check': name, 'passed': bool(ok), 'detail': detail})
        print(('PASS' if ok else 'FAIL'), name, detail)

    data = pathlib.Path(tempfile.mkdtemp(prefix='group-relay-runtime-'))
    port = free_port()
    proc, base = start(data, port)
    pid = proc.pid
    try:
        with httpx.Client(trust_env=False, timeout=30) as c:
            # SPA + static assets carry the new controls
            html = c.get(base + '/').text
            js = c.get(base + '/static/app.js').text
            check('SPA served', 'id="review-submit-btn"' in html and 'id="pref-focus"' in html)
            check('app.js has write controls',
                  'action_id' in js and 'expected_revision' in js and 'randomUUID' in js)

            # create project + import sample
            r = c.post(base + '/api/projects', json={'name': '运行时自检', 'summary': 'runtime'})
            scope = r.json()['scope_id']
            st = c.get(base + f'/api/projects/{scope}/state').json()
            md = (ROOT / 'samples' / 'algae-experiment.md').read_text(encoding='utf-8')
            r = c.post(base + f'/api/projects/{scope}/import',
                       data={'format': 'markdown', 'content': md,
                             'action_id': 'runtime-import-1', 'expected_revision': st['revision']})
            check('sample import (multi-section MD)', r.status_code == 200, r.text[:120])
            st = c.get(base + f'/api/projects/{scope}/state').json()
            check('import produced reviews+tasks', len(st['reviews']) >= 2 and len(st['tasks']) >= 7,
                  f"reviews={len(st['reviews'])} tasks={len(st['tasks'])}")

            # second process on same data dir must be refused
            port2 = free_port()
            log2 = open(ROOT / 'evidence' / 'runtime-second.log', 'wb')
            p2 = subprocess.Popen(
                [PY, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
                 '--port', str(port2), '--app-dir', str(ROOT), '--workers', '1'],
                env={**os.environ, 'DATA_DIR': str(data)},
                stdout=log2, stderr=subprocess.STDOUT,
            )
            try:
                code2 = p2.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p2.terminate(); code2 = p2.wait(timeout=20)
            log2.close()
            msg = (ROOT / 'evidence' / 'runtime-second.log').read_text(encoding='utf-8', errors='replace')
            check('second process refused on same DATA_DIR',
                  code2 != 0 and ('占用' in msg or '拒绝启动' in msg), f'exit={code2}')

            # event cap: fill to 500 then confirm refusal + readable report
            rev = st['revision']
            written = 0
            refused = False
            for i in range(600):
                r = c.post(base + f'/api/projects/{scope}/preferences',
                           json={'preferences': {'report_title': f'cap-{i}'},
                                 'action_id': f'cap-{i}', 'expected_revision': rev})
                if r.status_code == 200:
                    rev = r.json()['sequence']
                    written += 1
                else:
                    refused = True
                    break
            st = c.get(base + f'/api/projects/{scope}/state').json()
            rep = c.get(base + f'/api/projects/{scope}/report')
            check('event cap refuses writes at 500',
                  refused and st['event_count'] == 500,
                  f"written={written} event_count={st['event_count']}")
            check('report still readable at cap', rep.status_code == 200 and rep.json()['report'].startswith('# '))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=10)

    out = {'passed': all(r['passed'] for r in results), 'pid': pid, 'data': str(data), 'checks': results}
    (ROOT / 'evidence' / 'runtime-results.json').write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': out['passed'], 'pid': pid}, ensure_ascii=False))
    return 0 if out['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())