# The board and what ARGUS does on its own

## The board

The dashboard opens on your board: **Inbox, Today, Doing, Parked, Done**.

### Capture

Type in the box at the top, or press **Ctrl+Alt+Space** from any app:

```
call Sam at 5pm
review PR tomorrow 10am
pay rent friday
```

A clock time ("5pm", "in 30 min") sets a reminder that fires as a toast.
Capturing the same work twice adds a note to the existing card, not a
duplicate.

### Schedule

```
schedule sync with Priya tomorrow 4pm for 30 min
block 2h for atlas
```

The event goes on your work or personal Google calendar, and a card with a
reminder lands on the board - even while Google is signed out.
`block ...` with no time takes the next free stretch of your working day.

### Cards ARGUS creates for you

- **Unfinished work.** Uncommitted or unpushed work becomes a card that closes
  itself once it is committed and pushed.
- **Dropped projects.** A project that went quiet mid-way becomes
  "Pick back up: X?", with the last thing you asked Claude Code.
- **GitHub.** Pull requests waiting for your review, your failing CI and
  changes requested become cards (for every account `gh` is logged in to) and
  close themselves when resolved.
- **Stale branches.** Merged branches older than 30 days become a clean-up
  card with the command to copy. ARGUS never deletes them itself.

Every project card says the next step - "Commit 2 changed files on feat/x",
"Push 5 commits", "Continue: (your last Claude ask)".

### Where exactly was I?

Click a card: branch, files not committed yet, last files touched, last Claude
asks, commits, windows and pages - plus **Open in VS Code** and **resume the
Claude Code session** (`claude --resume`) in one click.

### The daily rhythm

| When | What |
|---|---|
| 09:15, work days | **Plan my day** - pick 3 to 5 cards; the briefings follow your list. *Start + block time* puts each picked card on the calendar in the next free hour. |
| While working | **Focus 25 / 50** on a card: a countdown in the top bar, nudges on hold, a toast at the end. Sessions are logged on the card. |
| If you drift | Start a card and wander off it for 45 minutes, and ARGUS asks whether to park it with a note. |
| 18:45, work days | **Wrap up** - park what is open with a note, and copy the day's timesheet entry: hours, projects, what shipped. |
| Friday | **Review** - the week's shipped wall, a copyable report, hours by day split office / personal, your finish streak, and cards that went stale. |

Ask **"what should I do now?"** for an instant answer from the board.

### Keys

`n` new · `j` / `k` move · `Enter` start · `d` done · `t` today · `p` park ·
`r` remind · `x` dismiss · `[` collapse the sidebar. Theme: Auto / Light / Dark.

## On your phone (Telegram)

Make a bot with @BotFather, then:

```
argus telegram setup <token>
```

and send the bot `/start`. Reminders and important alerts reach your phone;
anything you send the bot is captured onto the board; `/today`, `/next` and
`/now` answer from it. Only your own chat is listened to.

## Automations

Everything an automation does is logged - `argus runs`, or the System tab.

| Automation | What it does |
|---|---|
| **Morning brief** (08:30) | The one thing to do today, yesterday's real output, meetings, loose ends. |
| **Evening wrap** (23:30) | A verdict on the day, what shipped, where the time went. Also writes `data/journal/YYYY-MM-DD.md`. |
| **Weekly retro** (Sunday 20:00) | What moved, what stalled, patterns in your hourly rhythm. |
| **Standup writer** (09:45) | Drafts yesterday's standup from commits and files - office work only. |
| **Focus guardian** | Notices a distraction spiral or a context-switch storm while it is happening. |
| **Meeting sentinel** | Warns 10 and 2 minutes out, with the video link ready and prep notes: project state, your cards, unread mail from the attendees. |
| **Inbox triage** | Sorts unread mail into needs-reply / VIP / FYI / noise and can draft replies. **It never sends mail.** |
| **Repo janitor** | Nags about uncommitted work older than 24 hours. |
| **System watchdog** | Disk space, sustained CPU, low battery, memory pressure. |
| **Security watch** | New autostart entries and new privileged listening ports. |
| **Break reminder**, **downloads janitor** | What they say. |

A few behaviours worth knowing:

- **Click a toast** and it opens what it is about - the meeting's video link,
  the mail thread in the right Gmail account, or the relevant dashboard tab.
- **No nudges in meetings.** Focus and break reminders hold while your
  calendar says you are in one.
- **Missed reminders** from while the laptop slept arrive as one toast.
- **Answers are kept.** Every `argus ask` answer is saved and listed with the
  briefings on the Ask tab.

## Developer hygiene

- Secrets staged for commit (AWS, GitHub, OpenAI, Slack, Google keys, private
  keys) raise an urgent alert *before* you commit. Huge staged files and a
  tracked or un-ignored `.env` raise a warning.
- Dev servers you left running are listed on the System page, with a stop
  button.

## Timesheets

`argus timesheet` turns the week's sessions into timesheet evidence: first and
last activity per day, active and work hours, the work projects you touched,
and your work commits. `--csv week.csv` writes it paste-ready; the dashboard's
Work tab has the same table with a CSV button.
