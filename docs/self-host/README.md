# Self-hosting WeightPicks

WeightPicks runs on one small Ubuntu machine at home (bare metal, a Proxmox VM, a mini PC or a Raspberry Pi 4/5), with two Docker containers and a Cloudflare Tunnel. Nothing listens on your network: the app binds to `127.0.0.1`, and the tunnel is the only way in.

| Guide | When |
| --- | --- |
| [install.md](install.md) | First install, start to finish (about 30 minutes) |
| [cloudflared.md](cloudflared.md) | Creating the Cloudflare Tunnel and its public hostname |
| [garmin.md](garmin.md) | Connecting the subject's Garmin account |
| [upgrade.md](upgrade.md) | Moving to a new release |
| [operations.md](operations.md) | Day to day: logs, health, backups, restarts, reset, uninstall |
| [troubleshooting.md](troubleshooting.md) | When something doesn't work |
| [go-live.md](go-live.md) | A checklist for the first weeks with real people |

Optional integrations each have their own page: [Discord](../discord.md), [Workers AI](../ai.md), email is set in Admin → Settings. Backups: [backups.md](../backups.md). Security model: [threat-model.md](../threat-model.md).

## What you need

| Item | Minimum | Recommended |
| --- | --- | --- |
| OS | Ubuntu 22.04 LTS | Ubuntu 24.04 LTS |
| CPU / RAM | 2 cores / 2 GB | 2 cores / 4 GB |
| CPU type | amd64 (x86-64) or arm64 | |
| Disk | 10 GB free, local SSD (not NFS or SMB) | 20 GB |
| Accounts | A domain on Cloudflare (free plan is fine); the subject's Garmin Connect account | Discord server; Cloudflare Workers AI; an SMTP provider |
| Time | Clock synchronized (`timedatectl` says `System clock synchronized: yes`) | |

The installer adds Docker Engine (from Docker's apt repository) and `cloudflared` (from Cloudflare's) if they're missing.

## The pieces

```text
 Browser ──HTTPS──► Cloudflare ──tunnel──► cloudflared (systemd, on the host)
                                                │
                                                ▼  http://127.0.0.1:8000
                                   web container ◄─► SQLite on the wp_data volume ◄─► worker container
                                                                                        │
                                                       Garmin Connect, Discord, Workers AI, SMTP
```

- **web** serves the site and applies database migrations when it starts.
- **worker** syncs Garmin data, drops markets, locks and settles bets, sends Discord and email, and makes nightly backups.
- **Volumes:** `wp_data` (database, backups, uploads), `wp_garmin` (Garmin downloads) and `wp_garmin_tokens` (the Garmin login token). Removing a volume deletes that data.
