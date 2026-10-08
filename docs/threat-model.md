# Threat model

This covers one self-hosted WeightPicks instance: a Docker Compose stack (`web` and `worker`) on an Ubuntu host, reachable only through a Cloudflare Tunnel. It uses STRIDE (Spoofing, Tampering, Repudiation, Information disclosure, Denial of service, Elevation of privilege) per component. Reviewed in STAGE16; findings S1–S9 are GitHub issues #18–#26, each fixed with a regression test.

## What we protect

| Asset | Why it matters |
| --- | --- |
| The subject's health data (weigh-ins, activity) | Personal data of one real person |
| Player accounts (email, password hash, sessions) | Account takeover; reused passwords |
| Integration secrets (Discord webhooks, Workers AI token, SMTP password) | Spam, cost, impersonation in Discord |
| Garmin login token | Full access to the subject's Garmin account |
| The ledger | Fairness: fake money, real bragging rights |
| `APP_SECRET_KEY` | Decrypts every integration secret |
| Backups | Hold everything above except the Garmin token |

## Trust boundaries

```text
 Internet ──► Cloudflare edge ──► cloudflared (host) ──► 127.0.0.1:8000 ──► web container
                                                                              │ shared SQLite (WAL) on wp_data
 Garmin Connect ◄── GarminDB subprocess ◄── worker container ─────────────────┘
 Workers AI, Discord, SMTP ◄── worker (outbox dispatcher)
```

- **Roles:** anonymous visitors, players and one admin. The admin is also the subject and never bets.
- **web and worker:** they share nothing but the database. Commands and outgoing messages are rows in it.
- **Outbound only:** only the worker talks to third parties, and nothing calls into the stack except through the tunnel.

## Components

### 1. Ingress: tunnel and port

| | Threat | Mitigation |
| --- | --- | --- |
| S | A client fakes `CF-Connecting-IP` to dodge per-IP limits | Port bound to `127.0.0.1` (`WP_BIND`); the only path is the tunnel. Production logs `cf_header_missing` once if requests ever arrive without it (R13, S5 #22, `app/web/main.py`). Login also has an account-wide limit that ignores IPs (S4 #21). |
| I | Plain HTTP on the LAN | Cookies are `Secure` by default; `WP_COOKIE_SECURE=false` is refused outside dev (`app/core/config.py`). |
| D | Floods | Cloudflare at the edge. The app caps every request body before reading it (S1 #18). |

### 2. Web app: auth, sessions, CSRF, routes

| | Threat | Mitigation |
| --- | --- | --- |
| S | Password guessing | argon2id; 5 failures per email per IP and 30 attempts per IP per 15 min, plus 20 failures per account across all IPs (S4). An unknown email costs the same time as a known one. |
| S | Session theft | 256-bit tokens; only the SHA-256 is stored; `HttpOnly`, `Secure`, `SameSite=Lax`; 12 h admin and 30 day player sliding expiry; banned users and password resets end every session. |
| T | CSRF | A global dependency (`app/web/security.py`). With a session, only that session's token is accepted (S2 #19), so a cookie planted by a sibling subdomain doesn't count. Requests the browser marks `cross-site` or `same-site` are refused. The route matrix test checks every route (`tests/integration/test_route_matrix.py`). |
| E | Player reaches admin routes; IDOR | The admin router requires the admin role. Bets, pools and My bets take the user from the session, never the request. Both are covered by the route matrix. |
| T | Malicious input | Money and weights go through bounded, exponent-free parsers (S7 #24). The Workers AI account ID and webhooks are strictly matched (S6 #23). Templates autoescape. CSP is `script-src 'self'` with no inline script, and nothing is loaded from a CDN. |
| I | Tokens in logs | uvicorn's access log is off. The app logs route templates such as `/reset/{token}`, never raw paths (S3 #20). Emails, secrets and tokens are never logged. |
| D | Huge or endless bodies | `BodyLimit` counts streamed bytes before any parser runs: 16 KB for forms and JSON, 2 MB for a logo, 256 MB for a restore upload. Uploads need a Content-Length, and multipart is parsed only for the admin's session (S1 #18). |
| D | Write contention | Writers queue in-process before `BEGIN IMMEDIATE` (S8 #25). The load test runs 50 bettors during settlement with no `database is locked`. |
| R | Admin denies an action | Every admin mutation writes `audit_log` (append-only by trigger) with before/after, IP and real time. Failed password re-checks are logged. |

### 3. Setup and password reset

| | Threat | Mitigation |
| --- | --- | --- |
| S | Someone else finishes `/setup` first | One-time 256-bit token printed only to the web container's logs, valid 24 h, 5 wrong tries per IP per hour; `/setup` returns 404 once done. |
| S | Reset-link poisoning | Links use the public URL set by the admin (re-checked password, audited) or `WP_BASE_URL`, never the request's Host header. |
| I | Account enumeration | The reset request always gives the same answer. Login errors don't say which part was wrong. |
| T | Reset token replay | 256-bit, hash-only, 1 h, single use. Using one ends every session and voids other open links. The token sits encrypted in the outbox until the email is sent, then is dropped. |

### 4. Worker and jobs

| | Threat | Mitigation |
| --- | --- | --- |
| T | Double payouts after crashes or restarts | Settlement is unique per market in one transaction. Ledger rows are append-only with idempotency keys. Every scheduled job is keyed per period in `job_runs`, and claims left by a dead worker are released at start-up (S9 #26). The chaos run covers this (`tests/season/test_chaos.py`). |
| T | Settling on stale or partial data | The readiness gate requires a successful sync after the market's settle time and complete data through its window. The chaos run checks it with 1 sync in 5 failing. |
| D | Third-party outage | The outbox retries with backoff and moves messages to dead letters. AI and Discord failures never block settlement. |

### 5. Data at rest: SQLite, uploads, backups

| | Threat | Mitigation |
| --- | --- | --- |
| I | Volume or backup copied | Integration secrets are Fernet-encrypted with `APP_SECRET_KEY` (keep it apart from backups). Passwords are argon2id. Session and reset tokens are hash-only. Backups never include the Garmin token, `.env` or Garmin data. |
| T | Malicious backup restored | The manifest is checked file by file (SHA-256 and size). Only `app.db`, `uploads/*` and `manifest.json` are allowed: no links, absolute paths or `..`, and size caps apply. A backup from a newer version is refused. It's applied at start-up, never under a running app. |
| T | Malicious logo | Content-checked PNG, JPEG or WebP; pixel cap; re-encoded by Pillow; SVG refused; served as `image/png` with `nosniff`. |
| T | Ledger edited by hand | Append-only triggers on `ledger_entries`, `observations` and `audit_log`. A nightly `ledger_verify` recomputes every balance. |

### 6. Garmin (GarminDB subprocess)

| | Threat | Mitigation |
| --- | --- | --- |
| I | Garmin password exposure | The password is typed once into `garmin-login` and never stored; only the token is kept, mode 600, on its own volume mounted only by the worker. |
| E | A GarminDB dependency compromised | Separate venv with exact versions and hashes (`docker/garmindb-requirements.txt`), run as a subprocess with no app imports. The container is non-root, read-only, with all capabilities dropped. |

### 7. Workers AI, Discord, SMTP

| | Threat | Mitigation |
| --- | --- | --- |
| T | Prompt injection makes the AI set odds or settle | It can't. AI only fills a fixed template menu, code re-validates and prices every proposal, and the admin reviews them by default. AI text is length-limited and plain. |
| D | AI cost runaway | A hard daily neuron cap in code (`AI_DAILY_NEURON_CAP`), counted on real time. |
| I | Webhook leak | Webhooks are stored encrypted and never logged or shown back. Only `discord.com` webhook URLs are accepted. |
| S | Email spoofing or snooping via SMTP settings | Only the admin can set SMTP, with a password re-check, and it's audited. Use STARTTLS or SSL; `none` exists for local test servers. |

### 8. Admin powers

| | Threat | Mitigation |
| --- | --- | --- |
| E | Stolen admin session does damage | 12 h sessions. Destructive and money actions (void, ban, bailout, adjust, restore, reset, secrets, logo) ask for the password again. Factory reset also needs the instance name typed. |
| R | — | Everything is in the audit log. |

### 9. Supply chain and build

| | Threat | Mitigation |
| --- | --- | --- |
| T | Compromised dependency or action | `uv.lock` hashes; base images pinned by digest; GitHub Actions pinned to commit SHAs with `permissions: contents: read`; Dependabot for uv, Docker and Actions. Front-end libraries are vendored with checksums (`app/web/static/vendor/VERSIONS.md`). |
| I | Secrets committed | gitleaks in pre-commit and CI (full history scanned clean in STAGE16). `.env`, databases and backups are git-ignored. |
| T | Known CVEs in the image | Runtime installs Debian security fixes; the unused system pip is removed. Trivy (fixable CRITICAL/HIGH) was clean in STAGE16, and `pip-audit` was clean for the app lock and the GarminDB pins. |
| R | Unsigned or misattributed commits | Every commit and tag is SSH-signed and uses the noreply address (pre-commit guard). |

### 10. The self-host install

| | Threat | Mitigation |
| --- | --- | --- |
| I | Weak or lost `APP_SECRET_KEY` | Production refuses to start with a key shorter than 32 characters. Losing it only means entering the integrations again. |
| E | Container escape | Non-root (uid 10001), read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, memory limits. |
| D | Disk loss | Nightly backups with retention, plus a documented off-box copy (`docs/backups.md`). |

## Residual risks (accepted)

- **The admin is fully trusted.** They can see player emails and move fake money. Both are audited, not prevented.
- **The host is trusted.** Root on the host can read the database and `.env`.
- **A Cloudflare outage or misconfiguration** takes the instance offline, or exposes it if someone binds the port to `0.0.0.0` against the docs. The `cf_header_missing` warning is the tell.
- **Unfixed OS CVEs** in the Debian base (no fix published yet) are tracked by Dependabot digest bumps and the build-time security upgrade.
- **SQLite is a single writer.** It's fine for a friends-and-family instance (the load test shows 50 simultaneous bettors), not for thousands.

## Re-running the checks

```bash
uv run pytest -q tests/integration/test_hardening.py tests/integration/test_route_matrix.py
uv run pytest -q -m load -s                       # 50 bettors while settlement runs
uv run pytest -q -s tests/season/test_chaos.py    # 30 days of kills, restarts, sync failures
uvx pip-audit -r <(uv export --frozen --no-dev --no-hashes --no-emit-project) --disable-pip --no-deps
gitleaks git --log-opts=--all .
# Image: docker save the built image, then scan the tarball (no Docker socket needed):
#   docker run --rm -v "$PWD/scan:/scan" aquasec/trivy image --input /scan/image.tar \
#     --severity CRITICAL,HIGH --ignore-unfixed --scanners vuln
```
