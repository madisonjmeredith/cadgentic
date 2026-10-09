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
python cadgentic.py resume ABC-123     # carry on a finished or failed run in its own session, unattended
python cadgentic.py open ABC-123       # open a run's session in Claude Code (open go-live ABC-123 for the go-live one)
```

There is no test suite, linter, or build step. `.venv/bin/python -m py_compile cadgentic.py` checks syntax without running anything. Importing the module does run it: config is read at import time, and the script exits if `.env` settings or `repos.json` are missing.

## Never try a change against real Jira

The first four commands above have real effects. A run moves the ticket in Jira, pushes branches to a client repo, deploys a review theme, and posts on client-visible tickets. A go-live run can deploy to production. `open` starts no run, but it launches an interactive `claude` on a real client session. Don't run any of them to check a change. Build a throwaway harness in the scratchpad instead:

- **A copy of the script.** Everything it reads and writes (`.env`, `repos.json`, `processed_tickets.json`, `logs/`, `plans/`, `decisions/`) is resolved next to the script, so copy `cadgentic.py` into a scratch directory with its own `repos.json`.
- **A fake Jira.** Shell variables win over `.env`, so set `JIRA_BASE_URL` to a local `http.server` along with a dummy `JIRA_EMAIL` and `JIRA_API_TOKEN`. It needs `/rest/api/3/search/jql`, plus `/rest/api/3/myself` and `/rest/api/3/issue/<KEY>/comment` when polling with `GO_LIVE` on, which is the default. The stand-in skill can't write to it, so the fake plays the handoff itself: add `/rest/api/3/issue/<KEY>/transitions`, and on a POST there take the ticket out of the trigger search. Put it back to test a ticket that's sent back.
- **A stand-in skill.** Use a throwaway git repo with a project skill that asks one `AskUserQuestion`, enters plan mode, and presents a plan. Swap it in with `AGENT_PROMPT="/demo {key}"` or `GO_LIVE_PROMPT="/demogl {key}"`. Never point a test at the real `/ticket` or `/go-live`.
- **The same tool limits.** A scratch copy has no `.env`, so set `EXTRA_DISALLOWED_TOOLS` in the shell to the value in the real one. Without it a test run can reach MCP servers that real runs are kept away from.
- **A fake `gh`** first on `PATH`, to capture the pull request comment.
- **A fake `ssh`** first on `PATH` when the stand-in follows a project's own deploy steps. Give the fixture a copy of the project's deploy script with its host changed to one that can't resolve, so a run that misses the fake reaches nothing.
- **A cheap model**, e.g., `ANTHROPIC_MODEL=haiku`.
- **For `open` only, a fake `claude`** first on `PATH` and a made-up `processed_tickets.json` next to the scratch copy. Leave the fake off `PATH` for run tests, which find the CLI with `shutil.which("claude")`.

To test a project's environment commands, give the scratch `repos.json` an object entry whose `check`, `up`, and `down` only touch marker files (`test -f ../env.up`, `touch ../env.up`, `rm -f ../env.up`, each appending its name to a log). Stub `run_ticket`, `start_progress`, `handoff_status`, and `notify`, then call `process_in_environment()`. Cover: down at the start, already up, `up` failing, `check` never passing (set `ENV_TIMEOUT` low), and a session that raises or is cancelled. Never point a test at a real project's commands.

To test `resume`, have the stand-in check a condition after its plan (a file that must exist) and stop when it fails, so the first run ends `finished`. Make the condition true and resume. The run should finish the stand-in's remaining steps with what it chose before it stopped, under the same session ID. To check which answers reach the pull request without an agent, replace `run_ticket` with a stub and call `process()` on a seeded state entry.

To test a wait on a background command, have the stand-in start `sleep 20` with `run_in_background`, end its turn, and after the wake ask a second `AskUserQuestion` and call `Read` once. Add `"Read"` to `JIRA_WRITE_TOOLS` in the scratch copy so `jira_gate()` answers for it. The log should have a second `[answered]` line and the agent should report the gate's own refusal. Cover two more stand-ins with `BACKGROUND_TIMEOUT` set low. One leaves `sleep 600` running and stops: the run should end at the limit with a `[gave up]` line and no `sleep` left. The other waits on a background subagent for longer than the limit while `sleep 600` runs, and must not be cut off.

A go-live test must show the stand-in's question being refused. If it comes back answered, go-live runs are auto-answering questions, which must never happen.

Test plans land in `~/.claude/plans/` under random names. Delete only the ones the test created.

## Architecture

### Two lanes through one runner

`run_ticket()` starts a session and streams it to the terminal and `logs/<KEY>.log`. Both lanes call it:

- **Ticket lane:** `process()` moves the ticket to In Progress over REST, runs `AGENT_PROMPT`, then asks Jira where the ticket ended up (`handoff_status()`). The agent doesn't report its own outcome. Still assigned and still in progress means it stopped early (`finished`). Anything else is `handed_off`.
- **Go-live lane:** `go_live()` runs `GO_LIVE_PROMPT` with `live=True` and reads the outcome from the last `go-live <KEY>: <outcome>` line of the agent's final message.

`run_ticket()` hands `query()` its prompt as a stream (`prompts()`) and holds it open, because the hook and `can_use_tool()` are answered over the CLI's stdin. Given a string, the SDK closes stdin at the first idle with no subagent running. A session that ended a turn waiting on a background command (`gh run watch`, a `Monitor`) then wakes with both dead: a Jira write comes back as "The user doesn't want to take this action right now" and a question as `Stream closed`. The stream is released when the CLI reports `idle` with no `local_bash` task in flight, and the SDK closes stdin from there. With one in flight the run waits `BACKGROUND_TIMEOUT` for a wake, then the cancel scope ends the query and the last result stands. Track only `local_bash` tasks, which covers background `Bash` and `Monitor`. Don't add agent tasks: the CLI stays `running` while a subagent is live, and a subagent can run longer than the timeout. The idle reports come from `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS` in the session's `env`. Without them a result counts as the end of a turn.

Every ticket-lane entry point (`poll()`, `run_named()`, `resume_session()`) reaches `process()` through `process_in_environment()`. A `repos.json` entry can be an object with `repo` plus `check`, `up`, and `down` shell commands, run from the repo's directory. When there's an `up` and `check` doesn't pass, it runs `up`, waits for `check`, calls `process()`, and runs `down` in a `finally`. An environment that was already up is left running: someone else started it. If it won't come up within `ENV_TIMEOUT`, it runs `down` and returns before `process()` records anything or moves the ticket, so a poll finds the ticket unchanged and tries again. `stranded` keeps that to one notification per ticket. Keep this ahead of the session and out of the skills: MCP servers that run inside the environment only connect when it's up at session start, and a skill can't guarantee the teardown. `go_live()` doesn't start an environment.

`poll()` works go-live tickets first, then new and sent-back tickets, one session at a time. Each ticket's random delay and its whole run are awaited inside the poll, so one poll can last hours, and `POLL_INTERVAL` counts from when it ends. `run_named()` (a key on the command line) skips the search, the delay, the status check, and the restart cleanup.

`resume_session()` (the `resume` command) calls `process()` with the ticket's saved state entry. `run_ticket()` then passes the saved ID to the SDK as `resume` in place of `session_id`, and sends `RESUME_PROMPT` plus any note from the command line in place of `AGENT_PROMPT`. The layers below are set up the same way. It runs only when the entry's status is in `RESUMABLE`, no process holds the session, and the repo has no uncommitted changes. Don't add `running` to `RESUMABLE`: a run sets that status before its `claude` process exists.

`open_session()` (the `open` command) is handled in the `__main__` block before `main()`, so it never touches Jira. It reads the repo and session ID from the ticket's state entry, or from its `go_live` object for `open go-live`, and execs `claude --resume` in that repo. It refuses when `pgrep -f` finds a process with the session ID in its arguments. It doesn't go by `status`, which stays `running` after an interrupted run until the next polling start.

### Who decides what a run may do

Four layers, set up in `run_ticket()`:

1. `DISALLOWED_TOOLS` removes every Jira write a run has no use for, plus whatever `EXTRA_DISALLOWED_TOOLS` names in `.env`. Madison's `.env` uses it to remove a client's Jira server, so never drop or empty that setting.
2. `ALLOWED_TOOLS` pre-approves the Jira reads and `~/Downloads/**`, where `/ticket` puts attachments. It also pre-approves the four `bitbucket` MCP tools the skills use on a Bitbucket repo: looking up the repo, listing its pull requests, creating one, and commenting on one. On GitHub the same work goes through `gh` in the shell, so it needs no entry here.
3. `jira_gate()` is a `PreToolUse` hook on the three `JIRA_WRITE_TOOLS`. It allows a write only on the run's own ticket, and `editJiraIssue` only when `assignee` is the one field. It both grants and refuses, so these writes never reach layer 4.
4. `can_use_tool()` gets whatever auto mode would still put to a person. It answers `AskUserQuestion` with the option marked "(Recommended)" or the first one, saves the plan from `ExitPlanMode` and approves it, approves browser calls that only look at a page (`BROWSER_READS`, `LOOKING_ACTIONS`), and refuses everything else. With `AUTO_APPROVE_PLAN=false` the plan is refused with a note to stop, and the run is recorded as `planned`.

With `live=True`, layer 4 refuses `AskUserQuestion` and `ExitPlanMode` before anything else. Nothing is answered or approved for a go-live run.

Auto mode's classifier runs ahead of layer 4 and refuses on its own. A refusal goes back to the agent as the tool result and never reaches `can_use_tool()`, so it leaves no `[denied]` line in the log. It refuses a push to `main` on a live-theme store as a production deploy. `/ticket` makes one such push when it starts a branch: a `[skip ci]` commit holding the theme's latest changes, which deploys nothing. That push gets through only because of the "Theme Sync Push" rule in `autoMode.allow` in `~/.claude/settings.json`. Without the rule, runs on live-theme stores stop at "Start the branch." It doesn't refuse the same things off Shopify. On a fixture shaped like a project that's reviewed from `main`, it let a run merge into `main`, push it, and run a deploy script that works over SSH, so those runs need no rule.

Sessions load Madison's real user settings, skills, hooks, and MCP servers (`setting_sources`, `skills="all"`), so a change under `~/.claude` changes what a run does. The agent reaches Jira through the `atlassian` MCP server. The script's own REST calls use the API token, which is blanked in the agent's environment.

The agent reaches Bitbucket through a second server, `bitbucket`, whose sign-in also covers Jira and Confluence. The layers above only watch the `atlassian` prefix, so the limits on `bitbucket` live in `~/.claude`: `hooks/bitbucket-server-scope.sh` refuses every tool on it that isn't a Bitbucket one, `hooks/enforce-pr-skill-bitbucket.sh` refuses a pull request the `pull-request` skill didn't open, and `settings.json` denies merging and approving. A run's Jira limits depend on that first hook, so don't remove it without adding the same limit here.

### The contract with the skills

The skills a run drives live outside this repo, in `~/.claude/skills/` (`ticket`, `ready-for-review`, `qa`, `go-live`), with `qa`'s checker in `~/.claude/agents/qa-checker.md`. These strings and conventions are shared, so change both sides together:

| In `cadgentic.py` | On the skill side |
|---|---|
| `UNATTENDED`, `GO_LIVE_UNATTENDED` (appended to the system prompt) | A skill treats a run as unattended only when the system prompt says Cadgentic started it. |
| `RECORD_HEADING` | `ticket` posts the answered questions on the pull request under this exact heading, on GitHub or Bitbucket. `record_on_pull_request()` posts `decisions/<KEY>.md` when no comment posted since the run started has it. It works through `gh`, so on Bitbucket it does nothing and the skill's comment is the only one. |
| `poll()` re-running a `handed_off` ticket with the same `AGENT_PROMPT` | `ticket` switches to its follow-up flow when the ticket's branch or `[<KEY>]` commits exist. In an unattended run it stops when the latest comment is Madison's own. |
| `RESUME_PROMPT` | Nothing. A resumed session isn't handed `/ticket` again, so it works from the skill text already in its context. |
| `OUTCOME` | `go-live` ends its report with `go-live <KEY>: <outcome>`. No match is recorded as `unknown`. |
| `default_option()` | Options are marked "(Recommended)" in their label. |
| `has_work()`, `record_on_pull_request()` | Branches are `feature/<KEY>` and commit subjects carry `[<KEY>]`. |
| `process_in_environment()` and a project's `check`, `up`, `down` in `repos.json` | On a Lightning repo, or any other project in a Docker stack, `ticket` expects the stack to be up when an unattended run starts. It leaves the containers alone, and stops the run if the local site doesn't answer. |
| `GO_LIVE_EXTRA_JQL` | `go-live` covers standard Shopify stores only. The setting keeps other projects' approved tickets out of the go-live search. |

A project that's neither a Shopify theme nor a Lightning build has no flow of its own in `ticket`. It deploys by the "Deploying for review" section in the project's `CLAUDE.md` (the skill's project flow), and the script has no part in it. That section can make `main` the review branch and name a command that deploys over SSH. A run on such a project merges into `main`, pushes it, and reaches a server from the agent's shell, with whatever SSH agent the script was started with. `go-live` doesn't cover these projects either, so keep them in `GO_LIVE_EXTRA_JQL`.

### State

`processed_tickets.json` has one entry per ticket. Its top-level fields belong to the ticket lane and its `go_live` object to the go-live lane. `record()` merges one level deep, so `go_live()` writes the whole `go_live` object each time.

A resumed run keeps the entry's `session_id` and `started_at`. `write_decisions()` uses `started_at` to tell whether `decisions/<KEY>.md` belongs to the session: a file written since then holds answers from before the run stopped, and new answers are appended to it. The append strips `RECORD_FOOTER` and counts the numbered lines, so a change to the footer or the numbering breaks it for records already on disk. With nothing new asked, `record_on_pull_request()` accepts a record comment from any point in the session. With a new answer, it wants one posted since the resume.

The lanes re-run on different rules. A poll works a ticket again only when its top-level `status` is `handed_off` and the search finds it. `handoff_status()` records `handed_off` only when the ticket no longer matched the search, so a later match means someone sent it back. Any other status is skipped. Don't widen this to `finished` or `failed` without another guard: a ticket that couldn't be moved to In Progress never leaves the search and would be re-run on every poll. A go-live is repeated when the last outcome is in `RECHECKED` and the ticket's Jira `updated` stamp differs from the saved `seen` (`go_live_due()`).

### Claude in Chrome

Keep `cli_path=shutil.which("claude")`. A `claude` started with Chrome enabled rewrites `~/.claude/chrome/chrome-native-host` to point at its own binary. After testing with any other binary (the SDK's bundled one, a scratch venv), check that file still points at the installed `claude` and stop any leftover `--chrome-native-host` process.
