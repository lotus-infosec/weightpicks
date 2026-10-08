# Cloudflare Tunnel

The tunnel gives your instance an HTTPS address (`https://picks.example.com`) without opening any port on your router or server. `cloudflared` runs on the host as a systemd service, makes an outbound connection to Cloudflare, and forwards requests to the app on `127.0.0.1:8000`. It never runs in a container.

## 1. Put a domain on Cloudflare

Any domain with its DNS on Cloudflare works (free plan). If yours is elsewhere, add it in the Cloudflare dashboard (**Add a domain**) and change the nameservers at your registrar as shown; wait until it's **Active**.

## 2. Create the tunnel

1. Open the Cloudflare dashboard → **Zero Trust** → **Networks** → **Tunnels** → **Create a tunnel**.
2. Type **Cloudflared**, name it (for example `weightpicks`), **Save**.
3. On the install page, pick **Debian** and **64-bit** (or **arm64** for a Pi). Don't run the commands it shows; just copy the **token**: the long string after `cloudflared service install` in the command. Keep it private: anyone with it can run your tunnel.

## 3. Add the public hostname

Still in the tunnel (**Public Hostname** tab, or the next step of the wizard):

| Field | Value |
| --- | --- |
| Subdomain | `picks` (anything you like) |
| Domain | your domain |
| Type | `HTTP` |
| URL | `127.0.0.1:8000` (or the `--bind` address you install with) |

Save. Cloudflare creates the DNS record and the HTTPS certificate.

Don't put **Cloudflare Access** in front of it: the app has its own logins, and players need to reach `/register`.

## 4. Install it on the server

`sudo ./scripts/install.sh` asks for the token at the end (hidden input) and runs `cloudflared service install` for you. To do it by hand, or later:

```bash
cd /opt/weightpicks
sudo ./scripts/install.sh --no-tunnel   # if the app isn't installed yet
sudo ./scripts/install.sh               # again: it keeps your settings and only adds the tunnel
# or directly (installs cloudflared from Cloudflare's apt repo first if it's missing):
sudo cloudflared service install <TOKEN>
```

## 5. Check it

```bash
systemctl status cloudflared --no-pager      # active (running)
curl -sI https://picks.example.com/healthz   # HTTP/2 200
```

The tunnel shows **Healthy** in the dashboard. The app logs a `cf_header_missing` warning if requests ever reach it without going through Cloudflare; if you see it, something else is exposing the port.

`WP_BASE_URL` in `/opt/weightpicks/.env` must be your public address (`https://picks.example.com`); it's used in emails and Discord posts. After changing it: `sudo docker compose up -d`. Or set it in Admin → Settings → Public URL, which takes precedence and needs no restart.

## Rotate or remove

- **New token** (if it leaked): in the dashboard, open the tunnel and rotate (refresh) its token, then `sudo cloudflared service uninstall && sudo cloudflared service install <NEW TOKEN>`.
- **Remove:** `sudo cloudflared service uninstall`, then delete the tunnel in the dashboard.
