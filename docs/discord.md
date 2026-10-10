# Discord

WeightPicks posts to Discord through webhooks: one webhook per kind of post. Kinds
without a webhook stay silent, and the app works fine with none at all.

## Make the webhooks

1. In your Discord server, make a channel for each kind of post you want (or reuse
   one channel for several kinds). Keep admin alerts in a private channel.
2. Channel settings → **Integrations** → **Webhooks** → **New Webhook** → **Copy Webhook URL**.
3. In WeightPicks, open **Admin → Discord** → *Set or clear a webhook*, pick the kind,
   paste the URL, confirm with your admin password and save.

URLs are stored encrypted (they need `APP_SECRET_KEY`) and are never shown again.
Anyone holding a webhook URL can post to that channel, so don't paste them anywhere else.
If one leaks, delete it in Discord and set a new one.

## Check them

Each kind that has a webhook gets a **Send test** button. The test post should appear
within a minute. The *Recent posts* list shows each post's status and, if something went
wrong, why (for example "Discord returned 404" when the webhook was deleted in Discord).

## When posts go out

| Setting | Effect |
| --- | --- |
| **Public posts on** (`discord_public`) | Off: only admin alerts post. Turn it on when players join. |
| No webhook for a kind | Posts of that kind are skipped. |

Posts are sent by the worker within a minute, gently paced to stay inside Discord's
limits (at most 5 posts per 2 seconds and 30 per minute per webhook). If Discord asks the
app to slow down, it waits as told. If Discord is down, it retries for a while (30 s,
2 min, 10 min, then hourly) and, after 8 failures, gives up and sends you an admin alert.
Posts older than a day are dropped rather than sent late.

No post can ping anyone: `@everyone`, `@here` and user or role mentions are disabled on
every post, even if a player picks such a display name.

## Kinds of post

| Kind | When |
| --- | --- |
| Bets placed / High-roller bets | A player places a bet (high roller: at or above the high-roller amount). Shows who and which market only: the side and stake stay private until the bet settles. |
| Bet results | A bet wins, loses, pushes or is refunded |
| Market settlements | A market settles or is voided |
| New markets and props | Each daily, weekly and monthly drop |
| Weekly standings | Monday 09:00: top 10 by profit and loss from finished bets and events, with last week's change and wins |
| Busts and badges | A player goes bust |
| Admin alerts (private) | Sync failures, stale markets, posts that could not be delivered |
| Parlay results | A parlay wins or loses once its legs are in (a refunded parlay is a bet result) |
| Special events | A special event opens, settles or is voided |
| Stat updates and hype | AI-written stat updates, while hype is on (docs/ai.md) |
| Goal reached | The goal weight is hit and the season ends |
