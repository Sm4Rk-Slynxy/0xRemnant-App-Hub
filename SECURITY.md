# Security policy

0xRemnant App Hub is intended for one trusted owner on a home network or VPN.
The current development line is 1.0.x. There is no formal support SLA or external
security certification.

## Trust boundaries

- The manager mounts the Docker socket. Anyone controlling its owner account or
  manager code effectively controls the Docker host. Use a dedicated VM and only
  upload apps and Dockerfiles you trust. Container limits are not a secure sandbox
  for hostile code, and image builds can run arbitrary uploaded build instructions.
- The first owner must supply a random token read from the VM terminal, then choose
  a password of at least 12 characters. The token is stored privately, is never
  exposed by the web API, and is deleted after setup.
- Dashboard sessions last up to 24 hours and are stored in the dashboard's
  sessionStorage. The database stores session hashes and a salted PBKDF2 password
  hash. Logout or a local password reset revokes sessions.
- HTTP is the default. Use trusted networks/VPNs; HTTP does not encrypt passwords,
  uploads, or sessions against someone who can observe that network. Do not port
  forward the dashboard or application ports to the public internet. A future
  public-facing deployment would need TLS and a reviewed access-control design.
- Hosted apps have their own ports and must implement their own authentication
  when needed. The dashboard password does not protect those ports. Apps also
  share a Docker network and can reach its peers and the LAN. Do not use this as a
  multi-tenant platform for untrusted users.
- App environment settings and temporary upload settings are stored unencrypted
  in the private registry volume. Treat registry/app volumes, Docker metadata,
  backups, and build contexts as sensitive. Values are passed through a temporary
  private env file, rather than process arguments.
- Logs redact exact nonempty environment values, including values split between
  subprocess reads. Encoded, transformed, unknown, or partial secrets may still
  appear. Review logs before sharing them; log redaction is not a secret vault.
- ZIP extraction blocks traversal, symlinks, special files, and excessive sizes.
  Common credentials, environment files, database files, and private-key files are
  excluded. Static serving blocks hidden files, common private files, project
  configuration files, and directory listing. This cannot discover secrets
  embedded in HTML/JavaScript, arbitrary JSON, or screenshots. Anything in a
  static app's published assets should be considered public to its users.

## Reporting a vulnerability

Do not put credentials or exploit details affecting live installations in public
issues. If this repository has GitHub **Private vulnerability reporting** enabled,
use **Security → Report a vulnerability**. Otherwise open an issue asking the
maintainer to enable a private reporting channel, without disclosing the details.

Include the affected version, prerequisites, impact, and a minimal reproduction
with synthetic data. Never upload your real registry database, `.env`, setup
token, app secrets, or a complete VM backup.

## Before publishing your fork

Run `python3 scripts/check_publication.py` and inspect all screenshots. This guard
checks known token formats, private paths, runtime filenames, and bundled example
ZIP contents; it is not a complete secret audit or a Git history scan. Review the
staged diff as well. If a real secret was committed, revoke/rotate it first, then
remove it from history; a new commit that deletes it is not sufficient.
