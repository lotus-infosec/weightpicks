# Real Garmin data

WeightPicks reads your weigh-ins, daily totals and workouts from Garmin Connect through
[GarminDB](https://github.com/tcgoetz/GarminDB). Your Garmin password is typed once and
never stored: only a login token is kept, in its own Docker volume.

## Before you start

Use your Garmin Connect app or scale as usual. Weigh-ins count when they fall inside the
weigh-in window (04:00–11:00 local by default). Manual entries in the app count too.

## Run it on this machine (before go-live)

Stop the simulated dev stack first (both use `127.0.0.1:8000`), then use the real-data
overlay. It has its own Compose project name, so its data never mixes with the
simulated dev data:

```bash
R="docker compose -p weightpicks-real -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.real.yml"
$R build
$R run --rm garmin-login     # once; see below
$R up -d                     # web + worker; the worker syncs every 15 min in the window
$R exec worker wp report reconcile --days 7
$R down                      # never add -v: that deletes the token and the Garmin data
```

## Log in once: `garmin-login`

It asks how to sign in:

1. **Email + password.** Typed into the terminal (the password is hidden). Garmin
   sometimes refuses scripted logins with "429" or "Cloudflare" errors. If so, don't
   retry in a loop; use option 2.
2. **Browser sign-in.**
   - It prints a link to Garmin's own sign-in page. Open it in your browser and sign in, including MFA.
   - The browser ends on a page that fails to load. That's expected: copy the whole address from the address bar (it contains `ticket=ST-…`).
   - Paste it at the hidden prompt within a couple of minutes. A ticket works only once.

It ends with `Logged in as …`. Only the token is saved, readable only by the app user;
the GarminDB config it writes has no username or password.

## When something goes wrong

The admin dashboard shows the reason for the last failed sync, and an admin alert is
queued once per failure streak. While syncs fail or no new data has arrived for 36
hours during a season, **markets wait: nothing settles on stale data**.

| Message | What to do |
| --- | --- |
| `no Garmin token yet: run garmin-login once` | Run `garmin-login`. |
| `Garmin login failed: the token expired or was revoked; rerun garmin-login` | Run `garmin-login` again (changing your Garmin password also revokes the token). |
| `GarminDB timed out after 15 min` | The first sync downloads history; later syncs continue where it stopped. If it persists, check the network. |
| `no new data for N h` | Make sure the app/scale has synced to Garmin Connect. Long outages: void affected markets from the admin panel. |

## What is read, and why

GarminDB downloads raw JSON into the `wp_garmin` volume; WeightPicks reads those files
read-only (not GarminDB's database, which drops the time of each weigh-in):

- **Weigh-ins:** every entry with its real time (`timestampGMT`), and manual vs scale. Today's
  weigh-ins are fetched separately because GarminDB's own download stops at yesterday.
- **Daily totals:** steps, active minutes, intensity minutes (moderate + 2 × vigorous) and
  active calories, once the day is over.
- **Workouts:** activities of 10 minutes or more.

`wp report reconcile --days N` prints, day by day, every weigh-in Garmin sent next to the
one the game used, so you can check it against the Garmin Connect app.
