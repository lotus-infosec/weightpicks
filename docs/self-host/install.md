# Install

From a fresh Ubuntu 22.04 or 24.04 machine to a working instance. Run the commands as a user who can `sudo`.

## 1. Before you start (5 minutes, in a browser)

1. **A domain on Cloudflare.** Any domain whose DNS is on Cloudflare (free plan). You'll use a subdomain such as `picks.example.com`.
2. **The tunnel.** Follow [cloudflared.md](cloudflared.md) steps 1–3: create a tunnel, copy its **token**, and add the public hostname `picks.example.com` → `http://127.0.0.1:8000`.

You can also install first with `--no-tunnel` and add the tunnel later.

## 2. Get the code

Pick the latest release on the [Releases page](https://github.com/lotus-infosec/weightpicks/releases), then:

```bash
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/lotus-infosec/weightpicks.git /opt/weightpicks
cd /opt/weightpicks
sudo git checkout v1.0.0        # the release you picked
```

## 3. Run the installer

```bash
sudo ./scripts/install.sh
```

It asks for your public hostname and, at the end, the tunnel token (typed input stays hidden). It:

1. checks the machine (OS, CPU, memory, disk, clock, free port);
2. installs Docker Engine and the Compose plugin if needed;
3. writes `/opt/weightpicks/.env` (readable only by root) with a random `APP_SECRET_KEY`;
4. pulls the release image for your CPU from `ghcr.io/lotus-infosec/weightpicks`;
5. starts the two containers and waits until both are healthy;
6. installs `cloudflared` as a system service with your token;
7. prints the **setup token**.

Useful options: `--hostname picks.example.com`, `--no-tunnel`, `--bind 127.0.0.1:8001` (another port), `--build` (build the image from the clone instead of pulling it), `--check` (only the checks). Run `./scripts/install.sh --help` for all of them. Running it again is safe: it keeps your `.env` and its key.

**Keep a copy of `APP_SECRET_KEY`** (`sudo grep APP_SECRET_KEY /opt/weightpicks/.env`) in your password manager. It encrypts the integration secrets; without it they must be entered again after a restore.

## 4. First run: `/setup`

Open `https://picks.example.com/setup` and enter the setup token (valid 24 hours; print it again with `sudo docker compose logs web | grep 'SETUP TOKEN'` from `/opt/weightpicks`).

> No tunnel yet? From your own computer: `ssh -L 8000:127.0.0.1:8000 you@your-server`, then open `http://localhost:8000/setup`.

The wizard has 12 steps; everything can be changed later in Admin.

| Step | What to enter |
| --- | --- |
| 1. Admin account | Your email, name and a password (10+ characters). The admin is the person being bet on and never bets. |
| 2. Subject and goal | Name shown to players, unit (lb/kg), starting weight and goal weight. |
| 3. Time zone and schedule | Your time zone; the weigh-in window and drop/lock times (defaults are fine). |
| 4. Economy | Starting bankroll, daily allowance, bailout, vig, maximum bet (defaults are fine). |
| 5. Stats to bet on | Weight, plus any of steps, active minutes, intensity minutes, calories, workouts. |
| 6. Garmin | Optional here; do it after setup with [garmin.md](garmin.md). |
| 7–9. Workers AI, Discord, Email | Optional; skip and add later in Admin → Settings ([ai.md](../ai.md), [discord.md](../discord.md)). |
| 10. Registration code | Write it down: players need it to sign up. |
| 11. Look and feel | App name and colors. |
| 12. Review | Check and finish. |

After **Finish**, log in with the admin email and password; you land on the admin dashboard.

## 5. Connect Garmin

```bash
cd /opt/weightpicks && sudo docker compose run --rm garmin-login
```

Details and troubleshooting: [garmin.md](garmin.md). The worker syncs every 15 minutes during the weigh-in window; the dashboard shows the last sync.

## 6. Invite players

Turn on **Registration** in Admin → Settings, then send people `https://picks.example.com/register` and the registration code. A new code can be made any time (Admin → Registration); the old one stops working.

## 7. Before you invite everyone

- Copy backups off the machine every night ([backups.md](../backups.md)).
- Recommended firewall: `sudo ufw default deny incoming && sudo ufw allow from 192.168.1.0/24 to any port 22 && sudo ufw enable` (use your LAN range). The app needs no open ports.
- Read [go-live.md](go-live.md).
