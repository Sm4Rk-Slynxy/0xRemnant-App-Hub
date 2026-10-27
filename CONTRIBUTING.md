# Contributing

Use Python 3.11+ on Linux. Production hosting requires Docker Engine and its
Compose plugin. The manager has no third-party Python dependencies.

Before submitting a change:

```sh
python3 -m unittest discover -v
python3 scripts/check_publication.py
node --check apphub/web/app.js
sh -n start.sh compose.sh install-docker.sh
```

For a real Docker check on a disposable Linux VM:

```sh
docker build -t apphub-manager-ci .
python3 -m tests.docker_smoke
```

The Docker check creates and removes its own manager, app containers, images,
network, and data volumes. Do not use a production machine for integration tests.
See [TESTING.md](TESTING.md) for browser checks and limitations.

Keep changes focused and explain the observed behavior, intended behavior, and
how you verified the fix. Include screenshots for visible UI changes using demo
data. Treat file uploads, manifests, names, and logs as untrusted input. Avoid
introducing cloud dependencies into the local deployment flow.

Report security issues using [SECURITY.md](SECURITY.md). Do not attach real `.env`
files, databases, tokens, or private application ZIPs to issues or pull requests.

Build a clean downloadable source archive with:

```sh
python3 scripts/make_release.py --output /tmp/0xRemnant-App-Hub.zip
```

The release builder includes only known source/documentation trees and approved
example ZIPs, runs the publication guard, excludes Git history/runtime caches,
and writes a SHA-256 file beside the ZIP. Normal GitHub source downloads work too.
