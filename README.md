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

Saved reports, each containing both tables:

- [Year ending September 17, 2026](results/2025-09-17T031100Z--2026-09-17T031100Z/contributions.md)
- [Year ending October 16, 2026](results/2025-10-16T143332Z--2026-10-16T143332Z/contributions.md)

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
counts are provisional and reused for one hour. Closed days have no arbitrary
expiry; GitHub can still revise old activity after repository or visibility
changes. Set `REFRESH_CACHE=true` to request new observations deliberately.

All searches use `type:user` and split by followers/account creation dates to
get past GitHub's 1,000-result cap. There is no truncated selection mode or
mandatory extra account. Country and cities are in `config.json`. Missing or
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

New observations use stable GitHub IDs; renames reuse them, reused logins do
not transfer another account's counts. Cache scopes are credential hashes
because visibility depends on the reader; no token is saved. Migrated results
retain their original numbers, timestamps, membership and metadata. Their
missing stable IDs and unspecified observer policy are explicit; they are not
silently assigned today's account identities or recounted under new semantics.

Each period has one `results/<from>--<to>/contributions.md` report containing both
rankings. Publication is a single atomic replacement after the snapshot commits.
A failed database write leaves the old snapshot; a failed projection leaves the
previous report, which can be regenerated. Collection and projection take an
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
period report after a successful collection; a failed push never removes local
observations or reports. No database or all-history artifact is uploaded.

```bash
python3 -m unittest discover -s scripts -p 'test_*.py'
```

Source project: [gayanvoice/top-github-users](https://github.com/gayanvoice/top-github-users),
generator: [gayanvoice/top-github-users-action](https://github.com/gayanvoice/top-github-users-action).
Upstream cache data is distributed under [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
