# Kazakhstan public and total contribution rankings

This fork calculates Kazakhstan rankings for exact periods up to one calendar year.

Saved period: **2025-09-17 03:11 UTC through 2026-09-17 03:11 UTC**.

- [Public ranking](results/2025-09-17T031100Z--2026-09-17T031100Z/public_contributions.md)
- [Total ranking, including disclosed restricted-detail counts](results/2025-09-17T031100Z--2026-09-17T031100Z/total_contributions.md)
- [Raw results and search partitions](results/2025-09-17T031100Z--2026-09-17T031100Z/data.json)
- [Run the workflow](../../actions/workflows/action.yml) using **Run workflow**.

The workflow uses the repository secret `CUSTOM_TOKEN` for API reads, falling back to the built-in GitHub Actions token if the secret is absent. The built-in token is used for committing results. No token is stored in source files. It writes updated results to `main` and uploads a result artifact. Scheduled runs are disabled because the contribution period is fixed.

By default the script preserves the upstream Kazakhstan/city location query and the first 1,000 search results sorted by followers. The optional `full_coverage` workflow input expands searches by followers and account creation dates; this mode is slow. The script calculates `totalContributions - restrictedContributionsCount` for every returned user. PhysShell is included explicitly if location search does not return it; this is recorded in the JSON. Equal contribution counts share the same rank.

Locations and follower counts reflect the time of collection, not historical profile settings. Search coverage depends on GitHub location search, and is not a census of all developers in Kazakhstan. A failed run does not publish partial results. Other country files inherited from upstream are not recalculated by this fork.

To run locally with Python 3.12 or later and an authenticated GitHub CLI:

```bash
export GH_TOKEN="$(gh auth token)"
python3 scripts/kazakhstan_ranking.py --from 2025-10-16 --to 2026-10-16
unset GH_TOKEN
```

Pass dates or ISO-8601 timestamps with `--from` and `--to`; date-only values use UTC midnight. Equivalent time zones are normalized to UTC. Each exact period has one `results/<from>--<to>/data.json` snapshot and two Markdown tables. A new period never overwrites another period. Repeating the same period updates that snapshot. Country and cities are in `config.json`. Set `FULL_COVERAGE=true` for the complete partitioned location search.

Both rankings use the same fixed period and participant set. GitHub supplies
`contributionCalendar.totalContributions` and `restrictedContributionsCount`;
the public-labelled difference is calculated locally. The JSON stores only the two source counters; each table derives the public-labelled difference and sorts independently. These are contributions, including more
than commits. Restricted means details unavailable to the acquisition viewer;
the difference is only strictly public under a suitable observer access policy.
The total includes disclosed restricted counts, not undisclosed private activity.

To rebuild both tables from the saved JSON without any API requests:

```bash
python3 -c 'import json; from scripts.kazakhstan_ranking import render_rankings; render_rankings(json.load(open("results/2025-09-17T031100Z--2026-09-17T031100Z/data.json")))'
```

Successful API responses are saved transactionally in one private, Git-ignored
`cache/api.sqlite3` database. Contribution responses are stored per user and exact
period; whole batch responses are not retained as duplicate copies. Restarts reuse these responses: location search
is cached for one day, completed contribution periods indefinitely, and ongoing
periods for one hour. Historical counts are snapshots: GitHub can revise them
after repository or visibility changes. Set `REFRESH_CACHE=true` to force fresh requests.
Case-insensitive login and exact-period keys mean
changes to the participant list, ordering, or batch boundaries only fetch missing
users. Overlapping periods have distinct totals and require new requests; no
approximate subtraction or monthly aggregation is used. Profile metadata comes from the location search, independently of historical
contributions; users explicitly added outside that search retain cached metadata.
Cache entries are separated by a hash of the credential because restricted counts
depend on the viewer. Tokens are never stored in the cache; rotating credentials
starts a separate cache namespace. Keep the database local.

Progress reports include an approximate ETA based on elapsed collection time and
the number of accounts still requiring requests. Accounts loaded from cache are
excluded from the rate estimate; retries and throttling can change it.

Run cache checks with `python3 -m unittest discover -s scripts -p 'test_*.py'`.

Network requests are sequential, with no fixed delay after successful requests.
On rate limits, the collector honors `Retry-After` and the primary quota reset
headers, including limits reported inside HTTP-200 GraphQL responses. When GitHub
provides no retry time, secondary limits use a bounded exponential backoff
starting at one minute; transient network/server failures use a shorter backoff.
Non-retryable authorization and GraphQL validation errors fail immediately.

Source project: [gayanvoice/top-github-users](https://github.com/gayanvoice/top-github-users), generator: [gayanvoice/top-github-users-action](https://github.com/gayanvoice/top-github-users-action). Upstream cache data is distributed under [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
