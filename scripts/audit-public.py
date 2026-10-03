#!/usr/bin/env python3
"""Audit a public working tree, final index, or committed tree without logging contents."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import shutil
import hashlib

ROOT = Path(__file__).resolve().parents[1]
RULES = [
    ('internal_brand', re.compile(r'\x73apira|\x70haro', re.I)),
    ('private_workspace', re.compile(r'/Users/[A-Za-z0-9_-]+/')),
    ('credential', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{20,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}')),
    ('private_service', re.compile(r'https://[a-zA-Z0-9-]+\.up\.railway\.app')),
]
FORBIDDEN = re.compile(r'(^|/)(?:\.runtime|\.toolchains|\.venv|node_modules|__pycache__|docs/goals)(?:/|$)|(?:^|/)(?:backend-config\.json|desktop-api-key|.*\.(?:sqlite3?|db|wav|mp3|m4a))$')


def git(*args):
    return subprocess.check_output((['rtk', 'proxy'] if shutil.which('rtk') else []) + ['git', *args], cwd=ROOT)


def audit(mode, tree=None):
    if mode == 'index':
        entries = [line.split('\t', 1) for line in git('ls-files', '--stage').decode().splitlines()]
        files = [(path, meta.split()[1]) for meta, path in entries if not meta.startswith('160000 ')]
    elif mode == 'tree':
        files = [(line.split('\t', 1)[1], line.split()[2]) for line in git('ls-tree', '-r', tree).decode().splitlines() if line.split()[1] == 'blob']
    else:
        files = [(path, None) for path in set(git('ls-files', '--cached', '--others', '--exclude-standard').decode().splitlines()) if (ROOT/path).is_file()]
    violations = []
    for path, blob in files:
        if FORBIDDEN.search(path) and path != 'frontend/src-tauri/tests/fixtures/he_aac_48k_5s.m4a': violations.append({'path': path, 'rule': 'private_or_runtime_path'})
        for label, pattern in RULES:
            if pattern.search(path): violations.append({'path': path, 'rule': label})
        content = git('show', blob) if blob else (ROOT/path).read_bytes()
        if path == 'frontend/src-tauri/tests/fixtures/he_aac_48k_5s.m4a' and hashlib.sha256(content).hexdigest() != 'ca0d4d2c4dbaf5933e4e2edd73f3ac0e5c9a5823b7bb34a090415039035102a2':
            violations.append({'path': path, 'rule': 'modified_upstream_audio_fixture'})
        if b'\x00' in content: continue
        try: text = content.decode('utf-8')
        except UnicodeDecodeError: continue
        for label, pattern in RULES:
            if pattern.search(text): violations.append({'path': path, 'rule': label})
    return {'mode': mode, 'tree': tree, 'files': len(files), 'violations': violations}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group()
    group.add_argument('--index', action='store_true')
    group.add_argument('--tree')
    p.add_argument('--history-since', help='Also audit every new commit tree after this upstream base')
    args = p.parse_args()
    result = [audit('index' if args.index else 'tree' if args.tree else 'working', args.tree)]
    if args.history_since:
        result += [audit('tree', commit) for commit in git('rev-list', args.history_since + '..HEAD').decode().splitlines()]
    passed = all(not r['violations'] for r in result)
    print(json.dumps({'status': 'PASS' if passed else 'FAIL', 'audits': result}, indent=2))
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__': main()
