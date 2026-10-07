"""
Cadgentic
Polls Jira for new tickets and hands each one to Claude Code's /ticket command,
in the repo its Jira project maps to.

Setup:
  pip install -r requirements.txt
  cp .env.example .env    (then fill it out; every setting is described there)
  cp repos.example.json repos.json    (then map each Jira project to its repo)

Run:
  python cadgentic.py              (poll for tickets until stopped)
  python cadgentic.py ABC-123      (run one ticket now, then exit)
  python cadgentic.py go-live ABC-123   (run /go-live for one ticket now, then exit)
  python cadgentic.py open ABC-123      (open a run's session in Claude Code)
"""

import asyncio
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import textwrap
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path

import anyio
import httpx
from claude_agent_sdk import (
    AssistantMessage,
    CanUseToolShadowedWarning,
    ClaudeAgentOptions,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    PermissionUpdate,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

REQUIRED = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
if missing := [name for name in REQUIRED if not os.environ.get(name)]:
    sys.exit(f"Missing settings: {', '.join(missing)}. Copy .env.example to .env and fill it out.")


def setting(name: str, default: str) -> str:
    return os.environ.get(name) or default


JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_EXTRA_JQL = setting("JIRA_EXTRA_JQL", "")
TRIGGER_STATUSES = [s.strip() for s in setting("TRIGGER_STATUSES", "Selected for Dev,Backlog").split(",") if s.strip()]
IN_PROGRESS_STATUS = setting("IN_PROGRESS_STATUS", "In Progress")
POLL_INTERVAL = int(setting("POLL_INTERVAL", "600"))
DELAY_MIN = int(setting("DELAY_MIN", "900"))
DELAY_MAX = int(setting("DELAY_MAX", "2700"))
AGENT_PROMPT = setting("AGENT_PROMPT", "/ticket {key}")
GO_LIVE = setting("GO_LIVE", "true").lower() in ("1", "true", "yes")
GO_LIVE_PROMPT = setting("GO_LIVE_PROMPT", "/go-live {key}")
APPROVED_STATUS = setting("APPROVED_STATUS", "Approved")
REVIEW_STATUSES = [s.strip() for s in setting("REVIEW_STATUSES", "Testing,Client Testing").split(",") if s.strip()]
AUTO_APPROVE_PLAN = setting("AUTO_APPROVE_PLAN", "true").lower() in ("1", "true", "yes")
RUN_TIMEOUT = int(setting("RUN_TIMEOUT", "7200"))
MAX_BUDGET_USD = float(setting("MAX_BUDGET_USD", "0")) or None
EXTRA_DISALLOWED_TOOLS = [s.strip() for s in setting("EXTRA_DISALLOWED_TOOLS", "").split(",") if s.strip()]

REPOS_FILE = ROOT / "repos.json"
STATE_FILE = ROOT / "processed_tickets.json"
LOG_DIR = ROOT / "logs"
PLAN_DIR = ROOT / "plans"
DECISION_DIR = ROOT / "decisions"

if not REPOS_FILE.exists():
    sys.exit(f"{REPOS_FILE.name} is missing. Copy repos.example.json to {REPOS_FILE.name} and map each Jira project key to its repo.")
if DELAY_MIN > DELAY_MAX:
    sys.exit("DELAY_MIN can't be greater than DELAY_MAX.")

# /ticket reads the ticket through the Jira tools and its attachments from ~/Downloads.
ALLOWED_TOOLS = [
    "Read(~/Downloads/**)",
    "mcp__atlassian__getJiraIssue",
    "mcp__atlassian__getJiraIssueRemoteIssueLinks",
]
JIRA_WRITE_TOOLS = (
    "mcp__atlassian__addCommentToJiraIssue",
    "mcp__atlassian__editJiraIssue",
    "mcp__atlassian__transitionJiraIssue",
)
# Every other Jira write is off.
DISALLOWED_TOOLS = [
    "mcp__atlassian__addWorklogToJiraIssue",
    "mcp__atlassian__create*",
    "mcp__atlassian__update*",
    *EXTRA_DISALLOWED_TOOLS,
]

# Plan mode sends browser calls to can_use_tool even when settings allow them, so looking at a page is approved here.
BROWSER_READS = {
    "chrome-devtools": {
        "new_page", "navigate_page", "list_pages", "select_page", "close_page", "wait_for",
        "take_snapshot", "take_screenshot", "evaluate_script", "get_css_styles", "hover",
        "list_console_messages", "get_console_message", "list_network_requests", "get_network_request",
        "emulate", "resize_page",
    },
    "claude-in-chrome": {
        "tabs_context_mcp", "tabs_create_mcp", "tabs_close_mcp", "navigate", "resize_window",
        "read_page", "get_page_text", "find", "read_console_messages", "read_network_requests",
    },
}
LOOKING_ACTIONS = {"screenshot", "zoom", "scroll", "scroll_to", "hover", "wait"}

UNATTENDED = (
    "Cadgentic started this session to work Jira ticket {key}, and the run is unattended: "
    "nobody is reading along or will reply. {plan} A question asked with AskUserQuestion is "
    'answered automatically with the option marked "(Recommended)", or the first option if none '
    "is marked. A question asked in prose gets no answer and ends the run."
)
PLAN_APPROVED = "A plan presented with ExitPlanMode is approved automatically."
PLAN_HELD = "A plan presented with ExitPlanMode is saved for review, and the run ends there."
PLAN_HELD_REPLY = (
    "The plan is saved for review, and this session will be resumed to approve it. "
    "Stop here: don't revise the plan or start on it."
)
# /ticket posts the questions a run asked under this heading, and the script checks for it.
RECORD_HEADING = "## Questions answered during planning"
NOBODY_HERE = (
    "This is an unattended run, so nobody is here to approve this. "
    "Carry on without it if you can. Otherwise stop and say what you need."
)

GO_LIVE_UNATTENDED = (
    "Cadgentic started this session to run /go-live for Jira ticket {key}, and the run is unattended: "
    "nobody is reading along or will reply. Nothing is approved or answered automatically in this run. "
    "A hold, a trial-mode pause, or anything else that waits on a person ends the run, and the session "
    "is resumed later."
)
GO_LIVE_WAITS = (
    "This is an unattended go-live run, so nobody can answer or give a go-ahead. "
    "Present the hold or the pause the way the skill says and end the run."
)
# /go-live ends its report with this line.
OUTCOME = re.compile(r"^go-live (\S+): (deployed|ready|held|not-approved|already-live|failed|dry-run)\s*$", re.M)
GO_LIVE_NOTES = {
    "deployed": ("✓", "deployed live"),
    "ready": ("■", "is clear to go live and waiting on your go-ahead"),
    "held": ("■", "go-live is on hold"),
    "not-approved": ("·", "isn't approved to go live yet"),
    "already-live": ("·", "is already live"),
    "dry-run": ("·", "go-live dry run finished"),
    "failed": ("✗", "go-live failed"),
    "unknown": ("✗", "go-live ended without saying how it went"),
}
RECHECKED = {"ready", "held", "not-approved", "unknown"}

log = logging.getLogger("cadgentic")
unroutable: set[str] = set()
comment_reads: dict[str, tuple[str | None, bool]] = {}
me = ""

COLOR = sys.stderr.isatty() and "NO_COLOR" not in os.environ
DIM, GREEN, YELLOW, CYAN = "2", "32", "33", "36"
HIDDEN_TOOLS = {"ToolSearch", "AskUserQuestion"}
TOOL_LABELS = {
    "EnterPlanMode": "Entered plan mode",
    "ExitPlanMode": "Presented the plan",
    "SubagentHandback": "Subagent finished",
}
DETAIL_KEYS = (
    "description", "file_path", "pattern", "skill", "url", "viewport",
    "issueIdOrKey", "jql", "query", "command", "message",
)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def paint(text: str, color: str) -> str:
    return f"\033[{color}m{text}\033[0m" if COLOR and color else text


def show(text: str, color: str = "", nested: bool = False, wrap: bool = False):
    stamp = datetime.now().strftime("%H:%M:%S")
    gutter = "     │ " + ("  ┆ " if nested else "")
    width = max(40, shutil.get_terminal_size((120, 24)).columns - 19 - len(gutter))
    if wrap:
        lines = [line for part in text.splitlines() for line in textwrap.wrap(part, width)]
    else:
        lines = [textwrap.shorten(text, width, placeholder="…")]
    for index, line in enumerate(lines):
        lead = stamp if index == 0 else " " * len(stamp)
        print(" " * 11 + paint(lead + gutter, DIM) + paint(line, color), file=sys.stderr, flush=True)


def is_browser_read(name: str, tool_input: dict) -> bool:
    if name == "mcp__claude-in-chrome__computer":
        return tool_input.get("action") in LOOKING_ACTIONS
    server, _, tool = name.removeprefix("mcp__").partition("__")
    return name.startswith("mcp__") and tool in BROWSER_READS.get(server, ())


def describe_tool(name: str, tool_input: dict, repo: Path) -> str | None:
    if name in HIDDEN_TOOLS:
        return None
    label = TOOL_LABELS.get(name, name)
    if name.startswith("mcp__"):
        server, _, tool = name.removeprefix("mcp__").partition("__")
        label = f"{server} {tool}"
    detail = next((str(tool_input[key]) for key in DETAIL_KEYS if tool_input.get(key)), "")
    detail = detail.replace(f"{repo}/", "").replace(str(Path.home()), "~")
    return f"{label} · {detail}" if detail else label


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

def load_state() -> dict[str, dict]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def record(key: str, **fields):
    state = load_state()
    state[key] = {**state.get(key, {}), **fields}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------

def ticket_jql(key: str | None = None, started: bool = False) -> str:
    names = TRIGGER_STATUSES + ([IN_PROGRESS_STATUS] if started else [])
    statuses = ", ".join(f'"{status}"' for status in names)
    clauses = ["assignee = currentUser()", f"status in ({statuses})"]
    if JIRA_EXTRA_JQL:
        clauses.append(f"({JIRA_EXTRA_JQL})")
    if key:
        clauses.append(f'key = "{key}"')
    return " AND ".join(clauses) + " ORDER BY created ASC"


def go_live_jql(statuses: list[str], recent: bool = False) -> str:
    names = ", ".join(f'"{status}"' for status in statuses)
    clauses = [f"status in ({names})", "(assignee = currentUser() OR assignee was currentUser())"]
    if recent:
        clauses.append("updated >= -14d")
    if JIRA_EXTRA_JQL:
        clauses.append(f"({JIRA_EXTRA_JQL})")
    return " AND ".join(clauses) + " ORDER BY updated ASC"


async def fetch_tickets(client: httpx.AsyncClient, key: str | None = None, started: bool = False) -> list[dict]:
    return await search(client, ticket_jql(key, started))


async def search(client: httpx.AsyncClient, jql: str) -> list[dict]:
    tickets = []
    params = {"jql": jql, "fields": "summary,status,assignee,updated", "maxResults": 100}
    while True:
        resp = await client.get("/rest/api/3/search/jql", params=params)
        resp.raise_for_status()
        page = resp.json()
        tickets += page.get("issues", [])
        if not page.get("nextPageToken"):
            return tickets
        params["nextPageToken"] = page["nextPageToken"]


def adf_parts(node, text: list[str], mentions: set[str]):
    if not isinstance(node, dict):
        return
    if node.get("type") == "text":
        text.append(node.get("text", ""))
    elif node.get("type") == "mention":
        mentions.add(node.get("attrs", {}).get("id", ""))
    for child in node.get("content", []):
        adf_parts(child, text, mentions)


async def approved_by_comment(client: httpx.AsyncClient, ticket: dict) -> bool:
    key, updated = ticket["key"], ticket["fields"].get("updated")
    if key in comment_reads and comment_reads[key][0] == updated:
        return comment_reads[key][1]
    resp = await client.get(f"/rest/api/3/issue/{key}/comment", params={"orderBy": "-created", "maxResults": 5})
    resp.raise_for_status()
    latest = next((c for c in resp.json().get("comments", []) if c.get("author", {}).get("accountId") != me), {})
    text, mentions = [], set()
    adf_parts(latest.get("body"), text, mentions)
    for_me = (ticket["fields"].get("assignee") or {}).get("accountId") == me or me in mentions
    comment_reads[key] = (updated, for_me and "approved" in " ".join(text).lower())
    return comment_reads[key][1]


def go_live_due(previous: dict | None, updated: str | None) -> bool:
    if not previous:
        return True
    return previous.get("status") in RECHECKED and previous.get("seen") != updated


async def approved_tickets(client: httpx.AsyncClient, state: dict) -> list[dict]:
    found = {ticket["key"]: ticket for ticket in await search(client, go_live_jql([APPROVED_STATUS]))}
    for ticket in await search(client, go_live_jql(REVIEW_STATUSES, recent=True)):
        if ticket["key"] not in found and await approved_by_comment(client, ticket):
            found[ticket["key"]] = ticket
    return [
        ticket for ticket in found.values()
        if go_live_due(state.get(ticket["key"], {}).get("go_live"), ticket["fields"].get("updated"))
    ]


async def start_progress(client: httpx.AsyncClient, key: str):
    try:
        resp = await client.get(f"/rest/api/3/issue/{key}/transitions")
        resp.raise_for_status()
        transition = next(
            (t for t in resp.json().get("transitions", []) if t.get("to", {}).get("name") == IN_PROGRESS_STATUS),
            None,
        )
        if not transition:
            log.info("    %s wasn't moved: it has no transition to %s", key, IN_PROGRESS_STATUS)
            return
        resp = await client.post(f"/rest/api/3/issue/{key}/transitions", json={"transition": {"id": transition["id"]}})
        resp.raise_for_status()
        log.info("    Moved %s to %s", key, IN_PROGRESS_STATUS)
    except httpx.HTTPError as e:
        log.warning("    Couldn't move %s to %s: %s", key, IN_PROGRESS_STATUS, e)


def status_rank(ticket: dict) -> int:
    name = ticket.get("fields", {}).get("status", {}).get("name")
    return TRIGGER_STATUSES.index(name) if name in TRIGGER_STATUSES else len(TRIGGER_STATUSES)


# ---------------------------------------------------------------------------
# Repos
# ---------------------------------------------------------------------------

def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def load_repos() -> dict[str, list[Path]]:
    repos = {}
    for project, paths in json.loads(REPOS_FILE.read_text()).items():
        repos[project] = [Path(p).expanduser() for p in ([paths] if isinstance(paths, str) else paths)]
    return repos


def has_work(repo: Path, key: str) -> bool:
    return bool(
        git(repo, "branch", "--all", "--list", f"*feature/{key}")
        or git(repo, "log", "--all", "-n", "1", "--oneline", "-F", f"--grep=[{key}]")
    )


def find_repo(key: str) -> Path:
    project = key.split("-")[0]
    candidates = load_repos().get(project, [])
    if not candidates:
        raise LookupError(f"no repo is mapped to {project} in {REPOS_FILE.name}")
    if stray := [str(repo) for repo in candidates if not (repo / ".git").exists()]:
        raise LookupError(f"{', '.join(stray)} isn't a git repo")
    if len(candidates) > 1:
        candidates = [repo for repo in candidates if has_work(repo, key)]
    if len(candidates) != 1:
        raise LookupError(f"{project} maps to more than one repo and none of them has work for {key} yet")
    return candidates[0]


def uncommitted_changes(repo: Path) -> str:
    return git(repo, "status", "--porcelain", "--untracked-files=no")


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def default_option(question: dict) -> dict:
    options = question["options"]
    return next((o for o in options if "(recommended)" in o["label"].lower()), options[0])


def describe(option: dict) -> str:
    label = re.sub(r"\s*\(recommended\)", "", option["label"], flags=re.I)
    return f"{label}. {option.get('description', '')}".strip()


def write_decisions(key: str, decisions: list[dict]) -> Path:
    lines = [RECORD_HEADING]
    for number, decision in enumerate(decisions, 1):
        lines += ["", f"{number}. {decision['question']}", "", f"   Answered: {describe(decision['answer'])}"]
        if decision["alternatives"]:
            lines += ["", "   Alternatives:", ""]
            lines += [f"   - {describe(option)}" for option in decision["alternatives"]]
    lines += ["", "🤖 Answered by [Claude Code](https://claude.com/claude-code)", "Message-Voice: neutral", ""]
    path = DECISION_DIR / f"{key}.md"
    path.write_text("\n".join(lines))
    return path


def record_on_pull_request(key: str, repo: Path, body: Path, since: datetime) -> str | None:
    def gh(*args: str) -> str:
        return subprocess.run(["gh", *args], cwd=repo, capture_output=True, text=True, check=True).stdout

    try:
        if "github.com" not in git(repo, "remote", "get-url", "origin"):
            return None
        pulls = json.loads(gh("pr", "list", "--head", f"feature/{key}", "--state", "all", "--json", "number", "--limit", "1"))
        if not pulls:
            return None
        number = str(pulls[0]["number"])
        comments = json.loads(gh("pr", "view", number, "--json", "comments"))["comments"]
        if any(
            RECORD_HEADING in comment["body"]
            and datetime.fromisoformat(comment["createdAt"].replace("Z", "+00:00")) >= since
            for comment in comments
        ):
            return "the run posted them on the pull request"
        gh("pr", "comment", number, "--body-file", str(body))
        return "the run didn't post them, so the plain record went on the pull request"
    except (OSError, KeyError, ValueError, subprocess.CalledProcessError):
        return None


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

def notify(message: str):
    if shutil.which("osascript"):
        subprocess.run(
            [
                "osascript",
                "-e", "on run argv",
                "-e", 'display notification (item 1 of argv) with title "Cadgentic"',
                "-e", "end run",
                message,
            ],
            capture_output=True,
        )


def jira_gate(key: str):
    async def gate(hook_input, tool_use_id, context):
        tool_input = hook_input["tool_input"]
        allowed = str(tool_input.get("issueIdOrKey", "")).upper() == key
        if allowed and hook_input["tool_name"].endswith("editJiraIssue"):
            allowed = set(tool_input.get("fields") or {}) == {"assignee"}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if allowed else "deny",
                "permissionDecisionReason": (
                    f"An unattended run may comment on {key}, reassign it, and transition it, "
                    "and nothing else in Jira. Pass the key as issueIdOrKey."
                ),
            }
        }

    return gate


async def run_ticket(
    key: str, repo: Path, session_id: str, decisions: list[dict], live: bool = False
) -> tuple[bool, str]:
    planned = False
    result = None
    last_shown = None

    with (LOG_DIR / f"{key}.log").open("a") as transcript:

        def write(line: str):
            transcript.write(line.rstrip() + "\n")
            transcript.flush()

        async def can_use_tool(tool_name, tool_input, context):
            nonlocal planned
            if live and tool_name in ("AskUserQuestion", "ExitPlanMode"):
                write(f"[denied] {tool_name}")
                show("Refused: go-live waits on a person for that", YELLOW)
                return PermissionResultDeny(message=GO_LIVE_WAITS)
            if tool_name == "AskUserQuestion" and all(q.get("options") for q in tool_input.get("questions", [])):
                answers = {}
                for question in tool_input["questions"]:
                    answer = default_option(question)
                    answers[question["question"]] = answer["label"]
                    decisions.append({
                        "question": question["question"],
                        "answer": answer,
                        "alternatives": [o for o in question["options"] if o is not answer],
                    })
                    write(f"[answered] {question['question']} -> {answer['label']}")
                    show(f"Asked: {question['question']}", CYAN, wrap=True)
                    show(f"Answered: {answer['label']}", CYAN)
                return PermissionResultAllow(updated_input={**tool_input, "answers": answers})
            if tool_name == "ExitPlanMode":
                planned = True
                (PLAN_DIR / f"{key}.md").write_text(tool_input.get("plan", ""))
                if not AUTO_APPROVE_PLAN:
                    return PermissionResultDeny(message=PLAN_HELD_REPLY)
                write("[plan approved automatically]")
                show("Plan approved automatically", GREEN)
                return PermissionResultAllow(
                    updated_input=tool_input,
                    updated_permissions=[
                        PermissionUpdate(type="setMode", mode="auto", destination="session")
                    ],
                )
            if is_browser_read(tool_name, tool_input):
                return PermissionResultAllow(updated_input=tool_input)
            write(f"[denied] {tool_name} {json.dumps(tool_input)[:300]}")
            show(f"Refused: {describe_tool(tool_name, tool_input, repo) or tool_name}", YELLOW)
            return PermissionResultDeny(message=NOBODY_HERE)

        if live:
            briefing = GO_LIVE_UNATTENDED.format(key=key)
        else:
            briefing = UNATTENDED.format(key=key, plan=PLAN_APPROVED if AUTO_APPROVE_PLAN else PLAN_HELD)

        options = ClaudeAgentOptions(
            cwd=repo,
            # The SDK's bundled CLI would re-point Claude in Chrome at itself, so prefer the installed one.
            cli_path=shutil.which("claude"),
            session_id=session_id,
            permission_mode="auto",
            system_prompt={"type": "preset", "preset": "claude_code", "append": briefing},
            can_use_tool=can_use_tool,
            hooks={"PreToolUse": [HookMatcher(matcher="|".join(JIRA_WRITE_TOOLS), hooks=[jira_gate(key)])]},
            allowed_tools=ALLOWED_TOOLS,
            disallowed_tools=DISALLOWED_TOOLS,
            setting_sources=["user", "project", "local"],
            skills="all",
            # /ticket downloads attachments, and qa checks the work, through Claude in Chrome.
            extra_args={"chrome": None},
            max_budget_usd=MAX_BUDGET_USD,
            # A screenshot comes back as one JSON line, and the SDK's 1 MB default can't hold it.
            max_buffer_size=64 * 1024 * 1024,
            # Keep the Jira token out of the agent's shell.
            env={"JIRA_API_TOKEN": ""},
            stderr=write,
        )

        write(f"--- {now()} session {session_id} in {repo}")
        prompt = (GO_LIVE_PROMPT if live else AGENT_PROMPT).format(key=key)
        with anyio.fail_after(RUN_TIMEOUT):
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    nested = message.parent_tool_use_id is not None
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            write(block.text)
                            show(block.text, nested=nested, wrap=True)
                            last_shown = None
                        elif isinstance(block, ToolUseBlock):
                            write(f"[{block.name}] {json.dumps(block.input)[:300]}")
                            summary = describe_tool(block.name, block.input, repo)
                            # A subagent's bare calls (a script, a screenshot) would bury the pages it visits.
                            if summary and summary != last_shown and (" · " in summary or not nested):
                                show(summary, DIM, nested)
                                last_shown = summary
                elif isinstance(message, ResultMessage):
                    result = message

        if result is None:
            raise RuntimeError("the run ended without a result")
        write(f"--- {now()} {result.subtype}, {result.num_turns} turns, ${result.total_cost_usd or 0:.2f}")
        show(f"Ended: {result.subtype}, {result.num_turns} turns, ${result.total_cost_usd or 0:.2f}", DIM)
        if result.is_error:
            raise RuntimeError(f"the run ended with {result.subtype}")

    return planned, result.result or ""


async def handoff_status(client: httpx.AsyncClient, key: str) -> str:
    try:
        return "finished" if await fetch_tickets(client, key, started=True) else "handed_off"
    except httpx.HTTPError:
        return "finished"


async def process(client: httpx.AsyncClient, key: str, repo: Path):
    session_id = str(uuid.uuid4())
    decisions: list[dict] = []
    started = datetime.now(timezone.utc)
    record(key, status="running", repo=str(repo), session_id=session_id, started_at=now())
    await start_progress(client, key)

    try:
        planned, said = await run_ticket(key, repo, session_id, decisions)
        detail = " ".join(said.split())[:300]
        status = "planned" if planned and not AUTO_APPROVE_PLAN else await handoff_status(client, key)
    except TimeoutError:
        status, detail = "failed", f"timed out after {RUN_TIMEOUT}s"
    except Exception as e:
        status, detail = "failed", str(e)
    record(key, status=status, detail=detail, finished_at=now())

    if decisions:
        answers = write_decisions(key, decisions)
        posted = record_on_pull_request(key, repo, answers, started)
        log.info("    %d question(s) answered: %s", len(decisions), posted or answers)

    resume = f"cd {repo} && claude --resume {session_id}"
    if status == "handed_off":
        log.info("  ✓ %s handed off for review", key)
        log.info("    To review the session: %s", resume)
        notify(f"{key} handed off for review")
    elif status == "planned":
        log.info("  ✓ %s planned: %s", key, PLAN_DIR / f"{key}.md")
        log.info("    To approve it: %s", resume)
        notify(f"{key}: plan ready for review")
    elif status == "finished":
        log.warning("  ■ %s stopped before the handoff. Its report is the last message above.", key)
        log.warning("    To pick it up: %s", resume)
        notify(f"{key} stopped before the handoff")
    else:
        log.error("  ✗ %s failed: %s", key, detail)
        log.error("    To pick it up: %s", resume)
        notify(f"{key} needs attention")


async def go_live(key: str, repo: Path, seen: str | None):
    session_id = str(uuid.uuid4())
    entry = {"status": "running", "seen": seen, "repo": str(repo), "session_id": session_id, "started_at": now()}
    record(key, go_live=entry)

    try:
        _, said = await run_ticket(key, repo, session_id, [], live=True)
        outcomes = [match.group(2) for match in OUTCOME.finditer(said) if match.group(1).upper() == key]
        status, detail = (outcomes[-1] if outcomes else "unknown"), ""
    except TimeoutError:
        status, detail = "failed", f"timed out after {RUN_TIMEOUT}s"
    except Exception as e:
        status, detail = "failed", str(e)
    record(key, go_live={**entry, "status": status, "detail": detail, "finished_at": now()})

    mark, words = GO_LIVE_NOTES[status]
    level = {"✗": logging.ERROR, "■": logging.WARNING}.get(mark, logging.INFO)
    log.log(level, "  %s %s %s%s", mark, key, words, f": {detail}" if detail else "")
    aim = "review the session" if level == logging.INFO else "pick it up"
    log.log(level, "    To %s: cd %s && claude --resume %s", aim, repo, session_id)
    if mark != "·":
        notify(f"{key} {words}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def route(key: str) -> Path | None:
    try:
        repo = find_repo(key)
    except LookupError as e:
        if key not in unroutable:
            unroutable.add(key)
            log.warning("  ✗ %s skipped: %s", key, e)
            notify(f"{key} skipped: {e}")
        return None
    unroutable.discard(key)
    return repo


async def wait_turn(key: str, summary: str, repo: Path):
    delay = random.randint(DELAY_MIN, DELAY_MAX)
    log.info("  → %s: %s (%s)", key, summary, str(repo).replace(str(Path.home()), "~"))
    log.info("    Waiting %dm before starting...", delay // 60)
    await asyncio.sleep(delay)


async def poll(client: httpx.AsyncClient):
    state = load_state()
    approved = [
        (ticket, repo)
        for ticket in (await approved_tickets(client, state) if GO_LIVE else [])
        if (repo := route(ticket["key"]))
    ]
    # A handed-off ticket had left the queue, so it's only found here once someone sends it back.
    ready = [
        (ticket, repo)
        for ticket in sorted(await fetch_tickets(client), key=status_rank)
        if state.get(ticket["key"], {}).get("status") in (None, "handed_off") and (repo := route(ticket["key"]))
    ]
    if not approved and not ready:
        log.info("No new tickets")
        return

    if approved:
        log.info("Found %d ticket(s) approved to go live", len(approved))
    for ticket, repo in approved:
        key = ticket["key"]
        await wait_turn(key, f"go live, {ticket['fields'].get('summary', '')}", repo)
        if uncommitted_changes(repo):
            log.warning("    %s has uncommitted changes, leaving %s for the next poll", repo, key)
            continue
        await go_live(key, repo, ticket["fields"].get("updated"))

    sent_back = {ticket["key"] for ticket, _ in ready if state.get(ticket["key"], {}).get("status")}
    if fresh := len(ready) - len(sent_back):
        log.info("Found %d new ticket(s)", fresh)
    if sent_back:
        log.info("Found %d ticket(s) sent back for more work", len(sent_back))
    for ticket, repo in ready:
        key = ticket["key"]
        summary = ticket.get("fields", {}).get("summary", "")
        await wait_turn(key, f"sent back, {summary}" if key in sent_back else summary, repo)

        if not await fetch_tickets(client, key):
            log.info("    %s is no longer waiting on you, skipping", key)
            continue
        if AUTO_APPROVE_PLAN and uncommitted_changes(repo):
            log.warning("    %s has uncommitted changes, leaving %s for the next poll", repo, key)
            continue

        await process(client, key, repo)


async def run_named(client: httpx.AsyncClient, keys: list[str], live: bool):
    for key in keys:
        try:
            repo = find_repo(key)
        except LookupError as e:
            log.error("  ✗ %s skipped: %s", key, e)
            continue
        if (live or AUTO_APPROVE_PLAN) and uncommitted_changes(repo):
            log.error("  ✗ %s skipped: %s has uncommitted changes", key, repo)
            continue
        log.info("  → %s (%s)", key, str(repo).replace(str(Path.home()), "~"))
        if live:
            await go_live(key, repo, None)
        else:
            await process(client, key, repo)


def open_session(args: list[str]):
    live = args[:1] == ["go-live"]
    keys = args[1 if live else 0:]
    if len(keys) != 1:
        sys.exit("open needs one ticket key: python cadgentic.py open ABC-123")
    key = keys[0].upper()
    what = "go-live session" if live else "session"
    entry = load_state().get(key, {})
    if live:
        entry = entry.get("go_live", {})
    if not (session_id := entry.get("session_id")):
        sys.exit(f"There's no {what} on record for {key}.")
    # An interrupted run stays "running" in the state file, so look for a live process instead.
    if subprocess.run(["pgrep", "-f", session_id], capture_output=True).returncode == 0:
        sys.exit(f"The {what} for {key} is in use, by a run that's still going or in another terminal.")
    os.chdir(entry["repo"])
    os.execvp("claude", ["claude", "--resume", session_id])


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for noisy in ("httpx", "claude_agent_sdk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)
    for folder in (LOG_DIR, PLAN_DIR, DECISION_DIR):
        folder.mkdir(exist_ok=True)

    global me
    live = sys.argv[1:2] == ["go-live"]
    named = [arg.upper() for arg in sys.argv[2 if live else 1:]]
    if live and not named:
        sys.exit("go-live needs a ticket key: python cadgentic.py go-live ABC-123")
    log.info("Cadgentic started")
    log.info("  Jira: %s", JIRA_BASE_URL)
    log.info("  Repos: %d Jira projects mapped in %s", len(load_repos()), REPOS_FILE.name)
    if named:
        log.info("  Running %s%s once", "/go-live for " if live else "", ", ".join(named))
    else:
        log.info("  Polling every %ss for %s", POLL_INTERVAL, " or ".join(f"'{s}'" for s in TRIGGER_STATUSES))
        if GO_LIVE:
            log.info(
                "  Going live from '%s', or an approving comment in %s",
                APPROVED_STATUS, " or ".join(f"'{s}'" for s in REVIEW_STATUSES),
            )
    log.info("  Plans are %s", "approved automatically" if AUTO_APPROVE_PLAN else "held for review")
    log.info("  Extra tools turned off: %s", ", ".join(EXTRA_DISALLOWED_TOOLS) or "none")

    async with httpx.AsyncClient(
        base_url=JIRA_BASE_URL,
        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
        headers={"Accept": "application/json"},
        timeout=30,
    ) as client:
        if named:
            await run_named(client, named, live)
            return

        if GO_LIVE:
            whoami = await client.get("/rest/api/3/myself")
            if whoami.status_code in (401, 403):
                sys.exit("Jira rejected the request. Check JIRA_EMAIL and JIRA_API_TOKEN.")
            whoami.raise_for_status()
            me = whoami.json()["accountId"]

        for key, entry in load_state().items():
            if entry.get("status") == "running":
                record(key, status="failed", detail="interrupted by a restart")
                log.warning("  %s was interrupted by a restart and is marked failed", key)
            if entry.get("go_live", {}).get("status") == "running":
                record(key, go_live={**entry["go_live"], "status": "failed", "detail": "interrupted by a restart"})
                log.warning("  %s's go-live was interrupted by a restart and is marked failed", key)

        while True:
            try:
                await poll(client)
            except httpx.HTTPStatusError as e:
                log.error("Jira returned %s: %s", e.response.status_code, e.response.text[:300])
                if e.response.status_code in (401, 403):
                    sys.exit("Jira rejected the request. Check JIRA_EMAIL and JIRA_API_TOKEN.")
            except httpx.HTTPError as e:
                log.error("Jira request failed: %s", e)
            except Exception:
                log.exception("Unexpected error")

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    if sys.argv[1:2] == ["open"]:
        open_session(sys.argv[2:])
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass
