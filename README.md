# youtube-slack-notifier

Posts a Slack message when one of six YouTube channels uploads a new long-form video.
Runs on GitHub Actions cron. No server, no database, no dependencies.

```
GitHub Actions (cron :07 / :37)
   │
   ├── fetch RSS for each channel
   ├── diff against state.json  →  new video IDs
   ├── drop scheduled premieres and Shorts
   ├── POST to Slack webhook
   └── commit state.json (only when it changed)
```

Watched channels live in [`channels.json`](channels.json): Arpit Bhayani, Ben Dicken,
Computerphile, Hussein Nasser, Veritasium, TBPN.

## Setup

### 1. Create the Slack webhook

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**.
2. Name it (e.g. `youtube-drops`), pick your workspace, **Create App**.
3. In the sidebar: **Incoming Webhooks** → toggle **Activate Incoming Webhooks** on.
4. **Add New Webhook to Workspace** → choose the destination channel → **Allow**.
5. Copy the webhook URL. It looks like
   `https://hooks.slack.com/services/T000.../B000.../XXXX`.

The webhook is a bearer credential: anyone holding it can post to that channel. It goes
in Actions secrets, never in this repo.

### 2. Add it as a repository secret

Either with the CLI:

```bash
gh secret set SLACK_WEBHOOK --repo <owner>/youtube-slack-notifier
# paste the URL when prompted, then press Enter
```

Or in the browser: **Settings → Secrets and variables → Actions → New repository secret**,
name `SLACK_WEBHOOK`, paste the URL.

### 3. Enable the workflow

Scheduled workflows only run from the **default branch**, so make sure this is pushed to
`main`. Open the **Actions** tab once and enable workflows if GitHub asks.

### 4. Do a dry run

**Actions → youtube-to-slack → Run workflow**, leave `dry_run` checked, **Run**. The log
shows exactly what it would post without touching Slack or committing anything. This is
also the safest way to confirm the feeds parse.

### 5. Seed the state

Run the workflow again with `dry_run` **unchecked**. The first real run finds
`state.json` empty, records every video currently in all six feeds as already-seen,
posts nothing, and commits the state. From then on only genuinely new uploads notify.

That cold-start guard is per channel, so adding a channel later never floods you either.

## Adding or removing a channel

Add an entry to `channels.json`. You need the `UC…` channel ID, not the handle:

```bash
curl -s https://www.youtube.com/@somehandle | grep -o 'channel/UC[A-Za-z0-9_-]\{22\}' | head -1
```

Removing a channel: delete its entry. Its `state.json` block is harmless if left behind.

## Local testing

```bash
python3 notify.py --dry-run   # print planned messages, write nothing
python3 notify.py --seed      # mark all current videos seen, post nothing
SLACK_WEBHOOK=https://... python3 notify.py   # a real run
```

Needs Python 3.9+ and nothing else.

## How the filtering works

**Duplicates** are keyed on video ID against the last 50 IDs per channel — not on a
single "latest ID", which would drop the older of two uploads landing in the same
window. Crucially it ignores the feed's `updated` timestamp, so editing a video's title
does not re-notify.

**Shorts** are detected with one `HEAD https://www.youtube.com/shorts/<id>` per *new*
video: `200` means it is a Short, `303` means YouTube redirects to `/watch`, i.e.
long-form. Zero extra requests on runs where nothing is new.

**Scheduled premieres and livestreams** carry a `published` timestamp in the future.
Those are skipped *without* being marked seen, so they notify once they actually air.

**A video is marked seen only after Slack accepts it.** If Slack is down the video stays
unseen and retries next run. The tradeoff is deliberate: a possible duplicate beats a
silently missed upload.

**One broken feed cannot affect the others.** A fetch failure logs, leaves that channel's
state untouched, and moves on. The run is only marked failed if *every* channel fails.

## Cost

GitHub Actions is free for public repositories on standard runners, so 48 runs/day costs
nothing. On a **private** repo this would consume a large share of the 2,000 free
monthly minutes — that is the reason this repo is public. Nothing here is sensitive
beyond the watchlist itself.

## Known limitations

- **"Within 30 minutes" is best-effort.** GitHub delays scheduled runs under load and can
  drop them entirely. This is a notifier, not an SLA.
- **Premiere suppression is a heuristic** resting on premieres carrying a future
  `published` value. If one ever slips through, the robust fix is a (free) YouTube Data
  API key plus a `snippet.liveBroadcastContent == "upcoming"` check — 1 unit of a
  10,000/day quota.
- **Shorts probe fails open.** If the probe errors, the video is treated as long-form and
  notified. Better a stray Short than a swallowed upload.
- **RSS holds only the latest ~15 videos.** Irrelevant at a 30-minute cadence; it would
  matter only if a channel posted 15+ videos between two runs.
- **60 days of total silence disables the cron.** GitHub auto-disables scheduled
  workflows in repos with no activity for 60 days. The state commits normally keep the
  repo active, but if all six channels went quiet for two months you would need to
  re-enable the workflow in the Actions tab.
