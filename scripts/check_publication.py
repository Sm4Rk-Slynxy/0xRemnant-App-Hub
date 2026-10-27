#!/usr/bin/env python3
"""Scan the allowlisted release files, including inside bundled example ZIPs.

This is a conservative release guard, not a substitute for manual review or a
full secret scanner. Matches report a rule and location, never the matched value.
"""
import io
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    '.dockerignore', '.editorconfig', '.env.example', '.gitattributes', '.gitignore',
    'Dockerfile', 'LICENSE', 'README.md', 'TESTING.md', 'SECURITY.md', 'CONTRIBUTING.md',
    'CHANGELOG.md', 'VERSION', 'compose.yaml', 'compose.selinux.yaml', 'compose.sh',
    'install-docker.sh', 'start.sh',
}
TREES = {'apphub', 'tests', 'scripts', 'examples', 'docs', '.github'}
EXAMPLES = {'examples/hello-app.zip', 'examples/python-counter.zip'}
SKIP = {'__pycache__', '.pytest_cache', 'node_modules', '.git'}
RULES = {
    'private key': re.compile(rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----'),
    'GitHub token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'cloud access key': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'API token': re.compile(rb'\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}\b'),
    'Slack token': re.compile(rb'\bxox[baprs]-[A-Za-z0-9-]{20,}\b'),
    'Google API key': re.compile(rb'\bAIza[A-Za-z0-9_-]{30,}\b'),
    'embedded JWT': re.compile(rb'\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b'),
    'URL credential': re.compile(rb'https?://[^\s/@:]+:[^\s/@]+@'),
    'personal home path': re.compile(rb'/(?:home|Users)/[A-Za-z0-9_.-]+/'),
    'personal media path': re.compile(rb'/(?:run/)?media/[A-Za-z0-9_.-]+/'),
}
PRIVATE_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.sqlite', '.sqlite3', '.db', '.log', '.bak', '.dump'}


def release_files(root=ROOT):
    result = []
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        if any(part in SKIP for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f'Symlink is not allowed: {relative}')
        if not path.is_file():
            continue
        if (len(relative.parts) == 1 and relative.as_posix() in ROOT_FILES) or relative.parts[0] in TREES:
            result.append(path)
    return sorted(result)


def inspect_bytes(name, data, findings, depth=0):
    path = PurePosixPath(name.split('!')[-1])
    if path.suffix.lower() in PRIVATE_SUFFIXES or (path.name.startswith('.env') and path.name != '.env.example') or path.name in {'.setup-token', 'credentials.json', '.npmrc', '.netrc'}:
        findings.append(f'{name}: private/runtime filename')
    if any(part in {'.ssh', '.aws', '.azure', '.kube', 'data', 'backups'} for part in path.parts):
        findings.append(f'{name}: private/runtime directory')
    if name.endswith('.zip'):
        if depth or name not in EXAMPLES:
            findings.append(f'{name}: archive is not an approved example')
            return
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 2_000_000:
                findings.append(f'{name}: unexpectedly large example archive')
                return
            for info in archive.infolist():
                member = PurePosixPath(info.filename)
                if member.is_absolute() or '..' in member.parts or '\\' in info.filename:
                    findings.append(f'{name}: unsafe archive path')
                    continue
                if not info.is_dir():
                    inspect_bytes(name+'!'+info.filename, archive.read(info), findings, depth+1)
        return
    if name.endswith('.png'):
        if not name.startswith('docs/screenshots/') or not data.startswith(b'\x89PNG\r\n\x1a\n'):
            findings.append(f'{name}: unexpected image')
        return  # Screenshot pixels must also be inspected by a person.
    for rule, pattern in RULES.items():
        for match in pattern.finditer(data):
            line = data[:match.start()].count(b'\n') + 1
            findings.append(f'{name}:{line}: {rule}')


def check(root=ROOT):
    files = release_files(root)
    findings = []
    for path in files:
        inspect_bytes(path.relative_to(root).as_posix(), path.read_bytes(), findings)
    if findings:
        raise ValueError('Publication scan failed:\n'+'\n'.join(findings))
    return files


if __name__ == '__main__':
    try:
        files = check()
        print(f'PASS: {len(files)} release files and approved example ZIP contents scanned; no configured secret-pattern matches or private runtime files.')
        print('Limits: no entropy/history scan, no detection of every hard-coded secret, and no image OCR. Review screenshots manually.')
    except (ValueError, zipfile.BadZipFile) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
