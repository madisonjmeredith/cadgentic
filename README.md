# Cadgentic

Cadgentic polls Jira for tickets assigned to you and hands each one to Claude Code's `/ticket` command in the repo its Jira project maps to. A run plans the ticket, builds it, deploys it for review, checks it, and posts the handoff comment with nobody at the keyboard. Once a ticket is approved, Cadgentic hands it to `/go-live`.

## Requirements

- Python 3.10 or later
- Claude Code, installed and logged in
- The `/ticket` and `/go-live` skills, the skills they call, and the `atlassian` MCP server they work with tickets through
- Chrome with the Claude extension, signed in to Jira
- The GitHub CLI, logged in
- A [Jira API token](https://id.atlassian.com/manage-profile/security/api-tokens)

Runs go through the Claude Agent SDK. It drives the `claude` on your path, or its own bundled copy when there isn't one. Either way it uses your existing login and loads the same settings, skills, hooks, and MCP servers as an interactive session. Chrome is how `/ticket` downloads a ticket's attachments and how `qa` checks the deployed work.

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp repos.example.json repos.json
```

Fill out `.env`. Every setting is described there.

Then fill out `repos.json`. It maps each Jira project key to the repo that project's tickets are worked in:

```json
{
  "ABC": "~/code/abc-store",
  "XYZ": ["~/code/xyz-b2c", "~/code/xyz-b2b"]
}
```

A path is the git repo itself, which isn't always the project root. A project with more than one repo lists them all (see [Known issues](#known-issues)). A ticket whose project isn't in the file is skipped with a notification. It's picked up on a later poll once the project is added.

Both files are in `.gitignore` so your Jira token and repo paths stay out of the repo.

## Usage

```sh
python cadgentic.py
```

It polls until you stop it with Ctrl-C. Each poll logs what it found. During a run the terminal shows what the agent says and one short line per tool call. `logs/<KEY>.log` has the same feed with the first 300 characters of each call's input. A macOS notification goes out when a run ends or a ticket is skipped.

To run one ticket right away, pass its key:

```sh
python cadgentic.py ABC-123
```

That skips the search and the wait. It runs the ticket whatever its status is and whether or not it has run before, then exits. Pass several keys to run them one after another.

The same works for a deploy:

```sh
python cadgentic.py go-live ABC-123
```

To look back at a run, open its session:

```sh
python cadgentic.py open ABC-123
python cadgentic.py open go-live ABC-123
```

The first opens the session from the ticket's latest `/ticket` run. The second opens the one from its latest `/go-live` run. Claude Code starts in the repo the run used with that session resumed. You can read back through the run, ask it about what it did, or carry on from where it stopped.

It won't open a session that's in use, whether by a run that's still going or in another terminal. Two processes on one session interleave their messages into one transcript. It checks for a process that has the session and ignores the status in `processed_tickets.json`. An interrupted run can be opened while its status still says `running`.

Every run ends by printing the `claude --resume` command for its session, which is the command `open` runs. A run's session doesn't show up in Claude Code's resume picker since the picker leaves out sessions started through the Agent SDK. Its ID is the way back to it.

## How it works

Every `POLL_INTERVAL` seconds the script searches Jira for tickets assigned to you in one of the `TRIGGER_STATUSES`, across every project. Statuses are worked in the order they're listed. Within a status the oldest ticket goes first.

Each new ticket gets its own Claude Code session, and sessions run one at a time:

1. The ticket's project key picks the repo from `repos.json`.
2. The ticket waits a random `DELAY_MIN` to `DELAY_MAX` seconds. It's then looked up again because a ticket can be reassigned or moved during the wait.
3. The ticket is moved to `IN_PROGRESS_STATUS`, and a session starts in that repo in auto mode with the prompt `/ticket <key>`. Its system prompt says the run is unattended. The `ticket` and `ready-for-review` skills change what they do on that and nothing else.
4. When the session ends, the script looks the ticket up once more. If it's still assigned to you and still in progress, `/ticket` stopped early and the log has its last words. Otherwise it's recorded as handed off.

A poll lasts as long as the waits and runs it starts. The next search comes `POLL_INTERVAL` seconds after the last of them ends.

A run won't start while its repo has uncommitted changes to tracked files since the agent would be switching branches underneath them. The ticket is left for the next poll. A ticket you passed by key is skipped with an error.

### What goes ahead without you

Three things that `/ticket` normally waits on go ahead in a run:

1. The plan. It's saved to `plans/<KEY>.md` and approved.
2. Questions. Each one gets the option marked as recommended, or the first one listed. The questions, the answers, and the alternatives are saved to `decisions/<KEY>.md`. `/ticket` also writes them up as a comment on the ticket's pull request when the repo is on GitHub and the pull request exists. If that comment is missing when the run ends, the script posts the saved record in its place.
3. The handoff. `ready-for-review` posts the comment, assigns the ticket, and moves it to Testing. It holds the handoff when its own checks say the work isn't ready, like a QA pass that failed.

Set `AUTO_APPROVE_PLAN=false` to hold plans instead. A run then ends once the plan is written, and the log prints the command that resumes the session so you can approve it:

```sh
cd /path/to/repo && claude --resume <session-id>
```

Questions are still answered in a run that holds its plan. Its repo isn't checked for uncommitted changes since the run ends at the plan.

### What a run can't do

Three limits apply to every run:

1. Its Jira writes stop at its own ticket. It can comment on that ticket, reassign it, and transition it. Every other Jira write is refused. `EXTRA_DISALLOWED_TOOLS` takes more tools out of the session, like a second Jira server a run should never reach.
2. It can't get a person's approval. A tool call that still needs one is refused with a note that the run is unattended. The one exception is looking at a page in a browser while planning, which the script approves itself.
3. It can't run forever. `RUN_TIMEOUT` stops a run by the clock and `MAX_BUDGET_USD` stops it by cost.

The Jira API token is also blanked in the session's environment so the agent's shell can't read it.

### Going live

Each poll also looks for tickets that are approved to go live and hands them to `/go-live`, ahead of any new tickets. A ticket counts when it is or was assigned to you and one of these is true:

1. It's in `APPROVED_STATUS`.
2. It's in one of the `REVIEW_STATUSES`, and its latest comment from someone else says "approved" and is meant for you. That means the ticket is assigned to you or the comment tags you. Only tickets updated in the last 14 days are checked this way.

That check only picks which tickets `/go-live` looks at. The skill does the real read of the approval and holds anything short of a plain go-ahead.

An approved ticket waits the same random delay as a new one. Its run is held back the same way while the repo has uncommitted changes.

Nothing is approved or answered for the skill in these runs, and the ticket isn't moved to In Progress. A hold ends the run. So does trial mode, for as long as that section is in the skill. The outcome is saved under the ticket's `go_live` entry in `processed_tickets.json`:

| Outcome | Meaning |
|---|---|
| `deployed` | The branch is live and the ticket was handed back. |
| `ready` | The read was clean and trial mode is waiting for your go-ahead. |
| `held` | Something about the approval or the repo needs you. |
| `not-approved` | The ticket has no approval yet. |
| `already-live` | The ticket was already deployed. |
| `dry-run` | `GO_LIVE_PROMPT` asked for a dry run and nothing was changed. |
| `failed` | The deploy or the run failed. |
| `unknown` | The run ended without reporting an outcome. |

A ticket that came back `ready`, `held`, `not-approved`, or `unknown` is looked at again once it changes in Jira. A poll doesn't come back to any other outcome. To run one of those again, pass its key with `go-live`.

Set `GO_LIVE=false` to leave approved tickets alone.

## State and logs

Everything the script writes sits next to it and is listed in `.gitignore`:

- `processed_tickets.json` has one entry per ticket with its status, repo, session ID, and either the agent's last words or the reason the run failed. `open` reads the repo and session ID from it.
- `logs/<KEY>.log` has what the agent said and every tool it called.
- `plans/<KEY>.md` has the plan as it was presented.
- `decisions/<KEY>.md` has the questions the run asked and how they were answered.

| Status | Meaning |
|---|---|
| `running` | A run is in progress. |
| `handed_off` | The run ended and the ticket has moved on from In Progress or to someone else. |
| `finished` | The run ended and the ticket is still yours and still In Progress. `/ticket` stopped before the handoff. |
| `planned` | The plan is waiting for approval (`AUTO_APPROVE_PLAN=false`). |
| `failed` | The run errored, timed out, or was interrupted. |

A poll hands a ticket to `/ticket` only once. It skips any ticket that has one of these statuses, whichever one it is. To run a ticket again, pass its key on the command line.

The `go_live` entry is tracked on its own. A ticket with a status is still picked up for `/go-live` once it's approved. A ticket with only a `go_live` entry is still picked up for `/ticket`.

## Known issues

- A project with more than one repo only routes a ticket that already has a branch or commits in one of them. A new ticket in one of those projects is skipped until its work is started by hand.
- A ticket that comes back with feedback isn't picked up again since it already has a status in `processed_tickets.json`.
- A failed run isn't retried even when the cause was temporary. Its ticket stays In Progress until you run it again or move it yourself.
- The notification is macOS only.
