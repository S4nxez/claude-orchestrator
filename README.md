# Claude Roadmap Orchestrator

Autonomous, conservative scheduler for four GitHub projects using Claude Code
from a Raspberry Pi.

It is designed around this workflow:

1. Pick the next project using round-robin rotation.
2. If that project has an open PR that the configured GitHub owner has NOT
   approved, skip it.
3. Otherwise ask Claude Code to implement exactly one roadmap item.
4. Claude creates a branch, commits, pushes and opens a PR.
5. The orchestrator never merges or approves.
6. Normal target: 2 PRs/day.
7. Weekly target: 10 PRs.
8. Sunday from the configured time: boost remaining weekly capacity.
9. If every project is blocked by an unapproved PR, use the QA/maintenance prompt.

## Important

This package intentionally does NOT attempt to bypass Claude's subscription
limits. It only controls your own execution rate. The actual Claude Code
account/subscription limits remain authoritative.

Do not put an ANTHROPIC_API_KEY in the service environment if you intend to use
Claude Code through your subscription authentication. An API key can route
usage through API billing instead.

## 1. Install prerequisites

On Raspberry Pi OS/Debian:

    sudo apt update
    sudo apt install -y git gh python3 python3-pip

Install PyYAML:

    python3 -m pip install --user -r requirements.txt

Install/authenticate Claude Code using the current official Claude Code
installation instructions, then verify:

    claude --version
    claude

Authenticate GitHub CLI:

    gh auth login

Then verify:

    gh auth status

The GitHub account used by `gh` needs enough repository permissions to:
- clone/fetch/push branches
- create pull requests
- read pull request reviews

## 2. Install this project

Recommended location:

    sudo mkdir -p /opt/claude-roadmap-orchestrator
    sudo chown -R "$USER:$USER" /opt/claude-roadmap-orchestrator

Copy the repository contents there.

Then:

    cp config.example.yaml config.yaml

Edit:

    nano config.yaml

At minimum configure:
- github_owner
- the four repo_dir paths
- the four github_repo values
- enabled=true/false

## 3. Verify each project

Each configured repo should already exist locally and be a normal Git clone.

Example:

    git -C /opt/projects/trade-analytics status
    git -C /opt/projects/trade-analytics remote -v

Each project should ideally contain:
- CLAUDE.md
- ROADMAP.md

The orchestrator can work without CLAUDE.md, but project-specific instructions
make autonomous changes substantially safer.

## 4. Dry run

Before enabling systemd:

    python3 orchestrator.py --dry-run

This checks project selection and PR blocking without running Claude.

## 5. Manual test

Run:

    python3 orchestrator.py

Watch:

    tail -f logs/$(date +%F).log

Claude's complete JSON result is stored in logs/ with a timestamp.

## 6. Install systemd

Copy:

    sudo cp systemd/claude-roadmap-orchestrator.service /etc/systemd/system/
    sudo cp systemd/claude-roadmap-orchestrator.timer /etc/systemd/system/

If your Linux username is not `sanxez`, edit the service's User= and paths.

Then:

    sudo systemctl daemon-reload
    sudo systemctl enable --now claude-roadmap-orchestrator.timer

Check:

    systemctl status claude-roadmap-orchestrator.timer
    systemctl list-timers | grep claude-roadmap

Manual invocation:

    sudo systemctl start claude-roadmap-orchestrator.service

Logs:

    journalctl -u claude-roadmap-orchestrator.service -n 100 --no-pager

## Scheduling

The included timer runs at approximately:

    08:00
    11:00
    14:00
    17:00
    20:00

with up to 10 minutes of systemd jitter.

The Python policy additionally enforces:
- 2.5 hours minimum between executions
- maximum 6 executions/day
- 2 successful PRs/day on normal days
- 10 successful PRs/week
- Sunday boost from 09:00 until the weekly target is reached

The Sunday boost only changes the policy after the configured Sunday time.
It does not guarantee that Claude will have unused subscription capacity.

## PR blocking rule

An open PR blocks a project unless the configured GitHub owner has an
`APPROVED` review on that PR.

Examples:

- open + no approval -> blocked
- open + your approval -> available
- merged -> available
- closed -> available

If all projects are blocked, the orchestrator switches to the maintenance/QA
prompt rather than repeatedly trying the same projects.

## State

`state.json` stores:
- current ISO week
- current day
- daily PR count
- weekly PR count
- last project index
- last execution
- bounded execution history

If you want to reset counters manually, stop the timer and remove state.json.
The next execution recreates it.

## Recommended repository conventions

Keep roadmap items small and independently reviewable:

    - [ ] Add Invoice entity
    - [ ] Add InvoiceRepository
    - [ ] Add POST /invoices
    - [ ] Add validation for invoice amount
    - [ ] Add InvoiceService tests

Avoid huge autonomous items such as:

    - [ ] Rewrite the entire backend

## Security model

The orchestrator is intentionally conservative:
- never merges
- never approves
- never closes PRs
- never works directly on the configured base branch
- limits Claude turns
- has a hard execution timeout
- requires an unapproved PR to exist before counting a successful run
- keeps execution logs
- uses ff-only pulls
- does not expose secrets in prompts

Still review every generated PR before merging.

## Optional notifications

The current package leaves notifications out deliberately. A next iteration can
send a Telegram/Discord/email notification only after a PR is actually created.
