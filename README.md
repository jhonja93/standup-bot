# standup-bot

Your daily standup, written for you before you wake up.

Every weekday at 05:00 it looks at what you did since the last report (commits across all your local repos, GitHub PRs, uncommitted work, Claude Code project notes and open Jira tickets), asks Claude to turn it into a short standup update and sends it to you on Telegram.

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
- **Weekdays only.** The timer fires Mon–Fri, and the script also refuses to run on weekends. That second check is needed because `Persistent=true` would otherwise run a missed Friday report when you boot on Saturday.
- **Survives a sleeping PC.** If the machine is off or suspended at 05:00, the report runs as soon as it's back.
- **Low noise.** Only your commits (matched by author email), no stashes, no release PRs titled `develop`/`main`, and only uncommitted files touched within the window.

## Requirements

- Linux with systemd
- Python 3.9+ (standard library only)
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
| `--force` | Run even on a weekend. |
| `--no-checkpoint` | Send, but don't move the time window. Use it for manual tests. |
| `--no-llm` | Use the plain template even if `LLM_ENABLED=true`. |
| `--since` | Override the window start, e.g. `--since 2026-10-10T00:00`. |

Change the schedule in `systemd/standup-bot.timer` (`OnCalendar=Mon..Fri 05:00`) and run `systemctl --user daemon-reload`.

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

## Uninstall

```bash
systemctl --user disable --now standup-bot.timer
rm ~/.config/systemd/user/standup-bot.{service,timer}
rm -r ~/.config/standup-bot ~/.local/state/standup-bot
systemctl --user daemon-reload
```

## License

[MIT](LICENSE)
