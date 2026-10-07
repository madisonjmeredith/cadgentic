# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Cadgentic is one script, `cadgentic.py`. It polls Jira for tickets assigned to Madison and runs Claude Code's `/ticket` skill on each one, unattended, in the client repo its Jira project maps to (`repos.json`). Once a ticket is approved it runs `/go-live`. Runs go through the Claude Agent SDK (`claude_agent_sdk.query`).

`README.md` is the reference for how it behaves, and `.env.example` describes every setting. Keep both current when behavior or a setting changes.

Keep client data out of committed files. Examples use placeholder keys (`ABC-123`) and paths. Anything specific to Madison's clients or machine goes in `.env` or `repos.json`, which are both ignored. `repos.example.json` is the committed template.

## Commands

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python cadgentic.py                    # poll until stopped
python cadgentic.py ABC-123            # run /ticket for one ticket now, then exit
python cadgentic.py go-live ABC-123    # run /go-live for one ticket now, then exit
python cadgentic.py open ABC-123       # open a run's session in Claude Code (open go-live ABC-123 for the go-live one)
```

There is no test suite, linter, or build step. `.venv/bin/python -m py_compile cadgentic.py` checks syntax without running anything. Importing the module does run it: config is read at import time, and the script exits if `.env` settings or `repos.json` are missing.

## Never try a change against real Jira

The first three commands above have real effects. A run moves the ticket in Jira, pushes branches to a client repo, deploys a review theme, and posts on client-visible tickets. A go-live run can deploy to production. `open` starts no run, but it launches an interactive `claude` on a real client session. Don't run any of them to check a change. Build a throwaway harness in the scratchpad instead:

- **A copy of the script.** Everything it reads and writes (`.env`, `repos.json`, `processed_tickets.json`, `logs/`, `plans/`, `decisions/`) is resolved next to the script, so copy `cadgentic.py` into a scratch directory with its own `repos.json`.
- **A fake Jira.** Shell variables win over `.env`, so set `JIRA_BASE_URL` to a local `http.server` along with a dummy `JIRA_EMAIL` and `JIRA_API_TOKEN`. It needs `/rest/api/3/search/jql`, plus `/rest/api/3/myself` and `/rest/api/3/issue/<KEY>/comment` when polling with `GO_LIVE` on, which is the default. The stand-in skill can't write to it, so the fake plays the handoff itself: add `/rest/api/3/issue/<KEY>/transitions`, and on a POST there take the ticket out of the trigger search. Put it back to test a ticket that's sent back.
- **A stand-in skill.** Use a throwaway git repo with a project skill that asks one `AskUserQuestion`, enters plan mode, and presents a plan. Swap it in with `AGENT_PROMPT="/demo {key}"` or `GO_LIVE_PROMPT="/demogl {key}"`. Never point a test at the real `/ticket` or `/go-live`.
- **The same tool limits.** A scratch copy has no `.env`, so set `EXTRA_DISALLOWED_TOOLS` in the shell to the value in the real one. Without it a test run can reach MCP servers that real runs are kept away from.
- **A fake `gh`** first on `PATH`, to capture the pull request comment.
- **A cheap model**, e.g., `ANTHROPIC_MODEL=haiku`.
- **For `open` only, a fake `claude`** first on `PATH` and a made-up `processed_tickets.json` next to the scratch copy. Leave the fake off `PATH` for run tests, which find the CLI with `shutil.which("claude")`.

A go-live test must show the stand-in's question being refused. If it comes back answered, go-live runs are auto-answering questions, which must never happen.

Test plans land in `~/.claude/plans/` under random names. Delete only the ones the test created.

## Architecture

### Two lanes through one runner

`run_ticket()` starts a session and streams it to the terminal and `logs/<KEY>.log`. Both lanes call it:

- **Ticket lane:** `process()` moves the ticket to In Progress over REST, runs `AGENT_PROMPT`, then asks Jira where the ticket ended up (`handoff_status()`). The agent doesn't report its own outcome. Still assigned and still in progress means it stopped early (`finished`). Anything else is `handed_off`.
- **Go-live lane:** `go_live()` runs `GO_LIVE_PROMPT` with `live=True` and reads the outcome from the last `go-live <KEY>: <outcome>` line of the agent's final message.

`poll()` works go-live tickets first, then new and sent-back tickets, one session at a time. Each ticket's random delay and its whole run are awaited inside the poll, so one poll can last hours, and `POLL_INTERVAL` counts from when it ends. `run_named()` (a key on the command line) skips the search, the delay, the status check, and the restart cleanup.

`open_session()` (the `open` command) is handled in the `__main__` block before `main()`, so it never touches Jira. It reads the repo and session ID from the ticket's state entry, or from its `go_live` object for `open go-live`, and execs `claude --resume` in that repo. It refuses when `pgrep -f` finds a process with the session ID in its arguments. It doesn't go by `status`, which stays `running` after an interrupted run until the next polling start.

### Who decides what a run may do

Four layers, set up in `run_ticket()`:

1. `DISALLOWED_TOOLS` removes every Jira write a run has no use for, plus whatever `EXTRA_DISALLOWED_TOOLS` names in `.env`. Madison's `.env` uses it to remove a client's Jira server, so never drop or empty that setting.
2. `ALLOWED_TOOLS` pre-approves the Jira reads and `~/Downloads/**`, where `/ticket` puts attachments.
3. `jira_gate()` is a `PreToolUse` hook on the three `JIRA_WRITE_TOOLS`. It allows a write only on the run's own ticket, and `editJiraIssue` only when `assignee` is the one field. It both grants and refuses, so these writes never reach layer 4.
4. `can_use_tool()` gets whatever auto mode would still put to a person. It answers `AskUserQuestion` with the option marked "(Recommended)" or the first one, saves the plan from `ExitPlanMode` and approves it, approves browser calls that only look at a page (`BROWSER_READS`, `LOOKING_ACTIONS`), and refuses everything else. With `AUTO_APPROVE_PLAN=false` the plan is refused with a note to stop, and the run is recorded as `planned`.

With `live=True`, layer 4 refuses `AskUserQuestion` and `ExitPlanMode` before anything else. Nothing is answered or approved for a go-live run.

Sessions load Madison's real user settings, skills, hooks, and MCP servers (`setting_sources`, `skills="all"`), so a change under `~/.claude` changes what a run does. The agent reaches Jira through the `atlassian` MCP server. The script's own REST calls use the API token, which is blanked in the agent's environment.

### The contract with the skills

The skills a run drives live outside this repo, in `~/.claude/skills/` (`ticket`, `ready-for-review`, `qa`, `go-live`). These strings and conventions are shared, so change both sides together:

| In `cadgentic.py` | On the skill side |
|---|---|
| `UNATTENDED`, `GO_LIVE_UNATTENDED` (appended to the system prompt) | A skill treats a run as unattended only when the system prompt says Cadgentic started it. |
| `RECORD_HEADING` | `ticket` posts the answered questions on the pull request under this exact heading. `record_on_pull_request()` posts `decisions/<KEY>.md` when no comment posted since the run started has it. |
| `poll()` re-running a `handed_off` ticket with the same `AGENT_PROMPT` | `ticket` switches to its follow-up flow when the ticket's branch or `[<KEY>]` commits exist. In an unattended run it stops when the latest comment is Madison's own. |
| `OUTCOME` | `go-live` ends its report with `go-live <KEY>: <outcome>`. No match is recorded as `unknown`. |
| `default_option()` | Options are marked "(Recommended)" in their label. |
| `has_work()`, `record_on_pull_request()` | Branches are `feature/<KEY>` and commit subjects carry `[<KEY>]`. |

### State

`processed_tickets.json` has one entry per ticket. Its top-level fields belong to the ticket lane and its `go_live` object to the go-live lane. `record()` merges one level deep, so `go_live()` writes the whole `go_live` object each time.

The lanes re-run on different rules. A poll works a ticket again only when its top-level `status` is `handed_off` and the search finds it. `handoff_status()` records `handed_off` only when the ticket no longer matched the search, so a later match means someone sent it back. Any other status is skipped. Don't widen this to `finished` or `failed` without another guard: a ticket that couldn't be moved to In Progress never leaves the search and would be re-run on every poll. A go-live is repeated when the last outcome is in `RECHECKED` and the ticket's Jira `updated` stamp differs from the saved `seen` (`go_live_due()`).

### Claude in Chrome

Keep `cli_path=shutil.which("claude")`. A `claude` started with Chrome enabled rewrites `~/.claude/chrome/chrome-native-host` to point at its own binary. After testing with any other binary (the SDK's bundled one, a scratch venv), check that file still points at the installed `claude` and stop any leftover `--chrome-native-host` process.
