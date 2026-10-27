# Publication review — 2026-10-27

Scope: the self-hosted Docker App Hub source prepared for **0xRemnant App Hub**,
including bundled example archives, generated static serving, the management API,
Docker process handling, frontend, platform helpers, and publication files.
This review does not cover privately uploaded apps, a running installation,
GitHub account settings, or an existing repository's history.

## Findings fixed before publication

| Severity | Finding | Change and evidence |
| --- | --- | --- |
| High | Any reachable LAN client could claim the first owner before the intended installer. | Setup now requires a random token read locally from the VM terminal. It has private permissions, persists across restarts until used, is not returned by the web API, and is deleted after setup. Invalid-token attempts are rate limited. |
| Medium | Per-chunk log redaction could miss a complete environment secret split across subprocess reads; short values were skipped. | Streaming redaction retains lookahead across reads, uses incremental UTF-8 decoding, and includes short nonempty values. Regression checks exercise every split point, one-character chunks, and a deployment log. Recovery errors also redact known values. |
| Medium | Environment values were included in Docker CLI process arguments. | Container creation uses a private temporary env file, removed after Docker reads it. Tests verify the value reaches the container model without appearing in command arguments. Docker metadata and the private registry still contain environment values. |
| Medium | Static uploads could publish project configuration/private files or directory listings. | Static assets are staged separately. Common secret/key/database files and credential folders are excluded from uploads and build contexts. The static server blocks private/configuration files and listing. Real HTTP tests verify rejection. Arbitrary source files still require owner review. |
| Medium | Ambiguous request headers and malformed Host values were accepted; connection concurrency was unbounded. | Duplicate Host/Content-Length headers and invalid host ports are rejected. Header/body timeouts and a 64-request concurrency limit bound common resource exhaustion paths. This is not a load-tested internet server. |
| Low | Broad Git ignore rules excluded example ZIPs and screenshot PNGs while missing other private artifacts. | Runtime/credential ignores are explicit. Only two intentional example ZIPs are allowed; documentation screenshots are tracked. A separate release allowlist prevents packaging runtime state and Git history. |

No live API keys or other credentials were found in the prepared source or bundled
example ZIP contents by the configured pattern scan and manual inspection.
No production database, `.env`, uploaded private project, or host configuration
was added to the prepared copy. Demo registries and browser sessions were kept
outside the source and are excluded from the archive. These statements apply to
the prepared copy, not every file on the workstation.

## Verification

- 56 Python tests pass on Python 3.14.4, including real loopback HTTP tests,
  generated static serving, a real Python/SQLite backend process restart, and
  modeled Docker deployment/update/rollback/failure paths.
- The Chromium workflow passes: first-run token/password setup, ZIP/HTML/HTM
  upload, HTML iframe interaction, version restore, safe names, search, stop/start,
  mobile layout, and logout. Docker and frame navigation are modeled.
- Four real UI screenshots were captured with synthetic data and visually
  inspected. No passwords, setup/session tokens, private hostnames, LAN IPs,
  production records, or personal application content are shown.
- Publication guard passes, including embedded example ZIP inspection. It checks
  known credential formats/private paths and runtime filenames. It is not an
  entropy scan, history scan, vulnerability scanner, or image OCR tool.
- Python compilation, JavaScript/shell syntax, and both Compose configurations pass.
- CI includes pinned actions, read-only workflow permissions, Python 3.11–3.14
  checks, a publication scan, and an opt-in real Docker smoke test.

## Remaining limitations and release checks

1. **Real Docker execution is unverified in this review environment.** The daemon
   socket is inaccessible. The new CI Docker smoke test is supplied but has not
   run here. Wait for it to pass after upload, then run the VM checklist in
   TESTING.md. Fedora package installation, SELinux enforcement, firewalld, and
   Proxmox networking remain unverified on a real VM.
2. **CodeRabbit did not run.** Its installed CLI is signed out. This is a manual
   code review with regression tests. For a separate service review, authenticate
   using `coderabbit auth login`, review the staged diff for secrets, then run
   `coderabbit review --agent` from this Git repository. Code diffs are sent to
   CodeRabbit when that service is used.
3. **Trusted owner and LAN/VPN only.** The Docker socket gives the manager powerful
   host access. Uploaded builds are trusted code, apps share a network, app ports
   have no hub login, and default HTTP traffic is unencrypted. See SECURITY.md.
4. **Operator tasks still matter.** Enable GitHub secret scanning/push protection
   and private vulnerability reporting when available, set branch protection,
   keep images/dependencies current, test backups/restores, and monitor disk use.
   Old successful images/data volumes are retained intentionally; there is no
   automatic retention/garbage-collection policy.

The repository includes the MIT license, setup/access/backup guidance, example
apps, screenshots, SECURITY.md, CONTRIBUTING.md, a changelog/version, issue
templates, CI/Dependabot, and a source-only ZIP builder with a SHA-256 checksum.
