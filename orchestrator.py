#!/usr/bin/env python3
"""
Claude Roadmap Orchestrator

One-shot scheduler/orchestrator intended to be invoked by systemd.
It never merges PRs. It skips projects with an open PR that has not
been approved by the configured GitHub owner.

Dependencies:
  - Python 3.10+
  - PyYAML
  - Git
  - GitHub CLI (gh), authenticated
  - Claude Code CLI (claude), authenticated through the user's subscription

The orchestrator deliberately keeps policy/state local rather than
depending on Claude to decide how many projects to run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import yaml
except ImportError:
    print("Missing dependency: PyYAML", file=sys.stderr)
    sys.exit(2)

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yaml"
STATE = ROOT / "state.json"
LOG_DIR = ROOT / "logs"
PROMPT_DIR = ROOT / "prompts"


@dataclass
class Project:
    name: str
    repo_dir: Path
    github_repo: str
    roadmap: str
    enabled: bool = True


def now(cfg) -> datetime:
    return datetime.now(ZoneInfo(cfg.get("timezone", "Europe/Madrid")))


def load_config():
    if not CONFIG.exists():
        raise SystemExit(f"Create {CONFIG} from config.example.yaml first.")
    with CONFIG.open() as f:
        return yaml.safe_load(f)


def load_state():
    if not STATE.exists():
        return {
            "week": None,
            "day": None,
            "daily_prs": 0,
            "weekly_prs": 0,
            "last_project_index": -1,
            "last_run_at": None,
            "runs_today": 0,
            "history": [],
        }
    try:
        return json.loads(STATE.read_text())
    except Exception:
        # Preserve corrupt state rather than destroying it.
        backup = STATE.with_suffix(".corrupt.json")
        STATE.rename(backup)
        return {
            "week": None, "day": None, "daily_prs": 0, "weekly_prs": 0,
            "last_project_index": -1, "last_run_at": None, "runs_today": 0,
            "history": [],
        }


def save_state(state):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(STATE)


def iso_week(dt):
    return f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"


def reset_periods(state, current):
    week = iso_week(current)
    day = current.date().isoformat()

    if state.get("week") != week:
        state["week"] = week
        state["weekly_prs"] = 0
        state["history"] = []

    if state.get("day") != day:
        state["day"] = day
        state["daily_prs"] = 0
        state["runs_today"] = 0


def run(cmd, cwd=None, timeout=120, check=True):
    p = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=timeout
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"$ {' '.join(cmd)}\n{p.stdout}")
    return p.stdout


def log(message):
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"{datetime.now().date().isoformat()}.log"
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}\n"
    with path.open("a") as f:
        f.write(line)
    print(line, end="")


def parse_projects(cfg):
    result = []
    for p in cfg.get("projects", []):
        result.append(Project(
            name=p["name"],
            repo_dir=Path(p["repo_dir"]),
            github_repo=p["github_repo"],
            roadmap=p.get("roadmap", "ROADMAP.md"),
            enabled=bool(p.get("enabled", True)),
        ))
    return result


def github_open_prs(project: Project):
    raw = run([
        "gh", "pr", "list",
        "--repo", project.github_repo,
        "--state", "open",
        "--limit", "100",
        "--json", "number,title,url,author,reviews,isDraft,headRefName"
    ])
    return json.loads(raw or "[]")


def user_has_approved(pr, github_owner):
    for review in pr.get("reviews") or []:
        author = (review.get("author") or {}).get("login")
        state = (review.get("state") or "").upper()
        if author and author.lower() == github_owner.lower() and state == "APPROVED":
            return True
    return False


def blocking_pr(project: Project, github_owner: str):
    prs = github_open_prs(project)
    for pr in prs:
        # Draft PRs still represent unfinished work and therefore block
        # another autonomous implementation in this project.
        if not user_has_approved(pr, github_owner):
            return pr
    return None


def git_sync(project: Project, base_branch: str):
    if not project.repo_dir.is_dir():
        raise RuntimeError(f"Repository directory does not exist: {project.repo_dir}")

    run(["git", "fetch", "--prune", "origin"], cwd=project.repo_dir, timeout=180)
    run(["git", "checkout", base_branch], cwd=project.repo_dir, timeout=60)
    run(["git", "pull", "--ff-only", "origin", base_branch], cwd=project.repo_dir, timeout=180)


def read_prompt(name):
    return (PROMPT_DIR / name).read_text()


def run_claude(project: Project, cfg, mode: str):
    if mode == "roadmap":
        prompt = read_prompt("roadmap.txt")
    else:
        prompt = read_prompt("maintenance.txt")

    prompt = prompt.replace("{{PROJECT_NAME}}", project.name)
    prompt = prompt.replace("{{ROADMAP}}", project.roadmap)
    prompt = prompt.replace("{{BASE_BRANCH}}", cfg.get("base_branch", "main"))
    prompt = prompt.replace("{{GITHUB_REPO}}", project.github_repo)

    timeout = int(cfg.get("claude_timeout_minutes", 60)) * 60
    max_turns = int(cfg.get("max_turns", 15))

    # --print/-p is deliberately used as a one-shot non-interactive run.
    # --output-format json makes the result machine-readable.
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--max-turns", str(max_turns),
    ]

    log(f"START {project.name} mode={mode}")
    started = time.time()
    try:
        result = run(cmd, cwd=project.repo_dir, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT {project.name}")
        return False, "timeout"

    duration = round(time.time() - started, 1)

    # Save complete Claude output for inspection.
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (LOG_DIR / f"{stamp}-{project.name}.json").write_text(result)

    try:
        data = json.loads(result)
        text = data.get("result", "") if isinstance(data, dict) else str(data)
        is_error = bool(data.get("is_error")) if isinstance(data, dict) else False
    except Exception:
        text = result
        is_error = True

    if is_error:
        log(f"FAIL {project.name} duration={duration}s")
        return False, text[-2000:]

    # Verify that Claude actually created a PR. We do not count a run as a
    # successful PR merely because Claude claimed it did.
    prs = github_open_prs(project)
    approved = [p for p in prs if user_has_approved(p, cfg["github_owner"])]
    unapproved = [p for p in prs if not user_has_approved(p)]

    if not unapproved:
        log(f"NO_PR {project.name} duration={duration}s")
        return False, "Claude completed without leaving an unapproved open PR."

    # The orchestrator counts one successful run as one newly available PR.
    log(f"SUCCESS {project.name} duration={duration}s")
    return True, text[-3000:]


def choose_project(projects, state):
    enabled = [i for i, p in enumerate(projects) if p.enabled]
    if not enabled:
        return None

    last = state.get("last_project_index", -1)
    for offset in range(1, len(projects) + 1):
        idx = (last + offset) % len(projects)
        if idx in enabled:
            return idx
    return None


def should_boost(cfg, current, state):
    if current.weekday() != 6:  # Sunday
        return False
    target = int(cfg.get("weekly_pr_target", 10))
    if state["weekly_prs"] >= target:
        return False

    h, m = map(int, str(cfg.get("sunday_boost_start", "09:00")).split(":"))
    return current.time() >= dt_time(h, m)


def main(dry_run=False):
    cfg = load_config()
    current = now(cfg)
    state = load_state()
    reset_periods(state, current)

    projects = parse_projects(cfg)
    if not projects:
        raise SystemExit("No projects configured.")

    daily_target = int(cfg.get("daily_pr_target", 2))
    weekly_target = int(cfg.get("weekly_pr_target", 10))
    max_runs = int(cfg.get("max_runs_per_day", 6))
    min_hours = float(cfg.get("min_hours_between_runs", 2.5))

    # Never exceed the configured daily PR target except Sunday boost.
    boost = should_boost(cfg, current, state)
    if not boost and state["daily_prs"] >= daily_target:
        log("STOP daily PR target reached")
        save_state(state)
        return

    if state["runs_today"] >= max_runs:
        log("STOP max daily runs reached")
        save_state(state)
        return

    # Weekly target is a hard stop unless Sunday boost is active. During
    # normal weekdays, reaching the target means no more autonomous work.
    if not boost and state["weekly_prs"] >= weekly_target:
        log("STOP weekly target reached")
        save_state(state)
        return

    # Avoid accidentally launching several Claude agents close together.
    last_run = state.get("last_run_at")
    if last_run:
        previous = datetime.fromisoformat(last_run)
        if (current - previous).total_seconds() < min_hours * 3600:
            log("STOP minimum interval not reached")
            save_state(state)
            return

    idx = choose_project(projects, state)
    if idx is None:
        log("STOP no enabled projects")
        save_state(state)
        return

    # We may need to inspect several projects to find an unblocked one.
    checked = 0
    chosen = None
    while checked < len(projects):
        idx = choose_project(projects, state)
        if idx is None:
            break
        project = projects[idx]

        try:
            blocker = blocking_pr(project, cfg["github_owner"])
        except Exception as e:
            log(f"GITHUB_ERROR {project.name}: {e}")
            state["last_project_index"] = idx
            checked += 1
            continue

        state["last_project_index"] = idx
        if blocker:
            log(f"SKIP {project.name}: PR #{blocker['number']} awaiting approval")
            checked += 1
            continue

        chosen = (idx, project)
        break

    if chosen is None:
        # All projects are blocked. Use maintenance/QA only if the normal
        # daily target has not been reached; Sunday boost may also use it.
        log("ALL_PROJECTS_BLOCKED: maintenance/QA fallback")
        idx = choose_project(projects, state)
        if idx is None:
            save_state(state)
            return
        project = projects[idx]
        mode = "maintenance"
    else:
        idx, project = chosen
        mode = "roadmap"

    if dry_run:
        print(json.dumps({
            "project": project.name,
            "mode": mode,
            "boost": boost,
            "daily_prs": state["daily_prs"],
            "weekly_prs": state["weekly_prs"],
        }, indent=2))
        return

    try:
        git_sync(project, cfg.get("base_branch", "main"))
        success, detail = run_claude(project, cfg, mode)
    except Exception as e:
        log(f"ERROR {project.name}: {e}")
        success = False
        detail = str(e)

    state["runs_today"] += 1
    state["last_run_at"] = current.isoformat()
    state["history"].append({
        "timestamp": current.isoformat(),
        "project": project.name,
        "mode": mode,
        "success": success,
        "detail": detail[-1000:],
    })

    if success:
        state["daily_prs"] += 1
        state["weekly_prs"] += 1

    # Keep state bounded.
    state["history"] = state["history"][-100:]
    save_state(state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
