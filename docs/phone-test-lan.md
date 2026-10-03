# Testing on a phone over your home network (dev only)

The web container only listens on `127.0.0.1:8000` inside WSL. That never changes: production reaches it only through the Cloudflare tunnel. For a quick phone test on your home Wi-Fi, Windows forwards one port to it. Remove the forward when you're done.

Session cookies are normally marked `Secure`, which means HTTPS only. Plain `http://` on the LAN needs that flag off. The app allows this **only** with `APP_ENV=dev` and refuses to start in production with it.

## 1. Turn off Secure cookies for dev

Add this line to your dev settings file (`~/.config/weightpicks/dev.env`), then restart the stack:

```bash
WP_COOKIE_SECURE=false
```

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d
```

## 2. Create a registration code

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml exec web wp registration-code new
```

## 3. Forward a Windows port to WSL (PowerShell as Administrator)

```powershell
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=8080 connectaddress=127.0.0.1 connectport=8000
New-NetFirewallRule -DisplayName "WeightPicks dev 8080" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow -Profile Private
ipconfig   # note the IPv4 address of your Wi-Fi adapter, e.g. 192.168.1.23
```

On the phone (same Wi-Fi), open `http://<that IPv4 address>:8080/register`.

If the page doesn't load, check that `http://127.0.0.1:8000/healthz` works in a browser on Windows itself. WSL forwards its localhost to Windows by default.

## 4. Remove it afterwards

```powershell
netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=8080
Remove-NetFirewallRule -DisplayName "WeightPicks dev 8080"
```

Then remove `WP_COOKIE_SECURE=false` from the dev settings file.
