# Workers AI (optional)

WeightPicks can use Cloudflare Workers AI for two things:

- **AI props:** it suggests props from the same four templates the admin uses (milestone, streak, week vs week, future).
- **Stat updates:** it rewords short stat-update posts for the hype webhook.

AI never sets odds, settles bets or picks winners:

- Code builds the menu of allowed values.
- The line engine prices every prop.
- Settlement only ever reads Garmin data.

Without Workers AI, nothing breaks. AI cycles are skipped and logged, and everything else works.

## Set it up

1. In the Cloudflare dashboard, copy your **Account ID** (Workers & Pages → Overview).
2. Create an **API token** (My Profile → API Tokens → Create Token). Use the *Workers AI* template, or a custom token with the **Workers AI: Read** and **Workers AI: Edit** permissions.
3. In WeightPicks, open **Admin → Settings → Workers AI**. Paste both values, confirm with your admin password and save. They are stored encrypted (this needs `APP_SECRET_KEY`) and are never shown again.
4. Turn on **AI props** and/or **Stat-update posts** under *Features*. AI props also need **Props and futures** on.

## How AI props work

- **When it runs:** after each daily drop (2 suggestions, or 3 when something notable happened) and after the weekly drop (3). **Admin → Props → Run AI props now** runs one cycle on demand.
- **What the model gets:** a digest built by code:
  - current weight and trend, and the distance to the next round number;
  - streaks, and yesterday against a normal day;
  - your active **notes** (Admin → Notes);
  - crowd storylines with no names.

  Bettor names are never sent to Cloudflare.
- **What the model sees on the menu:** weight props appear only as options the engine has already priced.
- **Checks on each suggestion:** each one is checked on its own, and a failure drops only that suggestion. It must:
  - come from the menu;
  - be priced by the engine with a fair chance between 8% and 92%;
  - not duplicate an open or waiting prop;
  - fit under the cap of 8 open props;
  - have text with no links, mentions, markup, listed words or bettor names.
- **Review mode** is the default. Suggestions wait in **Admin → Props → AI suggestions**:
  - **Approve** re-prices with the latest data and posts the prop.
  - **Reject** discards it.
  - Anything not approved expires at the bet lock.
- **Auto-publish** posts valid suggestions straight away. Switch modes under *Publishing mode*.
- **On the board:** the market title is always written by code and is the binding question. The AI's one-line blurb is shown underneath as flavour.

## Stat updates

While **Stat-update posts** is on, code spots up to 4 events a day: a new season low, crossing a round number, a streak milestone, or a day far from normal. Each event is posted to the hype webhook.

With a token, the fp8-fast model rewords the post. If the rewrite drops a number, runs too long or fails a text check, the plain template is posted instead.

## Cost and the daily cap

Typical use is a few dozen to a few hundred neurons a day. The free allocation is 10,000 a day.

The app enforces `AI_DAILY_NEURON_CAP` (default 5,000):

- Each call is estimated before it is made and refused if it would cross the cap.
- The counter resets at 00:00 UTC, like Cloudflare's own.

**Admin → AI** shows usage today and every run: tokens, neurons, status and why suggestions were dropped.

| Setting | Default | Meaning |
| --- | --- | --- |
| `AI_DAILY_NEURON_CAP` | `5000` | Hard daily cap enforced by the app |
| `AI_MODEL_JSON` | `@cf/meta/llama-3.1-8b-instruct` | Model for prop suggestions (JSON Mode) |
| `AI_MODEL_TEXT` | `@cf/meta/llama-3.1-8b-instruct-fp8-fast` | Model for stat-update wording |
