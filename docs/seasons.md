# Special events, Goal Reached and seasons

## Special events (Price Is Right pots)

A special event is a pot outside the house bank:

- Each player buys in once and guesses the subject's weight on a target date.
- **Closest without going over takes the pot.** Ties split it evenly, and any leftover cent goes to the earliest entry.
- If every guess is over, if there's no weigh-in that day, or if nobody entered, everyone gets their buy-in back.

**Turn it on:** Admin → Settings → **Special events**. Players then see an **Events** tab on the board.

**Create one:** Admin → Events.
- With a Workers AI token, you can describe the event in plain words ("Thanksgiving pot, $200, guess my weight that morning") and the AI drafts it. Without one, fill in the form.
- Either way, the draft goes through the same checks:
  - the target date is 2–120 days out;
  - the buy-in is between $1 and half the starting bankroll (the default comes from Admin → Bank);
  - the text has no links, mentions or player names.
- Then you see a preview, with the trend's projection for that day for context. Publish it with your password.

**How it runs:**
- **Entering:** players can change their guess for free until the lock, which is the night before the target date at the bet lock. Other players' guesses stay hidden until then.
- **Settling:** after the target day's weigh-in window closes and a sync has run, code settles the pot from the scale. AI never picks winners, and nothing settles on stale data.
- **Busts:** money in an open pot keeps a player from going bust.
- **Calling it off:** Admin → Events → **Void and refund** returns every buy-in.

## Goal Reached

Set the goal during `/setup`, or for later seasons when you start one.

**Automatic trigger:** the worker watches each successful sync. Goal Reached runs once, when the first canonical weigh-in of a fully synced day reaches the goal (at or under it for a loss goal; at or over for a gain goal).

**What it does, in order:**
1. **Freezes** the instance first, so no bet can slip in.
2. **Settles** every market the data already decides. That includes the goal day's daily line, and any milestone the goal weigh-in crossed.
3. **Voids and refunds** everything that can't be decided yet: weekly and monthly windows still running, futures, and later props. A parlay drops those legs and resolves on the rest.
4. **Settles** pots whose target day is complete and **refunds** the others.
5. **Posts Goal Reached** to Discord, with the start, the goal, the days taken and the top three.

**Manual trigger:** Admin → Season → **Goal Reached**, if the sync missed the big day. Type GOAL and enter your password. It runs once per season either way.

## Freezing and new seasons

**Freezing:** Admin → Season → **Freeze** stops new bets and pool entries and locks every open market. Nothing is refunded. Players see a "Betting is paused" banner, and their history stays available.
- **Unfreeze** reopens betting.
- After Goal Reached you can't unfreeze; you start a new season instead.

**Starting a new season:** Admin → Season → **Start a new season**. It's available while the instance is frozen and nothing is still open.
- **Weights:** enter the starting weight (pre-filled with the latest weigh-in) and the new goal.
- **Balances carry over:** every player starts the new season with last season's ending balance. Profit and loss restarts at zero, so the leaderboard starts level. New players still get the starting bankroll.
- **Betting reopens,** and the next scheduled drop posts the new season's markets.

**Past seasons** stay on the leaderboard and in **My bets** (season tabs at the top).
