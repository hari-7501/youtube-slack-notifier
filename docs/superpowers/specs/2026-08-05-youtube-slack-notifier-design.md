# YouTube → Slack notifier — design

**Date:** 2026-08-05
**Status:** implemented

## Goal

Get a Slack message within roughly half an hour of a new long-form upload on six
YouTube channels. No server, no database, no dependencies.

Explicitly *not* in scope: digests, AI summaries, relevance filtering. Instant
per-upload ping only.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Host | GitHub Actions cron | No infrastructure. Free on public repos. |
| Repo visibility | Public | Actions minutes are free for public repos; 48 runs/day on a private repo would eat a large share of the 2,000 free monthly minutes. Nothing sensitive but the watchlist. |
| Language | Python 3, stdlib only | No `pip install` step; ~15s runs. |
| State store | `state.json` committed by the workflow | Durable and inspectable, git log is a free audit trail, and the periodic commits keep the repo active so GitHub never auto-disables the cron. Committed only when it changes (a few times a week, not 48×/day). |
| Dedup key | Set of last 50 video IDs per channel | A single "latest ID" drops the older of two uploads in one window. Ignoring `updated` means retitling a video does not re-notify. |
| Message format | Plain text + bare `youtu.be` link | Slack's own unfurl produces a better card (thumbnail, duration, channel) than hand-built Block Kit — and Block Kit attachments suppress the unfurl. |
| Cron minute | `7,37` not `0,30` | GitHub documents the top of every hour as a high-load window for scheduled workflows. |

## Verified facts

Established empirically before implementation, not assumed:

- `HEAD https://www.youtube.com/shorts/<id>` returns **200** for a real Short and
  **303** (redirect to `/watch`) for long-form. Tested across 7 video IDs, both
  outcomes, zero-byte bodies. This is the Shorts discriminator, and it needs no API key.
- The RSS feed carries exactly 15 entries (~23 KB) with `yt:videoId`, `title`,
  `published`, `updated`, `author/name`, description and view count. `published` and
  `updated` differ, which is why dedup must ignore `updated`.
- All six channel IDs resolve and their feed `author/name` matches the intended channel.
  `@TBPN` is a *different* channel ("The Booster Pack Network"); the wanted one is
  `@TBPNLive`. Ben Dicken is `@benjdicken`.
- GitHub docs: Actions is free "for public repositories that use standard GitHub-hosted
  runners"; scheduled workflows "run on the latest commit on the default branch", "can be
  delayed during periods of high loads", and are "automatically disabled when no
  repository activity has occurred in 60 days".
- **Not verified:** whether GitHub rounds each job up to the whole minute for billing.
  The public-repo choice makes it moot.

## Flow

1. Load `channels.json` and `state.json`.
2. Per channel, fetch RSS. On any failure: log, skip *that channel only*, leave its
   state untouched. One dead feed cannot block the others or lose a video.
3. Candidates = entries whose `videoId` is not in that channel's `seen` list.
4. Cold start (no state for a channel — first run, or a newly added channel): record all
   15 IDs as seen, notify nothing.
5. Per candidate:
   - `published` in the future → skip **without** marking seen, so it is reconsidered
     once it actually airs. This is the premiere/scheduled-stream guard.
   - `HEAD /shorts/<id>` → 200 means Short: mark seen, stay silent. 303 means notify.
6. Post survivors oldest → newest.
7. Write, commit and push `state.json` only if it changed.

## Failure semantics

- A video is marked seen **only after** Slack returns 2xx. Slack failure → stays unseen →
  retried next run. Deliberate: a possible duplicate beats a silently missed upload.
- The Shorts probe **fails open** — a probe error treats the video as long-form and
  notifies. Same reasoning.
- Run is marked red only when *every* channel fails, so a single transient timeout does
  not produce noise.
- `concurrency` group serialises runs so a manual dispatch cannot race the cron's push.

## Known limitations

1. Delivery is best-effort; GitHub delays and sometimes drops scheduled runs.
2. Premiere suppression is a heuristic (future `published`) that could not be verified
   against a live premiere. Fallback if it leaks: free YouTube Data API key plus
   `snippet.liveBroadcastContent == "upcoming"`, 1 unit of a 10,000/day quota.
3. RSS holds only ~15 videos, so >15 uploads between two runs would lose the overflow.
   Irrelevant at this cadence.
4. 60 days of silence across all six channels would still get the cron auto-disabled.

## Testing

`--dry-run` prints the exact messages without touching Slack or state, and is wired to a
`workflow_dispatch` input so it can be rehearsed in CI. Verified locally against all six
live feeds, both with empty state (cold start: 6/6 seeded, 0 posted) and with a state
deliberately 3 videos behind on every channel (17 posts formatted correctly in
oldest-first order, 1 real Short suppressed, Slack escaping confirmed on a title
containing `&`).
