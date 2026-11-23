# Plan: download lanes

Status: released 2026-11-23 in dl-queue 0.6.0 and harbor 0.2.4.

## What was asked

Separate download queues: named lanes (such as *manga* and *books*), each with its own number of downloads at once, speed limit and quiet hours. Admins manage them on the Admin page; a download, or a preset, picks a lane.

## Decisions

1. **Lanes inside one queue, in dl-queue.** One `DownloadQueue`, one id sequence, so harbor's owners, history, saved jobs and permissions keep working. `BaseQueue` (shared with cbz-webp) doesn't change; `DownloadQueue` decides which job runs next.
2. **No lane means today's behaviour.** The queue's own workers, speed limit and hours (`HARBOR_DOWNLOAD_WORKERS`, `_RATE`, `_HOURS`) are the default lane.
3. **A lane's limits sit under the queue's.** A lane's speed limit caps that lane; the queue's limit still caps all downloads together. A lane without hours follows the queue's hours, so a new lane can't quietly bypass quiet hours.
4. **Removing a lane is safe.** Its queued downloads run under the default lane. A preset or saved job naming a lane that's gone: adds are refused with dl-queue's message; restored jobs fall back to the default lane.
5. **Managed live on the Admin page** (new permission `download.lanes`), stored in the accounts database (migration 7), every change in the audit log and applied without a restart.
6. **Accounts off: no lanes.** There's no Admin page without accounts.
7. **Follow-ups (released 2026-11-23 in dl-queue 0.6.1 and harbor 0.2.6):** per-lane pause (dl-queue `set_lane_paused`, harbor `lane-pause` with `download.lanes`, not saved across restarts, like the overall pause); per-role access (migration 8, "Who can use it" on the Admin page, Admin always may, others don't see the lane and are refused on add); per-site fairness behind `HARBOR_DOWNLOAD_FAIR` (off by default; the site with the fewest running downloads in the lane goes next).

## Steps

1. dl-queue: `Lane`, `DownloadQueue(lanes=)`, `set_lanes`, `add(lane=)`, `download_lanes` in the API's state. Release 0.6.0.
2. harbor: migration and permission, `AdminAPI` lane actions with audit, loading lanes at start-up, saved jobs keep their lane, the Admin sub-tab, a Lane menu on the Download tab, `lane` in presets, the Chrome extension passing a preset's lane.
