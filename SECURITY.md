# Security Policy

WeightPicks is a self-hosted hobby project that handles personal health data and account credentials. Security reports are welcome.

## Reporting a vulnerability

- **Do not open a public issue.**
- Use GitHub's **[Report a vulnerability](https://github.com/lotus-infosec/weightpicks/security/advisories/new)** (private vulnerability reporting) on this repository.

Please include steps to reproduce, the affected version or commit, and the impact. You can expect an acknowledgement within 7 days. Fixes ship as a patch release with a GitHub security advisory.

## Supported versions

Only the latest release receives security fixes. Upgrade with `scripts/upgrade.sh` (see `docs/self-host/upgrade.md`).

## Project security baseline

The full analysis is in [docs/threat-model.md](docs/threat-model.md).

- Web container binds to `127.0.0.1`; the only ingress is a Cloudflare Tunnel on the host. Production warns if requests arrive without the tunnel's client-IP header.
- Passwords hashed with argon2id; server-side sessions (hash-only tokens); CSRF on every state-changing route; per-IP and per-account login limits.
- Request bodies are size-capped before anything reads them; uploads are admin-only and content-checked.
- Integration secrets encrypted at rest; never logged; never committed. Request logs carry route templates, never tokens.
- Containers run as non-root with a read-only root filesystem and all capabilities dropped; the image ships Debian security fixes.
- Release images are multi-arch, built by GitHub Actions from signed tags, with build-provenance and SBOM attestations (`gh attestation verify`).
- Commits are signed; dependencies, base images and actions are pinned and monitored by Dependabot; gitleaks, CodeQL and secret scanning run on the repository.
