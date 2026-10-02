# Security Policy

WeightPicks is a self-hosted hobby project that handles personal health data and account credentials. Security reports are welcome.

## Reporting a vulnerability

- **Do not open a public issue.**
- While the repository is private, contact the owner directly.
- Once public (Release R5), use GitHub's **Report a vulnerability** (private vulnerability reporting) on this repository.

Please include steps to reproduce, affected version/commit, and impact. You can expect an acknowledgement within 7 days.

## Supported versions

Only the latest released minor version receives security fixes.

## Project security baseline

- Web container binds to `127.0.0.1`; the only ingress is a Cloudflare Tunnel on the host.
- Passwords hashed with argon2id; server-side sessions; CSRF tokens; login rate limiting.
- Integration secrets encrypted at rest; never logged; never committed.
- Containers run as non-root with a read-only root filesystem and all capabilities dropped.
- Commits are signed; dependencies and actions are pinned and monitored by Dependabot; gitleaks runs on every commit and in CI.
