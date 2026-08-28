#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "textual>=8.0,<9",
#     "plotext>=6.0.0,<7",
#     "rich>=13.0.0",
# ]
# ///
"""
GHA Explorer — a TUI for exploring GitHub Actions workflow timing.

Usage:
    uvx gha-explorer [--repo owner/name] [--theme NAME]
    # or from a checkout:  ./gha_explorer.py

Incrementally fetches successful workflow runs for a repo straight from the
GitHub REST API (no `gh` needed — see `resolve_token()` for how it signs in),
caching results in SQLite. On subsequent launches, cached data displays
immediately and only new runs are fetched from the API.

Data (cache.db, log) lives in a per-user directory — see `data_dir()`; override
with GHA_EXPLORER_HOME. INFO+ log lines also stream to the in-app Status tab.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from bisect import bisect_left
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median, stdev

from rich.console import Group as RichGroup
from rich.style import Style as RichStyle
from rich.text import Text as RichText

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import (
    Button,
    Checkbox,
    ContentSwitcher,
    DataTable,
    Footer,
    Input,
    Label,
    OptionList,
    RadioButton,
    RadioSet,
    RichLog,
    SelectionList,
    Static,
    Tab,
    Tabs,
    TextArea,
)
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection
from textual.worker import Worker, WorkerState, get_current_worker

# ---------------------------------------------------------------------------
# Data directory — where cache.db and the log live
# ---------------------------------------------------------------------------

__version__ = "0.1.2"

DB_FILENAME = "gha-explorer.db"      # runs cache + settings + notes
LEGACY_DB_FILENAME = "cache.db"      # original (pre-release) name, renamed on first launch
PATHS_FILENAME = "paths.json"        # {"db_path": ...} — can't live inside the DB it points at


def data_dir() -> Path:
    """Per-user data directory.

    1. $GHA_EXPLORER_HOME if set.
    2. The script's own directory if a cache.db already sits there (a checkout
       that has been used before — keeps existing setups working).
    3. Platform default: $XDG_DATA_HOME/gha-explorer (~/.local/share/gha-explorer)
       or %LOCALAPPDATA%/gha-explorer on Windows. This is what `uvx` installs use,
       since the package itself lives in an ephemeral environment.
    """
    env = os.environ.get("GHA_EXPLORER_HOME")
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve().parent
    if (here / DB_FILENAME).exists() or (here / LEGACY_DB_FILENAME).exists():
        return here
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / "gha-explorer"


DATA_DIR = data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
PATHS_FILE = DATA_DIR / PATHS_FILENAME


def _read_paths() -> dict:
    try:
        return json.loads(PATHS_FILE.read_text(encoding="utf-8")) if PATHS_FILE.exists() else {}
    except Exception:
        return {}


def resolve_db_path() -> Path:
    """$GHA_EXPLORER_DB, else paths.json, else <data dir>/gha-explorer.db.

    A pre-release cache.db in the data dir is renamed to the new name (with its
    -wal/-shm siblings) the first time no gha-explorer.db exists.
    """
    env = os.environ.get("GHA_EXPLORER_DB")
    if env:
        return Path(env).expanduser()
    stored = _read_paths().get("db_path")
    if stored:
        return Path(stored).expanduser()
    default = DATA_DIR / DB_FILENAME
    legacy = DATA_DIR / LEGACY_DB_FILENAME
    if not default.exists() and legacy.exists():
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(legacy) + suffix)
            if src.exists():
                src.rename(str(default) + suffix)
    return default


def set_db_path(path: Path) -> None:
    """Remember a custom DB location and switch to it (connections reconnect lazily)."""
    global CACHE_DB
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    PATHS_FILE.write_text(json.dumps({"db_path": str(path)}, indent=2), encoding="utf-8")
    CACHE_DB = path


def reveal_in_file_manager(path: Path) -> None:
    """Show the file in Finder / Explorer / the desktop's file manager."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", f"/select,{path}"])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


FILE_MANAGER_NAME = {"darwin": "Finder", "win32": "Explorer"}.get(sys.platform, "file manager")

# ---------------------------------------------------------------------------
# Logging — file (DEBUG) + in-memory ring buffer (INFO) drained by the UI
# ---------------------------------------------------------------------------

LOG_FILE = DATA_DIR / "gha_explorer.log"

logging.basicConfig(
    filename=str(LOG_FILE),
    encoding="utf-8",  # log lines contain → — ·; Windows' default code page can't encode them
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gha_explorer")


class UILogHandler(logging.Handler):
    """Buffers log records so the Status tab can display them.

    deque.append/popleft are thread-safe, so worker threads can log freely and
    the UI drains on its own timer — no cross-thread widget access.
    """

    def __init__(self, maxlen: int = 1000) -> None:
        super().__init__(level=logging.INFO)
        self.records: deque[tuple[datetime, str, str]] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(
                (datetime.fromtimestamp(record.created), record.levelname, record.getMessage())
            )
        except Exception:
            pass


UI_LOG = UILogHandler()
logging.getLogger().addHandler(UI_LOG)

# ---------------------------------------------------------------------------
# Theme — dark with lavender accents. Plot colors derive from the active
# Textual theme so `--theme catppuccin-mocha` etc. restyle the charts too.
# ---------------------------------------------------------------------------

LAVENDER = {
    "background": "#14121C",
    "surface": "#1B1826",
    "panel": "#242034",
    "primary": "#B4A3F7",
    "secondary": "#8B7AD9",
    "accent": "#D4C4FF",
    "foreground": "#E8E4F3",
    "success": "#86D9A6",
    "warning": "#F0C674",
    "error": "#F07178",
}

GHA_THEME = Theme(
    name="gha-lavender",
    dark=True,
    **LAVENDER,
    variables={
        "footer-key-foreground": LAVENDER["accent"],
        "footer-description-foreground": "#A9A3BD",
        "block-cursor-background": LAVENDER["primary"],
        "block-cursor-foreground": LAVENDER["background"],
        "block-cursor-text-style": "bold",
        "block-cursor-blurred-background": "#3A3450",
        "block-cursor-blurred-foreground": LAVENDER["foreground"],
        "border": LAVENDER["secondary"],
        "border-blurred": "#3A3450",
        "scrollbar": "#3A3450",
        "scrollbar-hover": LAVENDER["secondary"],
        "scrollbar-active": LAVENDER["primary"],
        "input-cursor-background": LAVENDER["primary"],
        "input-selection-background": "#8B7AD9 35%",
        "link-color": LAVENDER["accent"],
    },
)


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


@dataclass
class PlotPalette:
    """RGB tuples for plotext, derived from the active Textual theme."""

    axes: tuple[int, int, int]
    series: list[tuple[int, int, int]]
    primary_hex: str
    success_hex: str
    error_hex: str
    warning_hex: str
    muted_hex: str

    @classmethod
    def from_theme(cls, theme: Theme) -> PlotPalette:
        primary = theme.primary
        accent = theme.accent or primary
        success = theme.success or "#86D9A6"
        warning = theme.warning or "#F0C674"
        error = theme.error or "#F07178"
        secondary = theme.secondary or primary
        return cls(
            axes=_hex_to_rgb("#7E7599") if theme.name == GHA_THEME.name else _hex_to_rgb(secondary),
            series=[_hex_to_rgb(c) for c in (accent, success, warning, "#7DC4F0", error, secondary)],
            primary_hex=primary,
            success_hex=success,
            error_hex=error,
            warning_hex=warning,
            muted_hex="#A9A3BD",
        )


DEFAULT_PALETTE = PlotPalette.from_theme(GHA_THEME)

# ---------------------------------------------------------------------------
# Sync stats — shared between the fetch thread and the UI
# ---------------------------------------------------------------------------


@dataclass
class SyncStats:
    """Live counters for the current sync. Written by worker threads, read by the UI.

    Individual attribute writes are atomic under the GIL; `snapshot()` takes the
    lock so the UI sees a consistent view.
    """

    phase: str = "idle"  # idle | cache | forward | details | backfill | done | error | rate-limited
    message: str = ""
    done: int = 0
    total: int = 0
    api_calls: int = 0
    api_errors: int = 0
    rate_limit_retries: int = 0
    new_runs: int = 0
    windows_done: int = 0
    current_window: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    last_error: str = ""
    rate_limit: dict | None = None  # {"limit", "remaining", "reset", "used"} from gh api rate_limit
    rate_limit_checked_at: float | None = None
    # First-sync panel: which steps ran, how long each took, what's left to estimate
    first_load: bool = False
    phase_started_at: float | None = None
    step_elapsed: dict = field(default_factory=dict)  # phase -> seconds spent so far
    listed_runs: int = 0        # runs found by the listing phase (this sync)
    backfill_total: int = 0     # estimated number of 90-day windows to walk
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def reset_for_sync(self) -> None:
        with self._lock:
            self.phase = "cache"
            self.message = ""
            self.done = self.total = 0
            self.new_runs = self.windows_done = 0
            self.current_window = ""
            self.started_at = time.monotonic()
            self.phase_started_at = self.started_at
            self.finished_at = None
            self.last_error = ""
            self.first_load = False
            self.step_elapsed = {}
            self.listed_runs = 0
            self.backfill_total = 0

    def set_phase(self, phase: str, message: str = "") -> None:
        with self._lock:
            now = time.monotonic()
            if self.phase_started_at is not None:
                self.step_elapsed[self.phase] = self.step_elapsed.get(self.phase, 0.0) + (now - self.phase_started_at)
            if phase != self.phase:
                self.done = self.total = 0  # progress counters belong to a phase
            self.phase = phase
            self.message = message
            self.phase_started_at = now
        log.info("%s", message or phase)

    def set_progress(self, done: int, total: int) -> None:
        with self._lock:
            self.done, self.total = done, total

    def snapshot(self) -> dict:
        with self._lock:
            snap = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
            snap["step_elapsed"] = dict(self.step_elapsed)
            return snap


STATS = SyncStats()

# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

MAX_WORKERS = 8
RUN_LIST_LIMIT = 1000  # hard cap of `gh run list --limit`
BACKFILL_WINDOW_DAYS = 90
MAX_BACKFILL_WINDOWS = 80  # ~20 years; safety valve only
GAP_DAYS = 7  # a hole this long between cached runs is checked against the API once

TIME_RANGES: dict[str, timedelta | None] = {
    "1d": timedelta(days=1),
    "1m": timedelta(days=30),
    "3m": timedelta(days=90),
    "6m": timedelta(days=180),
    "1y": timedelta(days=365),
    "all": None,
}

CACHE_DB = resolve_db_path()
CONFIG_FILE = DATA_DIR / "config.json"  # legacy; imported into the settings table once

_db_local = threading.local()


def _init_schema(conn: sqlite3.Connection) -> None:
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_meta (
            repo TEXT PRIMARY KEY,
            backfill_complete INTEGER NOT NULL DEFAULT 0,
            last_sync_at TEXT
        )
    """)
    # Sticky UI state: scope is a repo name (per-repo filters) or GLOBAL_SCOPE
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            scope TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (scope, key)
        )
    """)
    # Notes pinned to a point in time, drawn as vertical markers on the trend charts
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL,
            at TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_repo ON notes(repo, at)")
    # notes.jobs: NULL = all jobs, else JSON list of job names the note applies to
    cols = {row[1] for row in conn.execute("PRAGMA table_info(notes)").fetchall()}
    if "jobs" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN jobs TEXT")
    if "color" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN color TEXT")  # hex; NULL = default (theme error red)
    conn.commit()


def _cache_conn() -> sqlite3.Connection:
    """One connection per thread, WAL mode, schema ensured once per connection.
    Reconnects if the DB path changed since this thread last connected."""
    conn = getattr(_db_local, "conn", None)
    if conn is not None and getattr(_db_local, "path", None) != str(CACHE_DB):
        try:
            conn.close()
        except Exception:
            pass
        conn = None
    if conn is None:
        CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(CACHE_DB), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        _init_schema(conn)
        _db_local.conn = conn
        _db_local.path = str(CACHE_DB)
    return conn


def switch_db(new_path: Path) -> str:
    """Point the app at another DB file. If it doesn't exist yet, the current DB is
    copied there (after a WAL checkpoint) so renaming/moving is painless. The old
    file is left in place. Returns a short description of what happened."""
    new_path = new_path.expanduser()
    if new_path.exists() and new_path.resolve() == CACHE_DB.resolve():
        return "Already using that database."
    copied = False
    if not new_path.exists() and CACHE_DB.exists():
        try:
            _cache_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            log.debug("checkpoint before copy failed", exc_info=True)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CACHE_DB, new_path)
        copied = True
    old = CACHE_DB
    set_db_path(new_path)
    log.info("Switched database %s -> %s (%s)", old, new_path, "copied" if copied else "existing file")
    return (f"Copied the current database to {new_path} and switched to it. The old file at {old} was left in place."
            if copied else f"Switched to the existing database at {new_path}.")


def cache_get_jobs(run_id: int) -> tuple[dict, list[dict]] | None:
    """Look up by run_id only — run IDs are globally unique in GitHub."""
    row = _cache_conn().execute(
        "SELECT raw_run, raw_jobs FROM run_jobs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row:
        return json.loads(row[0]), json.loads(row[1])
    return None


def cache_put_jobs(repo: str, run_id: int, raw_run: dict, raw_jobs: list[dict]) -> None:
    conn = _cache_conn()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO run_jobs (repo, run_id, raw_run, raw_jobs, fetched_at) VALUES (?, ?, ?, ?, ?)",
            (repo, run_id, json.dumps(raw_run), json.dumps(raw_jobs), datetime.now(timezone.utc).isoformat()),
        )


def cache_get_all_ids(repo: str) -> set[int]:
    rows = _cache_conn().execute("SELECT run_id FROM run_jobs WHERE repo = ?", (repo,)).fetchall()
    return {row[0] for row in rows}


def cache_load_all(repo: str) -> list[RunData]:
    """Load all cached runs for a repo from SQLite, oldest first."""
    rows = _cache_conn().execute(
        "SELECT raw_run, raw_jobs, fetched_at FROM run_jobs WHERE repo = ?", (repo,)
    ).fetchall()
    runs = []
    for raw_run_json, raw_jobs_json, fetched_at in rows:
        try:
            run = build_run_data(json.loads(raw_run_json), json.loads(raw_jobs_json))
            try:
                run.fetched_at = datetime.fromisoformat(fetched_at)
                if run.fetched_at.tzinfo is None:
                    run.fetched_at = run.fetched_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
            runs.append(run)
        except Exception:
            log.debug("Skipping corrupt cache row", exc_info=True)
    runs.sort(key=lambda r: r.created_at)
    return runs


def cache_summary(repo: str) -> dict:
    """Row counts + date span for the status card."""
    conn = _cache_conn()
    repo_rows, oldest, newest = conn.execute(
        "SELECT COUNT(*), MIN(json_extract(raw_run, '$.createdAt')), MAX(json_extract(raw_run, '$.createdAt')) "
        "FROM run_jobs WHERE repo = ?",
        (repo,),
    ).fetchone()
    total_rows, repos = conn.execute("SELECT COUNT(*), COUNT(DISTINCT repo) FROM run_jobs").fetchone()
    meta = conn.execute(
        "SELECT backfill_complete, last_sync_at FROM sync_meta WHERE repo = ?", (repo,)
    ).fetchone()
    notes_count = conn.execute("SELECT COUNT(*) FROM notes WHERE repo = ?", (repo,)).fetchone()[0]
    size = 0
    for suffix in ("", "-wal"):
        p = Path(str(CACHE_DB) + suffix)
        if p.exists():
            size += p.stat().st_size
    return {
        "repo_rows": repo_rows or 0,
        "oldest": (oldest or "")[:10],
        "newest": (newest or "")[:10],
        "total_rows": total_rows or 0,
        "repos": repos or 0,
        "db_bytes": size,
        "backfill_complete": bool(meta and meta[0]),
        "last_sync_at": (meta[1] if meta else None),
        "notes": notes_count,
    }


def meta_get_backfill_complete(repo: str) -> bool:
    row = _cache_conn().execute(
        "SELECT backfill_complete FROM sync_meta WHERE repo = ?", (repo,)
    ).fetchone()
    return bool(row and row[0])


def meta_set(repo: str, backfill_complete: bool | None = None) -> None:
    conn = _cache_conn()
    with conn:
        conn.execute(
            "INSERT INTO sync_meta (repo, backfill_complete, last_sync_at) VALUES (?, 0, NULL) "
            "ON CONFLICT(repo) DO NOTHING",
            (repo,),
        )
        conn.execute("UPDATE sync_meta SET last_sync_at = ? WHERE repo = ?",
                     (datetime.now(timezone.utc).isoformat(), repo))
        if backfill_complete is not None:
            conn.execute("UPDATE sync_meta SET backfill_complete = ? WHERE repo = ?",
                         (int(backfill_complete), repo))


@dataclass
class StepTiming:
    name: str
    duration_s: float


@dataclass
class JobTiming:
    name: str
    base_name: str  # display name: the raw job name, or its group name once grouped
    matrix_key: str | None  # member name when the job belongs to a multi-member group, else None
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
    fetched_at: datetime | None = None  # when this run entered the cache


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def duration_s(start: str, end: str) -> float:
    return max(0, (parse_dt(end) - parse_dt(start)).total_seconds())


def _shard_sort_key(key: str) -> list[object]:
    """Natural sort: 'Playwright (2)' before 'Playwright (10)'."""
    return [(0, int(part)) if part.isdigit() else (1, part.lower()) for part in re.split(r"(\d+)", key)]


# ---------------------------------------------------------------------------
# GitHub REST API client + sign-in (stdlib urllib; `gh` is optional)
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"
# Public client ID of the "GHA Explorer" OAuth app. Device flow only — there is
# no client secret, so it is safe to ship in source.
GITHUB_OAUTH_CLIENT_ID = "Ov23liOAZ82yZCtwbZOr"
GITHUB_OAUTH_SCOPE = "repo"  # needed to read Actions data on private repos
AUTH_FILE = DATA_DIR / "auth.json"
USER_AGENT = f"gha-explorer/{__version__}"


class GitHubAPIError(Exception):
    """Any failed API call (HTTP error, network error, exhausted rate-limit retries)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AuthError(GitHubAPIError):
    """401 — no token, or the token is invalid/revoked. The UI reacts by signing in again."""


@dataclass
class AuthState:
    token: str | None = None
    source: str = "none"  # "env", "saved login", "gh CLI" or "none"
    login: str = ""


AUTH = AuthState()


def _read_auth_file() -> dict:
    try:
        return json.loads(AUTH_FILE.read_text(encoding="utf-8")) if AUTH_FILE.exists() else {}
    except Exception:
        log.debug("Could not read %s", AUTH_FILE, exc_info=True)
        return {}


def save_token(token: str, login: str = "") -> None:
    """Persist a token from the in-app sign-in, readable only by the current user."""
    AUTH_FILE.write_text(json.dumps({"token": token, "login": login}, indent=2), encoding="utf-8")
    try:
        os.chmod(AUTH_FILE, 0o600)  # no-op on Windows, where %LOCALAPPDATA% is already per-user
    except OSError:
        log.debug("chmod on %s failed", AUTH_FILE, exc_info=True)
    AUTH.token, AUTH.source, AUTH.login = token, "saved login", login


def clear_saved_token() -> bool:
    existed = AUTH_FILE.exists()
    if existed:
        AUTH_FILE.unlink()
    if AUTH.source == "saved login":
        AUTH.token, AUTH.source, AUTH.login = None, "none", ""
    return existed


_gh_token_cache: tuple[bool, str | None] = (False, None)  # (probed, token)


def gh_cli_token(refresh: bool = False) -> str | None:
    """The GitHub CLI's token, if `gh` is installed and signed in. Probed once per
    process (it spawns a subprocess); `refresh=True` re-probes."""
    global _gh_token_cache
    if _gh_token_cache[0] and not refresh:
        return _gh_token_cache[1]
    token: str | None = None
    if shutil.which("gh") is not None:
        try:
            result = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                token = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            log.debug("gh auth token failed", exc_info=True)
    _gh_token_cache = (True, token)
    return token


def gh_cli_available() -> bool:
    return gh_cli_token() is not None


AUTH_MODES = ("gh", "rest")


def auth_mode() -> str:
    """Settings → General → GitHub access. "gh" reuses the GitHub CLI's login, "rest"
    uses the built-in sign-in. Unset means: gh when it's available, rest otherwise."""
    explicit = settings_get(GLOBAL_SCOPE, "auth_mode")
    if explicit in AUTH_MODES:
        return explicit
    return "gh" if gh_cli_available() else "rest"


def resolve_token() -> AuthState:
    """Find a token without asking. $GH_TOKEN / $GITHUB_TOKEN always win; then the
    `gh` CLI's login (when the auth mode is "gh"), then the saved in-app login.
    Sets and returns AUTH; AUTH.token is None if nothing was found."""
    env = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if env:
        AUTH.token, AUTH.source, AUTH.login = env.strip(), "env", ""
    elif auth_mode() == "gh" and (cli := gh_cli_token()):
        AUTH.token, AUTH.source, AUTH.login = cli, "gh CLI", ""
    elif (saved := _read_auth_file()).get("token"):
        AUTH.token, AUTH.source, AUTH.login = saved["token"], "saved login", saved.get("login", "")
    else:
        AUTH.token, AUTH.source, AUTH.login = None, "none", ""
    log.info("Auth source: %s", AUTH.source)
    return AUTH


def auth_status_text() -> str:
    """One line for Settings / Status describing the credentials in use."""
    if AUTH.source == "env":
        return "Using $GH_TOKEN / $GITHUB_TOKEN from the environment (overrides the setting below)."
    if AUTH.source == "gh CLI":
        return "Using the GitHub CLI's login (gh auth token)."
    if AUTH.source == "saved login":
        who = f" as {AUTH.login}" if AUTH.login else ""
        return f"Signed in with the built-in login{who}. Token stored in {AUTH_FILE}."
    return "Not signed in — the app will ask on the next sync, or use Sign in… below."


def _api_request(url: str, params: dict | None = None, *, retries: int = 4, count: bool = True,
                 token: str | None = None) -> tuple[object, object]:
    """GET one URL. Returns (parsed JSON, response headers).

    Retries with backoff on network errors and on secondary rate limits (Retry-After);
    waits out a primary rate limit only if it resets within a couple of minutes.
    Raises AuthError on 401 and GitHubAPIError for anything else that fails.
    """
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
    token = token if token is not None else AUTH.token
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
                if count:
                    STATS.api_calls += 1
                return (json.loads(body) if body.strip() else None), resp.headers
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read().decode("utf-8", "replace")
            try:
                message = json.loads(body).get("message", body)
            except Exception:
                message = body
            message = (message or f"HTTP {status}").strip()
            if status == 401:
                STATS.api_errors += 1
                STATS.last_error = f"401 {message}"
                raise AuthError(f"GitHub rejected the token (401): {message}", status) from None
            retry_after = exc.headers.get("Retry-After")
            remaining = exc.headers.get("X-RateLimit-Remaining")
            rate_limited = status in (403, 429) and (
                retry_after is not None or remaining == "0" or "rate limit" in message.lower()
            )
            if rate_limited and attempt < retries:
                if retry_after:
                    delay = min(int(float(retry_after)), 120)
                elif remaining == "0":
                    reset_in = int(exc.headers.get("X-RateLimit-Reset", "0")) - time.time()
                    if reset_in > 120:
                        STATS.api_errors += 1
                        STATS.last_error = f"rate limited, resets in {int(reset_in // 60)}m"
                        raise GitHubAPIError(
                            f"API rate limit exhausted — resets in {int(reset_in // 60)} min", status
                        ) from None
                    delay = max(1, int(reset_in) + 1)
                else:
                    delay = 2 ** attempt
                STATS.rate_limit_retries += 1
                log.warning("Rate limited (%d), retrying in %ds (attempt %d/%d): %s",
                            status, delay, attempt + 1, retries, url.split("?")[0])
                time.sleep(delay)
                continue
            STATS.api_errors += 1
            STATS.last_error = f"{status} {message}"[-200:]
            log.error("API call failed (HTTP %d): %s\n%s", status, url, message)
            raise GitHubAPIError(f"HTTP {status}: {message}", status) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < retries:
                delay = 2 ** attempt
                log.warning("Network error, retrying in %ds: %s", delay, exc)
                time.sleep(delay)
                continue
            STATS.api_errors += 1
            STATS.last_error = f"network: {exc}"[-200:]
            log.error("API call failed (network): %s\n%s", url, exc)
            raise GitHubAPIError(f"Network error: {exc}") from None
    raise GitHubAPIError("unreachable")  # keeps type checkers happy


def _next_link(headers) -> str | None:
    link = headers.get("Link") if headers is not None else None
    if not link:
        return None
    for part in link.split(","):
        url, _, rel = part.partition(";")
        if 'rel="next"' in rel:
            return url.strip().strip("<>")
    return None


def api_get(path: str, params: dict | None = None, **kw) -> object:
    return _api_request(f"{GITHUB_API}/{path.lstrip('/')}", params, **kw)[0]


def api_get_all(path: str, params: dict | None = None, *, list_key: str | None = None,
                max_items: int | None = None, on_page=None) -> list:
    """Follow `Link: rel=next` pagination. `list_key` unwraps envelope responses
    like {"workflow_runs": [...]}; `max_items` stops early. `on_page(fetched, total)`
    is called after each page with the envelope's `total_count` (None if absent)."""
    url: str | None = f"{GITHUB_API}/{path.lstrip('/')}"
    params = {**(params or {}), "per_page": 100}
    items: list = []
    while url:
        data, headers = _api_request(url, params)
        params = None  # the next link already carries the query string
        page = data.get(list_key, []) if list_key else data
        items.extend(page)
        if on_page is not None:
            on_page(len(items), data.get("total_count") if list_key else None)
        if max_items is not None and len(items) >= max_items:
            return items[:max_items]
        if list_key and not page:
            break
        url = _next_link(headers)
    return items


def verify_token(token: str) -> str:
    """Return the login for `token`, raising AuthError if GitHub rejects it."""
    user = _api_request(f"{GITHUB_API}/user", token=token, retries=1, count=False)[0]
    return str((user or {}).get("login", ""))


# -- OAuth device flow (https://docs.github.com/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#device-flow)

def _oauth_post(url: str, data: dict) -> dict:
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode(), method="POST",
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def device_flow_start() -> dict:
    """Ask GitHub for a user code. Returns {device_code, user_code, verification_uri, interval, expires_in}."""
    return _oauth_post("https://github.com/login/device/code",
                       {"client_id": GITHUB_OAUTH_CLIENT_ID, "scope": GITHUB_OAUTH_SCOPE})


def device_flow_poll(device_code: str) -> dict:
    """One poll. Returns {"access_token": ...} on success or {"error": ...} while pending
    (authorization_pending, slow_down, expired_token, access_denied)."""
    return _oauth_post("https://github.com/login/oauth/access_token", {
        "client_id": GITHUB_OAUTH_CLIENT_ID,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    })


# -- Endpoints used by the app

def fetch_rate_limit() -> dict | None:
    """Core rate-limit bucket. This endpoint doesn't count against the limit."""
    try:
        data = api_get("rate_limit", retries=0, count=False)
        core = data["resources"]["core"]
        STATS.rate_limit = core
        STATS.rate_limit_checked_at = time.monotonic()
        return core
    except Exception:
        log.debug("Could not fetch rate limit", exc_info=True)
        return None


def fetch_user_repos() -> list[dict]:
    """List all repos the user can access — personal, collaborator, and org member."""
    raw = api_get_all("user/repos", {"affiliation": "owner,collaborator,organization_member", "sort": "pushed"})
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
    repos.sort(key=lambda r: r.get("pushedAt", ""), reverse=True)
    return repos


_GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def detect_current_repo() -> str | None:
    """If run from inside a git checkout whose `origin` points at GitHub, return owner/name."""
    if shutil.which("git") is None:
        return None
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        if result.returncode == 0:
            m = _GITHUB_REMOTE_RE.search(result.stdout.strip())
            if m:
                return f"{m.group(1)}/{m.group(2)}"
    except Exception:
        log.debug("Could not detect current repo", exc_info=True)
    return None


def fetch_repo_created_at(repo: str) -> datetime | None:
    """Repo creation date — the floor for backfill. None if unavailable."""
    try:
        return parse_dt(api_get(f"repos/{repo}", retries=1)["created_at"])
    except AuthError:
        raise
    except Exception:
        log.debug("Could not fetch repo createdAt", exc_info=True)
        return None


GLOBAL_SCOPE = "__global__"


def settings_get(scope: str, key: str, default=None):
    row = _cache_conn().execute(
        "SELECT value FROM settings WHERE scope = ? AND key = ?", (scope, key)
    ).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except Exception:
        return default


def settings_set(scope: str, key: str, value) -> None:
    conn = _cache_conn()
    with conn:
        conn.execute(
            "INSERT INTO settings (scope, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value",
            (scope, key, json.dumps(value)),
        )


def migrate_config_json() -> None:
    """One-time import of the old config.json into the settings table."""
    if settings_get(GLOBAL_SCOPE, "current_repo") is not None or not CONFIG_FILE.exists():
        return
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.debug("Could not read legacy config.json", exc_info=True)
        return
    for key in ("current_repo", "sidebar_visible"):
        if key in cfg:
            settings_set(GLOBAL_SCOPE, key, cfg[key])
    log.info("Migrated config.json into the settings table")


@dataclass
class Note:
    id: int
    repo: str
    at: datetime  # timezone-aware UTC
    text: str
    jobs: list[str] | None = None  # None = all jobs; else the job names it applies to
    color: str | None = None  # hex; None = theme default (red)

    @property
    def applies_to(self) -> str:
        return "All jobs" if self.jobs is None else ", ".join(self.jobs)


NOTE_COLORS: list[tuple[str, str]] = [
    ("Red", "#F07178"), ("Amber", "#F0C674"), ("Green", "#86D9A6"), ("Sky", "#7DC4F0"),
    ("Lavender", "#B4A3F7"), ("Pink", "#F5A3D0"), ("White", "#E8E4F3"),
]
DEFAULT_NOTE_COLOR = NOTE_COLORS[0][1]


def notes_list(repo: str) -> list[Note]:
    rows = _cache_conn().execute(
        "SELECT id, repo, at, text, jobs, color FROM notes WHERE repo = ? ORDER BY at", (repo,)
    ).fetchall()
    out = []
    for r in rows:
        jobs = None
        if r[4]:
            try:
                jobs = [str(j) for j in json.loads(r[4])]
            except Exception:
                jobs = None
        out.append(Note(id=r[0], repo=r[1], at=parse_dt(r[2]), text=r[3], jobs=jobs, color=r[5]))
    return out


def notes_add(repo: str, at: datetime, text: str, jobs: list[str] | None = None,
              color: str | None = None) -> int:
    conn = _cache_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO notes (repo, at, text, created_at, jobs, color) VALUES (?, ?, ?, ?, ?, ?)",
            (repo, at.astimezone(timezone.utc).isoformat(), text, datetime.now(timezone.utc).isoformat(),
             json.dumps(jobs) if jobs is not None else None, color),
        )
        return int(cur.lastrowid)


def notes_update(note_id: int, at: datetime, text: str, jobs: list[str] | None, color: str | None) -> None:
    conn = _cache_conn()
    with conn:
        conn.execute(
            "UPDATE notes SET at = ?, text = ?, jobs = ?, color = ? WHERE id = ?",
            (at.astimezone(timezone.utc).isoformat(), text, json.dumps(jobs) if jobs is not None else None,
             color, note_id),
        )


def notes_rename_job(repo: str, old: str, new: str) -> int:
    """Point notes attached to job `old` at `new` (used when a group is renamed). Returns count."""
    changed = 0
    conn = _cache_conn()
    with conn:
        for n in notes_list(repo):
            if n.jobs and old in n.jobs:
                jobs = [new if j == old else j for j in n.jobs]
                conn.execute("UPDATE notes SET jobs = ? WHERE id = ?", (json.dumps(jobs), n.id))
                changed += 1
    return changed


def notes_for_job(notes: list[Note], job: str, cfg: RepoConfig) -> list[Note]:
    """Notes to draw when viewing `job` (an effective, post-group name).

    - jobs=None notes apply everywhere — including jobs that don't exist yet or
      are currently excluded and re-included later.
    - A note attached to a job that has since been grouped shows on the group's
      chart with the source job prepended: "Lint: note text".
    """
    members = set(cfg.job_groups.get(job, []))
    out: list[Note] = []
    for n in notes:
        if n.jobs is None or job in n.jobs:
            out.append(n)
            continue
        sources = [j for j in n.jobs if j in members]
        if sources:
            out.append(replace(n, text=f"{', '.join(sources)}: {n.text}"))
    return out


def notes_delete(note_id: int) -> None:
    conn = _cache_conn()
    with conn:
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))


def time_to_x(times: list[datetime], t: datetime) -> float | None:
    """Map a moment onto the chart's x axis: days since the first plotted run.
    None if outside the plotted range. The axis is proportional to time, so
    equal widths mean equal elapsed time regardless of how many runs happened."""
    if not times or t < times[0] or t > times[-1]:
        return None
    return (t - times[0]).total_seconds() / 86400


def x_span_days(times: list[datetime]) -> float:
    """Width of the x axis in days (never zero, so a single run still plots)."""
    if len(times) < 2:
        return 1 / 24
    return max((times[-1] - times[0]).total_seconds() / 86400, 1 / 24)


TICK_STEPS_DAYS = (1, 2, 7, 14, 30, 60, 90, 180, 365, 730)


def nice_yticks(lo: float, hi: float, target: int = 6) -> tuple[list[float], list[str]]:
    """Round y ticks: a 1/2/5 x 10^n step giving about `target` ticks over [lo, hi].
    Labels drop decimals once the step is a whole unit (so a 10+ minute scale reads
    13, 14, 15 ... rather than 13.2, 14.7), and keep one decimal below that."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / max(1, target)
    magnitude = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    step = next((m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw), 10 * magnitude)
    step = max(step, 0.1)
    start = math.ceil(lo / step) * step
    positions = []
    v = start
    while v <= hi + 1e-9:
        positions.append(round(v, 6))
        v += step
    decimals = 0 if step >= 1 else 1
    return positions, [f"{p:.{decimals}f}" for p in positions]


def date_ticks(times: list[datetime], max_ticks: int = 8) -> tuple[list[float], list[str]]:
    """Ticks at evenly spaced calendar dates (not every N-th run, which bunches
    up on busy days). The first and last dates plotted are always labelled; the
    interior steps use the smallest day step from TICK_STEPS_DAYS that yields
    <= max_ticks, and any interior tick close enough to an end label to collide
    with it is dropped."""
    if not times:
        return [], []
    first, last = times[0], times[-1]
    if len(times) < 2 or first.date() == last.date():
        return [0.0], [first.strftime("%Y-%m-%d")]
    span_days = max(1, (last.date() - first.date()).days)
    step = next((d for d in TICK_STEPS_DAYS if span_days / d <= max_ticks), TICK_STEPS_DAYS[-1])
    x_last = time_to_x(times, last) or 0.0
    positions: list[float] = [0.0]
    labels: list[str] = [first.strftime("%Y-%m-%d")]
    day = datetime.combine(first.date(), datetime.min.time(), tzinfo=first.tzinfo) + timedelta(days=step)
    guard = 0.6 * step  # roughly one label width at this tick density
    while day < last:
        x = time_to_x(times, day)
        if x is not None and x > guard and x < x_last - guard:
            positions.append(x)
            labels.append(day.strftime("%Y-%m-%d"))
        day += timedelta(days=step)
    positions.append(x_last)
    labels.append(last.strftime("%Y-%m-%d"))
    return positions, labels


def note_x_positions(runs: list[RunData], notes: list[Note]) -> list[tuple[float, Note]]:
    """Map note timestamps onto the chart's run-index x axis.

    Charts plot one point per run (x = 0..n-1), not wall-clock time, so a note
    lands between the runs that bracket it, proportionally to elapsed time.
    Notes outside the plotted range are skipped. `runs` must be sorted by time.
    """
    times = [r.created_at for r in runs]
    out: list[tuple[float, Note]] = []
    for n in notes:
        x = time_to_x(times, n.at)
        if x is not None:
            out.append((x, n))
    out.sort(key=lambda t: t[0])
    return out


NOTE_MARKER = "ⓘ"


def attach_note_markers(text: RichText, notes: list[Note], color: str) -> RichText:
    """Make each ⓘ that plotext drew clickable, carrying its note id as Rich meta.

    plotext renders to plain ANSI, so we find the markers afterwards. They were
    drawn left→right in the same order as `notes` (sorted by x).
    """
    lines = text.split("\n")
    found: list[tuple[int, int]] = []
    for row, line in enumerate(lines):
        plain = line.plain
        start = 0
        while (col := plain.find(NOTE_MARKER, start)) >= 0:
            found.append((col, row))
            start = col + 1
    found.sort()
    if len(found) != len(notes):
        log.debug("Note marker mismatch: %d drawn vs %d notes", len(found), len(notes))
    for (col, row), note in zip(found, notes):
        marker = RichText(NOTE_MARKER, style=RichStyle(color=note.color or color, bold=True, meta={"note": note.id}))
        line = lines[row]
        lines[row] = line[:col] + marker + line[col + 1:]
    return RichText("\n").join(lines)


@dataclass
class RepoConfig:
    """Per-repo user configuration (Settings screen). Stored as one JSON blob in `settings`."""

    job_groups: dict[str, list[str]] = field(default_factory=dict)  # group name -> member job names
    excluded_workflows: set[str] = field(default_factory=set)
    excluded_jobs: set[str] = field(default_factory=set)  # effective (post-group) job names
    # Branches are opt-in (new ones appear constantly, so they start excluded);
    # workflows and jobs are opt-out. None = derived from the data at load time and
    # not persisted until the user changes a branch setting (see ensure_branch_inclusion).
    included_branches: set[str] | None = None
    legacy_excluded_branches: set[str] | None = None  # stored by pre-opt-in versions; left as-is
    rolling_window: int = 3
    # Outlier filter (Hampel: rolling median + MAD). Off by default; hides spikes only
    # unless outlier_both is set.
    outlier_filter: bool = False
    outlier_k: float = 3.0
    outlier_window: int = 41
    outlier_both: bool = False

    @property
    def member_to_group(self) -> dict[str, str]:
        return {m: g for g, members in self.job_groups.items() for m in members}

    def to_json(self) -> dict:
        return {
            "job_groups": self.job_groups,
            "excluded_workflows": sorted(self.excluded_workflows),
            "excluded_jobs": sorted(self.excluded_jobs),
            "included_branches": sorted(self.included_branches) if self.included_branches is not None else None,
            "excluded_branches": (sorted(self.legacy_excluded_branches)
                                  if self.legacy_excluded_branches is not None else None),
            "rolling_window": self.rolling_window,
            "outlier_filter": self.outlier_filter,
            "outlier_k": self.outlier_k,
            "outlier_window": self.outlier_window,
            "outlier_both": self.outlier_both,
        }

    @classmethod
    def from_json(cls, data: dict | None) -> RepoConfig:
        data = data or {}
        try:
            window = max(1, min(200, int(data.get("rolling_window", 3))))
        except (TypeError, ValueError):
            window = 3
        def _num(key, default, lo, hi, cast):
            try:
                return max(lo, min(hi, cast(data.get(key, default))))
            except (TypeError, ValueError):
                return default
        return cls(
            job_groups={str(g): [str(m) for m in ms] for g, ms in (data.get("job_groups") or {}).items()},
            excluded_workflows=set(data.get("excluded_workflows") or []),
            excluded_jobs=set(data.get("excluded_jobs") or []),
            included_branches=(set(data["included_branches"]) if data.get("included_branches") is not None else None),
            legacy_excluded_branches=(set(data["excluded_branches"]) if data.get("excluded_branches") is not None else None),
            rolling_window=window,
            outlier_filter=bool(data.get("outlier_filter", False)),
            outlier_k=_num("outlier_k", 3.0, 1.0, 20.0, float),
            # 15 was the original default and is too small to catch a cluster of slow
            # runs (a median filter only sees clusters shorter than half its window)
            outlier_window=41 if data.get("outlier_window") == 15 else _num("outlier_window", 41, 5, 201, int),
            outlier_both=bool(data.get("outlier_both", False)),
        )


def load_repo_config(repo: str) -> RepoConfig:
    return RepoConfig.from_json(settings_get(repo, "repo_config"))


def save_repo_config(repo: str, cfg: RepoConfig) -> None:
    settings_set(repo, "repo_config", cfg.to_json())


def workflow_label(run: RunData) -> str:
    return run.workflow or "(unknown)"


BRANCH_OPT_IN_SINCE = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)  # branches became opt-in


def ensure_branch_inclusion(cfg: RepoConfig, runs: list[RunData]) -> None:
    """Derive the opt-in branch set when none has been saved. In memory only —
    nothing is written until the user changes a branch setting.

    - Repo configured before branches were opt-in (has an exclude list): every
      branch already in the cache before the cutover, minus the excluded ones.
      Branches first fetched after the cutover start excluded.
    - Repo with no branch setting at all: the long-lived branches present
      (main, develop, ...), or the busiest branch if none of those exist.
    """
    if cfg.included_branches is not None or not runs:
        return
    counts: dict[str, int] = {}
    first_seen: dict[str, datetime] = {}
    for r in runs:
        if workflow_label(r) in cfg.excluded_workflows:
            continue
        counts[r.branch] = counts.get(r.branch, 0) + 1
        if r.fetched_at is not None:
            first_seen[r.branch] = min(first_seen.get(r.branch, r.fetched_at), r.fetched_at)
    if not counts:
        return
    if cfg.legacy_excluded_branches is not None:
        pre_existing = {b for b in counts if first_seen.get(b, BRANCH_OPT_IN_SINCE) < BRANCH_OPT_IN_SINCE}
        cfg.included_branches = pre_existing - cfg.legacy_excluded_branches
    else:
        cfg.included_branches = {b for b in DEFAULT_BRANCHES if b in counts} or {max(counts, key=counts.get)}


def apply_repo_config(runs: list[RunData], cfg: RepoConfig) -> list[RunData]:
    """Produce the runs the UI works with: exclusions removed, job groups applied.

    - Excluded workflows, and branches not opted in, drop the whole run (so their
      jobs disappear too).
    - Grouped jobs take the group name; if the group has several members, the
      member name becomes the shard key so the breakdown chart compares members
      (this is how matrix shards like "Tests (1)", "Tests (2)" get one line each).
    - Excluded jobs are removed, and the pipeline span is recomputed from what's left.
    """
    if not (cfg.job_groups or cfg.excluded_workflows or cfg.excluded_jobs or cfg.included_branches is not None):
        return runs
    m2g = cfg.member_to_group
    out: list[RunData] = []
    for r in runs:
        if workflow_label(r) in cfg.excluded_workflows:
            continue
        if cfg.included_branches is not None and r.branch not in cfg.included_branches:
            continue
        jobs: list[JobTiming] = []
        for j in r.jobs:
            group = m2g.get(j.base_name)
            effective = group or j.base_name
            if effective in cfg.excluded_jobs:
                continue
            if group:
                key = j.base_name if len(cfg.job_groups[group]) > 1 else None
                j = replace(j, base_name=group, matrix_key=key)
            jobs.append(j)
        if len(jobs) != len(r.jobs):
            total = 0.0
            if jobs:
                total = (max(j.completed_at for j in jobs) - min(j.started_at for j in jobs)).total_seconds()
            r = replace(r, jobs=jobs, total_duration_s=total)
        elif jobs is not r.jobs:
            r = replace(r, jobs=jobs)
        out.append(r)
    return out


def parse_note_when(raw: str) -> datetime | None:
    """Parse 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (local time) to aware UTC."""
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            local = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return local.astimezone().astimezone(timezone.utc)
    return None


_workflow_names: dict[str, dict[int, str]] = {}  # repo -> {workflow_id: workflow name}


def workflow_name_for(repo: str, workflow_id: int | None, fallback: str) -> str:
    """Resolve a run's workflow *name* from its workflow_id, the way `gh run list`
    did. A run's own `name` is the run title (Dependabot titles its runs after the
    PR, `run-name:` can be anything), so it is only a last resort.

    The repo's workflow list is loaded once; ids missing from it (renamed or
    deleted workflows) are looked up individually and cached.
    """
    if workflow_id is None:
        return fallback
    names = _workflow_names.setdefault(repo, {})
    if not names:
        try:
            for wf in api_get_all(f"repos/{repo}/actions/workflows", list_key="workflows"):
                names[int(wf["id"])] = wf.get("name") or fallback
        except Exception:
            log.debug("Could not list workflows for %s", repo, exc_info=True)
    if workflow_id not in names:
        try:
            data, _ = _api_request(f"{GITHUB_API}/repos/{repo}/actions/workflows/{workflow_id}", None)
            names[workflow_id] = data.get("name") or fallback
        except Exception:
            log.debug("Could not look up workflow %s for %s", workflow_id, repo, exc_info=True)
            names[workflow_id] = fallback
    return names[workflow_id]


def fetch_run_list(
    repo: str,
    since_date: str | None = None,
    until_date: str | None = None,
    on_page=None,
) -> list[dict]:
    """One listing of successful runs (all workflows), newest-first.

    Date bounds are inclusive at day granularity. Capped at RUN_LIST_LIMIT — the
    API stops serving pages past 1000 results — so use `fetch_run_list_complete`
    when the window might exceed that. Rows are normalised to the field names the
    cache has always stored (`databaseId`, `workflowName`, ...).
    """
    params: dict[str, str] = {"status": "success"}
    if since_date and until_date:
        params["created"] = f"{since_date[:10]}..{until_date[:10]}"
    elif since_date:
        params["created"] = f">={since_date[:10]}"
    elif until_date:
        params["created"] = f"<={until_date[:10]}"
    raw = api_get_all(f"repos/{repo}/actions/runs", params, list_key="workflow_runs", max_items=RUN_LIST_LIMIT,
                      on_page=on_page)
    return [
        {
            "databaseId": r["id"],
            "displayTitle": r.get("display_title") or r.get("name") or "",
            "headBranch": r.get("head_branch") or "",
            "conclusion": r.get("conclusion"),
            "createdAt": r.get("created_at") or "",
            "workflowName": workflow_name_for(repo, r.get("workflow_id"), r.get("name") or ""),
            "workflowId": r.get("workflow_id"),
        }
        for r in raw
    ]


def fetch_run_list_complete(
    repo: str,
    since_date: str | None = None,
    until_date: str | None = None,
    report_progress: bool = False,
) -> list[dict]:
    """Fetch every run in a window, walking backwards when the 1000 cap is hit.

    The API stops serving pages past 1000 results. If a batch comes back full, we
    move the upper bound down to the oldest day in that batch and go again (bounds
    are inclusive, so we overlap that day and dedupe by id).

    With `report_progress`, STATS.done/total track runs listed vs the window's
    `total_count` (known from the very first page), which is what makes the
    first-sync progress bar and ETA possible.
    """
    seen: set[int] = set()
    out: list[dict] = []
    until = until_date
    window_total: list[int | None] = [None]

    def on_page(fetched_in_batch: int, total_count) -> None:
        if window_total[0] is None and total_count:
            window_total[0] = int(total_count)
        total = window_total[0] or 0
        STATS.set_progress(min(len(out) + fetched_in_batch, total) if total else len(out) + fetched_in_batch, total)

    while True:
        batch = fetch_run_list(repo, since_date, until, on_page=on_page if report_progress else None)
        fresh = [r for r in batch if r["databaseId"] not in seen]
        out.extend(fresh)
        seen.update(r["databaseId"] for r in fresh)
        if len(batch) < RUN_LIST_LIMIT:
            break
        oldest_day = min(r["createdAt"] for r in batch)[:10]
        if until and oldest_day == until[:10]:
            log.warning("More than %d successful runs on %s — some runs on that day may be missing",
                        RUN_LIST_LIMIT, oldest_day)
            break
        log.info("Run list hit the %d cap — continuing from %s backwards", RUN_LIST_LIMIT, oldest_day)
        until = oldest_day
    return out


def fetch_run_jobs(repo: str, run_id: int) -> list[dict]:
    return api_get_all(f"repos/{repo}/actions/runs/{run_id}/jobs", list_key="jobs")


def build_run_data(raw_run: dict, raw_jobs: list[dict]) -> RunData:
    jobs: list[JobTiming] = []
    earliest_start = None
    latest_end = None

    for j in raw_jobs:
        if j.get("conclusion") not in ("success",):
            continue
        if not j.get("started_at") or not j.get("completed_at"):
            continue

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
            name=j["name"], base_name=j["name"], matrix_key=None,
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
        except AuthError:
            raise
        except GitHubAPIError:
            if attempt == retries:
                raise
            time.sleep(1 * (attempt + 1))
            log.warning("Retrying fetch for run %s (attempt %d)", run_id, attempt + 2)
    raise RuntimeError("unreachable")


def _fetch_jobs_for_runs(repo: str, raw_runs: list[dict]) -> list[RunData]:
    """Fetch job details for a list of raw runs using ThreadPoolExecutor."""
    runs: list[RunData] = []
    total = len(raw_runs)
    done = 0
    errors = 0
    STATS.set_progress(0, total)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_and_build, repo, raw): raw for raw in raw_runs}
        for future in as_completed(futures):
            done += 1
            raw = futures[future]
            try:
                runs.append(future.result())
            except AuthError:
                for f in futures:
                    f.cancel()
                raise
            except Exception:
                errors += 1
                log.exception("Failed to fetch/build run %s", raw.get("databaseId"))
            STATS.set_progress(done, total)

    if errors:
        log.warning("Completed with %d errors out of %d runs", errors, total)
    STATS.new_runs += len(runs)
    elapsed = time.monotonic() - (STATS.phase_started_at or time.monotonic())
    if total >= 50 and elapsed > 5:
        try:  # remembered so the next first sync can estimate its duration up front
            settings_set(GLOBAL_SCOPE, "detail_runs_per_s", round(total / elapsed, 2))
        except Exception:
            log.debug("Could not save fetch rate", exc_info=True)
    return runs


REST_NAME_BUG_SINCE = "2026-08-27T18:00"  # first REST-path build stored run titles as workflow names


def repair_rest_workflow_names(repo: str) -> int:
    """Re-label runs cached by the first REST-path build, which stored the *run*
    title as the workflow name (Dependabot runs then showed up as workflows named
    after their PRs). Affected rows are recognisable: fetched after the cutover
    and without a workflowId. Their date range is re-listed and names rewritten;
    once every row carries a workflowId this is a no-op."""
    conn = _cache_conn()
    rows = conn.execute(
        "SELECT run_id, raw_run FROM run_jobs WHERE repo = ? AND fetched_at >= ? "
        "AND json_extract(raw_run, '$.workflowId') IS NULL",
        (repo, REST_NAME_BUG_SINCE),
    ).fetchall()
    if not rows:
        return 0
    affected = {rid: json.loads(rr) for rid, rr in rows}
    dates = sorted(parse_dt(r["createdAt"]) for r in affected.values())
    log.info("%d runs cached with run titles as workflow names — re-listing %s → %s",
             len(affected), dates[0].date(), dates[-1].date())
    fixed = 0
    window_end = dates[-1]
    while window_end >= dates[0] and affected:
        window_start = window_end - timedelta(days=BACKFILL_WINDOW_DAYS)
        try:
            listed = fetch_run_list_complete(repo, since_date=window_start.isoformat(), until_date=window_end.isoformat())
        except subprocess.CalledProcessError:
            log.warning("Workflow-name re-list failed; will retry next launch")
            break
        with conn:
            for r in listed:
                raw_run = affected.pop(r["databaseId"], None)
                if raw_run is None:
                    continue
                raw_run["workflowName"] = r["workflowName"]
                raw_run["workflowId"] = r.get("workflowId")
                conn.execute("UPDATE run_jobs SET raw_run = ? WHERE run_id = ?", (json.dumps(raw_run), r["databaseId"]))
                fixed += 1
        window_end = window_start
    log.info("Workflow-name re-list: %d rows relabelled, %d not returned by the API", fixed, len(affected))
    return fixed


def repair_missing_workflow_names(repo: str) -> tuple[int, int, int]:
    """Fill in workflow names for cached runs that were stored without one.

    Early versions didn't request the workflow name, so those runs show up as
    "(unknown)" forever (the cache never re-fetches a run). Two passes:

    1. Re-list the affected date range from the API (cheap: a few list calls) and
       copy the names for runs GitHub still knows about.
    2. For runs GitHub has aged out, infer the workflow from the run's job names,
       using the repo's named runs as the reference: a job counts only if it
       belongs to a single workflow >= 90% of the time, and at least two thirds
       of the run's jobs must agree. Inferred names are flagged in raw_run.

    Returns (blank_before, fixed_from_api, fixed_by_inference).
    """
    conn = _cache_conn()
    rows = conn.execute(
        "SELECT run_id, raw_run, raw_jobs FROM run_jobs WHERE repo = ? "
        "AND COALESCE(json_extract(raw_run, '$.workflowName'), '') = ''",
        (repo,),
    ).fetchall()
    if not rows:
        return 0, 0, 0
    blanks = {rid: (json.loads(rr), json.loads(rj)) for rid, rr, rj in rows}
    log.info("%d cached runs have no workflow name — repairing", len(blanks))
    fixed_api = fixed_inferred = 0

    # Pass 1: the API, in 90-day windows over the blank runs' dates (newest first;
    # stop once a window comes back empty — older runs have aged out too)
    dates = sorted(parse_dt(r["createdAt"]) for r, _ in blanks.values())
    window_end = dates[-1]
    names_from_api: dict[int, str] = {}
    while window_end >= dates[0] and len(names_from_api) < len(blanks):
        window_start = window_end - timedelta(days=BACKFILL_WINDOW_DAYS)
        try:
            listed = fetch_run_list_complete(repo, since_date=window_start.isoformat(), until_date=window_end.isoformat())
        except subprocess.CalledProcessError:
            log.warning("Workflow-name repair: API window failed; will retry next launch")
            break
        hits = {r["databaseId"]: r.get("workflowName") or "" for r in listed if r["databaseId"] in blanks}
        names_from_api.update({k: v for k, v in hits.items() if v})
        if not listed:
            break
        window_end = window_start
    with conn:
        for rid, name in names_from_api.items():
            raw_run, _ = blanks.pop(rid)
            raw_run["workflowName"] = name
            conn.execute("UPDATE run_jobs SET raw_run = ? WHERE run_id = ?", (json.dumps(raw_run), rid))
            fixed_api += 1

    # Pass 2: infer from job names, using the named runs as the reference
    if blanks:
        signature: dict[str, dict[str, int]] = {}
        for rr, rj in conn.execute(
            "SELECT raw_run, raw_jobs FROM run_jobs WHERE repo = ? "
            "AND COALESCE(json_extract(raw_run, '$.workflowName'), '') != ''", (repo,)
        ).fetchall():
            wf = json.loads(rr)["workflowName"]
            for job in {j["name"] for j in json.loads(rj)}:
                signature.setdefault(job, {}).setdefault(wf, 0)
                signature[job][wf] += 1
        with conn:
            for rid, (raw_run, raw_jobs) in list(blanks.items()):
                jobs = {j["name"] for j in raw_jobs}
                votes: dict[str, int] = {}
                for job in jobs:
                    counts = signature.get(job)
                    if not counts:
                        continue
                    wf, n = max(counts.items(), key=lambda kv: kv[1])
                    if n / sum(counts.values()) >= 0.9:
                        votes[wf] = votes.get(wf, 0) + 1
                if votes:
                    wf, v = max(votes.items(), key=lambda kv: kv[1])
                    if v >= max(1, 0.67 * len(jobs)):
                        raw_run["workflowName"] = wf
                        raw_run["workflowNameInferred"] = True
                        conn.execute("UPDATE run_jobs SET raw_run = ? WHERE run_id = ?", (json.dumps(raw_run), rid))
                        fixed_inferred += 1
                        del blanks[rid]
    log.info("Workflow-name repair: %d from the API, %d inferred from job names, %d still unknown",
             fixed_api, fixed_inferred, len(blanks))
    return len(rows), fixed_api, fixed_inferred


def fetch_incremental(repo: str, force_backfill: bool = False) -> list[RunData]:
    """Incrementally fetch runs for `repo`, using cache to avoid re-fetching.

    1. Load all cached data (caller shows this immediately)
    2. Forward-fetch: runs newer than the newest cached run (uncapped)
    3. Backfill: walk backwards in 90-day windows to the repo's creation date,
       then remember that backfill is complete so future launches skip it
    4. Return merged + sorted result
    """
    # Step 1: cache (repairing runs stored without a workflow name first)
    STATS.set_phase("cache", "Loading cache...")
    try:
        STATS.set_phase("cache", "Checking cached workflow names...")
        relabelled = repair_rest_workflow_names(repo)
        blank, from_api, inferred = repair_missing_workflow_names(repo)
        if relabelled or blank:
            STATS.set_phase("cache", f"Repaired workflow names: {relabelled} relabelled, "
                                     f"{from_api} from API, {inferred} inferred")
    except Exception:
        log.exception("Workflow-name repair failed; continuing with cached data as-is")
    cached_runs = cache_load_all(repo)
    cached_ids = cache_get_all_ids(repo)
    log.info("Cache loaded for %s: %d runs (%d in DB)", repo, len(cached_runs), len(cached_ids))
    STATS.first_load = not cached_runs

    new_runs: list[RunData] = []

    # Step 2: forward fetch
    newest_date = cached_runs[-1].created_at.isoformat() if cached_runs else None
    if newest_date:
        STATS.set_phase("forward", f"Checking for new runs since {newest_date[:10]}...")
    else:
        STATS.set_phase("forward", "Listing every successful workflow run (first sync)...")

    try:
        forward_raw = fetch_run_list_complete(repo, since_date=newest_date, report_progress=True)
    except AuthError:
        raise
    except GitHubAPIError:
        log.warning("Forward fetch failed — likely rate limited")
        STATS.set_phase("rate-limited", "Rate limited — showing cached data")
        STATS.finished_at = time.monotonic()
        return cached_runs

    STATS.listed_runs = len(forward_raw)
    forward_new = [r for r in forward_raw if r["databaseId"] not in cached_ids]
    log.info("Forward fetch: %d listed, %d new", len(forward_raw), len(forward_new))

    if forward_new:
        STATS.set_phase("details", f"Fetching job timings for {len(forward_new):,} runs...")
        new_runs.extend(_fetch_jobs_for_runs(repo, forward_new))
        cached_ids.update(r.run_id for r in new_runs)

    # Step 2b: holes in the middle of cached history. Forward fetch only looks newer
    # than the newest cached run and backfill only older than the oldest, so a gap
    # left by an interrupted or capped sync would otherwise never be filled. Each
    # gap is queried once; genuinely quiet periods are remembered so they aren't
    # re-queried every launch (runs can't appear in the past).
    if force_backfill:
        settings_set(repo, "verified_gaps", [])
    verified = {tuple(g) for g in (settings_get(repo, "verified_gaps", []) or [])}
    known = sorted({r.created_at for r in cached_runs} | {r.created_at for r in new_runs})
    gaps = [
        (a, b) for a, b in zip(known, known[1:])
        if (b - a) >= timedelta(days=GAP_DAYS) and (a.date().isoformat(), b.date().isoformat()) not in verified
    ]
    if gaps:
        log.info("Checking %d gap(s) of %d+ days in cached history", len(gaps), GAP_DAYS)
    for a, b in gaps:
        label = f"{a:%Y-%m-%d} → {b:%Y-%m-%d}"
        STATS.set_phase("gaps", f"Checking gap {label}...")
        try:
            gap_raw = fetch_run_list_complete(repo, since_date=a.isoformat(), until_date=b.isoformat())
        except AuthError:
            raise
        except GitHubAPIError:
            log.warning("Gap check %s failed — likely rate limited; will retry next launch", label)
            break
        gap_new = [r for r in gap_raw if r["databaseId"] not in cached_ids]
        log.info("Gap %s: %d listed, %d new", label, len(gap_raw), len(gap_new))
        if gap_new:
            STATS.set_phase("details", f"Fetching details for {len(gap_new)} runs in gap {label}...")
            gap_runs = _fetch_jobs_for_runs(repo, gap_new)
            new_runs.extend(gap_runs)
            cached_ids.update(r.run_id for r in gap_runs)
        verified.add((a.date().isoformat(), b.date().isoformat()))
        settings_set(repo, "verified_gaps", sorted(list(g) for g in verified))

    # Step 3: backfill
    backfill_done = meta_get_backfill_complete(repo) and not force_backfill
    oldest_date = cached_runs[0].created_at if cached_runs else None
    if oldest_date is None and new_runs:
        new_runs.sort(key=lambda r: r.created_at)
        oldest_date = new_runs[0].created_at

    if oldest_date and not backfill_done:
        repo_created = fetch_repo_created_at(repo)
        if repo_created:
            log.info("Repo created %s — backfilling to there", repo_created.date())
            STATS.backfill_total = max(1, -(-max(0, (oldest_date - repo_created).days) // BACKFILL_WINDOW_DAYS))
        window_end = oldest_date
        reached_floor = False
        for _ in range(MAX_BACKFILL_WINDOWS):
            window_start = window_end - timedelta(days=BACKFILL_WINDOW_DAYS)
            label = f"{window_start:%Y-%m-%d} → {window_end:%Y-%m-%d}"
            STATS.current_window = label
            verb = "Checking older history" if STATS.first_load else "Backfilling"
            STATS.set_phase("backfill", f"{verb} {label}...")
            STATS.set_progress(STATS.windows_done, STATS.backfill_total)

            try:
                backfill_raw = fetch_run_list_complete(
                    repo, since_date=window_start.isoformat(), until_date=window_end.isoformat(),
                )
            except AuthError:
                raise
            except GitHubAPIError:
                log.warning("Backfill stopped — API error (likely rate limited)")
                STATS.set_phase("rate-limited", "Backfill paused — rate limited, will resume next launch")
                break

            backfill_new = [r for r in backfill_raw if r["databaseId"] not in cached_ids]
            STATS.windows_done += 1
            log.info("Backfill window %s: %d listed, %d new", label, len(backfill_raw), len(backfill_new))

            if backfill_new:
                STATS.set_phase("details", f"Fetching details for {len(backfill_new)} older runs...")
                backfill_runs = _fetch_jobs_for_runs(repo, backfill_new)
                new_runs.extend(backfill_runs)
                cached_ids.update(r.run_id for r in backfill_runs)

            if repo_created is not None:
                if window_start <= repo_created:
                    reached_floor = True
                    break
            elif not backfill_new:
                # Unknown creation date: fall back to "first empty window ends it"
                reached_floor = True
                break

            window_end = window_start

        if reached_floor:
            meta_set(repo, backfill_complete=True)
            log.info("Backfill complete for %s — future launches will only forward-fetch", repo)
        STATS.current_window = ""
    elif backfill_done:
        log.info("Backfill previously completed for %s — skipping (shift+R to force)", repo)

    # Step 4: merge
    total_new = len(new_runs)
    all_runs = cached_runs + new_runs
    all_runs.sort(key=lambda r: r.created_at)
    meta_set(repo)

    if STATS.phase != "rate-limited":
        if total_new:
            STATS.set_phase("done", f"+{total_new} new runs fetched — {len(all_runs)} total")
        else:
            STATS.set_phase("done", f"Up to date — {len(all_runs)} runs")
    STATS.finished_at = time.monotonic()

    log.info("Fetch complete: %d cached + %d new = %d total", len(cached_runs), total_new, len(all_runs))
    return all_runs


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s" if m > 0 else f"{s}s"


def fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def get_job_durations(runs: list[RunData], base_name: str) -> list[float]:
    durations = []
    for run in runs:
        matrix_durs = [j.duration_s for j in run.jobs if j.base_name == base_name]
        if matrix_durs:
            durations.append(mean(matrix_durs))
    return durations


def get_step_durations_by_name(runs: list[RunData], job_base_name: str) -> dict[str, list[float]]:
    """Per-step per-run mean durations, for one job, in a single pass over the data."""
    out: dict[str, list[float]] = {}
    for run in runs:
        per_step: dict[str, list[float]] = {}
        for j in run.jobs:
            if j.base_name != job_base_name:
                continue
            for s in j.steps:
                per_step.setdefault(s.name, []).append(s.duration_s)
        for name, durs in per_step.items():
            out.setdefault(name, []).append(mean(durs))
    return out


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


def hampel_outliers(values: list[float], window: int = 15, k: float = 3.0, both: bool = False) -> list[bool]:
    """Flag transient spikes with a Hampel filter: rolling median + MAD.

    For each point, take the `window` runs around it, their median M and median
    absolute deviation MAD. The point is an outlier if it lies more than
    k * scale above M (or below too, when `both`), where
    scale = max(1.4826 * MAD, 5% of M, 5 s) so a flat series doesn't flag noise.

    Because the reference is the *local* median, a one-off 20x spike is flagged
    while a sustained level shift (e.g. a permanent 90% speed-up) is not — the
    median moves with the data. Only the few points straddling a shift can be
    misjudged, bounded by half the window.

    A cluster of consecutive bad runs (a GitHub incident) is only visible if it is
    shorter than half the window, so the window should be generous. A second pass
    recomputes each run's reference from its *unflagged* neighbours, so a cluster
    that was only partly caught in the first pass doesn't shield its remaining
    members.
    """
    n = len(values)
    flags = [False] * n
    if n < 5:
        return flags
    half = max(2, window // 2)

    def is_outlier(i: int, exclude: list[bool]) -> bool:
        lo, hi = max(0, i - half), min(n, i + half + 1)
        win = [values[j] for j in range(lo, hi) if not exclude[j] or j == i]
        if len(win) < 5:
            win = values[lo:hi]
        m = median(win)
        mad = median(abs(v - m) for v in win)
        scale = max(1.4826 * mad, 0.05 * m, 5.0)
        dev = values[i] - m
        return dev > k * scale or (both and -dev > k * scale)

    first = [is_outlier(i, flags) for i in range(n)]
    return [first[i] or is_outlier(i, first) for i in range(n)]


# ---------------------------------------------------------------------------
# Plot helper
# ---------------------------------------------------------------------------


_plot_lock = threading.Lock()


_ANSI_BACKGROUND = re.compile(r"\x1b\[48;(?:5;\d+|2;\d+;\d+;\d+)m")


class Chart:
    """The handful of chart operations we use, on top of plotext 6's figure API.

    plotext 6 is a single global figure object (``plotext.figure``) driven by
    signals and pixels; this wraps it so the chart code reads like a plotting
    API and the plotext specifics live in one place. Colours are RGB tuples.
    """

    def __init__(self, width: int, height: int, palette: PlotPalette) -> None:
        import plotext as plt

        self._plt = plt
        self._fig = fig = plt.figure
        fig.clear()
        plt.terminal.limit(False, False)  # Textual knows the real size; plotext's tty probe may not
        fig.plot_size(width, height)
        fig.theme("colorless")
        fig.canvas("default")
        self._frame = plt.pixel(palette.axes)
        fig.axes(pixel=self._frame)
        fig.ruler("both").pixel(self._frame)
        fig.legend(active=False)  # plotext 6 draws a boxed legend over the data; we add our own line
        self.legend: list[tuple[str, tuple[int, int, int]]] = []

    def plot(self, xs: list, ys: list, color: tuple[int, int, int], label: str | None = None) -> None:
        """Braille line through the points."""
        signal = self._fig.signal(xs, ys, marker=self._plt.marker("braille", self._plt.pixel(color)))
        signal.lines(True)
        if label:
            self.legend.append((label, color))
        self._fig.draw(signal)

    def bar(self, labels: list[str], values: list[float], color: tuple[int, int, int]) -> None:
        self._fig.draw(self._fig.bar(labels, values, marker=self._plt.marker("full", self._plt.pixel(color))))

    def vline(self, x: float, color: tuple[int, int, int]) -> None:
        self._fig.line(x, orientation="vertical", pixel=self._plt.pixel(color))

    def text(self, label: str, x: float, y: float, color: tuple[int, int, int]) -> None:
        self._fig.draw(self._fig.text(x, y, self._plt.colorize(label, self._plt.pixel(color))))

    def xticks(self, positions: list, labels: list[str]) -> None:
        self._fig.ruler("x").ticks(positions, labels)

    def ylim(self, lower: float, upper: float) -> None:
        self._fig.ruler("y").lim(lower, upper)

    def xlim(self, lower: float, upper: float) -> None:
        self._fig.ruler("x").lim(lower, upper)

    def yticks(self, positions: list[float], labels: list[str]) -> None:
        self._fig.ruler("y").ticks(positions, labels)

    def yaxis(self, values: list[float], from_zero: bool) -> float:
        """Fix the y range for `values` (optionally from 0) with rounded tick labels.
        Returns the top of the range (where note markers go)."""
        hi = max(values) if values else 1.0
        lo = 0.0 if from_zero else min(values)
        if from_zero:
            hi *= 1.05
        if hi <= lo:
            hi = lo + 1
        self.ylim(lo, hi)
        self.yticks(*nice_yticks(lo, hi))
        return hi

    def title(self, label: str) -> None:
        self._fig.title(self._plt.colorize(label, self._frame))

    def ylabel(self, label: str) -> None:
        self._fig.label(self._plt.colorize(label, self._frame), axis="y")

    def build(self) -> str:
        # plotext 6 back-fills axis/ruler backgrounds from its package default (white)
        # and offers no transparent value, so drop background codes: the chart sits
        # on the Textual background.
        out = _ANSI_BACKGROUND.sub("", self._fig.build().string())
        if self.legend:
            # One compact legend line under the title (row 0), like plotext 5's inline legend
            entries = "  ".join(f"\x1b[38;2;{r};{g};{b}m━━ {label}\x1b[0m" for label, (r, g, b) in self.legend)
            lines = out.split("\n")
            lines.insert(1, "     " + entries)
            out = "\n".join(lines)
        return out


def render_plot(plot_func, width: int, height: int, palette: PlotPalette) -> RichText:
    """Render a chart to a Rich Text object (ANSI colors preserved).

    `plot_func` receives a `Chart`. plotext's figure is a process-wide singleton,
    hence the lock.
    """
    with _plot_lock:
        chart = Chart(width, height, palette)
        plot_func(chart)
        return RichText.from_ansi(chart.build())


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


class Gauge(Static):
    """Single-line labelled bar:  `Label  ━━━━━━╌╌╌╌  4,312 / 5,000  detail`.

    The bar shrinks to fit the widget width so it never wraps inside a card.
    """

    DEFAULT_CSS = """
    Gauge { height: 1; }
    """

    def __init__(self, label: str, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._label = label
        self.set_value(0, 0)

    def set_value(self, value: int, total: int, detail: str = "", color: str = "#B4A3F7",
                  low_is_bad: bool = False) -> None:
        frac = (value / total) if total else 0.0
        frac = max(0.0, min(1.0, frac))
        numbers = f"  {value:,} / {total:,}" if total else "  —"
        tail = f"  {detail}" if detail else ""
        avail = self.size.width if self.size.width else 60
        bar_width = max(6, min(30, avail - 10 - 3 - len(numbers) - len(tail)))
        filled = int(round(frac * bar_width))
        if low_is_bad:
            color = "#F07178" if frac < 0.1 else "#F0C674" if frac < 0.3 else color
        text = RichText(no_wrap=True, overflow="ellipsis")
        text.append(f"{self._label:<10}", style="bold")
        text.append("━" * filled, style=color)
        text.append("╌" * (bar_width - filled), style="#3A3450")
        text.append(numbers)
        if tail:
            text.append(tail, style="#A9A3BD")
        self.update(text)


class LoginScreen(ModalScreen[str | None]):
    """Sign in to GitHub: OAuth device flow (code + browser) or a pasted token.

    Dismisses with the token (already saved to AUTH_FILE) or None if cancelled.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    CSS = """
    LoginScreen {
        align: center middle;
    }
    #login-box {
        width: 78;
        height: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #login-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    .login-hint {
        color: $text-muted;
        height: auto;
        margin-bottom: 1;
    }
    .login-heading {
        text-style: bold;
        margin-top: 1;
    }
    #login-code {
        height: auto;
        margin: 1 0;
    }
    #login-actions, #login-token-row {
        height: 3;
    }
    #login-actions Button, #login-token-row Button {
        margin-right: 1;
    }
    #login-token {
        width: 1fr;
        margin-right: 1;
    }
    #login-status {
        height: auto;
        color: $warning;
        margin-bottom: 1;
    }
    """

    def __init__(self, reason: str = "") -> None:
        super().__init__()
        self._reason = reason
        self._verification_uri = "https://github.com/login/device"
        self._user_code = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="login-box"):
            yield Label("Sign in to GitHub", id="login-title")
            yield Static(
                self._reason or "GHA Explorer reads workflow runs through the GitHub API and needs to sign in "
                "once. The token is stored in your data directory, readable only by you.",
                classes="login-hint",
            )
            yield Label("Option 1 — authorize in your browser", classes="login-heading")
            yield Static("Requesting a code from GitHub…", id="login-code")
            with Horizontal(id="login-actions"):
                yield Button("Open browser", id="login-open", variant="primary", disabled=True)
                yield Button("New code", id="login-restart", disabled=True)
            yield Static("", id="login-status")
            yield Label("Option 2 — paste a personal access token", classes="login-heading")
            with Horizontal(id="login-token-row"):
                yield Input(placeholder="ghp_… or github_pat_…", password=True, id="login-token")
                yield Button("Use token", id="login-use-token")
            yield Static(
                "Classic tokens need the repo scope; fine-grained tokens need Actions: read and Metadata: read "
                "on the repositories you want to explore. Esc cancels.",
                classes="login-hint",
            )

    def on_mount(self) -> None:
        self._device_flow()

    # -- device flow (runs in a thread; UI updates hop back via call_from_thread) --

    @work(thread=True, exclusive=True, group="device-flow", exit_on_error=False)
    def _device_flow(self) -> None:
        worker = get_current_worker()
        try:
            info = device_flow_start()
        except Exception as exc:
            log.warning("Device flow start failed: %s", exc)
            self.app.call_from_thread(self._set_status, f"Couldn't start the browser sign-in: {exc}")
            self.app.call_from_thread(self._set_restart_enabled, True)
            return
        self.app.call_from_thread(self._show_code, info)
        interval = int(info.get("interval", 5))
        deadline = time.monotonic() + int(info.get("expires_in", 900))
        while not worker.is_cancelled and time.monotonic() < deadline:
            time.sleep(interval)
            if worker.is_cancelled:
                return
            try:
                result = device_flow_poll(info["device_code"])
            except Exception as exc:
                log.debug("Device flow poll failed: %s", exc)
                continue
            if token := result.get("access_token"):
                try:
                    login = verify_token(token)
                except Exception:
                    login = ""
                self.app.call_from_thread(self._finish, token, login)
                return
            error = result.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error == "expired_token":
                break
            if error == "access_denied":
                self.app.call_from_thread(self._set_status, "Authorization was denied on GitHub. Press New code to try again.")
                self.app.call_from_thread(self._set_restart_enabled, True)
                return
            self.app.call_from_thread(self._set_status, f"GitHub replied: {error}. Press New code to try again.")
            self.app.call_from_thread(self._set_restart_enabled, True)
            return
        if not worker.is_cancelled:
            self.app.call_from_thread(self._set_status, "That code expired. Press New code for a fresh one.")
            self.app.call_from_thread(self._set_restart_enabled, True)

    def _show_code(self, info: dict) -> None:
        self._verification_uri = info.get("verification_uri", self._verification_uri)
        self._user_code = info.get("user_code", "")
        text = RichText()
        text.append("Open  ", style="bold")
        text.append(self._verification_uri, style="underline")
        text.append("  and enter the code  ", style="bold")
        text.append(f" {self._user_code} ", style="bold reverse")
        self.query_one("#login-code", Static).update(text)
        self.query_one("#login-open", Button).disabled = False
        self.query_one("#login-restart", Button).disabled = False
        self._set_status("Waiting for you to authorize in the browser…")

    def _set_status(self, message: str) -> None:
        try:
            self.query_one("#login-status", Static).update(message)
        except Exception:
            pass  # screen already dismissed

    def _set_restart_enabled(self, enabled: bool) -> None:
        try:
            self.query_one("#login-restart", Button).disabled = not enabled
        except Exception:
            pass

    def _finish(self, token: str, login: str) -> None:
        save_token(token, login)
        log.info("Signed in to GitHub%s", f" as {login}" if login else "")
        self.dismiss(token)

    @on(Button.Pressed, "#login-open")
    def _open_browser(self) -> None:
        try:
            opened = webbrowser.open(self._verification_uri)
        except Exception:
            opened = False
        if not opened:
            self._set_status(f"Couldn't open a browser here — visit {self._verification_uri} yourself and enter {self._user_code}.")

    @on(Button.Pressed, "#login-restart")
    def _restart(self) -> None:
        self.query_one("#login-restart", Button).disabled = True
        self.query_one("#login-code", Static).update("Requesting a new code from GitHub…")
        self._device_flow()

    # -- pasted token --

    @on(Button.Pressed, "#login-use-token")
    @on(Input.Submitted, "#login-token")
    def _use_token(self) -> None:
        token = self.query_one("#login-token", Input).value.strip()
        if not token:
            self._set_status("Paste a token first.")
            return
        self._set_status("Checking the token…")
        self._verify_pasted(token)

    @work(thread=True, exclusive=True, group="verify-token", exit_on_error=False)
    def _verify_pasted(self, token: str) -> None:
        try:
            login = verify_token(token)
        except AuthError:
            self.app.call_from_thread(self._set_status, "GitHub rejected that token.")
            return
        except Exception as exc:
            self.app.call_from_thread(self._set_status, f"Couldn't verify the token: {exc}")
            return
        self.app.call_from_thread(self._finish, token, login)

    def action_cancel(self) -> None:
        self.workers.cancel_group(self, "device-flow")
        self.dismiss(None)


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
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #picker-title {
        text-style: bold;
        color: $primary;
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
            err = event.worker.error
            msg = ("Not signed in — Esc, then sign in via Settings → General → GitHub access."
                   if isinstance(err, AuthError) else f"Error loading repos: {err}")
            self.query_one("#picker-status", Static).update(msg)

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
        self.query_one("#picker-status", Static).update(
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
        search = self.query_one("#picker-search", Input)
        if search.has_focus:
            lst = self.query_one("#repo-list", OptionList)
            if lst.option_count > 0:
                lst.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Small yes/no dialog. Dismisses True on confirm."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #confirm-box {
        width: 60;
        height: auto;
        border: round $error;
        background: $surface;
        padding: 1 2;
    }
    #confirm-title {
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }
    #confirm-message {
        height: auto;
        margin-bottom: 1;
    }
    #confirm-buttons {
        height: 3;
    }
    #confirm-buttons Button {
        margin-right: 1;
    }
    """

    def __init__(self, title: str, message: str, confirm_label: str = "Delete") -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self._title, id="confirm-title")
            yield Static(self._message, id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Button(self._confirm_label, id="confirm-yes", variant="error")
                yield Button("Cancel", id="confirm-no")

    def on_mount(self) -> None:
        self.query_one("#confirm-no", Button).focus()

    @on(Button.Pressed, "#confirm-yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def action_cancel(self) -> None:
        self.dismiss(False)


def _note_preview(note: Note) -> str:
    text = note.text if len(note.text) <= 80 else note.text[:77] + "..."
    return f"{note.at.astimezone():%Y-%m-%d %H:%M} — {text}"


class ColorSwatch(Static):
    """One clickable colour square."""

    DEFAULT_CSS = """
    ColorSwatch {
        width: 3;
        height: 1;
        margin-right: 1;
        content-align: center middle;
        color: #14121C;
        text-style: bold;
    }
    """

    def __init__(self, color: str, label: str) -> None:
        super().__init__(" ")
        self.color = color
        self.tooltip = label

    def on_mount(self) -> None:
        self.styles.background = self.color

    def on_click(self) -> None:
        self.post_message(ColorPicker.Picked(self.color))


class ColorPicker(Horizontal):
    """Row of colour swatches; the selected one shows a check mark."""

    DEFAULT_CSS = """
    ColorPicker {
        height: 1;
        width: auto;
    }
    """

    class Picked(Message):
        def __init__(self, color: str) -> None:
            super().__init__()
            self.color = color

    class Changed(Message):
        def __init__(self, color: str) -> None:
            super().__init__()
            self.color = color

    def __init__(self, value: str = DEFAULT_NOTE_COLOR, **kwargs) -> None:
        super().__init__(**kwargs)
        self.value = value

    def compose(self) -> ComposeResult:
        for label, color in NOTE_COLORS:
            yield ColorSwatch(color, label)

    def on_mount(self) -> None:
        self._refresh_marks()

    def set_value(self, color: str) -> None:
        self.value = color
        self._refresh_marks()

    def _refresh_marks(self) -> None:
        for swatch in self.query(ColorSwatch):
            swatch.update("✓" if swatch.color == self.value else " ")

    @on(Picked)
    def _picked(self, event: Picked) -> None:
        event.stop()
        self.set_value(event.color)
        self.post_message(self.Changed(event.color))


class NoteBubble(Vertical):
    """Small anchored popover showing one note; opened by clicking its ⓘ on a chart.

    Lives on its own CSS layer, so it lays out at the screen origin and `offset`
    places it next to the marker that was clicked.
    """

    DEFAULT_CSS = """
    NoteBubble {
        layer: notes;
        width: 50;
        height: auto;
        max-height: 14;
        border: round $error;
        background: $panel;
        padding: 0 1;
    }
    NoteBubble .bubble-when {
        color: $error;
        text-style: bold;
        height: 1;
    }
    NoteBubble .bubble-text {
        height: auto;
        max-height: 8;
    }
    NoteBubble .bubble-scope {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }
    NoteBubble Horizontal {
        height: 1;
    }
    NoteBubble Button {
        min-width: 9;
        margin-right: 1;
        color: $text;
        text-style: bold;
    }
    """
    BINDINGS = [Binding("escape", "close", "Close", show=False)]

    class DeleteRequested(Message):
        def __init__(self, note: Note) -> None:
            super().__init__()
            self.note = note

    class EditRequested(Message):
        def __init__(self, note: Note) -> None:
            super().__init__()
            self.note = note

    def __init__(self, note: Note) -> None:
        super().__init__()
        self.note = note

    def compose(self) -> ComposeResult:
        yield Static(f"{NOTE_MARKER} {self.note.at.astimezone():%Y-%m-%d %H:%M} (local)", classes="bubble-when")
        yield Static(self.note.text, classes="bubble-text")
        yield Static(f"Applies to: {self.note.applies_to}", classes="bubble-scope")
        with Horizontal():
            yield Button("Close", id="bubble-close", compact=True)
            yield Button("Edit", id="bubble-edit", variant="primary", compact=True)
            yield Button("Delete", id="bubble-delete", variant="error", compact=True)

    def on_mount(self) -> None:
        color = self.note.color or DEFAULT_NOTE_COLOR
        self.styles.border = ("round", color)
        self.query_one(".bubble-when").styles.color = color
        self.query_one("#bubble-close", Button).focus()

    @on(Button.Pressed, "#bubble-edit")
    def _edit(self) -> None:
        self.post_message(self.EditRequested(self.note))

    @on(Button.Pressed, "#bubble-close")
    def action_close(self) -> None:
        self.remove()

    @on(Button.Pressed, "#bubble-delete")
    def _delete(self) -> None:
        self.post_message(self.DeleteRequested(self.note))


class NotesScreen(ModalScreen[bool]):
    """Notes manager for the current repo: list, add, delete. Dismisses with True if anything changed."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("delete", "delete_selected", "Delete", show=False),
    ]

    CSS = """
    NotesScreen {
        align: center middle;
    }
    #notes-box {
        width: 96;
        height: 80%;
        border: round $error;
        background: $surface;
        padding: 1 2;
    }
    #notes-title {
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }
    #notes-table {
        height: 1fr;
        margin-bottom: 1;
    }
    #notes-empty {
        height: 1fr;
        color: $text-muted;
        content-align: center middle;
        margin-bottom: 1;
    }
    .notes-label {
        color: $text-muted;
    }
    #notes-form {
        height: 3;
    }
    #note-when {
        width: 24;
        margin-right: 1;
    }
    #note-text {
        width: 1fr;
    }
    #note-error {
        color: $error;
        height: 1;
    }
    #notes-buttons {
        height: 3;
    }
    #notes-buttons Button {
        margin-right: 1;
    }
    #note-scope-row {
        height: auto;
        max-height: 9;
        margin-bottom: 1;
    }
    #note-scope {
        width: 30;
        height: auto;
        border: none;
        padding: 0;
        background: $surface;
        margin-right: 2;
    }
    #note-jobs {
        width: 1fr;
        height: auto;
        max-height: 9;
        border: round $secondary;
    }
    #note-color-row {
        height: 1;
        margin-bottom: 1;
    }
    #note-editing {
        height: 1;
        color: $primary;
    }
    """

    def __init__(self, repo: str, current_job: str, job_names: list[str], edit_note_id: int | None = None) -> None:
        super().__init__()
        self.repo = repo
        self.current_job = current_job
        self.job_names = job_names  # effective job names after repo settings (Pipeline first)
        self.changed = False
        self._notes: list[Note] = []
        self._editing: Note | None = None
        self._edit_note_id = edit_note_id

    def compose(self) -> ComposeResult:
        with Vertical(id="notes-box"):
            yield Label(f"{NOTE_MARKER} Notes — {self.repo}", id="notes-title")
            yield DataTable(id="notes-table", cursor_type="row", zebra_stripes=True)
            yield Static("No notes yet. Add one below to mark when something changed.", id="notes-empty")
            yield Label("Add a note   (when is local time: YYYY-MM-DD or YYYY-MM-DD HH:MM)", classes="notes-label")
            with Horizontal(id="notes-form"):
                yield Input(value=datetime.now().strftime("%Y-%m-%d %H:%M"), id="note-when")
                yield Input(placeholder="What changed? e.g. Switched runners to ubuntu-24.04", id="note-text")
            with Horizontal(id="note-scope-row"):
                with RadioSet(id="note-scope"):
                    yield RadioButton(f"This job ({self.current_job})", value=True, id="scope-this")
                    yield RadioButton("Multiple jobs", id="scope-multi")
                    yield RadioButton("All jobs", id="scope-all")
                yield SelectionList[str](
                    *[Selection(name, name, name == self.current_job) for name in self.job_names], id="note-jobs",
                )
            with Horizontal(id="note-color-row"):
                yield Label("Color ", classes="notes-label")
                yield ColorPicker(id="note-color")
            yield Static("", id="note-editing", classes="notes-label")
            yield Static("", id="note-error")
            with Horizontal(id="notes-buttons"):
                yield Button("Add", id="note-add", variant="primary")
                yield Button("Cancel edit", id="note-cancel-edit")
                yield Button("Delete selected", id="note-delete", variant="error")
                yield Button("Close", id="note-close")

    def on_mount(self) -> None:
        self.query_one("#note-jobs").display = False
        self.query_one("#note-cancel-edit").display = False
        self._reload()
        if self._edit_note_id is not None:
            note = next((n for n in self._notes if n.id == self._edit_note_id), None)
            if note:
                self._set_editing(note)
                return
        self.query_one("#note-text", Input).focus()

    def _set_editing(self, note: Note | None) -> None:
        """Load a note into the form (or clear the form when None)."""
        self._editing = note
        when = self.query_one("#note-when", Input)
        text = self.query_one("#note-text", Input)
        jobs = self.query_one("#note-jobs", SelectionList)
        scope = self.query_one("#note-scope", RadioSet)
        picker = self.query_one("#note-color", ColorPicker)
        if note is None:
            text.value = ""
            picker.set_value(DEFAULT_NOTE_COLOR)
        else:
            when.value = note.at.astimezone().strftime("%Y-%m-%d %H:%M")
            text.value = note.text
            picker.set_value(note.color or DEFAULT_NOTE_COLOR)
            with self.prevent(RadioSet.Changed, SelectionList.SelectedChanged):
                if note.jobs is None:
                    self.query_one("#scope-all", RadioButton).value = True
                elif note.jobs == [self.current_job]:
                    self.query_one("#scope-this", RadioButton).value = True
                else:
                    self.query_one("#scope-multi", RadioButton).value = True
                    jobs.deselect_all()
                    for j in note.jobs:
                        jobs.select(j)
            pressed = scope.pressed_button
            jobs.display = bool(pressed and pressed.id == "scope-multi")
        self.query_one("#note-add", Button).label = "Save" if note else "Add"
        self.query_one("#note-cancel-edit").display = note is not None
        self.query_one("#note-editing", Static).update(
            f"Editing note from {note.at.astimezone():%Y-%m-%d %H:%M} — Save to apply, Esc or Cancel edit to stop."
            if note else ""
        )
        self.query_one("#note-error", Static).update("")
        text.focus()

    @on(DataTable.RowSelected, "#notes-table")
    def _on_note_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        note = next((n for n in self._notes if str(n.id) == str(event.row_key.value)), None)
        if note:
            self._set_editing(note)

    @on(Button.Pressed, "#note-cancel-edit")
    def _cancel_edit(self) -> None:
        self._set_editing(None)

    @on(RadioSet.Changed, "#note-scope")
    def _on_scope_changed(self, event: RadioSet.Changed) -> None:
        self.query_one("#note-jobs").display = event.pressed.id == "scope-multi"

    def _selected_scope(self) -> list[str] | None | str:
        """None = all jobs; list = specific jobs; "" = invalid (multi with nothing checked)."""
        pressed = self.query_one("#note-scope", RadioSet).pressed_button
        which = pressed.id if pressed else "scope-this"
        if which == "scope-all":
            return None
        if which == "scope-multi":
            chosen = list(self.query_one("#note-jobs", SelectionList).selected)
            return chosen or ""
        return [self.current_job]

    def _reload(self) -> None:
        self._notes = notes_list(self.repo)
        table = self.query_one("#notes-table", DataTable)
        table.clear(columns=True)
        table.add_columns(" ", "When (local)", "Applies to", "Note")
        for n in self._notes:
            dot = RichText("●", style=n.color or DEFAULT_NOTE_COLOR)
            table.add_row(dot, f"{n.at.astimezone():%Y-%m-%d %H:%M}", n.applies_to, n.text, key=str(n.id))
        has = bool(self._notes)
        table.display = has
        self.query_one("#notes-empty").display = not has

    @on(Button.Pressed, "#note-add")
    @on(Input.Submitted, "#note-text")
    def _add(self) -> None:
        error = self.query_one("#note-error", Static)
        when = parse_note_when(self.query_one("#note-when", Input).value)
        text = self.query_one("#note-text", Input).value.strip()
        if when is None:
            error.update("Couldn't parse the date — use YYYY-MM-DD or YYYY-MM-DD HH:MM")
            return
        if not text:
            error.update("Note text is empty")
            return
        scope = self._selected_scope()
        if scope == "":
            error.update("Pick at least one job, or choose All jobs")
            return
        color = self.query_one("#note-color", ColorPicker).value
        color_value = None if color == DEFAULT_NOTE_COLOR else color
        if self._editing:
            notes_update(self._editing.id, when, text, scope, color_value)
            log.info("Updated note %d", self._editing.id)
        else:
            notes_add(self.repo, when, text, scope, color_value)
            log.info("Added note at %s: %s", when.isoformat(), text[:60])
        self.changed = True
        self._set_editing(None)
        self._reload()

    @on(Button.Pressed, "#note-delete")
    def action_delete_selected(self) -> None:
        table = self.query_one("#notes-table", DataTable)
        if not self._notes or table.cursor_row is None or table.cursor_row >= len(self._notes):
            return
        note = self._notes[table.cursor_row]

        def _confirmed(yes: bool | None) -> None:
            if not yes:
                return
            notes_delete(note.id)
            self.changed = True
            self._reload()
            log.info("Deleted note %d", note.id)

        self.app.push_screen(ConfirmScreen("Delete this note?", _note_preview(note)), _confirmed)

    @on(Button.Pressed, "#note-close")
    def action_close(self) -> None:
        if self._editing:
            self._set_editing(None)
            return
        self.dismiss(self.changed)


class SettingsScreen(Screen[bool]):
    """Full-page per-repo settings: General · Groups · Workflows · Jobs · Branches.

    Works on a copy of the RepoConfig and saves on every change; dismisses with
    True when anything changed so the app can recompute its view of the runs.
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("1", "tab('general')", "General"),
        Binding("2", "tab('groups')", "Groups"),
        Binding("3", "tab('workflows')", "Workflows"),
        Binding("4", "tab('jobs')", "Jobs"),
        Binding("5", "tab('branches')", "Branches"),
    ]

    CSS = """
    SettingsScreen {
        background: $background;
        layout: vertical;
    }
    #settings-top {
        height: 1;
        background: $panel;
        padding: 0 1;
    }
    #settings-title {
        width: 1fr;
        height: 1;
        text-style: bold;
        color: $primary;
    }
    #settings-close {
        width: auto;
        color: $text;
    }
    #settings-tabs {
        height: 2;
        width: 60;
        margin: 0 1;
    }
    #settings-tabs Tab {
        padding: 0 1;
    }
    #settings-content {
        height: 1fr;
        padding: 1 2;
    }
    .settings-hint {
        color: $text-muted;
        height: auto;
        margin-bottom: 1;
    }
    .settings-heading {
        text-style: bold;
        color: $primary;
    }
    .settings-table {
        height: 1fr;
        margin-bottom: 1;
    }
    .settings-actions {
        height: 3;
    }
    .settings-actions Button {
        margin-right: 1;
    }
    #general Input {
        width: 12;
    }
    #general-row {
        height: 3;
    }
    #general-row Label, #outlier-row Label {
        height: 3;
        content-align: left middle;
        margin-right: 2;
    }
    #outlier-row {
        height: 3;
    }
    #outlier-row Input {
        margin-right: 3;
    }
    #general Checkbox {
        background: $background;
        border: none;
        padding: 0;
        margin-bottom: 1;
    }
    #db-row {
        height: 3;
    }
    #general #db-path {
        width: 1fr;
        margin-right: 1;
    }
    #db-row Button {
        margin-right: 1;
    }
    #db-status {
        color: $success;
    }
    #auth-mode {
        height: auto;
        margin-bottom: 1;
    }
    #auth-row {
        height: 3;
    }
    #auth-row Button {
        margin-right: 1;
    }
    #auth-status {
        color: $success;
    }
    #group-members {
        height: 2fr;
        min-height: 5;
        margin-bottom: 1;
        border: round $secondary;
    }
    #group-form {
        height: 3;
    }
    #group-name {
        width: 1fr;
        margin-right: 1;
    }
    #groups-table {
        height: 1fr;
        min-height: 4;
        margin-bottom: 1;
    }
    #group-error {
        color: $error;
        height: 1;
    }
    #group-editing {
        height: 1;
        margin-bottom: 0;
        color: $primary;
    }
    #group-new {
        margin-left: 1;
    }
    """

    TAB_IDS = ("general", "groups", "workflows", "jobs", "branches")

    def __init__(self, repo: str, runs: list[RunData], cfg: RepoConfig) -> None:
        super().__init__()
        self.repo = repo
        self.raw_runs = runs
        self.cfg = RepoConfig.from_json(cfg.to_json())  # work on a copy
        self.changed = False
        self.db_changed = False
        self._editing: str | None = None  # group currently loaded in the editor

    # -- layout --

    def compose(self) -> ComposeResult:
        with Horizontal(id="settings-top"):
            yield Static(f"Settings ({self.repo})", id="settings-title")
            yield Button("Close  (Esc)", id="settings-close", compact=True)
        yield Tabs(
            Tab("General", id="general"), Tab("Groups", id="groups"), Tab("Workflows", id="workflows"),
            Tab("Jobs", id="jobs"), Tab("Branches", id="branches"), id="settings-tabs",
        )
        with ContentSwitcher(initial="general", id="settings-content"):
            with Vertical(id="general"):
                yield Static(
                    "Settings apply to this repository only and are saved to the database as you change them.",
                    classes="settings-hint",
                )
                with Horizontal(id="general-row"):
                    yield Label("Rolling average window (runs)")
                    yield Input(value=str(self.cfg.rolling_window), type="integer", id="rolling-window")
                yield Static(
                    "The trend chart's smoothed line averages this many consecutive runs.",
                    classes="settings-hint",
                )
                yield Label("Outliers", classes="settings-heading")
                yield Checkbox("Hide outlier runs on the Trends tab", value=self.cfg.outlier_filter, id="outlier-filter")
                with Horizontal(id="outlier-row"):
                    yield Label("Sensitivity k")
                    yield Input(value=f"{self.cfg.outlier_k:g}", type="number", id="outlier-k")
                    yield Label("Window (runs)")
                    yield Input(value=str(self.cfg.outlier_window), type="integer", id="outlier-window")
                yield Checkbox("Also hide unusually fast runs", value=self.cfg.outlier_both, id="outlier-both")
                yield Static(
                    "Hampel filter: a run is hidden when it is more than k robust deviations (median absolute "
                    "deviation) away from the median of the surrounding window. Transient spikes — a flaky runner, "
                    "a stuck queue — get hidden; a lasting change in duration moves the local median with it and "
                    "stays visible. Lower k = more aggressive; 3 is a common default. The window must be more than "
                    "twice the longest streak of bad runs you want to catch (an incident slowing 10 runs in a row "
                    "needs a window over 20); 41 is a good default. Hidden runs still count in the Runs tab.",
                    classes="settings-hint",
                )
                yield Label("Database", classes="settings-heading")
                with Horizontal(id="db-row"):
                    yield Input(value=str(CACHE_DB), id="db-path")
                    yield Button("Apply path", id="db-apply", variant="primary")
                    yield Button(f"Reveal in {FILE_MANAGER_NAME}", id="db-reveal")
                yield Static("", id="db-status", classes="settings-hint")
                yield Static(
                    "Runs cache, settings and notes all live in this one SQLite file. Change the path to rename or "
                    "move it: if nothing exists at the new path the current database is copied there first; the old "
                    "file is not deleted. The location is remembered in paths.json in the data directory "
                    "(or set GHA_EXPLORER_DB).",
                    classes="settings-hint",
                )
                yield Label("GitHub access  (applies to every repository)", classes="settings-heading")
                gh_ok = gh_cli_available()
                mode = auth_mode()
                with RadioSet(id="auth-mode"):
                    yield RadioButton("GitHub CLI — reuse the login from `gh auth login`", value=mode == "gh",
                                      id="auth-gh", disabled=not gh_ok)
                    yield RadioButton("Built-in sign-in — authorize in your browser, or paste a token",
                                      value=mode == "rest", id="auth-rest")
                yield Static(auth_status_text(), id="auth-status", classes="settings-hint")
                with Horizontal(id="auth-row"):
                    yield Button("Sign in…", id="auth-login", variant="primary")
                    yield Button("Sign out of built-in login", id="auth-logout", disabled=not AUTH_FILE.exists())
                yield Static(self._auth_help_text(gh_ok), id="auth-help", classes="settings-hint")
            with Vertical(id="groups"):
                yield Static(
                    "Combine jobs that should be treated as one — matrix shards like Tests (1) / Tests (2), or a "
                    "job that was renamed. The group appears as a single job in the filter, and the Trends tab adds "
                    "a per-member breakdown chart. Check the jobs, name the group, then Group selected. "
                    "Select a group below to edit its name or members. A job can be in one group at a time.",
                    classes="settings-hint",
                )
                yield Label("Jobs  (check the ones to group)", classes="settings-heading")
                yield SelectionList[str](id="group-members")
                yield Static("", id="group-editing", classes="settings-hint")
                with Horizontal(id="group-form"):
                    yield Input(placeholder="Group name", id="group-name")
                    yield Button("Group selected", id="group-create", variant="primary")
                    yield Button("Cancel edit", id="group-new")
                yield Static("", id="group-error")
                yield Label("Groups  (select one to edit it)", classes="settings-heading")
                yield DataTable(id="groups-table", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="settings-actions"):
                    yield Button("Remove selected group", id="group-remove", variant="error")
            for kind, noun in (("workflows", "workflow"), ("jobs", "job"), ("branches", "branch")):
                with Vertical(id=kind):
                    if kind == "branches":
                        hint = ("Branches are opt-in: new branches show up excluded until you include them here, "
                                "since they're created all the time. Select a row (Enter or click) to toggle. "
                                "A new repo starts with its long-lived branches (main, develop, ...).")
                    else:
                        hint = (f"Everything is included by default. Select a row (Enter or click) to exclude or "
                                f"re-include that {noun}."
                                + (" Excluding a workflow removes its runs, so its jobs drop out of the Jobs tab and "
                                   "the job filter." if kind == "workflows" else ""))
                    yield Static(hint, classes="settings-hint")
                    yield DataTable(id=f"{kind}-table", classes="settings-table", cursor_type="row", zebra_stripes=True)
                    with Horizontal(classes="settings-actions"):
                        yield Button("Include all", id=f"{kind}-include-all")
                        yield Button("Exclude all", id=f"{kind}-exclude-all", variant="warning")
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self._fit_tabs)
        self.query_one("#group-new").display = False
        members = self.query_one("#group-members", SelectionList)
        if hasattr(members, "wrap"):
            members.wrap = True  # long job names wrap instead of being cut with "…"
        self._refresh_all()

    def _fit_tabs(self) -> None:
        tabs = self.query_one("#settings-tabs", Tabs)
        total = sum(t.region.width for t in tabs.query(Tab))
        if total:
            tabs.styles.width = total

    # -- derived data --

    def _view_runs(self) -> list[RunData]:
        """Raw runs minus excluded workflows/branches, with groups applied (job exclusions kept visible)."""
        cfg = RepoConfig.from_json(self.cfg.to_json())
        cfg.excluded_jobs = set()
        return apply_repo_config(self.raw_runs, cfg)

    def _workflow_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.raw_runs:
            counts[workflow_label(r)] = counts.get(workflow_label(r), 0) + 1
        return counts

    def _branch_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.raw_runs:
            if workflow_label(r) in self.cfg.excluded_workflows:
                continue
            counts[r.branch] = counts.get(r.branch, 0) + 1
        return counts

    def _included_branches(self) -> set[str]:
        ensure_branch_inclusion(self.cfg, self.raw_runs)
        return self.cfg.included_branches if self.cfg.included_branches is not None else set()

    def _excluded_branches(self) -> set[str]:
        """Branches are opt-in, so 'excluded' = every known branch not in the set."""
        return set(self._branch_counts()) - self._included_branches()

    def _job_counts(self) -> tuple[dict[str, int], dict[str, str]]:
        """Effective (grouped) job names from included workflows/branches, plus the
        workflow(s) each one runs in (busiest first)."""
        counts: dict[str, int] = {}
        wf_counts: dict[str, dict[str, int]] = {}
        for r in self._view_runs():
            wf = workflow_label(r)
            for name in {j.base_name for j in r.jobs}:
                counts[name] = counts.get(name, 0) + 1
                per = wf_counts.setdefault(name, {})
                per[wf] = per.get(wf, 0) + 1
        workflows = {
            name: ", ".join(sorted(per, key=lambda w: (-per[w], w))) for name, per in wf_counts.items()
        }
        return counts, workflows

    def _raw_job_names(self) -> dict[str, int]:
        """Ungrouped job names (what groups are made of) that are still included.

        A job is hidden when it — or the group it belongs to — is excluded, except
        members of the group currently being edited, so an excluded group can
        still have its membership changed.
        """
        m2g = self.cfg.member_to_group
        keep = set(self.cfg.job_groups.get(self._editing, [])) if self._editing else set()
        counts: dict[str, int] = {}
        for r in self.raw_runs:
            if workflow_label(r) in self.cfg.excluded_workflows or r.branch not in self._included_branches():
                continue
            for name in {j.base_name for j in r.jobs}:
                if m2g.get(name, name) in self.cfg.excluded_jobs and name not in keep:
                    continue
                counts[name] = counts.get(name, 0) + 1
        return counts

    # -- rendering --

    def _refresh_all(self) -> None:
        self._render_exclusion_table("workflows", self._workflow_counts(), self.cfg.excluded_workflows)
        job_counts, job_workflows = self._job_counts()
        self._render_exclusion_table("jobs", job_counts, self.cfg.excluded_jobs, extra=("Workflow", job_workflows))
        self._render_exclusion_table("branches", self._branch_counts(), self._excluded_branches())
        self._render_groups()

    def _render_exclusion_table(
        self, kind: str, counts: dict[str, int], excluded: set[str],
        extra: tuple[str, dict[str, str]] | None = None,
    ) -> None:
        table = self.query_one(f"#{kind}-table", DataTable)
        cursor = table.cursor_row
        table.clear(columns=True)
        columns = ["Status", kind[:-1].capitalize() if kind != "branches" else "Branch"]
        if extra:
            columns.append(extra[0])
        columns.append("Runs")
        table.add_columns(*columns)
        names = sorted(counts, key=lambda n: (-counts[n], n))
        # Keep excluded names that no longer appear in the data, so they can be re-included
        names += sorted(n for n in excluded if n not in counts)
        for name in names:
            status = (RichText("✗ excluded", style="#F07178") if name in excluded
                      else RichText("✓ included", style="#86D9A6"))
            row: list = [status, name]
            if extra:
                row.append(extra[1].get(name, ""))
            row.append(str(counts.get(name, 0)))
            table.add_row(*row, key=name)
        if names:
            table.move_cursor(row=min(cursor, len(names) - 1))

    def _render_groups(self) -> None:
        members = self.query_one("#group-members", SelectionList)
        selected = set(members.selected)
        raw = self._raw_job_names()
        m2g = self.cfg.member_to_group
        with self.prevent(SelectionList.SelectedChanged):
            members.clear_options()
            for name in sorted(raw, key=lambda n: (-raw[n], n)):
                label = RichText(name)
                if name in m2g:
                    label.append(f"  → {m2g[name]}", style="bold #B4A3F7")
                label.append(f"  ({raw[name]})", style="#A9A3BD")
                members.add_option(Selection(label, name, name in selected))
        table = self.query_one("#groups-table", DataTable)
        cursor = table.cursor_row
        table.clear(columns=True)
        table.add_columns("Group", "Members")
        for group in sorted(self.cfg.job_groups):
            table.add_row(group, ", ".join(self.cfg.job_groups[group]), key=group)
        if self.cfg.job_groups:
            table.move_cursor(row=min(cursor, len(self.cfg.job_groups) - 1))

    # -- persistence --

    def _save(self) -> None:
        save_repo_config(self.repo, self.cfg)
        self.changed = True

    # -- events --

    @on(Tabs.TabActivated, "#settings-tabs")
    def _on_tab(self, event: Tabs.TabActivated) -> None:
        event.stop()  # don't let the App's tab handler see this
        self.query_one("#settings-content", ContentSwitcher).current = event.tab.id or "general"

    def action_tab(self, tab_id: str) -> None:
        self.query_one("#settings-tabs", Tabs).active = tab_id

    @on(Input.Changed, "#rolling-window")
    def _on_rolling(self, event: Input.Changed) -> None:
        try:
            value = int(event.value)
        except ValueError:
            return
        if 1 <= value <= 200 and value != self.cfg.rolling_window:
            self.cfg.rolling_window = value
            self._save()

    @on(Checkbox.Changed, "#outlier-filter")
    def _on_outlier_toggle(self, event: Checkbox.Changed) -> None:
        self.cfg.outlier_filter = event.value
        self._save()

    @on(Checkbox.Changed, "#outlier-both")
    def _on_outlier_both(self, event: Checkbox.Changed) -> None:
        self.cfg.outlier_both = event.value
        self._save()

    @on(Input.Changed, "#outlier-k")
    def _on_outlier_k(self, event: Input.Changed) -> None:
        try:
            value = float(event.value)
        except ValueError:
            return
        if 1.0 <= value <= 20.0 and value != self.cfg.outlier_k:
            self.cfg.outlier_k = value
            self._save()

    @on(Input.Changed, "#outlier-window")
    def _on_outlier_window(self, event: Input.Changed) -> None:
        try:
            value = int(event.value)
        except ValueError:
            return
        if 5 <= value <= 201 and value != self.cfg.outlier_window:
            self.cfg.outlier_window = value
            self._save()

    # -- GitHub access --

    @staticmethod
    def _auth_help_text(gh_ok: bool) -> str:
        text = ("Runs are always fetched from the GitHub REST API; this only chooses whose credentials are sent. "
                "The GitHub CLI option is the default whenever `gh` is installed and signed in, so existing gh users "
                "never see a sign-in prompt. The built-in sign-in stores its own token in auth.json in the data "
                "directory. $GH_TOKEN / $GITHUB_TOKEN, when set, override both.")
        if not gh_ok:
            text = ("The GitHub CLI option is disabled because `gh` isn't installed or isn't signed in — install it "
                    "from https://cli.github.com/ and run `gh auth login`, then reopen Settings. Until then the "
                    "built-in sign-in is used.  " + text)
        return text

    def _refresh_auth_status(self) -> None:
        self.query_one("#auth-status", Static).update(auth_status_text())
        self.query_one("#auth-logout", Button).disabled = not AUTH_FILE.exists()

    @on(RadioSet.Changed, "#auth-mode")
    def _on_auth_mode(self, event: RadioSet.Changed) -> None:
        mode = "gh" if (event.pressed.id == "auth-gh") else "rest"
        settings_set(GLOBAL_SCOPE, "auth_mode", mode)
        resolve_token()
        self._refresh_auth_status()
        if AUTH.token is None:
            self.app.push_screen(LoginScreen(), self._after_login)

    @on(Button.Pressed, "#auth-login")
    def _sign_in(self) -> None:
        if self.query_one("#auth-gh", RadioButton).value:
            # Signing in explicitly means the built-in login from now on.
            with self.prevent(RadioSet.Changed):
                self.query_one("#auth-rest", RadioButton).value = True
            settings_set(GLOBAL_SCOPE, "auth_mode", "rest")
        self.app.push_screen(LoginScreen(), self._after_login)

    def _after_login(self, token: str | None) -> None:
        resolve_token()
        self._refresh_auth_status()

    @on(Button.Pressed, "#auth-logout")
    def _sign_out(self) -> None:
        clear_saved_token()
        resolve_token()
        self._refresh_auth_status()
        if AUTH.token is None:
            self.query_one("#auth-status", Static).update(
                "Signed out. The app will ask you to sign in on the next sync.")

    @on(Button.Pressed, "#db-reveal")
    def _reveal_db(self) -> None:
        try:
            reveal_in_file_manager(CACHE_DB)
        except Exception as exc:
            self.query_one("#db-status", Static).update(f"Couldn't open {FILE_MANAGER_NAME}: {exc}")

    @on(Button.Pressed, "#db-apply")
    @on(Input.Submitted, "#db-path")
    def _apply_db_path(self) -> None:
        status = self.query_one("#db-status", Static)
        raw = self.query_one("#db-path", Input).value.strip()
        if not raw:
            status.update("Enter a path for the database file")
            return
        new_path = Path(raw).expanduser()
        if new_path.is_dir():
            new_path = new_path / DB_FILENAME
        if getattr(self.app, "loading", False):
            status.update("A sync is running — wait for it to finish before switching databases")
            return
        try:
            message = switch_db(new_path)
        except Exception as exc:
            log.exception("Switching database failed")
            status.update(f"Couldn't switch: {exc}")
            return
        self.query_one("#db-path", Input).value = str(CACHE_DB)
        status.update(message)
        self.db_changed = True
        self.changed = True

    @on(DataTable.RowSelected, "#workflows-table")
    @on(DataTable.RowSelected, "#jobs-table")
    @on(DataTable.RowSelected, "#branches-table")
    def _toggle_row(self, event: DataTable.RowSelected) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        name = str(event.row_key.value)
        kind = (event.data_table.id or "").removesuffix("-table")
        if kind == "branches":
            included = set(self._included_branches())
            included.symmetric_difference_update({name})
            self.cfg.included_branches = included  # first edit persists the opt-in set
        else:
            excluded = self._excluded_set(kind)
            if name in excluded:
                excluded.discard(name)
            else:
                excluded.add(name)
        self._save()
        self._refresh_all()

    def _excluded_set(self, kind: str) -> set[str]:
        return {"workflows": self.cfg.excluded_workflows, "jobs": self.cfg.excluded_jobs}[kind]

    def _all_names(self, kind: str) -> set[str]:
        if kind == "workflows":
            return set(self._workflow_counts())
        if kind == "jobs":
            return set(self._job_counts()[0])
        return set(self._branch_counts())

    @on(Button.Pressed, "#workflows-include-all")
    @on(Button.Pressed, "#jobs-include-all")
    @on(Button.Pressed, "#branches-include-all")
    def _include_all(self, event: Button.Pressed) -> None:
        kind = (event.button.id or "").removesuffix("-include-all")
        if kind == "branches":
            self.cfg.included_branches = set(self._branch_counts())
        else:
            self._excluded_set(kind).clear()
        self._save()
        self._refresh_all()

    @on(Button.Pressed, "#workflows-exclude-all")
    @on(Button.Pressed, "#jobs-exclude-all")
    @on(Button.Pressed, "#branches-exclude-all")
    def _exclude_all(self, event: Button.Pressed) -> None:
        kind = (event.button.id or "").removesuffix("-exclude-all")
        if kind == "branches":
            self.cfg.included_branches = set()
        else:
            self._excluded_set(kind).update(self._all_names(kind))
        self._save()
        self._refresh_all()

    def _set_editing(self, group: str | None) -> None:
        """Load a group into the editor (or clear it when None)."""
        self._editing = group
        self._render_groups()  # list contents depend on what's being edited
        members = self.query_one("#group-members", SelectionList)
        with self.prevent(SelectionList.SelectedChanged):
            members.deselect_all()
            if group:
                for m in self.cfg.job_groups.get(group, []):
                    members.select(m)
        self.query_one("#group-name", Input).value = group or ""
        self.query_one("#group-create", Button).label = "Save group" if group else "Group selected"
        self.query_one("#group-new").display = group is not None
        self.query_one("#group-editing", Static).update(
            f"Editing group “{group}” — change the name or members, then Save group (Esc or Cancel edit to stop)."
            if group else ""
        )
        self.query_one("#group-error", Static).update("")

    @on(DataTable.RowSelected, "#groups-table")
    def _on_group_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self._set_editing(str(event.row_key.value))

    @on(Button.Pressed, "#group-new")
    def _new_group(self) -> None:
        self._set_editing(None)

    @on(Button.Pressed, "#group-create")
    @on(Input.Submitted, "#group-name")
    def _create_group(self) -> None:
        error = self.query_one("#group-error", Static)
        name = self.query_one("#group-name", Input).value.strip()
        chosen = list(self.query_one("#group-members", SelectionList).selected)
        if not name:
            error.update("Give the group a name")
            return
        if not chosen:
            error.update("Check at least one job to put in the group")
            return
        editing = self._editing
        if editing and editing in self.cfg.job_groups:
            del self.cfg.job_groups[editing]  # re-added below under its (possibly new) name
        # A job belongs to one group: pull the chosen jobs out of any other group first
        for group, members in list(self.cfg.job_groups.items()):
            remaining = [m for m in members if m not in chosen]
            if remaining:
                self.cfg.job_groups[group] = remaining
            else:
                del self.cfg.job_groups[group]
        existing = self.cfg.job_groups.get(name, [])
        self.cfg.job_groups[name] = existing + [m for m in chosen if m not in existing]
        if editing and editing != name:
            # Things attached to the old group name follow the rename
            if editing in self.cfg.excluded_jobs:
                self.cfg.excluded_jobs.discard(editing)
                self.cfg.excluded_jobs.add(name)
            moved = notes_rename_job(self.repo, editing, name)
            log.info("Renamed job group %r -> %r (%d notes updated)", editing, name, moved)
        self._save()
        self._set_editing(None)
        self._refresh_all()
        log.info("Job group %r = %s", name, self.cfg.job_groups[name])

    @on(Button.Pressed, "#group-remove")
    def _remove_group(self) -> None:
        table = self.query_one("#groups-table", DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return
        key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key
        group = str(key.value) if key and key.value is not None else None
        if group and group in self.cfg.job_groups:
            del self.cfg.job_groups[group]
            self._save()
            if self._editing == group:
                self._set_editing(None)
            self._refresh_all()
            log.info("Removed job group %r", group)

    @on(Button.Pressed, "#settings-close")
    def action_close(self) -> None:
        if self._editing and self.query_one("#settings-content", ContentSwitcher).current == "groups":
            self._set_editing(None)  # Esc backs out of group editing before it closes Settings
            return
        self.dismiss(self.changed)


@dataclass
class ChartGeometry:
    """Where plotext drew the first chart's canvas inside the rendered text."""

    top: int      # row of the top border
    bottom: int   # row of the bottom axis
    left: int     # first canvas column
    right: int    # last canvas column

    def col_to_fraction(self, col: int) -> float:
        """0.0 at the left edge of the canvas, 1.0 at the right (the x axis is time)."""
        span = max(1, self.right - self.left)
        return (min(max(col, self.left), self.right) - self.left) / span


def chart_geometry(text: RichText) -> ChartGeometry | None:
    lines = [line.plain for line in text.split("\n")]
    # The frame's top-left corner is the first ┌; the x axis is the *last* line with a
    # └ (the legend box, drawn inside the canvas, has corners of its own).
    top = next((i for i, l in enumerate(lines) if "┌" in l), None)
    bottom = next((i for i in range(len(lines) - 1, -1, -1) if "└" in lines[i]), None)
    if top is None or bottom is None:
        return None
    axis = lines[bottom]
    left = axis.index("└") + 1
    right = axis.rindex("┘") - 1 if "┘" in axis else len(axis) - 1
    if right <= left:
        return None
    return ChartGeometry(top, bottom, left, right)


class TrendChart(Static):
    """The Trends body. Renders the chart parts and supports click-and-drag on the
    first chart to zoom: the selection is highlighted while dragging and, on
    release, a ZoomSelected message carries the two run indices."""

    class ZoomSelected(Message):
        def __init__(self, start_fraction: float, end_fraction: float) -> None:
            super().__init__()
            self.start_fraction = start_fraction
            self.end_fraction = end_fraction

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._parts: list = []
        # (part index, row offset of that part within the body, geometry) for every
        # chart that shares the time axis — the trend chart and the group-member chart
        self._charts: list[tuple[int, int, ChartGeometry]] = []
        self._drag_chart: tuple[int, int, ChartGeometry] | None = None
        self._drag_start: int | None = None
        self._drag_cur: int | None = None

    def set_content(self, parts: list, geoms: dict[int, ChartGeometry | None]) -> None:
        """`geoms` maps a part index to that chart's geometry (None for non-draggable parts)."""
        self._parts = parts
        self._charts = []
        row = 0
        for i, part in enumerate(parts):
            geom = geoms.get(i)
            if geom is not None:
                self._charts.append((i, row, geom))
            text = part.plain if isinstance(part, RichText) else str(part)
            row += text.count("\n") + 1
        self._drag_chart = None
        self._drag_start = self._drag_cur = None
        self.update(RichGroup(*parts))

    def set_message(self, text: str) -> None:
        self._parts, self._charts = [], []
        self.update(text)

    def _col(self, event: events.MouseEvent) -> int:
        return event.offset.x - int(self.styles.padding.left)

    def _chart_at(self, event: events.MouseEvent) -> tuple[int, int, ChartGeometry] | None:
        y = event.offset.y - int(self.styles.padding.top)
        for entry in self._charts:
            _, row, g = entry
            if row + g.top <= y <= row + g.bottom:
                return entry
        return None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        hit = self._chart_at(event)
        if hit is None:
            return
        self._drag_chart = hit
        self._drag_start = self._drag_cur = self._col(event)
        self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag_start is None:
            return
        self._drag_cur = self._col(event)
        self._render_drag()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._drag_start is None:
            return
        self.release_mouse()
        start, end = self._drag_start, self._col(event)
        chart = self._drag_chart
        self._drag_chart = None
        self._drag_start = self._drag_cur = None
        self.update(RichGroup(*self._parts))
        if chart is not None and abs(end - start) >= 1:
            g = chart[2]
            f0, f1 = sorted((g.col_to_fraction(start), g.col_to_fraction(end)))
            self.post_message(self.ZoomSelected(f0, f1))

    def _render_drag(self) -> None:
        if self._drag_chart is None or self._drag_start is None or self._drag_cur is None:
            return
        index, _, g = self._drag_chart
        part = self._parts[index]
        if not isinstance(part, RichText):
            return
        c0, c1 = sorted((self._drag_start, self._drag_cur))
        c0, c1 = max(c0, g.left), min(c1, g.right)
        lines = part.copy().split("\n")
        highlight = RichStyle(bgcolor="#3A3450")
        for row in range(g.top + 1, g.bottom):
            if row < len(lines):
                lines[row].stylize(highlight, c0, c1 + 1)
        parts = list(self._parts)
        parts[index] = RichText("\n").join(lines)
        self.update(RichGroup(*parts))


class DateRangeScreen(ModalScreen[tuple[date, date] | None]):
    """From / Through date picker for the Custom time range (whole days)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    CSS = """
    DateRangeScreen {
        align: center middle;
    }
    #range-box {
        width: 56;
        height: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #range-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    #range-box Label {
        margin-top: 1;
    }
    #range-error {
        color: $error;
        height: 1;
        margin-top: 1;
    }
    #range-buttons {
        height: 3;
        margin-top: 1;
    }
    #range-buttons Button {
        margin-right: 1;
    }
    """

    def __init__(self, current: tuple[date, date] | None) -> None:
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        start, end = self._current or (None, None)
        with Vertical(id="range-box"):
            yield Label("Custom time range (UTC dates, inclusive)", id="range-title")
            yield Label("From: YYYY-MM-DD")
            yield Input(value=start.isoformat() if start else "", placeholder="2025-01-01", id="range-from")
            yield Label("Through: YYYY-MM-DD")
            yield Input(value=end.isoformat() if end else "", placeholder=date.today().isoformat(), id="range-through")
            yield Static("", id="range-error")
            with Horizontal(id="range-buttons"):
                yield Button("Apply", id="range-apply", variant="primary")
                yield Button("Cancel", id="range-cancel")

    def on_mount(self) -> None:
        self.query_one("#range-from", Input).focus()

    @on(Button.Pressed, "#range-apply")
    @on(Input.Submitted)
    def _apply(self) -> None:
        error = self.query_one("#range-error", Static)
        try:
            start = date.fromisoformat(self.query_one("#range-from", Input).value.strip())
            end = date.fromisoformat(self.query_one("#range-through", Input).value.strip())
        except ValueError:
            error.update("Dates must be YYYY-MM-DD")
            return
        if end < start:
            error.update("Through must be on or after From")
            return
        self.dismiss((start, end))

    @on(Button.Pressed, "#range-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Filter sidebar — one instance per data tab, all views of the App's filter state
# ---------------------------------------------------------------------------

MAX_BRANCH_OPTIONS = 12
DEFAULT_BRANCHES = ("main", "master", "develop", "dev", "trunk")
TIME_RANGE_BUTTONS = [("1d", "Day"), ("1m", "Mo"), ("3m", "3Mo"), ("6m", "6Mo"), ("1y", "1Yr"), ("all", "All")]
CUSTOM_RANGE = "custom"


class FilterSidebar(Vertical):
    """Workflow / job / branch / time-range / display filters.

    Lives inside each data tab (Trends, Runs). The App owns the selected
    values; every sidebar is re-populated from that state, so switching tabs
    shows the same filters. Child widgets use classes, not ids, because two
    sidebars exist in the DOM at once.
    """

    DEFAULT_CSS = """
    FilterSidebar {
        width: 30;
        height: 1fr;
        border-right: solid $secondary;
        padding: 1 0 1 1;
        background: $surface;
    }
    FilterSidebar > Label {
        text-style: bold;
        color: $primary;
    }
    FilterSidebar OptionList, FilterSidebar SelectionList, FilterSidebar Checkbox {
        background: $surface;
        border: none;
        padding: 0;
    }
    /* List heights are set in fit_lists(): sized to content, shrunk only when they
       don't all fit, so filters stay packed at the top. */
    FilterSidebar .workflow-select, FilterSidebar .job-select, FilterSidebar .branch-select {
        height: auto;
        margin-bottom: 1;
    }
    FilterSidebar .time-range-bar {
        height: auto;
        padding: 0;
        margin-bottom: 1;
        layout: grid;
        grid-size: 3;
        grid-gutter: 0 1;
    }
    FilterSidebar .time-range-bar Button {
        min-width: 4;
        width: 100%;
        background: $panel;
        color: $text-muted;
    }
    FilterSidebar .time-range-bar Button:hover {
        background: $secondary 40%;
        color: $text;
    }
    FilterSidebar .time-range-bar Button.tr-custom {
        column-span: 3;
    }
    FilterSidebar .time-range-bar Button.-selected {
        background: $primary;
        color: $background;
        text-style: bold;
    }
    FilterSidebar OptionList:focus, FilterSidebar SelectionList:focus {
        border: none;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Workflow")
        yield OptionList(classes="workflow-select")
        yield Label("Job")
        yield OptionList(classes="job-select")
        yield Label("Branches")
        yield SelectionList[str](classes="branch-select")
        yield Label("Time Range")
        with Horizontal(classes="time-range-bar"):
            for key, label in TIME_RANGE_BUTTONS:
                yield Button(label, name=key, classes="tr", compact=True)
            yield Button("Custom", name=CUSTOM_RANGE, classes="tr tr-custom", compact=True)
        yield Label("Display")
        yield Checkbox("Y-axis from 0", value=False, classes="y-from-zero")

    def populate(
        self,
        workflows: list[str], selected_workflow: str,
        jobs: list[str], selected_job: str,
        branches: list[tuple[str, int]], selected_branches: set[str],
        timerange: str, y_from_zero: bool, custom_range: tuple[date, date] | None = None,
    ) -> None:
        """Render the given filter state without emitting change events."""
        app = self.app
        with app.prevent(OptionList.OptionHighlighted, SelectionList.SelectedChanged, Checkbox.Changed):
            wf = self.query_one(".workflow-select", OptionList)
            wf.clear_options()
            wf.add_options([Option(n, id=n) for n in workflows])
            wf.highlighted = workflows.index(selected_workflow) if selected_workflow in workflows else 0

            jl = self.query_one(".job-select", OptionList)
            jl.clear_options()
            jl.add_options([Option(n, id=n) for n in jobs])
            jl.highlighted = jobs.index(selected_job) if selected_job in jobs else 0

            bl = self.query_one(".branch-select", SelectionList)
            bl.clear_options()
            bl.add_options([Selection(f"{b}  ({c})", b, b in selected_branches) for b, c in branches])

            for btn in self.query(".time-range-bar Button"):
                btn.set_class(btn.name == timerange, "-selected")
                if btn.name == CUSTOM_RANGE:
                    btn.label = f"{custom_range[0]} → {custom_range[1]}" if custom_range else "Custom"

            self.query_one(".y-from-zero", Checkbox).value = y_from_zero
        self.call_after_refresh(self.fit_lists)

    MIN_LIST_ROWS = 3

    @staticmethod
    def _rows_needed(widget: OptionList) -> int:
        """Rows to show every option: OptionList wraps long labels (SelectionList truncates)."""
        if isinstance(widget, SelectionList):
            return widget.option_count
        width = widget.content_size.width or 28
        rows = 0
        for i in range(widget.option_count):
            prompt = widget.get_option_at_index(i).prompt
            text = prompt if isinstance(prompt, str) else getattr(prompt, "plain", str(prompt))
            rows += max(1, -(-len(text) // width))  # ceil
        return rows

    def fit_lists(self) -> None:
        """Give each list exactly the rows its items need; if the three together
        overflow the sidebar, share the free space (water-fill, min 3 rows each) so
        only the lists that don't fit become scrollable."""
        lists = [
            self.query_one(".workflow-select", OptionList),
            self.query_one(".job-select", OptionList),
            self.query_one(".branch-select", SelectionList),
        ]
        wants = [max(1, self._rows_needed(w)) for w in lists]
        fixed = sum(
            c.outer_size.height + c.styles.margin.height for c in self.children if c not in lists
        ) + sum(w.styles.margin.height for w in lists)
        available = self.content_size.height - fixed
        if available <= 0:
            return  # not laid out yet
        if sum(wants) <= available:
            heights = wants
        else:
            heights = [0] * len(lists)
            remaining = available
            order = sorted(range(len(lists)), key=lambda i: wants[i])
            for n, i in enumerate(order):
                share = remaining // (len(lists) - n)
                heights[i] = max(min(wants[i], self.MIN_LIST_ROWS), min(wants[i], share))
                remaining -= heights[i]
        for widget, h in zip(lists, heights):
            widget.styles.height = h


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class GHAExplorerApp(App):
    TITLE = "GHA Explorer"
    CSS = """
    Screen {
        background: $background;
        layers: base toggles notes;
    }
    #trends-header {
        height: 1;
        padding: 0 1;
    }
    #trends-stats {
        width: 1fr;
        height: 1;
    }
    #notes-button {
        width: auto;
        height: 1;
        margin-left: 2;
        color: $error;
        text-style: bold;
    }
    #notes-button:hover {
        text-style: bold reverse;
    }
    /* Top bar: tabs at the far left, sync status inline, repo at the far right */
    #top-bar {
        height: 2;
        background: $panel;
    }
    #tabs {
        width: 22;  /* refined to the exact tab widths in _fit_tabs() */
        height: 2;
        margin-right: 2;
    }
    #tabs Tab {
        padding: 0 1;
    }
    #status-bar {
        width: 1fr;
        height: 1;
        color: $text;
    }
    #repo-label {
        width: auto;
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    #settings-button {
        width: auto;
        height: 1;
        color: $primary;
        padding: 0 1;
    }
    #settings-button:hover {
        text-style: reverse;
    }
    #content {
        height: 1fr;
    }
    .tab-body {
        width: 1fr;
        height: 1fr;
    }
    #runs-table {
        height: 1fr;
    }
    #trends-scroll {
        height: 1fr;
        /* Always reserve the scrollbar's 2 columns: charts are sized to this pane's
           width, and a scrollbar appearing after a render would otherwise wrap them. */
        scrollbar-gutter: stable;
    }
    #trends-body {
        height: auto;
        padding: 0 1;
    }
    .sidebar-strip {
        width: 3;
        height: 1fr;
        border-right: solid $secondary;
        background: $surface;
        display: none;
    }
    /* The < / > sidebar toggle: one per tab, on its own layer, docked bottom and
       offset so it sits astride the sidebar's border line (see _apply_sidebar_visibility). */
    .sidebar-toggle {
        layer: toggles;
        dock: bottom;
        min-width: 5;
        width: 5;
        height: 3;
        border: round $primary;
        background: $surface;
        color: $primary;
        text-style: bold;
    }
    .sidebar-toggle:hover {
        background: $primary 30%;
        color: $text;
        border: round $primary;
    }
    .sidebar-toggle:focus {
        border: round $primary;
        text-style: bold;
    }

    /* Status tab */
    .status-pane {
        height: 1fr;
        padding: 0 1;
    }
    #status-cards {
        height: auto;
        margin-bottom: 1;
    }
    .card {
        width: 1fr;
        height: auto;
        border: round $secondary;
        padding: 0 1;
        margin-right: 1;
    }
    .card:last-of-type {
        margin-right: 0;
    }
    .card-title {
        text-style: bold;
        color: $primary;
    }
    .card Static {
        height: auto;
    }
    #log-title {
        text-style: bold;
        color: $primary;
    }
    #log-view {
        height: 1fr;
        border: round $secondary;
        background: $surface;
        padding: 0 1;
    }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("R", "full_rescan", "Full rescan", show=False),
        Binding("s", "switch_repo", "Switch Repo"),
        Binding("f", "toggle_sidebar", "Filters"),
        Binding("n", "open_notes", "Notes"),
        Binding("comma", "open_settings", "Settings"),
        Binding("escape", "close_bubbles", "Close note", show=False),
        Binding("1", "tab_trends", "Trends"),
        Binding("2", "tab_runs", "Runs"),
        Binding("3", "tab_status", "Status"),
    ]

    runs: reactive[list[RunData]] = reactive(list, init=False)
    current_repo: reactive[str] = reactive("", init=False)
    selected_workflow: reactive[str] = reactive("All workflows", init=False)
    selected_job: reactive[str] = reactive("Pipeline", init=False)
    selected_timerange: reactive[str] = reactive("all", init=False)
    loading: reactive[bool] = reactive(False)

    def __init__(self, initial_repo: str | None = None, theme_name: str | None = None):
        super().__init__()
        self._initial_repo = initial_repo
        self._resize_timer = None
        self._force_backfill = False
        self._selected_branches: set[str] = set()
        self._y_from_zero = False
        self._custom_range: tuple[date, date] | None = None
        self._plot_run_dates: list[datetime] = []
        migrate_config_json()
        self._sidebar_visible = bool(settings_get(GLOBAL_SCOPE, "sidebar_visible", True))
        self._notes: list[Note] = []
        self._config = RepoConfig()
        self._view_runs: list[RunData] = []
        self._last_settings_screen: SettingsScreen | None = None
        self._log_count = 0
        self.register_theme(GHA_THEME)
        self.theme = theme_name or GHA_THEME.name

    # App-level keys only make sense on the main screen; hide them (and stop them
    # firing) while Settings, the notes manager, the picker or a dialog is on top.
    MAIN_SCREEN_ACTIONS = {
        "refresh", "full_rescan", "switch_repo", "toggle_sidebar", "open_notes",
        "open_settings", "close_bubbles", "tab_trends", "tab_runs", "tab_status",
    }

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in self.MAIN_SCREEN_ACTIONS and self.screen is not self.screen_stack[0]:
            return False
        return True

    # -- Palette --

    @property
    def palette(self) -> PlotPalette:
        try:
            return PlotPalette.from_theme(self.current_theme)
        except Exception:
            return DEFAULT_PALETTE

    # -- Layout --

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-bar"):
            yield Tabs(Tab("Trends", id="trends"), Tab("Runs", id="runs-tab"), Tab("Status", id="status-tab"), id="tabs")
            yield Static("Loading...", id="status-bar")
            yield Static("", id="repo-label")
            yield Static("[@click=app.open_settings]⚙ Settings[/]", id="settings-button")
        with ContentSwitcher(initial="trends", id="content"):
            with Horizontal(id="trends"):
                yield Vertical(classes="sidebar-strip")
                yield FilterSidebar()
                yield Button("<", classes="sidebar-toggle")
                with Vertical(classes="tab-body"):
                    with Horizontal(id="trends-header"):
                        yield Static("", id="trends-stats")
                        yield Static("", id="notes-button")
                    with VerticalScroll(id="trends-scroll"):
                        yield TrendChart(id="trends-body")
            with Horizontal(id="runs-tab"):
                yield Vertical(classes="sidebar-strip")
                yield FilterSidebar()
                yield Button("<", classes="sidebar-toggle")
                yield DataTable(id="runs-table", classes="tab-body")
            with Vertical(id="status-tab", classes="status-pane"):
                with Horizontal(id="status-cards"):
                    with Vertical(classes="card", id="card-sync"):
                        yield Label("Sync", classes="card-title")
                        yield Static("idle", id="sync-phase")
                        yield Gauge("Progress", id="sync-progress")
                        yield Static("", id="sync-details")
                    with Vertical(classes="card", id="card-api"):
                        yield Label("GitHub API", classes="card-title")
                        yield Static("", id="api-status")
                        yield Gauge("Remaining", id="rate-gauge")
                        yield Static("", id="api-details")
                    with Vertical(classes="card", id="card-cache"):
                        yield Label("Cache", classes="card-title")
                        yield Static("", id="cache-details")
                yield Label("Log  (INFO+ · full DEBUG log in gha_explorer.log)", id="log-title")
                yield RichLog(id="log-view", highlight=False, markup=False, wrap=True, max_lines=1000)
        yield Footer()

    def on_mount(self) -> None:
        log.info("App mounted (theme=%s)", self.theme)
        self._apply_sidebar_visibility()
        self.call_after_refresh(self._fit_tabs)
        self.set_interval(0.25, self._tick)
        resolve_token()
        if AUTH.token is None:
            self._update_status_bar("", "Sign in to GitHub to begin...")
            self.push_screen(LoginScreen(), self._on_signed_in)
        else:
            self._begin()

    def _on_signed_in(self, token: str | None) -> None:
        if not token:
            self.exit(message="Sign-in cancelled — GHA Explorer needs GitHub access to fetch runs.")
            return
        self._begin()

    def _begin(self) -> None:
        """Start up once credentials are in place: rate-limit polling, then the repo."""
        self.set_interval(30, self._poll_rate_limit)
        self._poll_rate_limit()
        # --repo wins; then the GitHub repo of the directory we were launched in (so
        # `uvx gha-explorer` inside a checkout just works); then the last-used repo.
        detected = None if self._initial_repo else detect_current_repo()
        repo = self._initial_repo or detected or settings_get(GLOBAL_SCOPE, "current_repo")
        if detected and detected != settings_get(GLOBAL_SCOPE, "current_repo"):
            log.info("Detected %s from the current directory", detected)
            self.notify(f"Exploring {detected} — detected from the current directory. Press s to switch.", timeout=6)
        if repo:
            self._use_repo(repo)
            saved_tab = settings_get(GLOBAL_SCOPE, "active_tab", "trends")
            if saved_tab in ("trends", "runs-tab", "status-tab") and saved_tab != "trends":
                self.call_after_refresh(lambda: setattr(self.query_one(Tabs), "active", saved_tab))
        else:
            self._update_status_bar("", "Select a repository to begin...")
            self.push_screen(RepoPickerScreen(), self._on_repo_picked)

    # -- Repo selection --

    def _use_repo(self, repo: str) -> None:
        """Activate the given repo — load its cached data and start the fetch."""
        log.info("Using repo: %s", repo)
        self.current_repo = repo
        self.query_one("#repo-label", Static).update(repo)
        settings_set(GLOBAL_SCOPE, "current_repo", repo)
        self._load_filters(repo)
        self._notes = notes_list(repo)
        self._config = load_repo_config(repo)
        cached = cache_load_all(repo)
        self.runs = cached
        ensure_branch_inclusion(self._config, cached)  # in memory; persisted only if the user edits branches
        self._view_runs = apply_repo_config(cached, self._config)
        self._populate_sidebar()
        if cached:
            log.info("Showing %d cached runs immediately", len(cached))
            self._update_status_bar(self._cache_status_text(), "Syncing...")
            self.set_timer(0.1, self._render_all_tabs)
        else:
            self._update_status_bar(f"{repo}: no cache", "First load — fetching from GitHub...")
            self._render_all_tabs()
        self._start_fetch()

    def _on_repo_picked(self, repo: str | None) -> None:
        if not repo:
            if not self.current_repo:
                self.exit()
            return
        if self.loading:
            self.notify("A sync is still running — switching repo after it finishes.", severity="warning")
            return
        self._use_repo(repo)

    def action_switch_repo(self) -> None:
        self.push_screen(RepoPickerScreen(), self._on_repo_picked)

    # -- Status bar + status tab --

    def _update_status_bar(self, cache_msg: str = "", activity_msg: str = "") -> None:
        try:
            status = self.query_one("#status-bar", Static)
            text = RichText()
            if cache_msg:
                text.append(cache_msg)
            if activity_msg:
                if cache_msg:
                    text.append("  ·  ", style="#3A3450")
                text.append(activity_msg, style=self.palette.primary_hex)
            rl = STATS.rate_limit
            if rl:
                text.append("  ·  ", style="#3A3450")
                frac = rl["remaining"] / rl["limit"] if rl.get("limit") else 1
                color = self.palette.error_hex if frac < 0.1 else self.palette.warning_hex if frac < 0.3 else self.palette.muted_hex
                text.append(f"API {rl['remaining']:,}/{rl['limit']:,}", style=color)
            status.update(text if text.plain else "Loading...")
        except Exception:
            pass

    def _cache_status_text(self) -> str:
        runs = self.runs
        if not runs:
            return "No cached data"
        oldest = runs[0].created_at.strftime("%Y-%m-%d")
        newest = runs[-1].created_at.strftime("%Y-%m-%d")
        return f"{len(runs)} runs ({oldest} → {newest})"

    def _activity_text(self, s: dict) -> str:
        labels = {"details": "Fetching job timings", "forward": "Listing runs", "backfill": "Checking older history"}
        if s["phase"] in labels and s["total"]:
            done, total = min(s["done"], s["total"]), s["total"]
            pct = int(done / total * 100)
            filled = int(done / total * 20)
            bar = "━" * filled + "╌" * (20 - filled)
            eta = self._phase_eta(s)
            tail = f" · ~{fmt_elapsed(eta)} left" if eta else ""
            return f"{labels[s['phase']]}  {bar}  {pct}% ({done:,}/{total:,}){tail}"
        return s["message"] or ("Syncing..." if self.loading else "Idle")

    def _tick(self) -> None:
        """Quarter-second heartbeat: drain logs, refresh status bar + Status tab."""
        try:
            self._drain_logs()
            s = STATS.snapshot()
            if self.loading:
                self._update_status_bar(self._cache_status_text(), self._activity_text(s))
                if not self.runs and self.current_repo:
                    self._render_first_sync_panel(s)
            self._render_status(s)
        except Exception:
            log.debug("tick error", exc_info=True)

    # -- First-sync panel (main pane, while a repo has no cached data yet) --

    FIRST_SYNC_STEPS = (
        ("cache", "Load local cache"),
        ("forward", "List workflow runs"),
        ("details", "Fetch job timings"),
        ("backfill", "Check older history"),
    )
    DEFAULT_PAGE_SECONDS = 2.3     # one /actions/runs page of 100 (measured; GitHub is slow here)
    DEFAULT_DETAIL_RUNS_PER_S = 12  # jobs requests with MAX_WORKERS in flight, until measured

    @staticmethod
    def _phase_elapsed(s: dict) -> float:
        return max(0.0, time.monotonic() - (s.get("phase_started_at") or time.monotonic()))

    def _phase_eta(self, s: dict) -> float | None:
        """Seconds left in the current phase from its measured rate, or None if too early to say."""
        done, total = s["done"], s["total"]
        elapsed = self._phase_elapsed(s)
        if not total or done <= 0 or elapsed < 2:
            return None
        return max(0.0, (total - done) * elapsed / done)

    def _first_sync_step_index(self, s: dict) -> int:
        phase = s["phase"]
        if phase in ("done", "error", "rate-limited"):
            return len(self.FIRST_SYNC_STEPS)
        if phase == "details" and "backfill" in s["step_elapsed"]:
            return 3  # job timings for runs found while checking older history
        if phase == "gaps":
            return 3
        return {"cache": 0, "forward": 1, "details": 2, "backfill": 3}.get(phase, 0)

    def _render_first_sync_panel(self, s: dict) -> None:
        body = self.query_one("#trends-body", TrendChart)
        p = self.palette
        width = min(self._content_width() - 2, 96)
        now = time.monotonic()
        total_elapsed = now - (s["started_at"] or now)
        current = self._first_sync_step_index(s)
        phase_elapsed = self._phase_elapsed(s)
        done, total = s["done"], s["total"]

        # -- rates: measured in-phase when possible, else remembered / default
        page_s = self.DEFAULT_PAGE_SECONDS
        if s["phase"] == "forward" and done >= 100 and phase_elapsed > 2:
            page_s = phase_elapsed / max(1, -(-done // 100))
        elif s["step_elapsed"].get("forward") and s["listed_runs"] >= 100:
            page_s = s["step_elapsed"]["forward"] / max(1, -(-s["listed_runs"] // 100))
        detail_rate = float(settings_get(GLOBAL_SCOPE, "detail_runs_per_s", 0) or 0) or self.DEFAULT_DETAIL_RUNS_PER_S
        if s["phase"] == "details" and current == 2 and done >= 20 and phase_elapsed >= 3:
            detail_rate = done / phase_elapsed

        # -- how many runs are we talking about?
        runs_known = s["listed_runs"] or (total if s["phase"] == "forward" else 0)

        # -- remaining time: rest of the current step + full estimates of pending steps
        remaining: float | None = 0.0
        step_eta: dict[str, float | None] = {}
        for idx, (key, _label) in enumerate(self.FIRST_SYNC_STEPS):
            if idx < current:
                continue
            if idx == current:
                eta = self._phase_eta(s)
                if eta is None and key == "forward" and total:
                    eta = max(0.0, -(-(total - done) // 100) * page_s)
                elif eta is None and key == "details" and total:
                    eta = (total - done) / detail_rate
                elif eta is None and key == "backfill" and s["backfill_total"]:
                    eta = max(0, s["backfill_total"] - s["windows_done"]) * page_s
                step_eta[key] = eta
            else:
                if key == "forward":
                    step_eta[key] = None
                elif key == "details":
                    step_eta[key] = (runs_known / detail_rate) if runs_known else None
                elif key == "backfill":
                    step_eta[key] = (s["backfill_total"] or 5) * page_s
                else:
                    step_eta[key] = 0.0
            if remaining is not None:
                remaining = None if step_eta[key] is None else remaining + step_eta[key]

        # -- header
        out = RichText()
        out.append(f"First sync of {self.current_repo}", style=f"bold {p.primary_hex}")
        out.append(f"   elapsed {fmt_elapsed(total_elapsed)}", style=p.muted_hex)
        if remaining is not None and current < len(self.FIRST_SYNC_STEPS):
            out.append(f"   ·   about {fmt_elapsed(remaining) if remaining >= 10 else 'a few seconds'} left",
                       style=p.muted_hex)
        out.append("\n")
        out.append(
            f"GitHub's API hands out 100 runs per request (about {max(page_s, 0.5):.1f} s each), and every run needs one more "
            f"request for its job timings ({MAX_WORKERS} in flight at a time). Everything lands in the local SQLite "
            "cache, so this full pass happens once per repository — later launches fetch only new runs and show "
            "charts immediately.\n\n",
            style=p.muted_hex,
        )

        # -- steps
        for idx, (key, label) in enumerate(self.FIRST_SYNC_STEPS):
            if idx < current:
                glyph, style = "✓", p.success_hex
            elif idx == current:
                glyph, style = "▶", p.primary_hex
            else:
                glyph, style = "·", p.muted_hex
            out.append(f"  {glyph}  ", style=f"bold {style}")
            out.append(f"{label:<22}", style=("bold " if idx == current else "") + (style if idx <= current else p.muted_hex))
            out.append(self._first_sync_step_detail(key, idx, current, s, step_eta.get(key), page_s, detail_rate),
                       style=p.muted_hex if idx != current else "")
            out.append("\n")

        # -- overall bar: elapsed vs elapsed + remaining
        if current >= len(self.FIRST_SYNC_STEPS):
            frac = 1.0
        elif remaining is None:
            frac = None
        else:
            frac = total_elapsed / (total_elapsed + remaining) if (total_elapsed + remaining) > 0 else 0.0
        bar_w = max(20, width - 10)
        out.append("\n  ")
        if frac is None:
            out.append("╌" * bar_w, style="#3A3450")
            out.append("  estimating…", style=p.muted_hex)
        else:
            filled = int(round(min(1.0, frac) * bar_w))
            out.append("━" * filled, style=p.primary_hex)
            out.append("╌" * (bar_w - filled), style="#3A3450")
            out.append(f"  {int(min(1.0, frac) * 100):>3}%", style="bold")
        out.append("\n")
        if s["phase"] in ("rate-limited", "error"):
            out.append(f"\n  {s['message']}", style=p.warning_hex if s["phase"] == "rate-limited" else p.error_hex)
        elif s["message"]:
            out.append(f"\n  {s['message']}", style=p.muted_hex)
        body.set_message(out)

    def _first_sync_step_detail(self, key: str, idx: int, current: int, s: dict, eta: float | None,
                                page_s: float, detail_rate: float) -> str:
        done, total = s["done"], s["total"]
        spent = s["step_elapsed"].get(key, 0.0)
        left = f" · ~{fmt_elapsed(eta)} left" if eta and eta >= 5 else ""
        if key == "cache":
            return "nothing cached for this repository yet" if idx <= current else ""
        if key == "forward":
            if idx < current:
                return f"{s['listed_runs']:,} runs found in {fmt_elapsed(spent)}"
            if idx == current:
                if total:
                    return f"{min(done, total):,} of {total:,} runs · page {max(1, -(-done // 100))} of {-(-total // 100)}{left}"
                return "asking GitHub how many runs there are…"
            return "100 runs per request"
        if key == "details":
            if idx < current:
                return f"{s['new_runs']:,} runs in {fmt_elapsed(spent)}"
            if idx == current:
                rate = f" · {detail_rate:.0f} runs/s" if done >= 20 else ""
                return f"{min(done, total):,} of {total:,} runs{rate}{left}" if total else "starting…"
            runs = s["listed_runs"] or (total if s["phase"] == "forward" else 0)
            if runs:
                return f"~{fmt_elapsed(runs / detail_rate)} for {runs:,} runs, {MAX_WORKERS} requests at a time"
            return f"one request per run, {MAX_WORKERS} at a time"
        if key == "backfill":
            if idx < current:
                return f"{s['windows_done']} window{'s' if s['windows_done'] != 1 else ''} in {fmt_elapsed(spent)}"
            if idx == current:
                win = f" · {s['current_window']}" if s["current_window"] else ""
                if s["backfill_total"]:
                    return f"window {min(s['windows_done'] + 1, s['backfill_total'])} of ~{s['backfill_total']}{win}{left}"
                return f"looking up when the repository was created{win}"
            return f"back to the repository's creation date, {BACKFILL_WINDOW_DAYS} days per request"
        return ""

    def _drain_logs(self) -> None:
        view = self.query_one("#log-view", RichLog)
        level_colors = {
            "DEBUG": "#7E7599", "INFO": self.palette.muted_hex, "WARNING": self.palette.warning_hex,
            "ERROR": self.palette.error_hex, "CRITICAL": self.palette.error_hex,
        }
        while UI_LOG.records:
            ts, level, msg = UI_LOG.records.popleft()
            line = RichText()
            line.append(ts.strftime("%H:%M:%S "), style="#7E7599")
            line.append(f"{level[:4]:<5}", style=f"bold {level_colors.get(level, '')}")
            line.append(msg.splitlines()[0] if msg else "")
            view.write(line)
            self._log_count += 1

    def _render_status(self, s: dict) -> None:
        # Only do the work if the tab is visible (cheap, but no need otherwise)
        if self.query_one(ContentSwitcher).current != "status-tab":
            return
        p = self.palette
        phase = s["phase"]
        phase_colors = {
            "done": p.success_hex, "error": p.error_hex, "rate-limited": p.error_hex, "idle": p.muted_hex,
        }
        phase_text = RichText()
        labels = {"cache": "CACHE", "forward": "LIST", "details": "FETCH", "backfill": "BACKFILL", "gaps": "GAPS",
                  "done": "DONE", "error": "ERROR", "rate-limited": "LIMITED", "idle": "IDLE"}
        phase_text.append(f"{labels.get(phase, phase.upper()):<9}",
                          style=f"bold {phase_colors.get(phase, p.primary_hex)}")
        phase_text.append(s["message"] or "", style="")
        self.query_one("#sync-phase", Static).update(phase_text)

        gauge = self.query_one("#sync-progress", Gauge)
        if s["total"]:
            gauge.set_value(s["done"], s["total"], "runs" if phase == "details" else "runs (last batch)",
                            color=p.primary_hex)
        else:
            gauge.set_value(0, 0)

        elapsed = ""
        if s["started_at"]:
            end = s["finished_at"] or time.monotonic()
            elapsed = fmt_elapsed(end - s["started_at"])
        details = RichText()
        details.append(f"New runs this sync: {s['new_runs']:,}\n")
        details.append(f"Backfill windows:   {s['windows_done']}")
        if s["current_window"]:
            details.append(f"  ({s['current_window']})", style=p.muted_hex)
        details.append(f"\nElapsed:            {elapsed or '—'}")
        details.append(f"\nRepo:               {self.current_repo or '—'}")
        self.query_one("#sync-details", Static).update(details)

        rl = s["rate_limit"]
        api_status = self.query_one("#api-status", Static)
        rate_gauge = self.query_one("#rate-gauge", Gauge)
        if rl:
            reset_in = max(0, rl["reset"] - time.time())
            rate_gauge.set_value(rl["remaining"], rl["limit"], f"reset {fmt_elapsed(reset_in)}",
                                 color=p.success_hex, low_is_bad=True)
            checked = ""
            if s["rate_limit_checked_at"]:
                checked = f"checked {fmt_elapsed(time.monotonic() - s['rate_limit_checked_at'])} ago"
            api_status.update(RichText(f"core bucket · {checked}", style=p.muted_hex))
        else:
            rate_gauge.set_value(0, 0)
            api_status.update(RichText("rate limit unknown", style=p.muted_hex))
        api_details = RichText()
        auth_label = {"env": "$GH_TOKEN", "gh CLI": "gh CLI login", "saved login": "built-in login",
                      "none": "not signed in"}.get(AUTH.source, AUTH.source)
        if AUTH.login:
            auth_label += f" ({AUTH.login})"
        api_details.append(f"Auth:                {auth_label}\n")
        api_details.append(f"Calls this session:  {s['api_calls']:,}\n")
        api_details.append(f"Rate-limit retries:  {s['rate_limit_retries']}\n",
                           style=p.warning_hex if s["rate_limit_retries"] else "")
        api_details.append(f"Errors:              {s['api_errors']}",
                           style=p.error_hex if s["api_errors"] else "")
        if s["last_error"]:
            api_details.append(f"\nLast error: {s['last_error'][:80]}", style=p.error_hex)
        self.query_one("#api-details", Static).update(api_details)

        # Cache card — cheap SQL, but only every ~2s
        if int(time.monotonic() * 4) % 8 == 0 or not getattr(self, "_cache_card_drawn", False):
            self._cache_card_drawn = True
            cache_details = RichText()
            if self.current_repo:
                try:
                    c = cache_summary(self.current_repo)
                    cache_details.append(f"{self.current_repo}\n", style="bold")
                    cache_details.append(f"Runs cached:   {c['repo_rows']:,}\n")
                    cache_details.append(f"Span:          {c['oldest'] or '—'} → {c['newest'] or '—'}\n")
                    cache_details.append("Backfill:      ")
                    cache_details.append(
                        "complete\n" if c["backfill_complete"] else "incomplete\n",
                        style=p.success_hex if c["backfill_complete"] else p.warning_hex,
                    )
                    cache_details.append(f"All repos:     {c['total_rows']:,} runs / {c['repos']} repos\n")
                    cache_details.append(f"Notes:         {c['notes']}\n")
                    cache_details.append(f"DB size:       {fmt_bytes(c['db_bytes'])}\n")
                    cache_details.append(f"Database:      {CACHE_DB}", style=p.muted_hex)
                except Exception:
                    log.debug("cache summary failed", exc_info=True)
                    cache_details.append("unavailable", style=p.muted_hex)
            else:
                cache_details.append("no repo selected", style=p.muted_hex)
            self.query_one("#cache-details", Static).update(cache_details)

    @work(thread=True, exclusive=True, group="rate_limit", exit_on_error=False)
    def _poll_rate_limit(self) -> None:
        fetch_rate_limit()

    # -- Fetching --

    @work(thread=True, exit_on_error=False)
    def fetch_data(self) -> list[RunData]:
        repo = self.current_repo
        if not repo:
            return []
        force = self._force_backfill
        self._force_backfill = False
        data = fetch_incremental(repo, force_backfill=force)
        log.info("Fetch complete for %s: %d total runs", repo, len(data))
        return data

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "fetch_data":
            return
        if event.state == WorkerState.SUCCESS:
            self._data_loaded(event.worker.result)
        elif event.state == WorkerState.ERROR:
            log.error("Worker failed: %s", event.worker.error)
            self._data_error(str(event.worker.error))
            if isinstance(event.worker.error, AuthError):
                self._handle_auth_error()

    def _handle_auth_error(self) -> None:
        """GitHub returned 401: drop a bad saved login and ask the user to sign in again."""
        source = AUTH.source
        if source == "saved login":
            clear_saved_token()
        reason = {
            "env": "GitHub rejected the token in $GH_TOKEN / $GITHUB_TOKEN. Fix or unset it, or sign in here "
                   "(the environment variable will still take precedence next launch).",
            "gh CLI": "GitHub rejected the GitHub CLI's token. Run `gh auth login`, or sign in here instead.",
            "saved login": "Your saved login is no longer valid (revoked or expired). Sign in again.",
        }.get(source, "GitHub requires authentication to read this repository's workflow runs.")
        if any(isinstance(scr, LoginScreen) for scr in self.screen_stack):
            return
        self.push_screen(LoginScreen(reason=reason), self._on_reauth)

    def _on_reauth(self, token: str | None) -> None:
        if token:
            resolve_token()
            self._start_fetch()
        else:
            self._update_status_bar(self._cache_status_text(), "Not signed in — press r to retry after signing in.")

    def _data_loaded(self, data: list[RunData]) -> None:
        try:
            self.runs = data
            ensure_branch_inclusion(self._config, data)
            self._view_runs = apply_repo_config(data, self._config)
            self.loading = False
            self._update_status_bar(self._cache_status_text(), STATS.message or "Up to date")
            self._populate_sidebar()
            self._render_status(STATS.snapshot())
            self.set_timer(0.1, self._render_all_tabs)
            self._poll_rate_limit()
        except Exception:
            log.exception("Error in _data_loaded")

    def _data_error(self, error: str) -> None:
        try:
            self.loading = False
            STATS.phase = "error"
            STATS.message = error[-120:]
            STATS.finished_at = time.monotonic()
            self._update_status_bar(
                self._cache_status_text(),
                f"Error: {error[-120:]}  (see {LOG_FILE.name})",
            )
        except Exception:
            log.exception("Error displaying error message")

    def _start_fetch(self) -> None:
        if self.loading:
            self.notify("A sync is already running.", severity="warning", timeout=3)
            return
        self.loading = True
        STATS.reset_for_sync()
        self._update_status_bar(self._cache_status_text(), "Refreshing...")
        self.fetch_data()

    def action_refresh(self) -> None:
        self._start_fetch()

    def action_full_rescan(self) -> None:
        """Re-run backfill and re-check every gap, even ones previously verified as quiet."""
        if self.loading:
            self.notify("A sync is already running.", severity="warning", timeout=3)
            return
        self._force_backfill = True
        self.notify("Full rescan: re-walking history back to repo creation.", timeout=4)
        self._start_fetch()

    # -- Custom time range / zoom --

    def action_custom_range(self) -> None:
        """Open the From/Through dialog, pre-filled with the current range or visible span."""
        current = self._custom_range
        if current is None and self._plot_run_dates:
            current = (self._plot_run_dates[0].date(), self._plot_run_dates[-1].date())
        self.push_screen(DateRangeScreen(current), self._on_custom_range_picked)

    def _on_custom_range_picked(self, result: tuple[date, date] | None) -> None:
        if result is None:
            return
        self._apply_custom_range(*result)

    def _apply_custom_range(self, start: date, end: date) -> None:
        self._custom_range = (min(start, end), max(start, end))
        self.selected_timerange = CUSTOM_RANGE
        self._populate_sidebar()
        self._save_filters()
        self._render_all_tabs()
        log.info("Custom time range %s → %s", *self._custom_range)

    @on(TrendChart.ZoomSelected)
    def _on_zoom_selected(self, message: TrendChart.ZoomSelected) -> None:
        """Drag on the chart → zoom to the days under the two endpoints (never sub-day)."""
        dates = self._plot_run_dates
        if not dates:
            return
        t0, span = dates[0], timedelta(days=x_span_days(dates))
        start = (t0 + span * message.start_fraction).date()
        end = (t0 + span * message.end_fraction).date()
        self._apply_custom_range(start, end)

    # -- Settings --

    def action_open_settings(self) -> None:
        if not self.current_repo:
            self.notify("Select a repository first.", severity="warning")
            return
        self.action_close_bubbles()
        self._last_settings_screen = SettingsScreen(self.current_repo, self.runs, self._config)
        self.push_screen(self._last_settings_screen, self._on_settings_closed)

    def _on_settings_closed(self, changed: bool | None) -> None:
        if not changed:
            return
        screen = self._last_settings_screen
        if screen is not None and screen.db_changed:
            # New database file: reload everything for the current repo from it
            self.notify(f"Using database {CACHE_DB}", timeout=5)
            self._use_repo(self.current_repo)
            return
        self._config = load_repo_config(self.current_repo)
        self._view_runs = apply_repo_config(self.runs, self._config)
        self._populate_sidebar()
        self.call_after_refresh(self._render_all_tabs)  # main screen is just back: let it lay out first
        ensure_branch_inclusion(self._config, self.runs)
        log.info("Repo settings applied: %d groups, %d/%d excluded workflows/jobs, %d branches included",
                 len(self._config.job_groups), len(self._config.excluded_workflows),
                 len(self._config.excluded_jobs), len(self._config.included_branches or []))

    # -- Notes --

    def action_open_notes(self) -> None:
        if not self.current_repo:
            self.notify("Select a repository first.", severity="warning")
            return
        self.action_close_bubbles()
        self.push_screen(NotesScreen(self.current_repo, self.selected_job, self._job_names()), self._on_notes_closed)

    def _on_notes_closed(self, changed: bool | None) -> None:
        self._notes = notes_list(self.current_repo)
        if changed:
            self._render_trends()

    def action_close_bubbles(self) -> None:
        for bubble in self.screen.query(NoteBubble):
            bubble.remove()

    def on_click(self, event: events.Click) -> None:
        """A click on a chart marker (ⓘ carrying note meta) opens that note's bubble."""
        note_id = event.style.meta.get("note") if event.style else None
        if note_id is None:
            return
        note = next(
            (n for n in notes_for_job(self._notes, self.selected_job, self._config) if n.id == note_id), None
        )
        if note is None:
            return
        self.action_close_bubbles()
        bubble = NoteBubble(note)
        self.screen.mount(bubble)
        width, height = 50, 8
        x = max(0, min(event.screen_x + 1, self.size.width - width - 1))
        y = event.screen_y + 1
        if y + height > self.size.height - 1:
            y = max(0, event.screen_y - height)
        bubble.styles.offset = (x, y)

    @on(NoteBubble.EditRequested)
    def _on_note_edit_requested(self, message: NoteBubble.EditRequested) -> None:
        self.action_close_bubbles()
        self.push_screen(
            NotesScreen(self.current_repo, self.selected_job, self._job_names(), edit_note_id=message.note.id),
            self._on_notes_closed,
        )

    @on(NoteBubble.DeleteRequested)
    def _on_note_delete_requested(self, message: NoteBubble.DeleteRequested) -> None:
        note = message.note

        def _confirmed(yes: bool | None) -> None:
            if not yes:
                return
            notes_delete(note.id)
            self.action_close_bubbles()
            self._notes = notes_list(self.current_repo)
            self.notify("Note deleted.", timeout=2)
            self._render_trends()

        self.push_screen(ConfirmScreen("Delete this note?", _note_preview(note)), _confirmed)

    # -- Rendering --

    def _render_all_tabs(self) -> None:
        try:
            t0 = time.monotonic()
            self._render_trends()
            self._render_runs_table()
            log.debug("All tabs rendered in %.2fs", time.monotonic() - t0)
        except Exception:
            log.exception("Error in _render_all_tabs")

    def _workflow_names(self) -> list[str]:
        counts: dict[str, int] = {}
        for r in self._view_runs:
            wf = r.workflow or "(unknown)"
            counts[wf] = counts.get(wf, 0) + 1
        return ["All workflows"] + sorted(counts, key=lambda n: -counts[n])

    def _job_names(self) -> list[str]:
        """Jobs scoped to the selected workflow, busiest first."""
        counts: dict[str, int] = {}
        for r in self._view_runs:
            if self.selected_workflow != "All workflows" and (r.workflow or "(unknown)") != self.selected_workflow:
                continue
            for j in r.jobs:
                counts[j.base_name] = counts.get(j.base_name, 0) + 1
        return ["Pipeline"] + sorted(counts, key=lambda n: -counts[n])

    def _branch_options(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for r in self._view_runs:
            counts[r.branch] = counts.get(r.branch, 0) + 1
        return [(b, counts[b]) for b in sorted(counts, key=lambda b: -counts[b])[:MAX_BRANCH_OPTIONS]]

    def _populate_sidebar(self) -> None:
        """Reconcile filter state with the data, then render it into every sidebar.

        Default branch selection: the usual long-lived branches if present, else
        the busiest branch. Previously-selected values are preserved when valid.
        """
        workflows = self._workflow_names()
        if self.selected_workflow not in workflows:
            self.selected_workflow = "All workflows"
        jobs = self._job_names()
        if self.selected_job not in jobs:
            self.selected_job = "Pipeline"
        branches = self._branch_options()
        top = [b for b, _ in branches]
        selected = {b for b in self._selected_branches if b in top}
        if not selected and top:
            selected = {b for b in DEFAULT_BRANCHES if b in top} or {top[0]}
        self._selected_branches = selected
        self._save_filters()

        for sidebar in self.query(FilterSidebar):
            sidebar.populate(
                workflows, self.selected_workflow,
                jobs, self.selected_job,
                branches, selected,
                self.selected_timerange, self._y_from_zero, self._custom_range,
            )

    # -- Sticky filters (per repo, in SQLite) --

    def _load_filters(self, repo: str) -> None:
        f = settings_get(repo, "filters", {}) or {}
        self.selected_workflow = f.get("workflow", "All workflows")
        self.selected_job = f.get("job", "Pipeline")
        self._selected_branches = set(f.get("branches", []))
        tr = f.get("timerange")
        self.selected_timerange = tr if (tr in TIME_RANGES or tr == CUSTOM_RANGE) else "all"
        self._y_from_zero = bool(f.get("y_from_zero", False))
        self._custom_range = None
        try:
            if f.get("custom_from") and f.get("custom_through"):
                self._custom_range = (date.fromisoformat(f["custom_from"]), date.fromisoformat(f["custom_through"]))
        except ValueError:
            pass
        if self.selected_timerange == CUSTOM_RANGE and self._custom_range is None:
            self.selected_timerange = "all"

    def _save_filters(self) -> None:
        if not self.current_repo:
            return
        settings_set(self.current_repo, "filters", {
            "workflow": self.selected_workflow,
            "job": self.selected_job,
            "branches": sorted(self._selected_branches),
            "timerange": self.selected_timerange,
            "y_from_zero": self._y_from_zero,
            "custom_from": self._custom_range[0].isoformat() if self._custom_range else None,
            "custom_through": self._custom_range[1].isoformat() if self._custom_range else None,
        })

    def _content_width(self) -> int:
        """Columns available for a chart: the Trends body's content width once laid
        out, else derived from the sidebar/strip width plus the scroll pane's
        2-column scrollbar and the body's 1-column padding on each side."""
        try:
            width = self.query_one("#trends-body").content_size.width
        except Exception:
            width = 0
        if width <= 0:  # not laid out yet: sidebar/strip + 2 scrollbar gutter + 2 body padding
            width = self.size.width - (self.SIDEBAR_WIDTH if self._sidebar_visible else self.STRIP_WIDTH) - 4
        return max(width, 60)

    def _fit_tabs(self) -> None:
        """Shrink the Tabs strip to its labels so the underline doesn't run past them."""
        tabs = self.query_one(Tabs)
        total = sum(t.region.width for t in tabs.query(Tab))
        if total:
            tabs.styles.width = total

    SIDEBAR_WIDTH = 30  # FilterSidebar CSS width, border included
    STRIP_WIDTH = 3     # .sidebar-strip width when collapsed

    def _apply_sidebar_visibility(self) -> None:
        visible = self._sidebar_visible
        for sidebar in self.query(FilterSidebar):
            sidebar.display = visible
        for strip in self.query(".sidebar-strip"):
            strip.display = not visible
        # Centre the 5-wide toggle on the border line: line at (width - 1), glyph at offset + 2.
        line_x = (self.SIDEBAR_WIDTH if visible else self.STRIP_WIDTH) - 1
        for btn in self.query(".sidebar-toggle"):
            btn.label = "<" if visible else ">"
            btn.tooltip = "Collapse filters (f)" if visible else "Show filters (f)"
            btn.styles.offset = (line_x - 2, 0)

    @on(Button.Pressed, ".sidebar-toggle")
    def _on_sidebar_button(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_toggle_sidebar()

    def action_toggle_sidebar(self) -> None:
        """Collapse/expand the filter sidebar (persisted across launches)."""
        self._sidebar_visible = not self._sidebar_visible
        self._apply_sidebar_visibility()
        settings_set(GLOBAL_SCOPE, "sidebar_visible", self._sidebar_visible)
        if self.runs:
            self.call_after_refresh(self._render_all_tabs)  # re-render once the layout has the new width

    def _plot_height(self, fraction: float = 0.5, minimum: int = 12) -> int:
        available = max(self.size.height - 7, 20)
        return max(minimum, int(available * fraction))

    def on_resize(self, event) -> None:
        """Re-render charts when the terminal is resized (debounced)."""
        if self._resize_timer is not None:
            self._resize_timer.stop()
        for sidebar in self.query(FilterSidebar):
            sidebar.fit_lists()
        if self.runs:
            self._resize_timer = self.set_timer(0.15, self._render_all_tabs)

    # -- Filter events --

    @on(Button.Pressed, ".time-range-bar Button")
    def on_timerange_pressed(self, event: Button.Pressed) -> None:
        key = event.button.name or "all"
        if key == CUSTOM_RANGE:
            self.action_custom_range()
            return
        if key == self.selected_timerange:
            return
        self.selected_timerange = key
        self._custom_range = None  # picking a preset resets the Custom button
        self._populate_sidebar()
        self._save_filters()
        self._render_all_tabs()

    @on(OptionList.OptionHighlighted, ".workflow-select")
    def on_workflow_selected(self, event: OptionList.OptionHighlighted) -> None:
        if event.option and event.option.id and str(event.option.id) != self.selected_workflow:
            self.selected_workflow = str(event.option.id)
            self._populate_sidebar()  # re-scopes the job list
            self._save_filters()
            self._render_all_tabs()

    @on(OptionList.OptionHighlighted, ".job-select")
    def on_job_selected(self, event: OptionList.OptionHighlighted) -> None:
        if event.option and event.option.id and str(event.option.id) != self.selected_job:
            self.selected_job = str(event.option.id)
            self._save_filters()
            try:
                self._render_trends()
            except Exception:
                log.exception("Error rendering trends for job %s", self.selected_job)

    @on(SelectionList.SelectedChanged, ".branch-select")
    def on_branch_changed(self, event: SelectionList.SelectedChanged) -> None:
        self._selected_branches = set(event.selection_list.selected)
        self._save_filters()
        self._render_all_tabs()

    @on(Checkbox.Changed, ".y-from-zero")
    def on_y_axis_changed(self, event: Checkbox.Changed) -> None:
        self._y_from_zero = event.value
        self._save_filters()
        try:
            self._render_trends()
        except Exception:
            log.exception("Error rendering after display change")

    @on(Tabs.TabActivated, "#tabs")
    def on_tab_activated(self, event: Tabs.TabActivated) -> None:
        tab_id = event.tab.id or "trends"
        self.query_one(ContentSwitcher).current = tab_id
        settings_set(GLOBAL_SCOPE, "active_tab", tab_id)
        if tab_id == "status-tab":
            self._cache_card_drawn = False
            self._render_status(STATS.snapshot())
        elif self.runs:
            # Sync this tab's sidebar with changes made on the other data tab
            self._populate_sidebar()

    def _get_selected_branches(self) -> set[str]:
        return set(self._selected_branches)

    def _y_starts_zero(self) -> bool:
        return self._y_from_zero

    def _empty_state_message(self) -> str:
        if not self.runs:
            if self.loading:
                return "Fetching workflow data from GitHub..."
            return "No cached data. Press [bold]r[/] to refresh."
        if not self._view_runs:
            return "Everything is excluded by the repo settings. Press [bold],[/] to adjust."
        branches = self._get_selected_branches()
        if self.selected_timerange == CUSTOM_RANGE and self._custom_range:
            return f"No runs between {self._custom_range[0]} and {self._custom_range[1]}. Pick another range or press All."
        if branches and self.selected_timerange != "all":
            return f"No runs for {', '.join(sorted(branches))} in the last {self.selected_timerange}."
        if branches:
            return f"No runs found for {', '.join(sorted(branches))}."
        if self.selected_timerange != "all":
            return f"No runs in the last {self.selected_timerange}."
        return "No matching data. Try adjusting filters."

    def _get_filtered_runs(self) -> list[RunData]:
        runs = self._view_runs
        if self.selected_workflow != "All workflows":
            runs = [r for r in runs if (r.workflow or "(unknown)") == self.selected_workflow]
        branches = self._get_selected_branches()
        if branches:
            runs = [r for r in runs if r.branch in branches]
        if self.selected_timerange == CUSTOM_RANGE and self._custom_range:
            start, end = self._custom_range
            return [r for r in runs if start <= r.created_at.date() <= end]
        td = TIME_RANGES.get(self.selected_timerange)
        if td is not None and runs:
            cutoff = datetime.now(runs[0].created_at.tzinfo) - td
            runs = [r for r in runs if r.created_at >= cutoff]
        return runs

    # -- Trends tab --

    def _render_notes_button(self, visible: int = 0) -> None:
        """`ⓘ Notes (n)` at the right end of the stats line — n = notes on this job's chart."""
        label = f"{NOTE_MARKER} Notes ({visible})" if self._notes else f"{NOTE_MARKER} Add note"
        self.query_one("#notes-button", Static).update(f"[@click=app.open_notes]{label}[/]")

    def _render_trends(self) -> None:
        filtered = self._get_filtered_runs()
        job_name = self.selected_job
        is_pipeline = job_name == "Pipeline"
        body = self.query_one("#trends-body", TrendChart)
        p = self.palette
        job_notes = notes_for_job(self._notes, job_name, self._config)
        self._render_notes_button(len(job_notes))
        if not filtered:
            self.query_one("#trends-stats", Static).update("")
            self._plot_run_dates = []
            body.set_message(self._empty_state_message())
            return

        w = self._content_width()
        parts: list = []

        durs = [r.total_duration_s for r in filtered] if is_pipeline else get_job_durations(filtered, job_name)
        s = stats_summary(durs)
        self.query_one("#trends-stats", Static).update(
            f"[bold {p.primary_hex}]{job_name}[/]  "
            f"Avg: [{p.success_hex}]{s['avg']}[/]  "
            f"Med: {s['median']}  "
            f"Min: {s['min']}  "
            f"Max: [{p.error_hex}]{s['max']}[/]  "
            f"Std: {s['stdev']}  "
            f"[{p.muted_hex}]({s['count']} runs)[/]"
        )

        # Runs that actually appear on the x axis (a job may not exist in every run);
        # notes are positioned against this same list so markers line up.
        plot_runs = filtered if is_pipeline else [
            r for r in filtered if any(j.base_name == job_name for j in r.jobs)
        ]
        hidden_outliers = 0
        if self._config.outlier_filter and len(plot_runs) >= 5:
            series = [
                r.total_duration_s if is_pipeline
                else mean(j.duration_s for j in r.jobs if j.base_name == job_name)
                for r in plot_runs
            ]
            # Judge each run against runs of the *same workflow*: with "All workflows"
            # selected, a 5 s housekeeping run and a 2 min build are neighbours on the
            # x axis but not comparable.
            flags = [False] * len(plot_runs)
            by_wf: dict[str, list[int]] = {}
            for i, r in enumerate(plot_runs):
                by_wf.setdefault(workflow_label(r), []).append(i)
            for idxs in by_wf.values():
                sub = hampel_outliers([series[i] for i in idxs], self._config.outlier_window,
                                      self._config.outlier_k, self._config.outlier_both)
                for i, bad in zip(idxs, sub):
                    flags[i] = bad
            hidden_outliers = sum(flags)
            plot_runs = [r for r, bad in zip(plot_runs, flags) if not bad]
            durs = [r.total_duration_s for r in plot_runs] if is_pipeline else get_job_durations(plot_runs, job_name)
            s = stats_summary(durs)
            self.query_one("#trends-stats", Static).update(
                f"[bold {p.primary_hex}]{job_name}[/]  "
                f"Avg: [{p.success_hex}]{s['avg']}[/]  "
                f"Med: {s['median']}  "
                f"Min: {s['min']}  "
                f"Max: [{p.error_hex}]{s['max']}[/]  "
                f"Std: {s['stdev']}  "
                f"[{p.muted_hex}]({s['count']} runs · {hidden_outliers} outliers hidden)[/]"
            )
        positioned = note_x_positions(plot_runs, job_notes)
        note_rgb = _hex_to_rgb(p.error_hex)
        self._plot_run_dates = [r.created_at for r in plot_runs]
        geoms: dict[int, ChartGeometry | None] = {}

        def draw_notes(plt, y_top: float) -> None:
            for x, note in positioned:
                rgb = _hex_to_rgb(note.color) if note.color else note_rgb
                plt.vline(x, rgb)
                plt.text(NOTE_MARKER, x, y_top, rgb)

        try:
            def plot_trend(plt):
                values = []
                for run in plot_runs:
                    if is_pipeline:
                        values.append(run.total_duration_s / 60.0)
                    else:
                        values.append(mean(j.duration_s for j in run.jobs if j.base_name == job_name) / 60.0)
                if not values:
                    plt.title(f"No data for {job_name}")
                    return
                xs = [time_to_x(self._plot_run_dates, r.created_at) or 0.0 for r in plot_runs]
                plt.xlim(0, x_span_days(self._plot_run_dates))
                plt.plot(xs, values, p.series[0], label="duration")
                window = self._config.rolling_window
                if len(values) >= max(3, window):
                    rolling = [mean(values[max(0, i - window + 1):i + 1]) for i in range(len(values))]
                    plt.plot(xs, rolling, p.series[1], label="rolling avg")
                plt.xticks(*date_ticks(self._plot_run_dates))
                top = plt.yaxis(values, self._y_starts_zero())
                draw_notes(plt, top)
                plt.title(f"{job_name} Duration Trend (minutes)")
                plt.ylabel("Minutes")

            chart = render_plot(plot_trend, w, self._plot_height(0.5), p)
            geoms[len(parts)] = chart_geometry(chart)
            parts.append(attach_note_markers(chart, [n for _, n in positioned], p.error_hex))
        except Exception:
            log.exception("Error rendering trend plot for %s", job_name)
            parts.append(f"[{p.error_hex}]Error rendering trend chart — see gha_explorer.log[/]")

        if is_pipeline:
            body.set_content(parts, geoms)
            return

        # Group member breakdown (matrix shards, renamed jobs) — only for multi-member groups
        has_matrix = any(
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
                    runs_with_job = plot_runs
                    member_xs = [time_to_x(self._plot_run_dates, r.created_at) or 0.0 for r in runs_with_job]
                    plt.xlim(0, x_span_days(self._plot_run_dates))
                    all_ys: list[float] = []
                    for ci, key in enumerate(shard_keys):
                        ys = []
                        for r in runs_with_job:
                            sd = [j.duration_s for j in r.jobs if j.base_name == job_name and j.matrix_key == key]
                            ys.append(sd[0] / 60.0 if sd else 0)
                        if not ys:
                            continue
                        if len(ys) >= 5:
                            window = max(self._config.rolling_window, len(ys) // 40)
                            ys = [mean(ys[max(0, i - window + 1):i + 1]) for i in range(len(ys))]
                        all_ys.extend(ys)
                        plt.plot(member_xs, ys, p.series[ci % len(p.series)], label=key)
                    plt.xticks(*date_ticks(self._plot_run_dates))
                    top = plt.yaxis(all_ys, self._y_starts_zero()) if all_ys else 1.0
                    draw_notes(plt, top)
                    plt.title(f"{job_name} — Group Members (minutes, rolling avg)")
                    plt.ylabel("Minutes")

                chart = render_plot(plot_shards, w, self._plot_height(0.4), p)
                geoms[len(parts)] = chart_geometry(chart)  # same time axis: draggable too
                parts.append(attach_note_markers(chart, [n for _, n in positioned], p.error_hex))
            except Exception:
                log.exception("Error rendering shard plot for %s", job_name)
                parts.append(f"[{p.error_hex}]Error rendering shard chart — see gha_explorer.log[/]")

        # Key step durations
        try:
            by_step = get_step_durations_by_name(plot_runs, job_name)
            step_avgs = sorted(
                ((name, mean(durs)) for name, durs in by_step.items() if mean(durs) > 5),
                key=lambda t: -t[1],
            )
            if step_avgs:
                parts.append("")

                def plot_steps(plt):
                    plt.bar([n[:40] for n, _ in step_avgs], [a for _, a in step_avgs], p.series[0])
                    plt.yaxis([a for _, a in step_avgs], from_zero=True)
                    plt.title(f"{job_name} — Avg Step Durations (seconds)")
                    plt.ylabel("Seconds")

                parts.append(render_plot(plot_steps, w, self._plot_height(0.35, 10), p))
        except Exception:
            log.exception("Error rendering step durations for %s", job_name)
            parts.append(f"[{p.error_hex}]Error rendering steps chart — see gha_explorer.log[/]")

        body.set_content(parts, geoms)

    # -- Runs tab --

    def _render_runs_table(self) -> None:
        try:
            table = self.query_one("#runs-table", DataTable)
            table.clear(columns=True)
            table.zebra_stripes = True
            table.cursor_type = "row"

            filtered = self._get_filtered_runs()
            if not filtered:
                return

            job_counts: dict[str, int] = {}
            for r in filtered:
                for j in r.jobs:
                    job_counts[j.base_name] = job_counts.get(j.base_name, 0) + 1
            job_cols = sorted(job_counts, key=lambda n: -job_counts[n])

            show_workflow = self.selected_workflow == "All workflows"
            columns = ["Date", "Branch"] + (["Workflow"] if show_workflow else []) + ["Title", "Total", *job_cols]
            table.add_columns(*columns)

            for run in reversed(filtered):
                def _avg(base_name, r=run):
                    durs = [j.duration_s for j in r.jobs if j.base_name == base_name]
                    return fmt_duration(mean(durs)) if durs else "-"

                row = [run.created_at.strftime("%Y-%m-%d %H:%M"), run.branch]
                if show_workflow:
                    row.append((run.workflow or "(unknown)")[:24])
                row += [run.title[:45], fmt_duration(run.total_duration_s), *[_avg(col) for col in job_cols]]
                table.add_row(*row)
        except Exception:
            log.exception("Error rendering runs table")

    # -- Tab actions --

    def action_tab_trends(self) -> None:
        self.query_one(Tabs).active = "trends"

    def action_tab_runs(self) -> None:
        self.query_one(Tabs).active = "runs-tab"

    def action_tab_status(self) -> None:
        self.query_one(Tabs).active = "status-tab"


def main():
    import argparse

    parser = argparse.ArgumentParser(description="GHA Explorer — a TUI for GitHub Actions timing.")
    parser.add_argument(
        "--repo",
        help="owner/name of the repo to explore. If omitted, uses the last-selected repo or prompts.",
    )
    parser.add_argument(
        "--theme",
        help="Textual theme name (default: gha-lavender). Any built-in works, e.g. catppuccin-mocha, nord.",
    )
    parser.add_argument("--version", action="version", version=f"gha-explorer {__version__}")
    parser.add_argument(
        "--logout", action="store_true",
        help="Forget the built-in GitHub sign-in (auth.json) and exit. Does not touch the gh CLI's login.",
    )
    parser.add_argument(
        "--data-dir", action="store_true",
        help=f"Print the data directory (cache.db, log) and exit. Currently: {DATA_DIR}",
    )
    args = parser.parse_args()
    if args.data_dir:
        print(DATA_DIR)
        return

    if args.logout:
        print("Removed the saved GitHub login." if clear_saved_token() else "No saved GitHub login to remove.")
        return

    log.info("=" * 60)
    log.info("Starting GHA Explorer %s (data dir: %s)", __version__, DATA_DIR)
    try:
        GHAExplorerApp(initial_repo=args.repo, theme_name=args.theme).run()
    except Exception:
        log.exception("Unhandled exception — app crashed")
        print(f"\nGHA Explorer crashed. See log: {LOG_FILE}")
        raise
    finally:
        log.info("App exited")


if __name__ == "__main__":
    main()
