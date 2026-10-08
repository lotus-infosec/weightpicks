# Troubleshooting

Start with `sudo docker compose ps` and `sudo docker compose logs --tail 100 web worker` in `/opt/weightpicks`.

| Symptom | Likely cause and fix |
| --- | --- |
| `install.sh`: `port 8000 is already in use` | Something else listens there. Install with `--bind 127.0.0.1:8001` and point the tunnel's public hostname at `127.0.0.1:8001`. |
| `install.sh`: `Docker Engine X is too old` | Docker 25 or newer is needed (health-check timing). Remove the distribution's `docker.io` package and re-run the installer, which installs Docker's own packages. |
| `install.sh`: `can't reach Docker` | Run it with `sudo`. |
| Containers won't start in a Proxmox LXC: `open sysctl net.ipv4.ip_unprivileged_port_start file: reopen fd 8: permission denied` | A known clash between newer containerd/runc and LXC. Use a VM (recommended), or update Proxmox to 9.1+, or add `lxc.sysctl.net.ipv4.ip_unprivileged_port_start=0` to the container's config on the Proxmox host and restart it. |
| Times are hours off (drops, locks, "today") | The instance time zone is wrong, usually because the server was on UTC during setup. Editing `WP_TIMEZONE` in `.env` doesn't change it after setup; change it in Admin → Settings ([operations.md](operations.md#time-zone)). |
| Links in Discord posts or reset emails open `127.0.0.1` or the wrong site | Set the Public URL in Admin → Settings ([operations.md](operations.md#public-url)). |
| Pull fails: `denied` or `manifest unknown` | Check `WP_VERSION` in `.env` matches a published release (without the `v`), and that the machine is amd64 or arm64. Or install with `--build`. |
| A container stays `unhealthy` or restarts | `sudo docker compose logs web worker`. A crash on start with `APP_SECRET_KEY must be at least 32 characters` means `.env` lost its key: restore it from your copy. |
| The site shows a Cloudflare error (502, 1033) | The tunnel is up but can't reach the app, or the tunnel is down. `systemctl status cloudflared`; check the public hostname points to `http://127.0.0.1:8000` (or your `--bind`); `curl -s http://127.0.0.1:8000/healthz` on the server. |
| `/setup` says the token is invalid | Tokens last 24 hours and five wrong tries per hour lock you out for an hour. Get a fresh one: `sudo docker compose exec web wp setup-token`. |
| Log in works locally but not through the tunnel, or the reverse | `WP_BASE_URL` in `.env` must be the public `https://` address. Cookies are HTTPS-only: plain `http://` works only on `localhost` (for example over `ssh -L`). |
| Login: "Too many attempts" | Wait 15 minutes. Limits are per address and per account. |
| No markets appear | Markets drop at the scheduled times (daily 11:00 by default) and need recent Garmin data; check the dashboard's last sync and [garmin.md](garmin.md). |
| Markets stay locked and never settle | By design when data is missing or stale: they wait for a successful sync after the weigh-in window. Fix the sync; or void the market (refunds) in Admin → Markets. |
| Discord posts don't arrive | Admin → Settings → send a test post; Admin → System shows dead letters with the reason. |
| `database is locked` in the logs | Shouldn't happen (writes are queued). Make sure the data volume is on a local disk, not NFS or SMB, and that only one copy of the stack uses it. |
| `Read-only file system` | Containers run read-only on purpose. Only `/data` and `/tmp` are writable; report it as a bug. |
| Disk full | Old backups beyond the newest 7 are pruned automatically; manual ones aren't. `sudo docker compose exec worker wp backup list`, and `sudo docker system prune` for old images. |

Still stuck? Open an issue with the version, the symptom and the relevant log lines (check them for anything private first). Security problems: [Report a vulnerability](https://github.com/lotus-infosec/weightpicks/security/advisories/new).
