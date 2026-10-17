# Kazakhstan contribution rankings

This fork collects accounts found by the configured Kazakhstan/city location
search and ranks their contributions in two ways: **visible to the acquisition
viewer** and **total including disclosed restricted-detail counts**. These are
contributions, not only commits. Equal counts share a rank.

The first count is `totalContributions - restrictedContributionsCount`. It can
include private activity visible to the authenticated reader, so it is not
unconditionally labelled public. The total cannot include undisclosed activity.
Neither ranking is a census of Kazakhstan. Locations and followers reflect
collection time; historical profile settings are unavailable.

Each completed collection creates a top-100 summary and two separate full
rankings under `results/`.
The authoritative data remains in the private SQLite database.

Latest report: [2025-10-16 through 2026-10-16](results/2025-10-16T000000Z--2026-10-16T000000Z/README.md).

## Collect locally

Python 3.12 or later on Linux, with an authenticated GitHub CLI:

```bash
export GH_TOKEN="$(gh auth token)"
python3 scripts/kazakhstan_ranking.py --from 2025-10-16 --to 2026-10-16
unset GH_TOKEN
```

Both dates are included, in UTC. GitHub's contribution calendar has day
precision: timestamps are converted to UTC and then reduced to dates. A range
can span at most one calendar year between its endpoints. The current day's
counts are provisional and reused for one hour. After that day closes, a final
observation must be fetched before its counts become reusable indefinitely.
Closed periods have no arbitrary expiry; GitHub can still revise old activity after repository or visibility
changes. Set `REFRESH_CACHE=true` to request new observations deliberately.

To expand location coverage for an existing period without updating its saved
counts, add `--expand` to the collection command. Existing snapshot counts are
retained regardless of age; new accounts are fetched normally. Omit `--expand`
when you want the normal freshness policy.
Reports retain `counts_provisional: true` in their frontmatter until all used
counts were acquired after the final UTC day ended, even when rebuilt later.

All searches use `type:user` and split by followers/account creation dates to
get past GitHub's 1,000-result cap. There is no truncated selection mode or
mandatory extra account. Country, cities and multilingual `locationAliases` are in
`config.json`. Location searches run in bounded groups and merge accounts by ID.
`excludedLocationFragments` removes known foreign or non-geographic matches
(such as Petropavlovsk-Kamchatsky and oral drug marketing). Missing or
deleted accounts are recorded explicitly and the report is marked incomplete;
valid accounts in the same response are retained. Other permanent errors fail
the run. Completed batches survive interrupted runs. Progress includes an ETA
based on accounts still requiring requests, excluding cached accounts.

Requests are sequential, without a fixed delay on success. Rate limits honor
`Retry-After` and quota reset headers, including GraphQL errors inside HTTP 200.
Without server timing, secondary limits use bounded exponential backoff from
one minute; transient HTTP/network/body-read failures use a shorter backoff.

## Storage and reports

`data/rankings.sqlite3` is the private, durable source of truth, **not a
disposable cache**. It holds observations, snapshot membership and metadata;
only the two source counters are stored. Snapshots reference observations
instead of duplicating counts. Search responses expire after one day.

Observations require stable GitHub IDs; renames reuse them, reused logins do
not transfer another account's counts. Cache scopes are credential hashes
because visibility depends on the reader; no token is saved. Old data without
stable IDs was discarded. There is no legacy import or compatibility mode.

Each period has three reports under `results/<from>--<to>/`: `README.md` shows
the top 100 accounts in each ranking and links to `visible-full.md` and
`total-full.md`. Full reports contain all accounts with a positive count in
the respective metric and link back to the summary.

Each report starts with YAML frontmatter for its period, snapshot completion time,
account counts, selection/observer policies and completeness flags. Field names,
types and order are defined by the shared
[ReportFrontmatter schema](scripts/kazakhstan_ranking.py); generation checks field
types and nonnegative counts. Dates and other strings are quoted, and flags are
YAML booleans. Metadata is separate from the explanatory text and ranking tables.

All three files are written and flushed before publication. Each file is replaced
atomically, with the summary published last, after the snapshot commits. A failed
database write leaves the old snapshot. If publication is interrupted, run
`--project` to rebuild the reports from the committed database. Collection and projection take an
exclusive local lock. Concurrent runs fail instead of interleaving writes.
Different periods never replace one another.

Rebuild every saved report offline, with no credential or network needed:

```bash
python3 scripts/kazakhstan_ranking.py --project
```

There is no JSON export. Only human-readable reports belong in Git or public
artifacts. The database and its WAL files are Git-ignored and owner-readable;
retain and back up the database across checkout/host replacement. For a
consistent backup while SQLite is open, use its backup API rather than copying
only the main file:

```bash
python3 - <<'PY'
import sqlite3
from pathlib import Path
from contextlib import closing
destination = Path('/your/private/backup/rankings.sqlite3')
destination.touch(mode=0o600, exist_ok=False)
with closing(sqlite3.connect('data/rankings.sqlite3')) as source:
    with closing(sqlite3.connect(destination)) as target:
        source.backup(target)
PY
```

Keep the backup destination private. To restore, stop collectors/projectors,
restore the consistent database at `data/rankings.sqlite3`, set permissions to
`0600`, then run `--project`. Do not delete the database as a cleanup operation.

## Checks

GitHub Actions runs tests only. Collection runs locally so the private database
survives independently of an ephemeral CI runner. Commit only the intended
period reports after a successful collection; a failed push never removes local
observations or reports. No database or all-history artifact is uploaded.

```bash
python3 -m unittest discover -s scripts -p 'test_*.py'
```

Source project: [gayanvoice/top-github-users](https://github.com/gayanvoice/top-github-users),
generator: [gayanvoice/top-github-users-action](https://github.com/gayanvoice/top-github-users-action).
Upstream cache data is distributed under [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
