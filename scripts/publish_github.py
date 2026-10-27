#!/usr/bin/env python3
"""Publish the checked source using an already authenticated GitHub CLI.

Run explicitly from the prepared copy on the machine where `gh auth status`
succeeds. This creates a PUBLIC repository and pushes its source and screenshots.
No credentials are read, copied, printed, or stored by this script.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
from check_publication import ROOT, check


def run(*args, capture=False):
    return subprocess.run(args, cwd=ROOT, check=True, text=True,
        stdout=subprocess.PIPE if capture else None).stdout


def main():
    if not shutil.which('gh'):
        raise SystemExit('GitHub CLI (gh) is not available in this environment. Run this script on your authenticated workstation.')
    if not shutil.which('git'):
        raise SystemExit('Git is required.')
    run('gh','auth','status','--hostname','github.com')
    files = check()
    profile = json.loads(run('gh','api','user',capture=True))
    login, identifier = profile['login'], profile['id']
    repository = login+'/0xRemnant-App-Hub'
    # A prepared copy may already have its first local commit. Never reuse an
    # enclosing repository or silently publish unrelated tracked/staged files.
    if not (ROOT/'.git').exists():
        run('git','init','-b','main')
    actual_root = Path(run('git','rev-parse','--show-toplevel',capture=True).strip()).resolve()
    if actual_root != ROOT:
        raise SystemExit('Refusing to use an enclosing Git repository.')
    names = {path.relative_to(ROOT).as_posix() for path in files}
    tracked = set(run('git','ls-files',capture=True).splitlines())
    unexpected = tracked - names
    if unexpected:
        raise SystemExit('Review unexpected tracked files before publishing: '+', '.join(sorted(unexpected)))
    if run('git','remote',capture=True).strip():
        raise SystemExit('This copy already has a remote. Review it and push with normal Git commands.')
    run('git','add','--',*sorted(names))
    changed = subprocess.run(['git','diff','--cached','--quiet'],cwd=ROOT).returncode
    if changed not in (0,1):
        raise SystemExit('Could not inspect staged changes.')
    if changed:
        run('git','-c','user.name='+login,'-c',f'user.email={identifier}+{login}@users.noreply.github.com',
            'commit','-m','Publish reviewed 0xRemnant App Hub source and demo screenshots')
    # GitHub CLI owns authentication and Git transport. Never force-push or
    # delete an existing repository. Creation fails safely if its name exists.
    run('gh','repo','create',repository,'--public','--source',str(ROOT),'--remote','origin','--push',
        '--description','A free self-hosted app shelf: upload HTML or ZIP apps to one Linux VM on your LAN or VPN.')
    print('Published: https://github.com/'+repository)


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(f'Publishing stopped: {error.cmd[0]} exited with status {error.returncode}. No force-push was attempted.')
