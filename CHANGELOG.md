# Changelog

## 1.0.0 — 2026-10-27

Initial public source preparation under the 0xRemnant App Hub name.

- Upload standalone HTML/HTM files or ZIP projects.
- Detect static apps, Vite/React builds, Node servers, and custom Dockerfiles.
- Open apps in dashboard tabs or directly at stable ports on one VM IP.
- Keep application data in named volumes and restore successful app images.
- Include Ubuntu/Debian/Fedora install helpers and an SELinux-aware Compose wrapper.
- Require a terminal setup token before claiming the first owner account.
- Redact known environment values across streamed log boundaries and keep them
  out of Docker process arguments.
- Exclude common private files from uploads/static assets, disable directory
  listing, and reject ambiguous request headers.
- Add demo screenshots, security documentation, regression tests, CI workflows,
  and a source-only release builder.

Real Docker/Proxmox/Fedora deployment still needs validation on a suitable host;
see TESTING.md and docs/REVIEW.md.
