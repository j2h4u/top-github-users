# Kazakhstan public and total contributions for a fixed period

This fork recalculates Kazakhstan for **2025-09-17 03:11 UTC through 2026-09-17 03:11 UTC**.

- [Public ranking](markdown/public_contributions/kazakhstan.md)
- [Total ranking, including disclosed restricted-detail counts](markdown/total_contributions/kazakhstan.md)
- [Raw results and search partitions](cache/kazakhstan-fixed-period.json)
- [Run the workflow](../../actions/workflows/action.yml) using **Run workflow**.

The workflow uses the repository secret `CUSTOM_TOKEN` for API reads, falling back to the built-in GitHub Actions token if the secret is absent. The built-in token is used for committing results. No token is stored in source files. It writes updated results to `main` and uploads a result artifact. Scheduled runs are disabled because the contribution period is fixed.

By default the script preserves the upstream Kazakhstan/city location query and the first 1,000 search results sorted by followers. The optional `full_coverage` workflow input expands searches by followers and account creation dates; this mode is slow. The script calculates `totalContributions - restrictedContributionsCount` for every returned user. PhysShell is included explicitly if location search does not return it; this is recorded in the JSON. Equal contribution counts share the same rank.

Locations and follower counts reflect the time of collection, not historical profile settings. Search coverage depends on GitHub location search, and is not a census of all developers in Kazakhstan. A failed run does not publish partial results. Other country files inherited from upstream are not recalculated by this fork.

To run locally with Python 3.12 or later and an authenticated GitHub CLI:

```bash
export GH_TOKEN="$(gh auth token)"
python scripts/kazakhstan_ranking.py
unset GH_TOKEN
```

Period constants are in `scripts/kazakhstan_ranking.py`. Country and cities are in `config.json`.

Both rankings use the same fixed period and participant set. GitHub supplies
`contributionCalendar.totalContributions` and `restrictedContributionsCount`;
the public-labelled difference is calculated locally. Each table includes all
three numbers and sorts independently. These are contributions, including more
than commits. Restricted means details unavailable to the acquisition viewer;
the difference is only strictly public under a suitable observer access policy.
The total includes disclosed restricted counts, not undisclosed private activity.

To rebuild both tables from the saved JSON without any API requests:

```bash
python3 -c 'import json; from scripts.kazakhstan_ranking import render_rankings; render_rankings(json.load(open("cache/kazakhstan-fixed-period.json")))'
```

Successful API responses are saved atomically as each request completes in the
Git-ignored `cache/api/` directory. Restarts reuse these responses: location search
is cached for one day, completed contribution periods indefinitely, and ongoing
periods for one hour. Historical counts are snapshots: GitHub can revise them
after repository or visibility changes. Set `REFRESH_CACHE=true` to force fresh requests.
Contributions are also cached per case-insensitive login and exact period, so
changes to the participant list, ordering, or batch boundaries only fetch missing
users. Existing cached batch responses populate this user cache without new API
calls. Profile metadata comes from the location search, independently of historical
contributions; users explicitly added outside that search retain cached metadata.
Cache entries are separated by a hash of the credential because restricted counts
depend on the viewer. Tokens are never stored in the cache; rotating credentials
starts a separate cache. Keep this directory local.

Run cache checks with `python3 -m unittest discover -s scripts -p 'test_*.py'`.

Network requests are sequential, with no fixed delay after successful requests.
On rate limits, the collector honors `Retry-After` and the primary quota reset
headers, including limits reported inside HTTP-200 GraphQL responses. When GitHub
provides no retry time, secondary limits use a bounded exponential backoff
starting at one minute; transient network/server failures use a shorter backoff.
Non-retryable authorization and GraphQL validation errors fail immediately.

Source project: [gayanvoice/top-github-users](https://github.com/gayanvoice/top-github-users), generator: [gayanvoice/top-github-users-action](https://github.com/gayanvoice/top-github-users-action). Upstream cache data is distributed under [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
