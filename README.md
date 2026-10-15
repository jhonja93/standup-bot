# standup-bot

Your daily standup, written for you before you wake up.

Every workday at 05:00 it looks at what you did since the last report (commits across all your local repos, GitHub PRs, uncommitted work, Claude Code project notes and open Jira tickets), asks Claude to turn it into a short standup update and sends it to you on Telegram.

```
Yesterday
• Shipped CSV export for reports (api + web)
• Fixed pagination on the invoices list
• Opened #128 with the new rate limiter

Today
• Address review feedback on #128
• Start PROJ-42: SSO for enterprise clients

Blockers
• PROJ-37: waiting on the client for API credentials
```

## How it works

```
collect                    render                   deliver
─────────────────────      ─────────────────────    ──────────
git log (all repos)   ┐
gh (PRs, reviews)     │    Claude (claude -p)       Telegram
uncommitted changes   ├──▶ or plain template   ──▶  bot
Claude Code memory    │    if Claude fails
Jira (acli)           ┘
```

- **One pipeline, independent stages.** Each source fails on its own: if Jira is down you still get your commits, and the error is listed at the end of the message.
- **Time window = since the last successful send**, not "yesterday". Monday's report covers Friday through Sunday, and a failed day is picked up by the next run. The checkpoint only moves after Telegram confirms delivery.
- **Workdays only.** The timer fires every day, and the script skips weekends and the public holidays of any country in `HOLIDAY_COUNTRIES`. Keeping that decision in the script, not the timer, keeps all the calendar logic in one place. The report after a day off covers the day off too.
- **Birthday greeting.** On `BIRTHDAY` you get a greeting, even on weekends, holidays and pauses, at most once per year.
- **Moved holidays.** Countries like Ecuador move holidays (Saturday → Friday, Sunday → Monday, Tuesday → Monday, Wednesday/Thursday → Friday). The [`holidays`](https://github.com/vacanza/holidays) package knows these rules; the original date counts as a workday again. Holidays moved by a one-off decree go in `EXTRA_DAYS_OFF` / `EXTRA_WORKDAYS`.
- **Survives a sleeping PC.** If the machine is off or suspended at 05:00, the report runs as soon as it's back.
- **Honest on empty days.** If you did nothing in the window (vacation, a day of meetings), the report says *no recorded activity* and lists your open tickets. Claude is skipped on those days, because with nothing to summarize it would fill the gap with plausible but invented work. Open tickets are plans, so they never count as work done.
- **Your actions, not your PRs' activity.** A PR counts only if you opened, merged or closed it within the window, and a review only if you submitted it then. Someone commenting on your PR doesn't make it your work.
- **Low noise.** Only your commits (matched by author email), no stashes, no release PRs titled `develop`/`main`, and only uncommitted files touched within the window.

## Requirements

- Linux with systemd
- Python 3.9+
- The [`holidays`](https://github.com/vacanza/holidays) package for public holidays (`sudo dnf install python3-holidays`, `sudo apt install python3-holidays` or `pip install --user holidays`). Without it, only weekends are skipped and the report says so.
- `git`
- Optional:
  - [`gh`](https://cli.github.com/), logged in, for PRs and reviews
  - [`acli`](https://developer.atlassian.com/cloud/acli/), logged in, for Jira tickets
  - [Claude Code](https://claude.com/claude-code), logged in, for the written summary and the project memory notes

Each optional source can be turned off in the config.

## Setup

1. **Create a Telegram bot.** Message [@BotFather](https://t.me/BotFather), send `/newbot` and copy the token.

2. **Install.**

   ```bash
   git clone git@github.com:jhonja93/standup-bot.git
   cd standup-bot
   ./install.sh
   ```

   This creates `~/.config/standup-bot/config.env` (mode `600`), installs the systemd user timer and enables lingering so the timer runs even when you're not logged in.

3. **Configure.** Put your bot token in `~/.config/standup-bot/config.env`, then send any message to your bot and get your chat id:

   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[0-9]*'
   ```

   Set `TELEGRAM_CHAT_ID`, `PROJECTS_DIR` and `GIT_EMAILS` (every email you commit with).

4. **Test it.**

   ```bash
   ./standup.py --dry-run --force               # print the report, send nothing
   ./standup.py --force --no-checkpoint         # send it, but keep tomorrow's window intact
   ```

## Configuration

`~/.config/standup-bot/config.env` (override the path with `STANDUP_CONFIG`). Environment variables take precedence over the file.

| Key | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Bot token from @BotFather. |
| `TELEGRAM_CHAT_ID` | — | Chat that receives the report. |
| `HOLIDAY_COUNTRIES` | — | Comma-separated ISO country codes, e.g. `US,EC`. A holiday in any of them is a day off. |
| `EXTRA_DAYS_OFF` | — | Comma-separated `YYYY-MM-DD` dates to skip: holidays moved by decree, company days off. |
| `EXTRA_WORKDAYS` | — | Comma-separated `YYYY-MM-DD` dates to run on even if they are holidays. |
| `BIRTHDAY` | — | `MM-DD`. Sends a greeting that day, whatever the day. Feb 29 is celebrated on Feb 28 in common years. |
| `BIRTHDAY_MESSAGE` | a short greeting | Custom greeting, Telegram HTML on one line, `\n` for line breaks. |
| `PAUSE_UNTIL` | — | Skip reports through this date (inclusive), e.g. `2026-12-31` for a vacation. |
| `PROJECTS_DIR` | `~/Documents` | Folder whose direct subfolders are git repos. |
| `GIT_EMAILS` | — | Comma-separated author emails to include. |
| `EXCLUDE_REPOS` | — | Comma-separated folder names to skip (forks, vendored code). |
| `GITHUB_ENABLED` | `true` | Include your PRs and the PRs you reviewed. |
| `MEMORY_ENABLED` | `true` | Include Claude Code memories of type `project` updated in the window. |
| `JIRA_ENABLED` | `true` | Include your open Jira tickets. |
| `ACLI_BIN` | `~/.local/bin/acli` | Path to the Atlassian CLI. |
| `JIRA_JQL` | your open tickets | JQL for the "Today" section. Narrow it (e.g. only In Progress) for a shorter list. |
| `LLM_ENABLED` | `false` | Let Claude write the summary instead of the plain template. |
| `CLAUDE_BIN` | `~/.local/bin/claude` | Path to the Claude Code CLI. |
| `CLAUDE_MODEL` | account default | e.g. `sonnet` for a faster summary. |

## Usage

```
standup.py [--dry-run] [--force] [--no-checkpoint] [--no-llm] [--since ISO_DATE]
```

| Flag | Effect |
|---|---|
| `--dry-run` | Print the message instead of sending it. Does not move the checkpoint. |
| `--force` | Run even on a weekend, holiday or pause. |
| `--no-checkpoint` | Send, but don't move the time window. Use it for manual tests. |
| `--no-llm` | Use the plain template even if `LLM_ENABLED=true`. |
| `--since` | Override the window start, e.g. `--since 2026-10-10T00:00`. |

Change the time in `systemd/standup-bot.timer` (`OnCalendar=*-*-* 05:00`) and run `systemctl --user daemon-reload`. Keep it daily: which days get a report is decided by the script.

## Privacy

With `LLM_ENABLED=true`, the following is sent to Anthropic: commit subjects, PR and ticket titles, and the text of your Claude Code project memories. **Diffs and source code are never sent.** The report itself goes through Telegram's servers. If this runs against work repositories, check that your employer's policy allows it.

Claude Code memories can contain hostnames, IPs or env var names. The prompt tells Claude never to copy them into the summary, but an LLM gives no hard guarantee, so read the report before sharing it.

Claude runs with its own system prompt, no tools and no session persistence, from a neutral directory, so your `CLAUDE.md` files and `claude --resume` history aren't affected.

## Troubleshooting

```bash
systemctl --user list-timers standup-bot.timer   # next scheduled run
journalctl --user -u standup-bot.service -n 50   # logs of the last runs
systemctl --user start standup-bot.service       # run now (moves the checkpoint)
cat ~/.local/state/standup-bot/last_run          # current window start
```

- **No report arrived:** check the journal. A missing token or chat id, or an expired `gh`/`acli`/`claude` login, shows up there.
- **Report looks like a raw list:** Claude failed or is disabled. The message ends with `LLM unavailable: …` when it failed.
- **Missing commits:** check that the author email is in `GIT_EMAILS` (`git log --format='%ae' | sort -u`). Commits made on another machine only show up after a `git fetch`.

## Tests

```bash
python3 -m unittest discover tests
```

The calendar tests pin known US and Ecuador dates, including moved holidays. Run them after upgrading the `holidays` package: if it changes how moved holidays are named, they fail instead of the bot silently skipping workdays.

## Uninstall

```bash
systemctl --user disable --now standup-bot.timer
rm ~/.config/systemd/user/standup-bot.{service,timer}
rm -r ~/.config/standup-bot ~/.local/state/standup-bot
systemctl --user daemon-reload
```

## License

[MIT](LICENSE)
