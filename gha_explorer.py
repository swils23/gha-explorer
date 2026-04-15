#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "textual>=3.0.0",
#     "plotext>=5.3.2",
#     "rich>=13.0.0",
# ]
# ///
"""
GHA Explorer — a TUI for exploring GitHub Actions workflow timing.

Usage:
    uv run gha_explorer.py

Incrementally fetches successful workflow runs from the current repo (detected
via the `gh` CLI), caching results in SQLite. On subsequent launches, cached
data displays immediately and only new runs are fetched from the API.

Logs errors to gha_explorer.log alongside the script.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median, stdev

from rich.console import Group as RichGroup
from rich.text import Text as RichText

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState

# ---------------------------------------------------------------------------
# Logging — always log to file next to this script
# ---------------------------------------------------------------------------

LOG_FILE = Path(__file__).parent / "gha_explorer.log"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gha_explorer")

# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

MAX_WORKERS = 8

TIME_RANGES: dict[str, timedelta | None] = {
    "1d": timedelta(days=1),
    "1m": timedelta(days=30),
    "3m": timedelta(days=90),
    "6m": timedelta(days=180),
    "1y": timedelta(days=365),
    "all": None,
}

CACHE_DB = Path(__file__).parent / "cache.db"
CONFIG_FILE = Path(__file__).parent / "config.json"


def _cache_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(CACHE_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_jobs (
            run_id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            raw_run TEXT NOT NULL,
            raw_jobs TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_run_jobs_repo ON run_jobs(repo)")
    return conn


def cache_get_jobs(run_id: int) -> tuple[dict, list[dict]] | None:
    """Look up by run_id only — run IDs are globally unique in GitHub."""
    with _cache_conn() as conn:
        row = conn.execute(
            "SELECT raw_run, raw_jobs FROM run_jobs WHERE run_id = ?", (run_id,)
        ).fetchone()
    if row:
        return json.loads(row[0]), json.loads(row[1])
    return None


def cache_put_jobs(repo: str, run_id: int, raw_run: dict, raw_jobs: list[dict]) -> None:
    with _cache_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO run_jobs (repo, run_id, raw_run, raw_jobs, fetched_at) VALUES (?, ?, ?, ?, ?)",
            (repo, run_id, json.dumps(raw_run), json.dumps(raw_jobs), datetime.now().isoformat()),
        )


def cache_get_all_ids(repo: str) -> set[int]:
    """Return the set of run_ids in the cache for a specific repo."""
    with _cache_conn() as conn:
        rows = conn.execute("SELECT run_id FROM run_jobs WHERE repo = ?", (repo,)).fetchall()
    return {row[0] for row in rows}


def cache_load_all(repo: str) -> list[RunData]:
    """Load all cached runs for a repo from SQLite."""
    with _cache_conn() as conn:
        rows = conn.execute(
            "SELECT raw_run, raw_jobs FROM run_jobs WHERE repo = ?", (repo,)
        ).fetchall()
    runs = []
    for raw_run_json, raw_jobs_json in rows:
        try:
            runs.append(build_run_data(json.loads(raw_run_json), json.loads(raw_jobs_json)))
        except Exception:
            log.debug("Skipping corrupt cache row", exc_info=True)
    runs.sort(key=lambda r: r.created_at)
    return runs


@dataclass
class StepTiming:
    name: str
    duration_s: float


@dataclass
class JobTiming:
    name: str
    base_name: str
    matrix_key: str | None
    duration_s: float
    started_at: datetime
    completed_at: datetime
    steps: list[StepTiming] = field(default_factory=list)


@dataclass
class RunData:
    run_id: int
    branch: str
    title: str
    created_at: datetime
    total_duration_s: float
    workflow: str = ""
    jobs: list[JobTiming] = field(default_factory=list)


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def duration_s(start: str, end: str) -> float:
    return max(0, (parse_dt(end) - parse_dt(start)).total_seconds())


def parse_matrix_suffix(name: str) -> tuple[str, str | None]:
    """Split a rendered GHA job name into its base name and matrix key.

    GitHub doesn't expose matrix info as structured data on the jobs endpoint,
    so we parse the rendered suffix:

        "Run tests (2)"               → ("Run tests", "2")
        "build (ubuntu-latest, 3.11)" → ("build", "ubuntu-latest, 3.11")
        "deploy (prod (us-east))"     → ("deploy", "prod (us-east)")
        "Lint"                        → ("Lint", None)

    We walk backward tracking paren depth to find the `(` that matches the
    trailing `)`, so nested parens inside the matrix display don't confuse us.

    False positives are possible (a non-matrix job like "Build (production)"
    also matches) but harmless — the suffix becomes the shard key and the job
    appears once in filters instead of multiple times.
    """
    if not name.endswith(")"):
        return name, None
    depth = 0
    for i in range(len(name) - 1, -1, -1):
        c = name[i]
        if c == ")":
            depth += 1
        elif c == "(":
            depth -= 1
            if depth == 0:
                if i == 0:
                    return name, None  # whole name is parenthesized
                inner = name[i + 1:-1].strip()
                if not inner:
                    return name, None
                return name[:i].strip(), inner
    return name, None  # unmatched trailing `)`


def _shard_sort_key(key: str) -> tuple[int, object]:
    """Sort numeric shard keys numerically, everything else alphabetically."""
    return (0, int(key)) if key.isdigit() else (1, key)


def _run_gh(*args: str, retries: int = 4) -> str:
    """Run a gh CLI command, returning stdout. Retries on 429 rate-limit."""
    for attempt in range(retries + 1):
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout
        if ("429" in result.stderr or "rate limit" in result.stderr.lower()) and attempt < retries:
            delay = 2 ** attempt  # 1, 2, 4, 8s
            log.warning("Rate limited, retrying in %ds (attempt %d/%d): %s", delay, attempt + 1, retries, args[1:3])
            time.sleep(delay)
            continue
        log.error("gh command failed (exit %d): %s\nstderr: %s", result.returncode, args, result.stderr.strip())
        result.check_returncode()
    return ""  # unreachable, keeps type checker happy


def gh_api(endpoint: str) -> dict:
    return json.loads(_run_gh("gh", "api", endpoint, "--paginate"))


def fetch_user_repos() -> list[dict]:
    """List all repos the user can access — personal, collaborator, and org member.

    Uses the `user/repos` API endpoint with affiliation=owner,collaborator,organization_member
    so private org repos show up alongside personal ones. `gh repo list` by itself only
    returns personal repos.
    """
    data = _run_gh(
        "gh", "api",
        "user/repos?affiliation=owner,collaborator,organization_member&per_page=100&sort=pushed",
        "--paginate",
    )
    raw = json.loads(data)
    # Normalize field names to match what the picker expects
    repos = [
        {
            "nameWithOwner": r["full_name"],
            "description": r.get("description") or "",
            "pushedAt": r.get("pushed_at") or "",
            "isPrivate": r.get("private", False),
            "isFork": r.get("fork", False),
        }
        for r in raw
    ]
    # Sort by most recently pushed (API sort=pushed already does this, but be explicit)
    repos.sort(key=lambda r: r.get("pushedAt", ""), reverse=True)
    return repos


def detect_current_repo() -> str | None:
    """If run from inside a git repo that has a GitHub remote, return owner/name."""
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return json.loads(result.stdout).get("nameWithOwner")
    except Exception:
        log.debug("Could not detect current repo", exc_info=True)
    return None


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            log.debug("Could not read config", exc_info=True)
    return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception:
        log.exception("Could not save config")


def fetch_run_list(
    repo: str,
    since_date: str | None = None,
    until_date: str | None = None,
) -> list[dict]:
    """Fetch the run list from GitHub for a specific repo, optionally scoped to a date range.

    Fetches all successful workflow runs across all workflows.
    Returns newest-first. gh run list caps at 1000 results per call.
    """
    args = [
        "gh", "run", "list",
        "--repo", repo,
        "--status", "success",
        "--limit", "1000",
        "--json", "databaseId,displayTitle,headBranch,conclusion,createdAt,workflowName",
    ]
    if since_date and until_date:
        args.extend(["--created", f"{since_date[:10]}..{until_date[:10]}"])
    elif since_date:
        args.extend(["--created", f">{since_date[:10]}"])
    elif until_date:
        args.extend(["--created", f"<{until_date[:10]}"])
    return json.loads(_run_gh(*args))


def fetch_run_jobs(repo: str, run_id: int) -> list[dict]:
    data = gh_api(f"repos/{repo}/actions/runs/{run_id}/jobs")
    return data.get("jobs", [])


def build_run_data(raw_run: dict, raw_jobs: list[dict]) -> RunData:
    jobs: list[JobTiming] = []
    earliest_start = None
    latest_end = None

    for j in raw_jobs:
        if j.get("conclusion") not in ("success",):
            continue
        if not j.get("started_at") or not j.get("completed_at"):
            continue

        base_name, matrix_key = parse_matrix_suffix(j["name"])
        started = parse_dt(j["started_at"])
        completed = parse_dt(j["completed_at"])
        dur = (completed - started).total_seconds()

        steps = []
        for s in j.get("steps", []):
            if s.get("started_at") and s.get("completed_at") and s.get("conclusion") == "success":
                sdur = duration_s(s["started_at"], s["completed_at"])
                if sdur > 0:
                    steps.append(StepTiming(name=s["name"], duration_s=sdur))

        jobs.append(JobTiming(
            name=j["name"], base_name=base_name, matrix_key=matrix_key,
            duration_s=dur, started_at=started, completed_at=completed, steps=steps,
        ))

        if earliest_start is None or started < earliest_start:
            earliest_start = started
        if latest_end is None or completed > latest_end:
            latest_end = completed

    total = (latest_end - earliest_start).total_seconds() if earliest_start and latest_end else 0.0

    return RunData(
        run_id=raw_run["databaseId"], branch=raw_run["headBranch"],
        title=raw_run["displayTitle"], created_at=parse_dt(raw_run["createdAt"]),
        total_duration_s=total, workflow=raw_run.get("workflowName", ""),
        jobs=jobs,
    )


def _fetch_and_build(repo: str, raw_run: dict, retries: int = 2) -> RunData:
    run_id = raw_run["databaseId"]
    cached = cache_get_jobs(run_id)
    if cached:
        log.log(5, "Cache hit for run %s", run_id)  # TRACE level — below DEBUG
        _, raw_jobs = cached
        return build_run_data(raw_run, raw_jobs)

    for attempt in range(retries + 1):
        try:
            raw_jobs = fetch_run_jobs(repo, run_id)
            cache_put_jobs(repo, run_id, raw_run, raw_jobs)
            return build_run_data(raw_run, raw_jobs)
        except subprocess.CalledProcessError:
            if attempt == retries:
                raise
            time.sleep(1 * (attempt + 1))
            log.warning("Retrying fetch for run %s (attempt %d)", run_id, attempt + 2)


def _fetch_jobs_for_runs(
    repo: str,
    raw_runs: list[dict],
    on_progress: callable | None = None,
) -> list[RunData]:
    """Fetch job details for a list of raw runs using ThreadPoolExecutor."""
    runs: list[RunData] = []
    total = len(raw_runs)
    done = 0
    errors = 0

    if on_progress:
        on_progress(0, total)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_and_build, repo, raw): raw for raw in raw_runs}
        for future in as_completed(futures):
            done += 1
            raw = futures[future]
            try:
                run_data = future.result()
                runs.append(run_data)
            except Exception:
                errors += 1
                log.exception("Failed to fetch/build run %s", raw.get("databaseId"))
            if on_progress:
                on_progress(done, total)

    if errors:
        log.warning("Completed with %d errors out of %d runs", errors, total)

    return runs


BACKFILL_WINDOW_DAYS = 90


def fetch_incremental(
    repo: str,
    on_progress: callable | None = None,
    on_status: callable | None = None,
) -> list[RunData]:
    """Incrementally fetch runs for `repo`, using cache to avoid re-fetching.

    1. Load all cached data (caller shows this immediately)
    2. Forward-fetch: runs newer than the latest cached date
    3. Backfill: walk backwards in 90-day windows until no new runs found
    4. Return merged + sorted result
    """
    # Step 1: Load cache
    if on_status:
        on_status("Loading cache...")
    cached_runs = cache_load_all(repo)
    # Use DB-level IDs to avoid re-fetching runs that are cached but failed to parse
    cached_ids = cache_get_all_ids(repo)
    log.info("Cache loaded for %s: %d runs (%d in DB)", repo, len(cached_runs), len(cached_ids))

    if on_status and cached_runs:
        on_status(f"Loaded {len(cached_runs)} cached runs")

    new_runs: list[RunData] = []

    # Step 2: Forward-fetch (runs newer than latest cached)
    # Subtract one day because GitHub's > filter is exclusive on date boundaries
    if cached_runs:
        one_day_before_newest = cached_runs[-1].created_at - timedelta(days=1)
        newest_date = one_day_before_newest.isoformat()
    else:
        newest_date = None
    if on_status:
        if newest_date:
            on_status(f"Checking for new runs since {newest_date[:10]}...")
        else:
            on_status("Fetching runs (first load)...")

    try:
        forward_raw = fetch_run_list(repo, since_date=newest_date)
    except subprocess.CalledProcessError:
        log.warning("Forward fetch failed — likely rate limited")
        if on_status:
            on_status("Rate limited — showing cached data")
        # Return whatever we have cached
        return cached_runs

    forward_new = [r for r in forward_raw if r["databaseId"] not in cached_ids]
    log.info("Forward fetch: %d listed, %d new", len(forward_raw), len(forward_new))

    if forward_new:
        if on_status:
            on_status(f"Fetching details for {len(forward_new)} new runs...")
        new_runs.extend(_fetch_jobs_for_runs(repo, forward_new, on_progress=on_progress))
        cached_ids.update(r.run_id for r in new_runs)

    # Step 3: Backfill — walk backwards in windows until no new runs found
    oldest_date = cached_runs[0].created_at if cached_runs else None
    if oldest_date is None and cached_runs == [] and new_runs:
        # First load: start backfill from the oldest run we just fetched
        new_runs.sort(key=lambda r: r.created_at)
        oldest_date = new_runs[0].created_at

    if oldest_date:
        window_end = oldest_date
        while True:
            window_start = window_end - timedelta(days=BACKFILL_WINDOW_DAYS)
            if on_status:
                on_status(f"Backfilling {window_start.strftime('%Y-%m-%d')} → {window_end.strftime('%Y-%m-%d')}...")

            try:
                backfill_raw = fetch_run_list(
                    repo,
                    since_date=window_start.isoformat(),
                    until_date=window_end.isoformat(),
                )
            except subprocess.CalledProcessError:
                log.warning("Backfill stopped — API error (likely rate limited)")
                if on_status:
                    on_status("Backfill paused — rate limited, will resume next launch")
                break

            backfill_new = [r for r in backfill_raw if r["databaseId"] not in cached_ids]
            log.info("Backfill window %s..%s: %d listed, %d new",
                     window_start.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d"),
                     len(backfill_raw), len(backfill_new))

            if not backfill_new:
                # No new runs in this window — we've reached the beginning
                break

            if on_status:
                on_status(f"Fetching details for {len(backfill_new)} older runs...")
            backfill_runs = _fetch_jobs_for_runs(repo, backfill_new, on_progress=on_progress)
            new_runs.extend(backfill_runs)
            cached_ids.update(r.run_id for r in backfill_runs)

            window_end = window_start

    # Step 4: Merge and sort
    total_new = len(new_runs)
    all_runs = cached_runs + new_runs
    all_runs.sort(key=lambda r: r.created_at)

    if on_status:
        if total_new:
            on_status(f"+{total_new} new runs fetched — {len(all_runs)} total")
        else:
            on_status(f"Up to date — {len(all_runs)} runs")

    log.info("Fetch complete: %d cached + %d new = %d total", len(cached_runs), total_new, len(all_runs))
    return all_runs


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s" if m > 0 else f"{s}s"


def get_job_durations(runs: list[RunData], base_name: str, branch: str | None = None) -> list[float]:
    durations = []
    for run in runs:
        if branch and run.branch != branch:
            continue
        matrix_durs = [j.duration_s for j in run.jobs if j.base_name == base_name]
        if matrix_durs:
            durations.append(mean(matrix_durs))
    return durations


def get_step_durations(
    runs: list[RunData], job_base_name: str, step_name: str, branch: str | None = None
) -> list[float]:
    durations = []
    for run in runs:
        if branch and run.branch != branch:
            continue
        step_durs = []
        for j in run.jobs:
            if j.base_name != job_base_name:
                continue
            for s in j.steps:
                if s.name == step_name:
                    step_durs.append(s.duration_s)
        if step_durs:
            durations.append(mean(step_durs))
    return durations


def stats_summary(values: list[float]) -> dict[str, str]:
    if not values:
        return {"avg": "-", "median": "-", "min": "-", "max": "-", "stdev": "-", "count": "0"}
    return {
        "avg": fmt_duration(mean(values)),
        "median": fmt_duration(median(values)),
        "min": fmt_duration(min(values)),
        "max": fmt_duration(max(values)),
        "count": str(len(values)),
        "stdev": fmt_duration(stdev(values)) if len(values) > 1 else "-",
    }


# ---------------------------------------------------------------------------
# Plot helper
# ---------------------------------------------------------------------------


_plot_lock = threading.Lock()


def render_plot(plot_func, width: int, height: int = 18) -> RichText:
    """Render a plotext chart to a Rich Text object (ANSI colors preserved)."""
    with _plot_lock:
        import plotext as plt
        plt.clear_figure()
        plt.clear_data()
        plt.theme("dark")
        plt.plotsize(width, height)
        plot_func(plt)
        return RichText.from_ansi(plt.build())


# ---------------------------------------------------------------------------
# Textual TUI
# ---------------------------------------------------------------------------


class RepoPickerScreen(ModalScreen[str | None]):
    """Modal screen that lists accessible repos with type-to-filter search."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("down", "focus_list", "Focus list", show=False),
    ]

    CSS = """
    RepoPickerScreen {
        align: center middle;
    }
    #picker-box {
        width: 80;
        height: 80%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #picker-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #picker-search {
        margin-bottom: 1;
    }
    #picker-status {
        color: $text-muted;
        margin-bottom: 1;
    }
    #repo-list {
        height: 1fr;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._all_repos: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Label("Select a repository", id="picker-title")
            yield Input(placeholder="Filter by name or description...", id="picker-search")
            yield Static("Loading repos...", id="picker-status")
            yield OptionList(id="repo-list")

    def on_mount(self) -> None:
        self.query_one("#picker-search", Input).focus()
        self.load_repos()

    @work(thread=True)
    def load_repos(self) -> list[dict]:
        return fetch_user_repos()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "load_repos":
            return
        if event.state == WorkerState.SUCCESS:
            self._all_repos = event.worker.result
            self._render_repo_list(self._all_repos)
            self.query_one("#picker-status", Static).update(
                f"{len(self._all_repos)} repos — type to filter, ↓ to list, Enter to select, Esc to cancel"
            )
        elif event.state == WorkerState.ERROR:
            self.query_one("#picker-status", Static).update(
                f"Error loading repos: {event.worker.error}"
            )

    def _render_repo_list(self, repos: list[dict]) -> None:
        lst = self.query_one("#repo-list", OptionList)
        lst.clear_options()
        for r in repos:
            name = r["nameWithOwner"]
            desc = (r.get("description") or "")[:60]
            marker = ""
            if r.get("isPrivate"):
                marker += " [private]"
            if r.get("isFork"):
                marker += " [fork]"
            label = f"{name}{marker}   {desc}" if desc else f"{name}{marker}"
            lst.add_option(Option(label, id=name))

    @on(Input.Changed, "#picker-search")
    def on_search_changed(self, event: Input.Changed) -> None:
        q = event.value.lower().strip()
        if not q:
            matching = self._all_repos
        else:
            matching = [
                r for r in self._all_repos
                if q in r["nameWithOwner"].lower() or q in (r.get("description") or "").lower()
            ]
        self._render_repo_list(matching)
        status = self.query_one("#picker-status", Static)
        status.update(
            f"{len(matching)} of {len(self._all_repos)} repos"
            f" — type to filter, ↓ to list, Enter to select, Esc to cancel"
        )

    @on(Input.Submitted, "#picker-search")
    def on_search_submitted(self, event: Input.Submitted) -> None:
        """Pressing Enter in the search box selects the first visible result."""
        lst = self.query_one("#repo-list", OptionList)
        if lst.option_count > 0:
            first = lst.get_option_at_index(0)
            if first and first.id:
                self.dismiss(str(first.id))

    @on(OptionList.OptionSelected, "#repo-list")
    def on_repo_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option and event.option.id:
            self.dismiss(str(event.option.id))

    def action_focus_list(self) -> None:
        """Move focus from the search box to the option list."""
        search = self.query_one("#picker-search", Input)
        if search.has_focus:
            lst = self.query_one("#repo-list", OptionList)
            if lst.option_count > 0:
                lst.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)


class GHAExplorerApp(App):
    TITLE = "GHA Explorer"
    CSS = """
    #sidebar {
        width: 28;
        border-right: solid $primary;
        padding: 1 0 1 1;
    }
    #sidebar Label {
        margin-bottom: 1;
        text-style: bold;
    }
    #workflow-select {
        height: 8;
        margin-bottom: 1;
    }
    #job-select {
        height: 1fr;
        margin-bottom: 1;
    }
    #main-area {
        width: 1fr;
    }
    #status-bar {
        height: 1;
        background: $primary-background;
        color: $text;
        padding: 0 1;
    }
    #runs-table {
        height: 1fr;
    }
    #trends-scroll {
        height: 1fr;
    }
    #trends-body {
        height: auto;
    }
    #time-range-bar {
        height: auto;
        padding: 0;
        layout: grid;
        grid-size: 3;
        grid-gutter: 1 1;
    }
    #time-range-bar Button {
        min-width: 4;
        width: 100%;
    }
    #time-range-bar Button.-active {
        background: $accent;
        color: $text;
    }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "switch_repo", "Switch Repo"),
        Binding("1", "tab_trends", "Trends"),
        Binding("2", "tab_runs", "Runs"),
    ]

    runs: reactive[list[RunData]] = reactive(list, init=False)
    current_repo: reactive[str] = reactive("", init=False)
    selected_workflow: reactive[str] = reactive("All workflows", init=False)
    selected_job: reactive[str] = reactive("Pipeline", init=False)
    selected_timerange: reactive[str] = reactive("all", init=False)
    loading: reactive[bool] = reactive(True)

    def __init__(self, initial_repo: str | None = None):
        super().__init__()
        self._initial_repo = initial_repo
        self._progress_done = 0
        self._progress_total = 0
        self._progress_status_msg = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Label("Workflow Filter")
                yield OptionList(id="workflow-select")
                yield Label("Job Filter")
                yield OptionList(id="job-select")
                yield Label("Branch")
                yield Checkbox("develop", id="branch-develop", value=True)
                yield Checkbox("main", id="branch-main", value=True)
                yield Label("Time Range")
                with Horizontal(id="time-range-bar"):
                    for key, label in [("1d", "Day"), ("1m", "Mo"), ("3m", "3Mo"), ("6m", "6Mo"), ("1y", "1Yr"), ("all", "All")]:
                        yield Button(label, id=f"tr-{key}", classes="-active" if key == "all" else "")
                yield Label("Display")
                yield Checkbox("Y-axis from 0", id="y-from-zero", value=False)
            with Vertical(id="main-area"):
                yield Static("Loading...", id="status-bar")
                with TabbedContent(id="tabs", initial="trends"):
                    with TabPane("Trends", id="trends"):
                        with VerticalScroll(id="trends-scroll"):
                            yield Static(id="trends-body")
                    with TabPane("Runs", id="runs-tab"):
                        yield DataTable(id="runs-table")
        yield Footer()

    def on_mount(self) -> None:
        log.info("App mounted")
        self._progress_timer = self.set_interval(0.25, self._poll_progress)
        # Determine initial repo: CLI arg → config → current directory → picker
        repo = self._initial_repo or load_config().get("current_repo") or detect_current_repo()
        if repo:
            self._use_repo(repo)
        else:
            self._update_status_bar("", "Select a repository to begin...")
            self.push_screen(RepoPickerScreen(), self._on_repo_picked)

    def _use_repo(self, repo: str) -> None:
        """Activate the given repo — load its cached data and start the fetch."""
        log.info("Using repo: %s", repo)
        self.current_repo = repo
        self.sub_title = repo
        save_config({"current_repo": repo})
        # Show cached data immediately so the UI is populated right away
        cached = cache_load_all(repo)
        self.runs = cached
        self._populate_sidebar()
        if cached:
            log.info("Showing %d cached runs immediately", len(cached))
            self._update_status_bar(self._cache_status_text(), "Syncing...")
            self.set_timer(0.1, self._render_all_tabs)
        else:
            self._update_status_bar(f"{repo}: no cache", "First load — fetching from GitHub...")
            self._render_all_tabs()  # render empty state
        self._start_fetch()

    def _on_repo_picked(self, repo: str | None) -> None:
        if not repo:
            if not self.current_repo:
                # No repo selected and none active — nothing to show, exit
                self.exit()
            return
        self._use_repo(repo)

    def action_switch_repo(self) -> None:
        self.push_screen(RepoPickerScreen(), self._on_repo_picked)

    def _update_status_bar(self, cache_msg: str = "", activity_msg: str = "") -> None:
        """Update the always-visible status bar."""
        try:
            status = self.query_one("#status-bar", Static)
            parts = []
            if cache_msg:
                parts.append(cache_msg)
            if activity_msg:
                parts.append(activity_msg)
            status.update(" | ".join(parts) if parts else "Loading...")
        except Exception:
            pass

    def _cache_status_text(self) -> str:
        """Generate the cache portion of the status bar."""
        runs = self.runs
        if not runs:
            return "No cached data"
        oldest = runs[0].created_at.strftime("%Y-%m-%d")
        newest = runs[-1].created_at.strftime("%Y-%m-%d")
        return f"{len(runs)} runs ({oldest} → {newest})"

    def _poll_progress(self) -> None:
        """Poll for progress updates from the worker thread."""
        if not self.loading:
            return
        try:
            total = self._progress_total
            done = self._progress_done
            status_msg = self._progress_status_msg
            cache_text = self._cache_status_text()

            if total > 0:
                pct = min(100, int(done / total * 100))
                filled = int(min(done, total) / total * 30)
                bar = "━" * filled + "╌" * (30 - filled)
                self._update_status_bar(cache_text, f"Fetching runs  {bar}  {pct}% ({done}/{total})")
            elif status_msg:
                self._update_status_bar(cache_text, status_msg)
            else:
                self._update_status_bar(cache_text, "Syncing...")
        except Exception:
            pass

    @work(thread=True)
    def fetch_data(self) -> list[RunData]:
        """Fetch data incrementally in background thread."""
        repo = self.current_repo
        if not repo:
            return []

        def _on_progress(done: int, total: int) -> None:
            self._progress_done = done
            self._progress_total = total

        def _on_status(msg: str) -> None:
            self._progress_status_msg = msg

        data = fetch_incremental(
            repo,
            on_progress=_on_progress,
            on_status=_on_status,
        )
        log.info("Fetch complete for %s: %d total runs", repo, len(data))
        return data

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Handle worker completion — runs on the event loop thread (safe for UI)."""
        if event.worker.name != "fetch_data":
            return
        if event.state == WorkerState.SUCCESS:
            log.info("Worker succeeded, rendering data")
            self._data_loaded(event.worker.result)
        elif event.state == WorkerState.ERROR:
            log.error("Worker failed: %s", event.worker.error)
            self._data_error(str(event.worker.error))

    def _data_loaded(self, data: list[RunData]) -> None:
        try:
            log.info("Rendering %d runs", len(data))
            self.runs = data
            self.loading = False
            # Show the final status from the fetch (e.g. "+8 new runs fetched")
            activity = self._progress_status_msg or "Up to date"
            self._update_status_bar(self._cache_status_text(), activity)
            self._populate_sidebar()
            self.set_timer(0.1, self._render_all_tabs)
        except Exception:
            log.exception("Error in _data_loaded")

    def _render_all_tabs(self) -> None:
        try:
            t0 = time.monotonic()
            self._render_trends()
            log.info("Trends rendered in %.2fs", time.monotonic() - t0)
            t1 = time.monotonic()
            self._render_runs_table()
            log.info("Runs table rendered in %.2fs", time.monotonic() - t1)
            log.info("All tabs rendered in %.2fs", time.monotonic() - t0)
        except Exception:
            log.exception("Error in _render_all_tabs")

    def _data_error(self, error: str) -> None:
        try:
            self.loading = False
            self._update_status_bar(
                self._cache_status_text(),
                f"Error: {error[-120:]}  (see {LOG_FILE.name})",
            )
        except Exception:
            log.exception("Error displaying error message")

    def _populate_sidebar(self) -> None:
        # Workflow list — all discovered workflows + "All workflows" option, sorted by frequency
        workflow_list = self.query_one("#workflow-select", OptionList)
        workflow_list.clear_options()
        workflow_counts: dict[str, int] = {}
        for r in self.runs:
            wf = r.workflow or "(unknown)"
            workflow_counts[wf] = workflow_counts.get(wf, 0) + 1
        workflow_names = ["All workflows"] + sorted(workflow_counts, key=lambda n: -workflow_counts[n])
        for name in workflow_names:
            workflow_list.add_option(Option(name, id=name))
        for i, name in enumerate(workflow_names):
            if name == self.selected_workflow:
                workflow_list.highlighted = i
                break

        self._populate_job_list()

    def _populate_job_list(self) -> None:
        """Populate job filter, scoped to the currently selected workflow."""
        job_list = self.query_one("#job-select", OptionList)
        job_list.clear_options()
        selected_wf = self.selected_workflow
        job_counts: dict[str, int] = {}
        for r in self.runs:
            if selected_wf != "All workflows" and r.workflow != selected_wf:
                continue
            for j in r.jobs:
                job_counts[j.base_name] = job_counts.get(j.base_name, 0) + 1
        job_names = ["Pipeline"] + sorted(job_counts, key=lambda n: -job_counts[n])
        for name in job_names:
            job_list.add_option(Option(name, id=name))
        # If current selection isn't in the new list, reset to Pipeline
        if self.selected_job not in job_names:
            self.selected_job = "Pipeline"
        for i, name in enumerate(job_names):
            if name == self.selected_job:
                job_list.highlighted = i
                break

    def _content_width(self) -> int:
        return max(self.size.width - 32, 60)

    def _plot_height(self, fraction: float = 0.5, minimum: int = 12) -> int:
        """Compute plot height as a fraction of available terminal height."""
        available = max(self.size.height - 7, 20)
        return max(minimum, int(available * fraction))

    def on_resize(self, event) -> None:
        """Re-render graphs when terminal is resized."""
        if not self.loading and self.runs:
            self._render_all_tabs()

    @on(Button.Pressed, "#time-range-bar Button")
    def on_timerange_pressed(self, event: Button.Pressed) -> None:
        key = event.button.id.removeprefix("tr-")
        self.selected_timerange = key
        for btn in self.query("#time-range-bar Button"):
            btn.remove_class("-active")
        event.button.add_class("-active")
        try:
            self._render_all_tabs()
        except Exception:
            log.exception("Error rendering for timerange %s", key)

    @on(OptionList.OptionHighlighted, "#workflow-select")
    def on_workflow_selected(self, event: OptionList.OptionHighlighted) -> None:
        if event.option and event.option.id:
            self.selected_workflow = str(event.option.id)
            try:
                self._populate_job_list()
                self._render_all_tabs()
            except Exception:
                log.exception("Error rendering for workflow %s", self.selected_workflow)

    @on(OptionList.OptionHighlighted, "#job-select")
    def on_job_selected(self, event: OptionList.OptionHighlighted) -> None:
        if event.option and event.option.id:
            self.selected_job = str(event.option.id)
            try:
                self._render_trends()
            except Exception:
                log.exception("Error rendering trends for job %s", self.selected_job)

    @on(Checkbox.Changed)
    def on_branch_changed(self, event: Checkbox.Changed) -> None:
        try:
            self._render_trends()
            self._render_runs_table()
        except Exception:
            log.exception("Error rendering after branch filter change")

    def _get_selected_branches(self) -> set[str]:
        branches = set()
        try:
            if self.query_one("#branch-develop", Checkbox).value:
                branches.add("develop")
            if self.query_one("#branch-main", Checkbox).value:
                branches.add("main")
        except Exception:
            pass
        return branches

    def _y_starts_zero(self) -> bool:
        try:
            return self.query_one("#y-from-zero", Checkbox).value
        except Exception:
            return False

    def _empty_state_message(self) -> str:
        if not self.runs:
            if self.loading:
                return "Fetching workflow data from GitHub..."
            return "No cached data. Press [bold]r[/] to refresh."
        branches = self._get_selected_branches()
        if branches and self.selected_timerange != "all":
            return f"No runs for {', '.join(sorted(branches))} in the last {self.selected_timerange}."
        if branches:
            return f"No runs found for {', '.join(sorted(branches))}."
        if self.selected_timerange != "all":
            return f"No runs in the last {self.selected_timerange}."
        return "No matching data. Try adjusting filters."

    def _get_filtered_runs(self) -> list[RunData]:
        runs = self.runs
        if self.selected_workflow != "All workflows":
            runs = [r for r in runs if (r.workflow or "(unknown)") == self.selected_workflow]
        branches = self._get_selected_branches()
        if branches:
            runs = [r for r in runs if r.branch in branches]
        td = TIME_RANGES.get(self.selected_timerange)
        if td is not None and runs:
            cutoff = datetime.now(runs[0].created_at.tzinfo) - td
            runs = [r for r in runs if r.created_at >= cutoff]
        return runs

    # -- Trends tab --

    def _render_trends(self) -> None:
        filtered = self._get_filtered_runs()
        job_name = self.selected_job
        is_pipeline = job_name == "Pipeline"
        if not filtered:
            self.query_one("#trends-body").update(self._empty_state_message())
            return

        w = self._content_width()
        parts: list = []

        if is_pipeline:
            durs = [r.total_duration_s for r in filtered]
        else:
            durs = get_job_durations(filtered, job_name)
        s = stats_summary(durs)
        parts.append(
            f"[bold cyan]{job_name}[/]  "
            f"Avg: [green]{s['avg']}[/]  "
            f"Med: {s['median']}  "
            f"Min: {s['min']}  "
            f"Max: [red]{s['max']}[/]  "
            f"Std: {s['stdev']}  "
            f"({s['count']} runs)"
        )
        parts.append("")

        try:
            def plot_trend(plt):
                labels = []
                values = []
                for run in filtered:
                    if is_pipeline:
                        labels.append(run.created_at.strftime("%m/%d %H:%M"))
                        values.append(run.total_duration_s / 60.0)
                    else:
                        matrix_durs = [j.duration_s for j in run.jobs if j.base_name == job_name]
                        if matrix_durs:
                            labels.append(run.created_at.strftime("%m/%d %H:%M"))
                            values.append(mean(matrix_durs) / 60.0)
                if not values:
                    plt.title(f"No data for {job_name}")
                    return
                plt.plot(list(range(len(values))), values, marker="braille", color="cyan", label="duration")
                if len(values) >= 3:
                    window = 3
                    rolling = [mean(values[max(0, i - window + 1):i + 1]) for i in range(len(values))]
                    plt.plot(list(range(len(values))), rolling, marker="braille", color="green", label="rolling avg")
                n = len(labels)
                if n > 10:
                    step = max(1, n // 8)
                    ticks = list(range(0, n, step))
                    plt.xticks(ticks, [labels[i] for i in ticks])
                else:
                    plt.xticks(list(range(n)), labels)
                if self._y_starts_zero() and values:
                    plt.ylim(0, max(values) * 1.05)
                plt.title(f"{job_name} Duration Trend (minutes)")
                plt.ylabel("Minutes")

            parts.append(render_plot(plot_trend, w, self._plot_height(0.5)))
        except Exception:
            log.exception("Error rendering trend plot for %s", job_name)
            parts.append("[red]Error rendering trend chart — see gha_explorer.log[/]")

        # Matrix shard breakdown (not applicable for Pipeline)
        has_matrix = not is_pipeline and any(
            j.matrix_key is not None
            for r in filtered for j in r.jobs if j.base_name == job_name
        )
        if has_matrix:
            parts.append("")
            try:
                def plot_shards(plt):
                    shard_keys = sorted(
                        {
                            j.matrix_key
                            for r in filtered for j in r.jobs
                            if j.base_name == job_name and j.matrix_key is not None
                        },
                        key=_shard_sort_key,
                    )
                    colors = ["cyan", "magenta", "green", "yellow", "red"]
                    # Iterate all runs where the job exists (matrix or not) so every
                    # shard line spans the same x-range; plot 0 where shard didn't run
                    runs_with_job = [r for r in filtered if any(j.base_name == job_name for j in r.jobs)]
                    all_ys_max = 0.0
                    for ci, key in enumerate(shard_keys):
                        ys = []
                        for r in runs_with_job:
                            sd = [j.duration_s for j in r.jobs if j.base_name == job_name and j.matrix_key == key]
                            ys.append(sd[0] / 60.0 if sd else 0)
                        if not ys:
                            continue
                        # Apply rolling average to smooth noise
                        if len(ys) >= 5:
                            window = max(3, len(ys) // 40)
                            ys = [mean(ys[max(0, i - window + 1):i + 1]) for i in range(len(ys))]
                        all_ys_max = max(all_ys_max, max(ys))
                        plt.plot(
                            list(range(len(ys))), ys,
                            marker="braille", color=colors[ci % len(colors)],
                            label=f"Shard {key}",
                        )
                    if self._y_starts_zero() and all_ys_max > 0:
                        plt.ylim(0, all_ys_max * 1.05)
                    plt.title(f"{job_name} — Shard Breakdown (minutes, rolling avg)")
                    plt.ylabel("Minutes")

                parts.append(render_plot(plot_shards, w, self._plot_height(0.4)))
            except Exception:
                log.exception("Error rendering shard plot for %s", job_name)
                parts.append("[red]Error rendering shard chart — see gha_explorer.log[/]")

        # Key step durations (not applicable for Pipeline)
        if is_pipeline:
            self.query_one("#trends-body").update(RichGroup(*parts))
            return
        try:
            available_steps: set[str] = set()
            for r in filtered:
                for j in r.jobs:
                    if j.base_name == job_name:
                        for st in j.steps:
                            available_steps.add(st.name)

            matched_steps = []
            for sn in sorted(available_steps):
                sd = get_step_durations(filtered, job_name, sn)
                if sd and mean(sd) > 5:
                    matched_steps.append(sn)

            if matched_steps:
                parts.append("")

                def plot_steps(plt):
                    step_labels = []
                    step_avgs = []
                    for sn in matched_steps:
                        sd = get_step_durations(filtered, job_name, sn)
                        if sd:
                            step_labels.append(sn[:40])
                            step_avgs.append(mean(sd))
                    if step_avgs:
                        plt.bar(step_labels, step_avgs, color="cyan")
                        if self._y_starts_zero():
                            plt.ylim(0, max(step_avgs) * 1.05)
                        plt.title(f"{job_name} — Avg Step Durations (seconds)")
                        plt.ylabel("Seconds")

                parts.append(render_plot(plot_steps, w, self._plot_height(0.35, 10)))
        except Exception:
            log.exception("Error rendering step durations for %s", job_name)
            parts.append("[red]Error rendering steps chart — see gha_explorer.log[/]")

        self.query_one("#trends-body").update(RichGroup(*parts))

    # -- Runs tab --

    def _render_runs_table(self) -> None:
        try:
            table = self.query_one("#runs-table", DataTable)
            table.clear(columns=True)

            filtered = self._get_filtered_runs()
            if not filtered:
                return

            # Dynamically discover job columns from the data, sorted by frequency
            job_counts: dict[str, int] = {}
            for r in filtered:
                for j in r.jobs:
                    job_counts[j.base_name] = job_counts.get(j.base_name, 0) + 1
            job_cols = sorted(job_counts, key=lambda n: -job_counts[n])

            table.add_columns("Date", "Branch", "Title", "Total", *job_cols)

            for run in reversed(filtered):
                def _avg(base_name, r=run):
                    durs = [j.duration_s for j in r.jobs if j.base_name == base_name]
                    return fmt_duration(mean(durs)) if durs else "-"

                table.add_row(
                    run.created_at.strftime("%Y-%m-%d %H:%M"),
                    run.branch,
                    run.title[:45],
                    fmt_duration(run.total_duration_s),
                    *[_avg(col) for col in job_cols],
                )
        except Exception:
            log.exception("Error rendering runs table")

    # -- Actions --

    def _start_fetch(self) -> None:
        self.loading = True
        self._progress_done = 0
        self._progress_total = 0
        self._progress_status_msg = ""
        self._update_status_bar(self._cache_status_text(), "Refreshing...")
        self.fetch_data()

    def action_refresh(self) -> None:
        self._start_fetch()

    def action_tab_trends(self) -> None:
        self.query_one(TabbedContent).active = "trends"

    def action_tab_runs(self) -> None:
        self.query_one(TabbedContent).active = "runs-tab"


def main():
    import argparse

    parser = argparse.ArgumentParser(description="GHA Explorer — a TUI for GitHub Actions timing.")
    parser.add_argument(
        "--repo",
        help="owner/name of the repo to explore. If omitted, uses the last-selected repo or prompts.",
    )
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Starting GHA Explorer TUI")
    try:
        GHAExplorerApp(initial_repo=args.repo).run()
    except Exception:
        log.exception("Unhandled exception — app crashed")
        print(f"\nGHA Explorer crashed. See log: {LOG_FILE}")
        raise
    finally:
        log.info("App exited")


if __name__ == "__main__":
    main()
