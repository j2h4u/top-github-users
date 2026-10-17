# Kazakhstan GitHub contribution rankings

Find active GitHub users in Kazakhstan and compare their contributions over a
chosen period. The project produces two rankings: visible contributions and
total contributions, including activity with closed details.

**[View the latest rankings: 16 October 2025 — 16 October 2026](results/2025-10-16T000000Z--2026-10-16T000000Z/README.md)**

The summary shows the top 100 in each ranking and links to the full lists.

## What it offers

- Search by country and city names in Russian, Kazakh and Latin spellings.
- Collect beyond GitHub's first 1,000 search results.
- Choose a date range of up to one calendar year and keep separate reports for each period.
- Reuse saved counts, resume interrupted collections and see an estimated time remaining.
- Expand the selection without downloading existing participants' counts again.

## Run a collection

Requirements: Linux, Python 3.12 or later, and an authenticated GitHub CLI (`gh`).
Run commands from the repository root. The collector uses Python's standard
library; no Python packages need installing.

```bash
GH_TOKEN="$(gh auth token)" python3 scripts/kazakhstan_ranking.py \
  --from 2025-10-16 --to 2026-10-16
```

Change the dates to collect another period. Existing reports for other periods
remain available. Both dates are included, in UTC.

## Update or expand the results

Run the collection command again to fetch missing or outdated counts. Completed
batches are saved, so an interrupted run can continue.

After changing the locations in [config.json](config.json), add `--expand` to
keep the saved snapshot's counts and collect counts for new participants:

```bash
GH_TOKEN="$(gh auth token)" python3 scripts/kazakhstan_ranking.py \
  --from 2025-10-16 --to 2026-10-16 --expand
```

To deliberately request fresh counts and search results, use `REFRESH_CACHE=true`:

```bash
REFRESH_CACHE=true GH_TOKEN="$(gh auth token)" python3 scripts/kazakhstan_ranking.py \
  --from 2025-10-16 --to 2026-10-16
```

To rebuild all saved reports from the local database, without GitHub or a token:

```bash
python3 scripts/kazakhstan_ranking.py --project
```

## Read the rankings

- **Visible contributions** are the total minus contributions with closed details.
  They can include private activity visible to the account used for collection.
- **Total contributions** include the disclosed counts for activity with closed
  details. Activity that a user does not disclose is unavailable.
- Counts include all contribution types, not just commits. Equal counts share a
  rank; followers and then username determine the order within a tie.
- Full lists show accounts with a positive count in the respective ranking.

Coverage depends on the locations users enter in their GitHub profiles and the
configured spellings. Locations and follower counts reflect collection time.
Contribution counts reflect when each account was queried. A report keeps its
provisional status if any counts were collected before its final UTC day ended;
rebuilding the report later does not make those counts final.

Report parameters are stored in YAML frontmatter, following the shared
[`ReportFrontmatter` schema](scripts/kazakhstan_ranking.py). **Accounts unavailable**
counts selected accounts that could not be fetched. **Last day may be incomplete**
is **Yes** when some counts were collected before the final UTC day ended.

## Data and backups

Each period has three files under `results/<from>--<to>/`: a `README.md` summary,
`visible-full.md` and `total-full.md`. Publish these Markdown reports.

Keep `data/rankings.sqlite3`: it holds the saved counts and report membership.
The database is private and Git-ignored. It lets you reuse results and rebuild
reports without querying GitHub.

Current-day counts are reused for one hour. Once the final day closes, new final
counts must be collected before they can be reused indefinitely. Closed-period
counts have no automatic expiry, though GitHub can revise old activity. Location
search results are reused for one day. Changing credentials uses a separate cache
because access to private activity can differ; tokens are never saved.

Reports are prepared before publication and each file is replaced atomically,
with the summary published last. Collection and report generation take an
exclusive local lock. After an interrupted publication, run `--project` again.

Use SQLite's backup API for a consistent backup while the database is open:

```python
import sqlite3
from pathlib import Path
from contextlib import closing

backup = Path('/your/private/backup/rankings.sqlite3')
backup.touch(mode=0o600, exist_ok=False)
with closing(sqlite3.connect('data/rankings.sqlite3')) as source:
    with closing(sqlite3.connect(backup)) as destination:
        source.backup(destination)
```

To restore, stop collection and report generation, restore the database at
`data/rankings.sqlite3`, set its permissions to `0600`, then run `--project`.

## Checks

GitHub Actions runs tests. Collection runs locally so the private database stays
available between runs. To run the checks locally:

```bash
python3 -m unittest discover -s scripts -p 'test_*.py'
```

## Sources

Based on [PhysShell/top-github-users](https://github.com/PhysShell/top-github-users).
Source project: [gayanvoice/top-github-users](https://github.com/gayanvoice/top-github-users),
generator: [gayanvoice/top-github-users-action](https://github.com/gayanvoice/top-github-users-action).
Upstream data is distributed under [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
