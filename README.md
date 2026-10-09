# Cadgentic

Cadgentic polls Jira for tickets assigned to you and hands each one to Claude Code's `/ticket` command in the repo its Jira project maps to. A run plans the ticket, builds it, deploys it for review, checks it, and posts the handoff comment with nobody at the keyboard. A ticket that's sent back for more work gets another run. Once a ticket is approved, Cadgentic hands it to `/go-live`.

## Requirements

- Python 3.10 or later
- Claude Code, installed and logged in
- The `/ticket` and `/go-live` skills, the skills they call, and the `atlassian` MCP server they work with tickets through
- Chrome with the Claude extension, signed in to Jira
- The GitHub CLI, logged in
- For a project that deploys over SSH, a key its server accepts without a prompt
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
  "XYZ": ["~/code/xyz-b2c", "~/code/xyz-b2b"],
  "LMN": {
    "repo": "~/code/lmn/src",
    "check": "curl -skf -o /dev/null https://lmn.test/",
    "up": "docker compose up -d",
    "down": "docker compose stop"
  }
}
```

A path is the git repo itself, which isn't always the project root. A project with more than one repo lists them all (see [Known issues](#known-issues)). A ticket whose project isn't in the file is skipped with a notification. It's picked up on a later poll once the project is added.

A project whose local site isn't left running, like one in a Docker stack, uses the longer form. `repo` is the path. `check`, `up`, and `down` are the commands Cadgentic uses to start that site before a run and stop it afterward (see [Local environments](#local-environments)).

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

To send a run that stopped early back into its own session, resume it:

```sh
python cadgentic.py resume ABC-123
python cadgentic.py resume ABC-123 the deploy check is fixed now
```

That starts another unattended run in the same session. The agent keeps its plan, the answers it was given, and everything it had read. Anything after the key is passed along as a note about what changed. See [When a run stops early](#when-a-run-stops-early).

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
3. If the project names a [local environment](#local-environments) that isn't up, Cadgentic starts it. It's stopped again when the session ends.
4. The ticket is moved to `IN_PROGRESS_STATUS`, and a session starts in that repo in auto mode with the prompt `/ticket <key>`. Its system prompt says the run is unattended. The `ticket` and `ready-for-review` skills change what they do on that and nothing else.
5. When the session ends, the script looks the ticket up once more. If it's still assigned to you and still in progress, `/ticket` stopped early and the log has its last words. Otherwise it's recorded as handed off.

A session can end a turn while it waits on a command it left running in the background, like a watch on a deploy. The run stays open until the command finishes and the session carries on. If nothing has woken the session `BACKGROUND_TIMEOUT` seconds after a turn (600 by default), the run ends and the command is stopped with it. That's the usual end for a run that never stopped its dev server. Raise the setting if your deploys take longer than that. Waits on subagents aren't limited by it.

A poll lasts as long as the waits and runs it starts. The next search comes `POLL_INTERVAL` seconds after the last of them ends.

A run won't start while its repo has uncommitted changes to tracked files since the agent would be switching branches underneath them. The ticket is left for the next poll. A ticket you passed by key is skipped with an error.

### What goes ahead without you

Three things that `/ticket` normally waits on go ahead in a run:

1. The plan. It's saved to `plans/<KEY>.md` and approved.
2. Questions. Each one gets the option marked as recommended, or the first one listed. The questions, the answers, and the alternatives are saved to `decisions/<KEY>.md`. `/ticket` also writes them up as a comment on the ticket's pull request when the repo is on GitHub or Bitbucket and the pull request exists. If that comment is missing when the run ends, the script posts the saved record in its place. It can only do that on GitHub.
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

### Local environments

Some projects need a local environment that a run can't count on being up, like a Docker stack you only start when you're working on it. Give that project's entry in `repos.json` three shell commands. Each one runs from the repo's directory.

| Command | What it does |
|---|---|
| `check` | Exits 0 when the environment is up. |
| `up` | Starts it. |
| `down` | Stops it. |

Before a ticket's run starts, Cadgentic runs `check`. That covers a polled ticket, a ticket passed by key, and `resume`.

- If `check` passes, the environment was already up. The run uses it. Cadgentic leaves it running afterward since it didn't start it.
- If `check` fails, Cadgentic runs `up`, runs `check` every 5 seconds until it passes, and then starts the run. When the run ends it runs `down`, however the run ended: handed off, stopped early, failed, timed out, or interrupted with Ctrl-C.

The environment is started before the session, not by the agent inside it, for two reasons:

1. It gets stopped even when the run fails or times out.
2. An MCP server that lives inside the environment, like one started with `docker exec`, only connects if the environment is up when the session starts.

If `up` fails, or `check` still fails `ENV_TIMEOUT` seconds after `up` (300 by default), the run doesn't start. Cadgentic runs `down` to clean up and logs the command that failed with the end of its output. Nothing is recorded for the ticket and it isn't moved in Jira, so a polled ticket is tried again on the next poll. You get one notification per ticket, not one per poll.

A few things to know when writing the commands:

- They run in your shell with nobody watching. `up` and `down` each get `ENV_TIMEOUT` seconds to finish. `check` gets 30 seconds per try, so have it give up sooner than that.
- `up` has to return once the environment is starting. A command that stays in the foreground, like a start script that ends in a file watcher, counts as failed when it times out.
- An entry with `up` and no `check` is started and stopped around every run since Cadgentic can't tell whether it was already up.
- Environments that bind the same ports can't be up together. Runs go one at a time so two of Cadgentic's never overlap. One you started yourself can still be in the way. Then `up` or `check` fails and the ticket waits for the next poll.

Go-live runs don't start an environment. Anything that lives inside the session is still the agent's to start, like a dev server or a file watcher the skill runs in the background.

### When a ticket is sent back

A ticket that was handed off comes back when a reviewer assigns it to you again and moves it to one of the `TRIGGER_STATUSES`. The search finds it there and it gets another run, with the same wait and the same checks as a new ticket.

The run is a new session with the same `/ticket <key>` prompt. `/ticket` finds the work that's already in the repo and plans only what the ticket's latest comment asks for. If that comment is your own, there's no feedback to act on. The run stops there and is recorded as `finished`.

Only a ticket whose last run ended `handed_off` gets another run. That status means the ticket had moved on to another status or another person. Finding it in the search again means someone sent it back. A ticket whose last run ended any other way is skipped (see [Known issues](#known-issues)).

`processed_tickets.json` and `open` keep the latest round's session. A round that presents a plan or asks questions overwrites `plans/<KEY>.md` or `decisions/<KEY>.md`. Its questions go on the pull request as their own comment. `logs/<KEY>.log` keeps every round, each under a line with its session ID.

### When a run stops early

A run that ends `finished` or `failed` stopped short of the handoff. A poll won't come back to it. Once you've dealt with whatever stopped it, there are three ways to pick it up:

1. `python cadgentic.py resume ABC-123` carries on unattended in the session the run used.
2. `python cadgentic.py open ABC-123` opens that session so you can carry on by hand.
3. `python cadgentic.py ABC-123` starts over in a new session.

A resumed run isn't handed `/ticket <key>` again. Its prompt is `RESUME_PROMPT`, which tells the agent to check whether the thing that stopped it is still in the way and then carry on. The note from the command line is added after it. Everything else matches a first run: the system prompt says the run is unattended, the same limits apply, and the ticket is looked up when the session ends to see whether it was handed off.

`resume` refuses in three cases:

- The ticket's last run ended some other way than `finished` or `failed`.
- The session is in use, by a run that's still going or in another terminal.
- The repo has uncommitted changes to tracked files. A run that failed partway through an edit leaves some behind. Commit or stash them first, or use `open`.

Questions answered before the run stopped stay in `decisions/<KEY>.md`. Any asked after the resume are added to the same file. If those answers aren't on the pull request when the resumed run ends, the script posts the saved record. `logs/<KEY>.log` puts the second leg under its own line with the same session ID, marked `resumed`.

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

`GO_LIVE_EXTRA_JQL` is ANDed onto the search for approved tickets and nothing else. Use it to keep a project that `/go-live` doesn't cover out of this lane, e.g., `GO_LIVE_EXTRA_JQL='project not in (LMN)'`. Without it an approved ticket in that project is handed to `/go-live`, which can only stop, and the ticket is looked at again each time it changes in Jira. Passing a key with `go-live` still runs it.

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

A poll skips any ticket that has one of these statuses except `handed_off`. A `handed_off` ticket gets another run once it's [sent back](#when-a-ticket-is-sent-back). To run any other ticket again, pass its key on the command line. A `finished` or `failed` one can be [resumed](#when-a-run-stops-early) instead.

The `go_live` entry is tracked on its own. A ticket with a status is still picked up for `/go-live` once it's approved. A ticket with only a `go_live` entry is still picked up for `/ticket`.

## Known issues

- A project with more than one repo only routes a ticket that already has a branch or commits in one of them. A new ticket in one of those projects is skipped until its work is started by hand.
- A ticket that's sent back is only picked up when its last run handed it off. If that run stopped early or failed and you finished the ticket by hand, it's skipped when it comes back. Pass its key to run it.
- A poll doesn't hand a ticket to `/go-live` a second time once it has gone live, even after a later round is approved. Pass its key with `go-live`.
- A ticket whose local environment won't come up is tried again on every poll, each time after its usual wait, until the environment starts or you take the ticket out of the search.
- A failed run isn't retried even when the cause was temporary. Its ticket stays In Progress until you resume it, run it again, or move it yourself.
- The notification is macOS only.
