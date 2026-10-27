#!/usr/bin/env python3
"""Build a source-only release ZIP from the same files checked for publication."""
import argparse
import hashlib
from pathlib import Path
import zipfile
from check_publication import ROOT, check

parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, default=ROOT.parent/'0xRemnant-App-Hub.zip')
args = parser.parse_args()
files = check()
output = args.output.resolve()
if output.is_relative_to(ROOT):
    parser.error('Keep release archives outside the source directory.')
output.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in files:
        archive.write(path, '0xRemnant-App-Hub/'+path.relative_to(ROOT).as_posix())
with zipfile.ZipFile(output) as archive:
    if archive.testzip() is not None:
        raise SystemExit('ZIP integrity check failed')
digest = hashlib.sha256(output.read_bytes()).hexdigest()
output.with_suffix('.sha256').write_text(digest+'  '+output.name+'\n')
print(f'Created {output.name}: {len(files)} files; SHA-256 {digest}')
