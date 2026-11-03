# BUzz

Buzzes your phone about eLearn deadlines, harder and more often as they get close, until you tap **Done**.

| Time left | How often | Loudness |
|---|---|---|
| up to 7 days | once a day | quiet |
| under 3 days | every 8 hours | normal |
| under 24 hours | every 3 hours | high |
| under 6 hours | every 30 min | max (🚨) |
| under 3 hours | every 15 min | max |
| final hour | every 10 min, 3 buzzes each time | max |
| overdue (up to 24h) | every 30 min | max |

Each alert has **Done ✅** (stops that assignment for good), **Snooze 1h**, and **Open** (jumps to eLearn when the feed has a link).

After you tap Done or Snooze, a quiet ✅ / 😴 confirmation shows up within 10 minutes. For an instant confirmation, also subscribe in ntfy to your topic name with `-done` on the end (e.g. `buzz-ron-8f3k2q9x-done`).

## Setup (about 10 minutes)

### 1. Phone: install ntfy
* Android: "ntfy" on Google Play or F-Droid. iPhone: "ntfy" on the App Store.
* Tap **+**, subscribe to a topic name only you know, e.g. `buzz-ron-8f3k2q9x`. Treat it like a password: anyone who knows it can see your alerts.
* Android, to make it really aggressive: open the topic, ⋮ → **Notification settings**, and turn on "Override Do Not Disturb" / allow the max-priority channel to vibrate and pop up. On iPhone, allow ntfy notifications with sound and Time Sensitive alerts.

### 2. eLearn: copy your calendar feed link
TSU eLearn (elearn.tnstate.edu) is D2L Brightspace: open **Calendar** (from the course nav bar or the Calendar widget on the home page), click **Subscribe** near the top, pick **All Courses**, and copy the link it shows.

* **D2L Brightspace** (most "eLearn" sites): Calendar → the course/calendar dropdown → **Subscribe** → choose "All courses" → copy the link.
* **Moodle**: Calendar → **Export calendar** → Events: All, Time: Recent and next 60 days → **Get calendar URL** → copy.

This link is private (it logs into your calendar), so keep it in a secret, never in a file.

### 3. Run it every 10 minutes for free (GitHub Actions)
1. Create a new GitHub repo (public is fine and gets unlimited free Actions minutes; your links live in secrets, not in the code). Upload `buzz.py` and `.github/workflows/buzz.yml`.
2. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   * `ELEARN_ICS_URL` = your calendar link
   * `NTFY_TOPIC` = your topic name
3. Optional, under the **Variables** tab: `TZ_NAME` (default `America/Chicago`) and `QUIET_HOURS` like `01-07` (silences anything that isn't due within 6 hours).
4. **Actions** tab → enable workflows → "buzz" → **Run workflow** to test it now.

GitHub turns off scheduled workflows on repos with no activity for 60 days; if buzzing stops, open the Actions tab and re-enable it.

### Or run it on your own computer
```
ELEARN_ICS_URL='https://...' NTFY_TOPIC='buzz-ron-8f3k2q9x' python3 buzz.py
```
Put that in cron (`*/10 * * * *`) or Task Scheduler. Python 3.9+ with no extra packages.

## Tuning
* `INCLUDE_REGEX` decides which calendar items count as work (default catches due, assignment, quiz, exam, lab, essay, project, dropbox...). Lectures and other events are ignored.
* `EXCLUDE_REGEX` to silence something, e.g. `Availability Starts|Office Hours`.
* `LOOKAHEAD_DAYS` (7), `OVERDUE_HOURS` (24). Frequencies live in the `TIERS` table at the top of `buzz.py`.
* Test without sending: `DRY_RUN=1 python3 buzz.py`.

## Limits
eLearn's calendar feed doesn't say whether you've submitted, so BUzz keeps going until you tap Done. If a deadline is moved, it starts buzzing again for the new time.
