# GHA Explorer

A terminal UI for exploring GitHub Actions workflow timing data. Fetches successful workflow runs from any repo you have access to via the `gh` CLI, caches everything locally in SQLite, and shows interactive trend charts of pipeline and job durations.

![Hero screenshot](docs/images/hero.png)

## Features

- **Incremental sync** — on launch, cached data displays instantly, then new runs fetch in the background. No re-fetching what you already have.
- **Multi-repo support** — switch between any repo you own or have access to. All data lives in a single local cache DB.
- **Dynamic discovery** — workflows, jobs, and matrix shards all auto-detected from your actual run history. Nothing hardcoded.
- **Trend charts** — duration trends with rolling averages, per-job and pipeline-wide.
- **Matrix shard breakdown** — see how individual shards of a matrix job compare over time.
- **Filters** — by workflow, branch, time range (1d / 1m / 3m / 6m / 1y / all).
- **Resilient** — handles GitHub API rate limiting gracefully; backfill resumes next launch.

## Requirements

- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv) — the script declares its deps inline and runs via `uv run`
- [`gh`](https://cli.github.com/) — GitHub CLI, authenticated (`gh auth login`)

## Install & Run

```bash
git clone https://github.com/swils23/gha-explorer.git
cd gha-explorer
./gha_explorer.py
# or: uv run --script gha_explorer.py
```

First launch will show a repo picker listing every repo you have access to. Pick one, and it starts fetching. On subsequent launches your choice is remembered (stored in `config.json`).

## Keybindings

| Key | Action |
| --- | --- |
| `q` | Quit |
| `r` | Refresh (force re-fetch) |
| `s` | Switch repo (open picker) |
| `1` | Trends tab |
| `2` | Runs tab |

## Architecture

The script is a single file (`gha_explorer.py`) with four layers:

1. **Cache (SQLite)** — stores the raw `gh run list` + `gh api runs/{id}/jobs` JSON per `run_id`, keyed by repo. Never re-fetches the same run.
2. **Fetch** — incremental sync: forward-fetch runs newer than the latest cached, then backfill backwards in 90-day windows until exhausted.
3. **Aggregation** — stats (mean / median / min / max / stdev) and rolling averages for trend smoothing.
4. **TUI (Textual + plotext)** — reactive UI with persistent status bar, sidebar filters, and ASCII charts.

Everything is derived from the data itself — no hardcoded workflow names, job names, or repo references. `classify_job()` strips matrix suffixes like `(1)`, `(2)` but otherwise leaves names as-is.

## License

MIT
