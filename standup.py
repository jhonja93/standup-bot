#!/usr/bin/env python3
"""Daily standup summary: commits, PRs, uncommitted work, Claude Code notes and Jira tickets.

Pipeline:  collect (git / gh / memory / jira)  ->  render (template or LLM)  ->  deliver (Telegram or stdout)

Usage:
    standup.py              # normal run (what the timer launches)
    standup.py --dry-run    # print the message; send nothing, don't move the checkpoint
    standup.py --since 2026-10-10T00:00   # force the start of the time window
    standup.py --force      # run even on a day off (weekend, holiday) or during a pause
    standup.py --no-checkpoint   # send a test without changing the next report's window
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("STANDUP_CONFIG", "~/.config/standup-bot/config.env")).expanduser()
STATE_PATH = Path("~/.local/state/standup-bot/last_run").expanduser()
BIRTHDAY_STATE_PATH = STATE_PATH.with_name("last_birthday")
BIRTHDAY_PROMPT = """\
Write a birthday message for a software engineer, delivered by their personal Telegram bot \
at 5 a.m. on their birthday. Output ONLY the message, in English.

- 2 to 4 short lines, warm and a little playful, like a friend would write it. One or two emojis.
- A fresh angle each year: don't fall back on the same clichés.
- No mention of meetings, standups, tasks or productivity: today is not about work.
- Telegram HTML only: <b> and <i> are allowed, nothing else; no markdown."""
BIRTHDAY_DEFAULT_MESSAGE = "🎂 <b>Happy birthday!</b>\nWishing you a great day and a great year ahead. 🎉"
TELEGRAM_LIMIT = 4096
CMD_TIMEOUT = 60
RELEASE_PR_TITLES = {"develop", "main", "master", "release", "staging"}
JIRA_STATUS_ORDER = ["In Progress", "In Review", "Blocked", "To Do"]


# ---------------------------------------------------------------- config

def load_config() -> dict[str, str]:
    """Read a KEY=VALUE file. Environment variables take precedence."""
    cfg: dict[str, str] = {}
    if CONFIG_PATH.exists():
        for line in CONFIG_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            cfg[key.strip()] = value.strip().strip('"').strip("'")
    for key in list(cfg):
        cfg[key] = os.environ.get(key, cfg[key])
    return cfg


def csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


# ---------------------------------------------------------------- time window

class Calendar:
    """Workdays: Monday to Friday, minus public holidays of ANY configured country.

    Holidays come from the `holidays` package, which already implements moving rules such as
    Ecuador's (Saturday -> Friday, Sunday -> Monday, Tuesday -> Monday, Wed/Thu -> Friday).
    Government decrees that move a holiday on short notice aren't predictable, so they go
    in EXTRA_DAYS_OFF / EXTRA_WORKDAYS.
    """

    def __init__(self, countries: list[str], extra_off: list[str], extra_work: list[str]) -> None:
        self.countries = countries
        self.extra_off = {date.fromisoformat(d): "Extra day off" for d in extra_off}
        self.extra_work = {date.fromisoformat(d) for d in extra_work}
        self.error: str | None = None
        self._cache: dict[int, dict[date, str]] = {}

    def _year(self, year: int) -> dict[date, str]:
        if year in self._cache:
            return self._cache[year]
        days: dict[date, str] = {}
        try:
            import holidays  # optional: without it only weekends are skipped
        except ImportError:
            self.error = "python package 'holidays' is not installed; holidays are ignored"
            self._cache[year] = days
            return days
        for country in self.countries:
            for day, name in moved_holidays(holidays.country_holidays(country, years=year)).items():
                days[day] = f"{days[day]}, " if day in days else ""
                days[day] += f"{country}: {name}"
        self._cache[year] = days
        return days

    def day_off(self, day: date) -> str | None:
        """Why `day` is not a workday, or None if it is one."""
        if day in self.extra_work:
            return None
        if day.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
            return "Weekend"
        return self.extra_off.get(day) or self._year(day.year).get(day)


def moved_holidays(calendar: dict[date, str]) -> dict[date, str]:
    """Holidays as actually taken: when one is moved, the original date is a workday again.

    The package lists both the original date and the "(observed)" one, e.g. Ecuador 2027:
    Tue Aug 10 and Mon Aug 9 (observed). Only the observed day is off.
    """
    days = {day: name.split("; ") for day, name in calendar.items()}
    for day, names in list(days.items()):
        for name in names:
            if not name.endswith(" (observed)"):
                continue
            original = name.removesuffix(" (observed)")
            for other in (day + timedelta(days=n) for n in range(-7, 8) if n):
                if original in days.get(other, []) and other.weekday() < 5:
                    days[other].remove(original)
    return {day: "; ".join(n.removesuffix(" (observed)") for n in names) for day, names in days.items() if names}


def previous_workday_start(now: datetime, calendar: Calendar) -> datetime:
    """Start (00:00) of the previous workday. On Monday it returns Friday, skipping holidays."""
    day = now - timedelta(days=1)
    while calendar.day_off(day.date()):
        day -= timedelta(days=1)
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


def resolve_since(arg: str | None, now: datetime, calendar: Calendar) -> datetime:
    if arg:
        return datetime.fromisoformat(arg).astimezone()
    if STATE_PATH.exists():
        try:
            return datetime.fromisoformat(STATE_PATH.read_text().strip())
        except ValueError:
            pass
    return previous_workday_start(now, calendar)


def save_checkpoint(now: datetime) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(now.isoformat())


# ---------------------------------------------------------------- collectors

@dataclass
class Report:
    since: datetime
    commits: dict[str, list[str]] = field(default_factory=dict)  # repo -> ["branch: subject"]
    dirty: dict[str, int] = field(default_factory=dict)          # repo -> archivos modificados
    prs: list[str] = field(default_factory=list)
    reviews: list[str] = field(default_factory=list)
    jira: list[str] = field(default_factory=list)
    memory: list[tuple[str, str, str]] = field(default_factory=list)  # (project, description, body)
    errors: list[str] = field(default_factory=list)

    def has_activity(self) -> bool:
        """Anything you actually did in the window. Open Jira tickets don't count."""
        return any([self.commits, self.prs, self.reviews, self.dirty, self.memory])


def run(cmd: list[str], cwd: Path | None = None, stdin: str | None = None,
        timeout: int = CMD_TIMEOUT) -> str:
    result = subprocess.run(cmd, cwd=cwd, input=stdin, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])}: {result.stderr.strip()[:200]}")
    return result.stdout


def find_repos(root: Path, exclude: list[str]) -> list[Path]:
    return sorted(p.parent for p in root.glob("*/.git") if p.parent.name not in exclude)


def collect_git(report: Report, repos: list[Path], emails: list[str]) -> None:
    author_args = [f"--author={e}" for e in emails]
    for repo in repos:
        try:
            # --since filters by commit date; we also filter by author date
            # so a rebase doesn't bring back old work.
            out = run(
                ["git", "log", "--branches", "--remotes", "--no-merges", f"--since={report.since.isoformat()}",
                 *author_args, "--format=%aI%x09%S%x09%s"],
                cwd=repo,
            )
            seen: set[str] = set()
            lines = []
            for row in out.splitlines():
                authored, ref, subject = row.split("\t", 2)
                if datetime.fromisoformat(authored) < report.since or subject in seen:
                    continue
                seen.add(subject)
                branch = ref.removeprefix("refs/heads/").removeprefix("refs/remotes/").removeprefix("origin/")
                lines.append(f"{subject} ({branch})")
            if lines:
                report.commits[repo.name] = list(reversed(lines))  # chronological order

            # Only count files touched within the window; otherwise a repo with
            # months-old uncommitted leftovers would show up every day.
            status = run(["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=repo)
            recent = 0
            for entry in filter(None, status.split("\0")):
                path = repo / entry[3:]
                try:
                    if datetime.fromtimestamp(path.stat().st_mtime).astimezone() >= report.since:
                        recent += 1
                except OSError:  # deleted file: also work in progress
                    recent += 1
            if recent:
                report.dirty[repo.name] = recent
        except Exception as exc:  # un repo roto no debe tumbar el reporte entero
            report.errors.append(f"git {repo.name}: {exc}")


def collect_github(report: Report) -> None:
    """PRs you opened, merged or closed, and PRs you reviewed, within the window.

    GitHub's `updated` filter only has day granularity and fires on any change (a comment,
    a CI status), so it's just a coarse pre-filter; the exact check happens here.
    """
    day = report.since.date().isoformat()
    fields = "repository,title,state,number,isDraft,createdAt,closedAt"

    def within(timestamp: str | None) -> bool:
        return bool(timestamp) and datetime.fromisoformat(timestamp.replace("Z", "+00:00")) >= report.since

    try:
        authored = json.loads(run(["gh", "search", "prs", "--author=@me", f"--updated=>={day}",
                                   "--json", fields, "--limit", "30"]))
        mine = {(pr["repository"]["name"], pr["number"]) for pr in authored}
        for pr in authored:
            if pr["title"].strip().lower() in RELEASE_PR_TITLES:
                continue
            if pr["state"] in ("merged", "closed") and within(pr["closedAt"]):
                action = pr["state"]
            elif within(pr["createdAt"]):
                action = "opened (draft)" if pr["isDraft"] else "opened"
            else:
                continue  # touched by someone else, not by you
            report.prs.append(f"[{action}] {pr['repository']['name']}#{pr['number']} {pr['title']}")

        reviewed = json.loads(run(["gh", "search", "prs", "--reviewed-by=@me", f"--updated=>={day}",
                                   "--json", fields, "--limit", "30"]))
        login = run(["gh", "api", "user", "--jq", ".login"]).strip()
        for pr in reviewed:
            if (pr["repository"]["name"], pr["number"]) in mine:
                continue
            # The search can't tell when you reviewed, so check the reviews themselves.
            repo = pr["repository"]["nameWithOwner"]
            submitted = run(["gh", "api", "--paginate", f"repos/{repo}/pulls/{pr['number']}/reviews",
                             "--jq", f'.[] | select(.user.login == "{login}") | .submitted_at'])
            if any(within(t) for t in submitted.split()):
                report.reviews.append(f"{pr['repository']['name']}#{pr['number']} {pr['title']}")
    except Exception as exc:
        report.errors.append(f"github: {exc}")


def collect_jira(report: Report, acli: str, jql: str) -> None:
    try:
        items = json.loads(run([acli, "jira", "workitem", "search", "--jql", jql,
                                "--fields", "key,summary,status", "--limit", "15", "--json"]))
        rank = {name.lower(): i for i, name in enumerate(JIRA_STATUS_ORDER)}
        items.sort(key=lambda it: rank.get(it["fields"]["status"]["name"].lower(), len(rank)))
        for item in items:
            f = item["fields"]
            report.jira.append(f"[{f['status']['name']}] {item['key']} {f['summary']}")
    except Exception as exc:
        report.errors.append(f"jira: {exc}")


def collect_memory(report: Report, root: Path, exclude: list[str], max_body: int = 1500) -> None:
    """Claude Code memories (type `project`) modified within the window.

    Claude Code stores each project in ~/.claude/projects/<path with / and . replaced by ->/memory/.
    They add context git doesn't have: decisions, what's in prod and what's pending, who we're waiting on.
    """
    projects_dir = Path("~/.claude/projects").expanduser()
    encoded_root = re.sub(r"[^A-Za-z0-9]", "-", str(root))
    try:
        for project in sorted(projects_dir.glob(encoded_root + "*")):
            name = project.name.removeprefix(encoded_root).lstrip("-") or "workspace"
            if name in exclude:
                continue
            for note in sorted((project / "memory").glob("*.md")):
                if note.name == "MEMORY.md":
                    continue
                if datetime.fromtimestamp(note.stat().st_mtime).astimezone() < report.since:
                    continue
                text = note.read_text(errors="replace")
                front, _, body = text.partition("\n---\n") if text.startswith("---") else ("", "", text)
                if not re.search(r"^\s*type:\s*project\b", front, re.M):
                    continue  # feedback / user / reference notes don't describe work
                desc = re.search(r"^description:\s*(.+)$", front, re.M)
                desc_text = desc.group(1).strip().strip('"\'') if desc else note.stem
                report.memory.append((name, desc_text, body.strip()[:max_body]))
    except Exception as exc:
        report.errors.append(f"memory: {exc}")


# ---------------------------------------------------------------- render

def render_template(r: Report) -> str:
    """Deterministic Telegram HTML format. Always works, no external dependencies."""
    e = html.escape
    out = [f"<b>🗓 Standup — {datetime.now():%a %b %d}</b>",
           f"<i>Since {r.since:%a %b %d, %H:%M}</i>", ""]

    out.append("<b>✅ Done</b>")
    if r.commits:
        for repo, lines in r.commits.items():
            out.append(f"<b>{e(repo)}</b>")
            out += [f"  • {e(l)}" for l in lines]
    elif r.has_activity():
        out.append("  (no commits)")
    else:
        out.append("  (no recorded activity)")
    if r.prs:
        out += ["", "<b>🔀 My PRs</b>"] + [f"  • {e(p)}" for p in r.prs]
    if r.reviews:
        out += ["", "<b>👀 Reviewed</b>"] + [f"  • {e(p)}" for p in r.reviews]
    if r.dirty:
        out += ["", "<b>🚧 Uncommitted work</b>"]
        out += [f"  • {e(repo)}: {n} file(s)" for repo, n in r.dirty.items()]
    if r.memory:
        out += ["", "<b>🧠 Notes (Claude memory)</b>"]
        out += [f"  • {e(project)}: {e(desc)}" for project, desc, _ in r.memory]
    if r.jira:
        out += ["", "<b>📋 Open tickets (Jira)</b>"] + [f"  • {e(j)}" for j in r.jira]
    if r.errors:
        out += ["", "<b>⚠️ Errors</b>"] + [f"  • {e(x)}" for x in r.errors]
    return "\n".join(out)


LLM_SYSTEM_PROMPT = """\
You turn raw activity data into a developer's daily standup update. Output ONLY the update, \
in English, with no preamble, no questions and no comments about the input.

Format (Telegram HTML: only <b> and <i>; no lists, headings or markdown; use "• " for bullets):
<b>Yesterday</b>
• Up to 6 bullets, built ONLY from commits, PRs, reviews, uncommitted work and project notes. \
Open Jira tickets are plans, not work done: never use them here. If there is no such activity, \
write a single bullet "• No recorded activity" and nothing else. Group related commits and PRs into outcomes ("Shipped CSV export for \
reports (api + web)"); never list commits one by one. Skip trivial ones (lint, \
wip, typo, merges, release PRs).
<b>Today</b>
• 2-4 bullets, based on In Progress / In Review tickets, uncommitted work and what the project \
notes say is still pending (e.g. merged to develop but not yet in prod, waiting on someone). \
Mention ticket keys.
<b>Blockers</b>
• Only if there are Blocked tickets or the notes say you are waiting on someone; otherwise omit \
this section.

Write it the way someone speaks in a meeting: short, first person, past tense for Yesterday. \
Use the project notes only as context: never copy hosts, IPs, credentials \
or env var names into the update. If the input has an Errors section, append it unchanged at the end."""


def ask_claude(cfg: dict[str, str], system_prompt: str, data: str) -> str:
    """One-shot Claude call: own system prompt, no tools, no saved session."""
    cmd = [os.path.expanduser(cfg.get("CLAUDE_BIN", "~/.local/bin/claude")), "-p",
           "--system-prompt", system_prompt, "--tools", "",
           "--output-format", "text", "--no-session-persistence"]
    if cfg.get("CLAUDE_MODEL"):
        cmd += ["--model", cfg["CLAUDE_MODEL"]]
    # Neutral cwd: no project CLAUDE.md to leak into the output.
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = run(cmd, cwd=STATE_PATH.parent, stdin=data, timeout=300).strip()
    if not out:
        raise RuntimeError("empty response")
    return out


def render_llm(r: Report, cfg: dict[str, str]) -> str:
    """Spoken-style summary by Claude. Only commit subjects and titles are sent, never diffs."""
    data = render_template(r)
    if r.memory:  # the LLM gets the full notes; the template only the description
        data += "\n\n# Project notes updated in this period (status, decisions, what is pending)\n"
        data += "\n\n".join(f"## {project}: {desc}\n{body}" for project, desc, body in r.memory)
    return ask_claude(cfg, LLM_SYSTEM_PROMPT, data)


# ---------------------------------------------------------------- deliver

def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line[:limit]
    if current.strip():
        chunks.append(current)
    return chunks


def send_telegram(token: str, chat_id: str, text: str, parse_mode: str | None = "HTML") -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in split_message(text):
        payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        data = urllib.parse.urlencode(payload).encode()
        for attempt in range(3):  # after resuming from suspend the network may take a while to come up
            try:
                with urllib.request.urlopen(url, data=data, timeout=20) as resp:
                    if json.load(resp).get("ok"):
                        break
            except Exception as exc:
                if attempt == 2:
                    raise RuntimeError(f"telegram: {exc}") from exc
                time.sleep(15 * (attempt + 1))


# ---------------------------------------------------------------- birthday

def is_birthday(today: date, month_day: str) -> bool:
    """`month_day` is MM-DD. A Feb 29 birthday is celebrated on Feb 28 in common years."""
    month, day = (int(x) for x in month_day.split("-"))
    if (month, day) == (2, 29) and not is_leap(today.year):
        day = 28
    return (today.month, today.day) == (month, day)


def is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def send_birthday(cfg: dict[str, str], today: date, dry_run: bool) -> None:
    """Greeting on BIRTHDAY, whatever the day: weekends, holidays and pauses don't apply.

    Sent at most once per year, so a manual run or a catch-up run doesn't repeat it.
    """
    if not cfg.get("BIRTHDAY") or not is_birthday(today, cfg["BIRTHDAY"]):
        return
    if BIRTHDAY_STATE_PATH.exists() and BIRTHDAY_STATE_PATH.read_text().strip() == str(today.year):
        return
    message = cfg.get("BIRTHDAY_MESSAGE", "").replace("\\n", "\n") or BIRTHDAY_DEFAULT_MESSAGE
    if cfg.get("LLM_ENABLED", "false") == "true":
        try:
            message = ask_claude(cfg, BIRTHDAY_PROMPT, f"Today is {today:%A, %B %d, %Y}.")
        except Exception as exc:  # the fixed message is the fallback
            print(f"birthday: Claude unavailable ({exc}), sending the fixed message", file=sys.stderr)
    if dry_run:
        print(message, end="\n\n")
        return
    try:
        token, chat_id = telegram_credentials(cfg)
        try:
            send_telegram(token, chat_id, message)
        except RuntimeError:  # most likely invalid HTML from Claude
            send_telegram(token, chat_id, message, parse_mode=None)
    except Exception as exc:  # a failed greeting must not block the standup report
        print(f"birthday: {exc}", file=sys.stderr)
        return
    BIRTHDAY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BIRTHDAY_STATE_PATH.write_text(str(today.year))


def telegram_credentials(cfg: dict[str, str]) -> tuple[str, str]:
    token, chat_id = cfg.get("TELEGRAM_BOT_TOKEN"), cfg.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError(f"Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in {CONFIG_PATH}")
    return token, chat_id


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    parser.add_argument("--since", help="start of the time window (ISO 8601)")
    parser.add_argument("--force", action="store_true", help="run even on a day off or during a pause")
    parser.add_argument("--no-checkpoint", action="store_true",
                        help="send without moving the window (for manual tests)")
    parser.add_argument("--no-llm", action="store_true", help="use the plain template only")
    args = parser.parse_args()

    now = datetime.now().astimezone()
    cfg = load_config()
    calendar = Calendar(csv(cfg.get("HOLIDAY_COUNTRIES", "")), csv(cfg.get("EXTRA_DAYS_OFF", "")),
                        csv(cfg.get("EXTRA_WORKDAYS", "")))
    send_birthday(cfg, now.date(), args.dry_run)

    reason = calendar.day_off(now.date())
    if reason and not args.force:
        # The timer fires every day (for the birthday greeting), so this is what keeps
        # weekends and holidays quiet. The checkpoint stays put, so the next report also
        # covers the day before the break.
        print(f"Day off ({reason}): no standup.")
        return 0

    pause_until = cfg.get("PAUSE_UNTIL", "")
    if pause_until and now.date() <= datetime.fromisoformat(pause_until).date() and not args.force:
        # The checkpoint stays put, so the first report back covers the days before the break.
        print(f"Paused until {pause_until}.")
        return 0

    report = Report(since=resolve_since(args.since, now, calendar))

    root = Path(cfg.get("PROJECTS_DIR", "~/Documents")).expanduser()
    repos = find_repos(root, csv(cfg.get("EXCLUDE_REPOS", "")))
    collect_git(report, repos, csv(cfg.get("GIT_EMAILS", "")))
    if cfg.get("GITHUB_ENABLED", "true") == "true":
        collect_github(report)
    if cfg.get("MEMORY_ENABLED", "true") == "true":
        collect_memory(report, root, csv(cfg.get("EXCLUDE_REPOS", "")))
    if cfg.get("JIRA_ENABLED", "true") == "true":
        collect_jira(report, os.path.expanduser(cfg.get("ACLI_BIN", "~/.local/bin/acli")),
                     cfg.get("JIRA_JQL", "assignee = currentUser() AND statusCategory != Done "
                                         "ORDER BY updated DESC"))

    if calendar.error:
        report.errors.append(calendar.error)
    message = render_template(report)
    # With nothing done there is nothing to summarize, and an LLM would fill the gap
    # with plausible but invented work. The plain template says it honestly.
    if cfg.get("LLM_ENABLED", "false") == "true" and not args.no_llm and report.has_activity():
        try:
            message = render_llm(report, cfg)
        except Exception as exc:  # if the LLM fails, the template is sent anyway
            message += f"\n\n⚠️ LLM unavailable: {html.escape(str(exc))}"

    if args.dry_run:
        print(message)
        return 0

    try:
        token, chat_id = telegram_credentials(cfg)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        send_telegram(token, chat_id, message)
    except RuntimeError:
        # Most likely invalid HTML from the LLM. Retry as plain text.
        send_telegram(token, chat_id, message, parse_mode=None)
    if not args.no_checkpoint:
        save_checkpoint(now)  # only move the window after a successful send
    return 0


if __name__ == "__main__":
    sys.exit(main())
