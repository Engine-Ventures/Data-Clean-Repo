# Environment Setup

How to rebuild this project's Python environment from scratch on macOS.
Verified on macOS 25.6 (Darwin), Apple Silicon (arm64), 2026-09-02.

## Prerequisites

**Python 3.11 or newer.** This project pins `pandas==3.0.5`, which requires
Python >= 3.11. macOS ships Python 3.9.6 at `/usr/bin/python3`, and that is
**not sufficient** — on 3.9, pip silently resolves pandas back to 2.3.3.

Check what you have:

```bash
python3 --version
```

If it reports 3.9.x, install a newer Python before continuing. Two options:

- **python.org installer** (what this project used) — download the macOS
  universal2 installer for 3.14 from <https://www.python.org/downloads/macos/>.
  Installs to `/Library/Frameworks/Python.framework/` and puts `python3` on
  your `PATH` at `/usr/local/bin/python3`, shadowing Apple's 3.9.6.
- **Homebrew** — `brew install python@3.14`. Only worth it if you want a
  package manager for other tools too; not required here.

Both leave Apple's system Python untouched.

## Setup

```bash
git clone <repo-url>
cd <repo-name>

python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
```

## Verify

```bash
echo $VIRTUAL_ENV                              # venv path, not empty
which python                                   # .../.venv/bin/python
python -c "import pandas; print(pandas.__version__)"   # 3.0.5
```

An end-to-end check across the whole stack:

```bash
python -c "
import pandas as pd, duckdb
df = pd.DataFrame({'name': [' Ann ', 'bob', None], 'amt': ['1,200', '3.5', 'x']})
print(duckdb.sql('select count(*) as rows from df').df())
"
```

## Daily use

```bash
source .venv/bin/activate    # start of each session
deactivate                   # when done
```

Activation is per-terminal-window and does not survive a new tab or a reboot.
Re-running `activate` when already active is harmless.

## Stack

Installed by `requirements.txt` (fully pinned, including transitive deps):

| Package | Purpose |
| --- | --- |
| `pandas` | primary dataframe library |
| `polars` | faster alternative; handles larger-than-RAM data |
| `duckdb` | SQL directly over dataframes and Parquet/CSV files |
| `pyarrow` | Parquet I/O and the Arrow interchange format |
| `openpyxl` | lets pandas read and write `.xlsx` |
| `pytest` | tests |
| `ruff` | linter and formatter (replaces black + flake8 + isort) |

To add a package: `pip install <name>` then `pip freeze > requirements.txt`
to re-pin.

## Gotchas

- **`.venv/` must never be committed.** It contains compiled binaries and
  absolute paths to one specific home directory, so it will not work on
  another machine. It is listed in `.gitignore`.
- **Bare `python` does not exist outside the venv** on a python.org install —
  only `python3`. This makes `which python` a reliable activation check: it
  returns a path when active and "not found" when not.
- **`pip install` outside a venv fails** with `externally-managed-environment`.
  That is a safety net, not a bug. The fix is to activate first.
- **Python 3.14 wheels.** All packages here ship prebuilt `cp314` arm64
  wheels, so nothing compiles from source. On a much newer Python than 3.14,
  verify wheel availability first with:
  `pip install --dry-run --only-binary=:all: -r requirements.txt`
