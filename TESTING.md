# Verification and remaining checks

Reviewed on October 27, 2026. This file distinguishes actual execution from
modeled operations. See [docs/REVIEW.md](docs/REVIEW.md) for security findings.

## Passed in the preparation environment

- **56 Python tests** on Python 3.14.4: `python3 -m unittest discover -v`.
- Real management HTTP requests: terminal-token setup, damaged-token rejection,
  token CLI access while the server holds the registry, password validation,
  session expiry/logout, origin/Host validation, duplicate-header rejection,
  upload settings, and ZIP/HTML/HTM uploads.
- ZIP traversal/symlink rejection, credential/database-file exclusions, HTML
  entry aliases, manifest validation, and Vite/Next.js detection.
- **Modeled Docker operations:** initial deployment, update, rollback, retained
  environment values, stable ports/data-volume names, build/startup failure,
  recovery, deletion, private temporary env files, and streamed log redaction.
- **Real Python services:** generated static HTTP serving, route fallback,
  asset/error behavior, private/config-file rejection, disabled directory listing,
  and the example SQLite counter surviving its process restart.
- **Chromium:** first-run setup, ZIP upload, HTML update, HTM drag/drop, safe app
  names, iframe HTML JavaScript, tabs, restore, search, stop/start, 390px layout,
  and logout. No browser JavaScript errors. Docker is modeled; iframe navigation
  is fulfilled by the test browser from synthetic HTML.
- Four screenshots captured from the real UI with synthetic records and visually
  inspected for layout and sensitive content.
- Publication guard scans source and embedded example ZIPs. Regression tests
  verify token detection without echoing token values, private file rejection,
  and exclusion of runtime trees from releases.
- Python compilation; JavaScript/shell syntax; base and SELinux Compose config
  validation. Platform tests use command-recording stubs for Fedora DNF5/DNF4,
  existing Docker, package conflicts, Podman, ostree, and SELinux detection.

## Not verified here

The Docker socket is inaccessible in this preparation environment. **No real
Docker build, app container, Docker volume behavior, Fedora installation,
SELinux enforcement, firewall configuration, or Proxmox deployment ran here.**
The real Docker CI smoke test is included but has not executed here. Python
3.11–3.13 are configured in CI; the local run used 3.14.4 only.

CodeRabbit is installed but signed out, so this delivery contains a manual code
review rather than a completed CodeRabbit service review.

## Real Docker check

On a disposable Linux VM or CI runner with Docker Engine available:

```sh
docker build -t apphub-manager-ci .
python3 -m tests.docker_smoke
```

This checks the actual manager image, terminal setup token, HTML/ZIP builds,
update/rollback, backend volume persistence, and manager restart. It creates and
cleans a unique manager container, network, registry volume, and app resources.
Use a disposable host, not a production installation. CI runs this job after
repository upload; inspect its result before calling Docker deployment verified.

## Browser workflow

Install Playwright and Chromium in a development environment. Use a dedicated
empty temporary registry so the setup test remains repeatable:

```sh
APPHUB_TEST_DATA=/tmp/apphub-browser-demo python3 -m tests.browser_server
```

In another terminal:

```sh
APPHUB_TEST_DATA=/tmp/apphub-browser-demo node tests/browser.cjs
```

Stop the fixture when finished and delete that disposable test registry before
running again. `APPHUB_PLAYWRIGHT` may point to an existing Playwright module.
Recreate all screenshots with `node tests/screenshots.cjs`; that script starts,
stops, and cleans its own disposable fixture. Never use production data.

## First check on your Proxmox VM

1. Install/start using README.md. Complete owner setup with the terminal token.
2. Upload `examples/hello-app.html`; open it both inside the dashboard and from a
   second LAN/VPN device. Update that card with `examples/hello-app.zip`.
3. Upload `examples/python-counter.zip` and increment its count.
4. Restart the app, upload an update to that card, then restore an older version.
   Its assigned address and saved counter should remain.
5. Stop/restart the hub. Confirm the registry, sessions, and apps recover.
6. On Fedora, check package installation, enforcing SELinux, and firewall access.
7. Make and restore a VM backup before storing important application data.

Custom runtimes still need a compatible Dockerfile and correct port/health path;
no claim is made that arbitrary ZIP projects can be understood automatically.
