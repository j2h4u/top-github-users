"""Contribution rankings for inclusive UTC calendar dates; standard library only."""

import argparse
import datetime
import fcntl
import hashlib
import http.client
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from contextlib import closing, suppress
from dataclasses import dataclass, field, fields
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import cast

import ranking_store as store
from ranking_types import (
    ApiUser,
    CliArguments,
    Config,
    GraphQLError,
    GraphQLResponse,
    LocationConfig,
    MaximumSearchData,
    ObservedUser,
    Period,
    Profile,
    RankingUser,
    SearchData,
    SnapshotMetadata,
    UnavailableUser,
)

FIELDS = """id login
contributionsCollection(from: $from, to: $to) {
  contributionCalendar { totalContributions }
  restrictedContributionsCount
}"""
next_request_at = 0.0
refresh_cache = False
API_ATTEMPTS = 5
HTTP_FORBIDDEN = 403
HTTP_TOO_MANY_REQUESTS = 429
SEARCH_RESULT_LIMIT = 1000
FEBRUARY = 2
LEAP_DAY = 29


def cache_key(key: object) -> str:
    # Separate credentials can see different restricted contribution counts.
    scope = credential_scope()
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    return scope + "/" + digest


def contribution_ttl(period: Period) -> int | None:
    historical = datetime.datetime.fromisoformat(period["to"]).date() < datetime.datetime.now(datetime.UTC).date()
    return None if historical else 3600


def read_cache(key: str, ttl: int | None) -> dict[str, object] | None:
    if not refresh_cache:
        with closing(store.connect()) as connection:
            row = cast(
                tuple[float, str] | None,
                connection.execute("SELECT fetched_at, data FROM responses WHERE key = ?", (key,)).fetchone(),
            )
        if row is not None and (ttl is None or 0 <= time.time() - row[0] < ttl):
            return {**cast(dict[str, object], json.loads(row[1])), "_fetchedAt": row[0]}
    return None


def write_cache(entries: dict[str, dict[str, object]]) -> None:
    with closing(store.connect()) as connection, connection:
        connection.execute("DELETE FROM responses WHERE fetched_at < ?", (time.time() - 86400,))
        connection.executemany(
            "INSERT OR REPLACE INTO responses VALUES (?, ?, ?)",
            [(key, time.time(), json.dumps(data, separators=(",", ":"))) for key, data in entries.items()],
        )


def api(query: str, variables: dict[str, str | None]) -> dict[str, object]:
    path = cache_key([query, variables])
    data = read_cache(path, 86400)
    if data is None:
        data = fetch_api(query, variables)
        write_cache({path: data})
        data = {**data, "_fetchedAt": time.time()}
    return data


def credential_scope() -> str:
    return hashlib.sha256(os.environ["GH_TOKEN"].encode()).hexdigest()


def cached_users(profiles: list[Profile], period: Period, reuse_snapshot: bool = False) -> dict[str, ObservedUser]:
    if refresh_cache:
        return {}
    return store.cached_observations(
        credential_scope(), profiles, period, contribution_ttl(period), reuse_snapshot=reuse_snapshot
    )


def validate_fetched_users(data: dict[str, object], profiles: list[Profile]) -> list[ApiUser]:
    valid: list[ApiUser] = []
    for index, profile in enumerate(profiles):
        alias = f"u{index}"
        if alias not in data:
            raise RuntimeError("GitHub omitted a requested account alias")
        user = cast(ApiUser | None, data[alias])
        if user is not None:
            if user.get("id") != profile["id"]:
                raise RuntimeError("GitHub returned a different account identity")
            if not isinstance(user.get("login"), str) or not user["login"]:
                raise RuntimeError("GitHub returned an account without a login")
            valid.append(user)
    return valid


def fetch_users(profiles: list[Profile], period: Period) -> dict[str, ObservedUser]:
    users = cached_users(profiles, period)
    missing = [profile for profile in profiles if profile["login"].lower() not in users]
    if not missing:
        return users
    fields = "\n".join(
        f"u{i}: node(id: {json.dumps(profile['id'])}) {{ ... on User {{ {FIELDS} }} }}"
        for i, profile in enumerate(missing)
    )
    try:
        data = fetch_api(
            "query($from: DateTime!, $to: DateTime!) { " + fields + " }", {"from": period["from"], "to": period["to"]}
        )
    except RuntimeError as error:
        if "RESOURCE_LIMITS_EXCEEDED" not in str(error) or len(missing) == 1:
            raise
        middle = len(missing) // 2
        users.update(fetch_users(missing[:middle], period))
        users.update(fetch_users(missing[middle:], period))
        return users
    valid = validate_fetched_users(data, missing)
    saved = store.save_observations(credential_scope(), period, valid)
    for i, profile in enumerate(missing):
        user = cast(ApiUser | None, data[f"u{i}"])
        if user is not None:
            counts = user["contributionsCollection"]
            users[profile["login"].lower()] = {
                **profile,
                "login": user["login"],
                "_observation": saved[user["id"]],
                "totalContributions": counts["contributionCalendar"]["totalContributions"],
                "restrictedContributionsCount": counts["restrictedContributionsCount"],
            }
    return users


def retry_delay(headers: Mapping[str, str], attempt: int, limited: bool) -> float:
    delay = None
    if headers.get("Retry-After") is not None:
        try:
            delay = max(0, float(headers["Retry-After"]))
        except ValueError:
            with suppress(ValueError, TypeError, OverflowError):
                delay = max(0, parsedate_to_datetime(headers["Retry-After"]).timestamp() - time.time())
    if headers.get("x-ratelimit-remaining") == "0":
        with suppress(KeyError, ValueError):
            delay = max(delay or 0, float(headers["x-ratelimit-reset"]) - time.time() + 1)
    return max(0, delay) if delay is not None else (60 if limited else 2) * 2**attempt


def fetch_api(query: str, variables: dict[str, str | None]) -> dict[str, object]:
    global next_request_at
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        json.dumps({"query": query, "variables": variables}).encode(),
        {
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Content-Type": "application/json",
            "User-Agent": "kazakhstan-ranking",
        },
    )
    for attempt in range(API_ATTEMPTS):
        wait = next_request_at - time.time()
        if wait > 0:
            print(f"GitHub retry/limit; waiting {wait:.1f}s", flush=True)
            time.sleep(wait)
        next_request_at = 0.0
        limited, headers = False, {}
        failure = None
        try:
            with cast(http.client.HTTPResponse, urllib.request.urlopen(request, timeout=90)) as stream:
                headers = cast(Mapping[str, str], stream.headers)
                result = cast(GraphQLResponse, json.load(stream))
            if headers.get("x-ratelimit-remaining") == "0":
                next_request_at = time.time() + retry_delay(headers, attempt, True)
            result_data, limited, failure = _graphql_response(result)
            if result_data is not None:
                return result_data
        except urllib.error.HTTPError as error:
            with closing(error):
                failure = error
                headers, limited = _http_retry_details(error)
        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.IncompleteRead,
            ConnectionError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as error:
            failure = error
        if attempt == API_ATTEMPTS - 1:
            raise RuntimeError(f"GitHub request failed after five attempts: {failure}")
        next_request_at = max(next_request_at, time.time() + retry_delay(headers, attempt, limited))
    raise RuntimeError("GitHub request retry loop ended unexpectedly")


def _graphql_response(result: GraphQLResponse) -> tuple[dict[str, object] | None, bool, str | None]:
    errors = result.get("errors", [])
    if errors:
        message = json.dumps(errors)
        limited = any(error.get("type") == "RATE_LIMITED" for error in errors) or "rate limit" in message.lower()
        data = result.get("data")
        if not limited and _only_missing_alias_errors(errors, data):
            assert isinstance(data, dict)
            return data, False, message
        if not limited:
            if "timeout" in message.lower() or "something went wrong" in message.lower():
                raise TimeoutError(message)
            raise RuntimeError(message)
        return None, True, message
    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("GitHub returned no data object")
    return data, False, None


def _only_missing_alias_errors(errors: list[GraphQLError], data: object) -> bool:
    if not isinstance(data, dict):
        return False
    for error in errors:
        path = error.get("path")
        if (
            error.get("type") != "NOT_FOUND"
            or path is None
            or len(path) != 1
            or not isinstance(path[0], str)
            or path[0] not in data
            or data[path[0]] is not None
        ):
            return False
    return True


def _http_retry_details(error: urllib.error.HTTPError) -> tuple[Mapping[str, str], bool]:
    headers = cast(Mapping[str, str], error.headers or {})
    if error.code in (HTTP_FORBIDDEN, HTTP_TOO_MANY_REQUESTS):
        body = error.read().decode("utf-8", errors="replace")
        limited = (
            error.code == HTTP_TOO_MANY_REQUESTS
            or headers.get("Retry-After") is not None
            or headers.get("x-ratelimit-remaining") == "0"
            or any(term in body.lower() for term in ("rate limit", "abuse", "temporarily"))
        )
        if not limited:
            raise RuntimeError(f"GitHub HTTP 403: {body}") from error
        return headers, limited
    if error.code not in (500, 502, 503, 504):
        raise error
    return headers, False


def _parse_period_dates(start_value: str, end_value: str) -> tuple[datetime.datetime, datetime.datetime]:
    try:
        start, end = (datetime.datetime.fromisoformat(value) for value in (start_value, end_value))
    except ValueError as error:
        raise ValueError("Invalid date: use YYYY-MM-DD, for example --from 2025-10-16 --to 2026-10-16") from error
    start, end = (
        value.replace(tzinfo=datetime.UTC) if value.tzinfo is None else value.astimezone(datetime.UTC)
        for value in (start, end)
    )
    start, end = (value.replace(hour=0, minute=0, second=0, microsecond=0) for value in (start, end))
    anniversary = start.replace(
        year=start.year + 1, day=28 if start.month == FEBRUARY and start.day == LEAP_DAY else start.day
    )
    if not start <= end <= anniversary:
        raise ValueError("Period must be ordered and at most one calendar year")
    if end.date() > datetime.datetime.now(datetime.UTC).date():
        raise ValueError("Period end must not be in the future")
    return start, end


def contribution_count(user: ObservedUser | RankingUser, metric: str) -> int:
    if metric == "visibleContributions":
        return user["totalContributions"] - user["restrictedContributionsCount"]
    if metric == "totalContributions":
        return user["totalContributions"]
    return user["restrictedContributionsCount"]


def result_directory(period: Period | SnapshotMetadata) -> Path:
    return Path("results") / (period["from"] + "--" + period["to"]).replace(":", "")


@dataclass(frozen=True)
class ReportFrontmatter:
    period: str = field(metadata={"label": "Period"})
    snapshot_completed: str = field(metadata={"label": "Collection completed"})
    accounts_fetched: int = field(metadata={"label": "Accounts included"})
    counts_provisional: bool = field(metadata={"label": "Last day may be incomplete"})
    unavailable_accounts: int = field(metadata={"label": "Accounts unavailable"})

    def to_lines(self) -> list[str]:
        lines = ["---"]
        for schema_field in fields(self):
            value = cast(object, getattr(self, schema_field.name))
            expected_type = cast(type[object], schema_field.type)
            label = cast(str, schema_field.metadata["label"])
            if type(value) is not expected_type:
                raise TypeError(f"Invalid frontmatter field {schema_field.name}: expected {expected_type.__name__}")
            if expected_type is int and type(value) is int and value < 0:
                raise ValueError(f"Invalid frontmatter field {schema_field.name}: expected a nonnegative count")
            # JSON scalars are valid YAML and safely quote timestamps and special characters.
            if expected_type is bool:
                value = "Yes" if value else "No"
            lines.append(f"{label}: {json.dumps(value, ensure_ascii=False)}")
        return [*lines, "---", ""]


def render_rankings(metadata: SnapshotMetadata) -> Path:
    for user in metadata["users"]:
        store.validate_counts(user["totalContributions"], user["restrictedContributionsCount"])
    label = "Visible to acquisition viewer"
    parameters = ReportFrontmatter(
        period=" — ".join(datetime.datetime.fromisoformat(metadata[key]).date().isoformat() for key in ("from", "to")),
        snapshot_completed=datetime.datetime.fromisoformat(metadata["generatedAt"])
        .astimezone(datetime.UTC)
        .strftime("%Y-%m-%d %H:%M:%S UTC"),
        accounts_fetched=len(metadata["users"]),
        counts_provisional=metadata.get("countsProvisional", False),
        unavailable_accounts=len(metadata.get("missingUsers", [])),
    )
    lines = [
        *parameters.to_lines(),
        "# Kazakhstan contribution rankings",
        "",
        "Both UTC dates are included. Counts reflect when each account was queried.",
        "Equal contribution counts share a rank. Counts are contributions, not only commits.",
        "Coverage is configured GitHub location search, not a census of Kazakhstan.",
        "The first metric is total minus restricted-detail counts. With an unrestricted or unknown observer, it may include private activity visible to that observer.",
        "Total includes disclosed restricted counts; undisclosed activity is unavailable.",
        "Locations and followers reflect collection time.",
        "",
    ]
    if metadata.get("missingUsers"):
        lines += ["Unavailable accounts: " + ", ".join(metadata.get("missingUsers", [])) + ".", ""]
    summary, reports = list(lines), {}
    for metric, title, filename in [
        ("visibleContributions", label, "visible-full.md"),
        ("totalContributions", "Total", "total-full.md"),
    ]:
        ranking = sorted(
            metadata["users"], key=lambda u: (-contribution_count(u, metric), -u["followers"], u["login"].lower())
        )
        table = [
            f"| Rank | User | {label} | Restricted detail | Total | Followers |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        rows = []
        previous = None
        current_rank = 0
        for position, user in enumerate(ranking, 1):
            count = contribution_count(user, metric)
            if count != previous:
                current_rank = position
            previous = count
            if count > 0:
                rows.append(
                    f"| {current_rank} | [{user['login']}](https://github.com/{user['login']}) | "
                    f"{contribution_count(user, 'visibleContributions')} | {user['restrictedContributionsCount']} | "
                    f"{user['totalContributions']} | {user['followers']} |"
                )
        summary += [
            f"## Top 100 — {title}",
            "",
            f"[Full ranking]({filename}) · {len(rows)} accounts with positive contributions.",
            "",
            *table,
            *rows[:100],
            "",
        ]
        reports[filename] = "\n".join(
            [*lines, "[Back to top 100](README.md)", "", f"## {title}", "", *table, *rows, ""]
        )
    # Publish the summary last, after both full tables have been prepared.
    reports["README.md"] = "\n".join(summary)
    directory = result_directory(metadata)
    directory.mkdir(parents=True, exist_ok=True)
    prepared = {}
    try:
        for filename, content in reports.items():
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as output:
                temporary = Path(output.name)
                prepared[filename] = temporary
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        for filename, temporary in prepared.items():
            temporary.replace(directory / filename)
        (directory / "contributions.md").unlink(missing_ok=True)
    finally:
        for temporary in prepared.values():
            temporary.unlink(missing_ok=True)
    return directory / "README.md"


def parse_period(arguments: list[str] | None = None) -> Period | None:
    parser = argparse.ArgumentParser(
        description="Collect Kazakhstan GitHub contribution rankings and create readable reports.\n"
        "Saved counts are reused automatically. Both dates are included, in UTC.\n"
        "The end date must be within one calendar year of the start and not in the future.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples (run from the repository root):
  Collect or resume a period:
    GH_TOKEN="$(gh auth token)" python3 scripts/kazakhstan_ranking.py --from 2025-10-16 --to 2026-10-16
  Keep saved counts after changing locations in config.json:
    GH_TOKEN="$(gh auth token)" python3 scripts/kazakhstan_ranking.py --from 2025-10-16 --to 2026-10-16 --keep-saved-counts
  Request fresh search results and counts: add --refresh to a collection command.
  Rebuild all saved reports without GitHub or a token:
    python3 scripts/kazakhstan_ranking.py --rebuild-reports

Authentication: set GH_TOKEN to a GitHub token, for example using gh auth token.
Only accounts matching the current location search appear in a new report.""",
    )
    parser.add_argument("--from", dest="start", metavar="YYYY-MM-DD", help="First included UTC date")
    parser.add_argument("--to", dest="end", metavar="YYYY-MM-DD", help="Last included UTC date")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--keep-saved-counts",
        action="store_true",
        help="Keep saved counts regardless of age; fetch only missing counts for users matching current locations",
    )
    modes.add_argument(
        "--refresh", action="store_true", help="Request fresh location search results and contribution counts"
    )
    modes.add_argument(
        "--rebuild-reports",
        action="store_true",
        help="Recreate all Markdown reports from the local database; no GitHub access or token needed",
    )
    args = cast(CliArguments, vars(parser.parse_args(arguments)))
    if args["rebuild_reports"]:
        if args["start"] or args["end"]:
            parser.error("--rebuild-reports rebuilds all saved periods; omit --from and --to")
        return None
    if not args["start"] or not args["end"]:
        parser.error("--from and --to are required for collection")
    try:
        start, end = _parse_period_dates(args["start"], args["end"])
    except (ValueError, OverflowError) as error:
        parser.error(str(error))
    if not os.environ.get("GH_TOKEN", "").strip():
        parser.error('GitHub authentication is required. Set GH_TOKEN, for example: GH_TOKEN="$(gh auth token)"')
    period: Period = {
        "from": start.isoformat().replace("+00:00", "Z"),
        "to": end.isoformat().replace("+00:00", "Z"),
    }
    if args["keep_saved_counts"]:
        period["_expand"] = True
    if args["refresh"]:
        period["_refresh"] = True
    return period


def _keep_profile(user: Profile, users: dict[str, Profile], country: LocationConfig, fetched_at: float) -> None:
    location = (user.get("location") or "").lower()
    excluded = country.get("excludedLocationFragments", [])
    if any(fragment.lower() in location for fragment in excluded):
        return
    previous_user = users.get(user["id"])
    if previous_user is None or previous_user.get("_profileFetchedAt", 0) <= fetched_at:
        users[user["id"]] = {**user, "_profileFetchedAt": fetched_at}


@dataclass
class ProfileSearch:
    base: str
    query: str
    country: LocationConfig
    users: dict[str, Profile] = field(default_factory=dict)
    partitions: list[dict[str, str | int]] = field(default_factory=list)

    def _split(
        self, low: int, high: int, created_start: datetime.date | None, created_end: datetime.date | None
    ) -> None:
        if low != high:
            middle = (low + high) // 2
            self.collect(low, middle)
            self.collect(middle + 1, high)
            return
        start = created_start or datetime.date(2008, 1, 1)
        end = created_end or datetime.datetime.now(datetime.UTC).date()
        if start == end:
            raise RuntimeError(f"Search limit exceeded at {low} followers on {start}")
        middle = start + (end - start) // 2
        self.collect(low, high, start, middle)
        self.collect(low, high, middle + datetime.timedelta(days=1), end)

    def collect(
        self, low: int, high: int, created_start: datetime.date | None = None, created_end: datetime.date | None = None
    ) -> None:
        query = f"{self.base} followers:{low}..{high} sort:followers-desc"
        if created_start is not None:
            assert created_end is not None
            query += f" created:{created_start.isoformat()}..{created_end.isoformat()}"
        cursor, seen, first = None, set(), True
        while True:
            response = api(self.query, {"q": query, "cursor": cursor})
            data = cast(SearchData, response)["search"]
            if first and data["userCount"] > SEARCH_RESULT_LIMIT:
                self._split(low, high, created_start, created_end)
                return
            if first:
                self.partitions.append({"query": query, "userCount": data["userCount"]})
            first = False
            fetched_at = cast(float, response.get("_fetchedAt", time.time()))
            for user in data["nodes"]:
                if not user.get("id") or not user.get("login"):
                    raise RuntimeError("User search returned an account without identity")
                _keep_profile(user, self.users, self.country, fetched_at)
            print(f"Fetched {len(self.users)} users", flush=True)
            if not data["pageInfo"]["hasNextPage"]:
                return
            cursor = data["pageInfo"]["endCursor"]
            if not cursor or cursor in seen:
                raise RuntimeError("GitHub pagination did not advance")
            seen.add(cursor)


def collect_profiles() -> tuple[list[Profile], list[dict[str, str | int]]]:
    config = cast(Config, json.loads(Path("config.json").read_text(encoding="utf-8")))
    country = config["locations"][0]
    locations = list(
        dict.fromkeys(
            place.lower() for place in [country["country"], *country["cities"], *country.get("locationAliases", [])]
        )
    )
    query = """query($q: String!, $cursor: String) {
      search(type: USER, query: $q, first: 100, after: $cursor) {
        userCount pageInfo { hasNextPage endCursor }
        nodes { ... on User { id login location followers { totalCount } } }
      }
    }"""
    search = ProfileSearch("", query, country)

    # Bound location groups so expanding aliases does not exceed GitHub search limits.
    for offset in range(0, len(locations), 12):
        base = "type:user " + " ".join(
            "location:" + (json.dumps(place, ensure_ascii=False) if " " in place else place)
            for place in locations[offset : offset + 12]
        )
        search.base = base
        maximum = cast(
            MaximumSearchData,
            api(
                """query($q: String!) {
          search(type: USER, query: $q, first: 1) {
            nodes { ... on User { followers { totalCount } } }
          }
        }""",
                {"q": base + " sort:followers-desc"},
            ),
        )["search"]["nodes"]
        if maximum:
            if "followers" not in maximum[0]:
                raise RuntimeError("Invalid country search")
            search.collect(0, maximum[0]["followers"]["totalCount"])
    if not search.users:
        raise RuntimeError("Empty country search")
    if len({user["login"].lower() for user in search.users.values()}) != len(search.users):
        raise RuntimeError("Account names changed during search; refresh the search before retrying")
    return list(search.users.values()), search.partitions


def collect_ranking(period: Period, expand: bool = False) -> None:
    started = time.monotonic()
    print(f"Included UTC calendar dates: {period['from'][:10]} -> {period['to'][:10]}", flush=True)
    profiles, partitions = collect_profiles()
    print(f"Location search completed in {time.monotonic() - started:.1f}s: {len(profiles)} users", flush=True)
    users = cached_users(profiles, period, reuse_snapshot=expand)
    missing = [profile for profile in profiles if profile["login"].lower() not in users]
    print(f"Contributions cached: {len(users)}/{len(profiles)}; to fetch: {len(missing)}", flush=True)
    fetching_started = time.monotonic()
    for start in range(0, len(missing), 20):
        users.update(fetch_users(missing[start : start + 20], period))
        completed = min(start + 20, len(missing))
        elapsed = time.monotonic() - fetching_started
        eta = datetime.timedelta(seconds=round(elapsed * (len(missing) - completed) / completed))
        print(f"Contributions fetched: {len(users)}/{len(profiles)}; ETA ~{eta}", flush=True)
    records: list[RankingUser] = [{**user, "followers": user["followers"]["totalCount"]} for user in users.values()]
    unavailable_users: list[UnavailableUser] = [
        {
            "id": profile["id"],
            "login": profile["login"],
            "location": profile.get("location"),
            "followers": profile["followers"],
        }
        for profile in profiles
        if profile["login"].lower() not in users
    ]
    metadata: SnapshotMetadata = {
        **period,
        "generatedAt": datetime.datetime.now(datetime.UTC).isoformat(),
        "selectionMode": "full-location-search",
        "periodSemantics": "inclusive-UTC-calendar-dates",
        "observerPolicy": "authenticated-viewer",
        "credentialScope": credential_scope(),
        "searchPartitions": partitions,
        "users": records,
        "unavailableUsers": unavailable_users,
    }
    members = [*records, *unavailable_users]
    if len({user["login"].lower() for user in members}) != len(members):
        raise RuntimeError("Account names changed during collection; refresh the search before retrying")
    store.save_snapshot(metadata)
    # Project the committed database, never an uncommitted in-memory candidate.
    saved = store.load_snapshots(period)[0]
    path = render_rankings(saved)
    print(f"Completed in {time.monotonic() - started:.1f}s; report: {path}", flush=True)


def main():
    global refresh_cache
    period = parse_period()
    refresh_cache = period.pop("_refresh", False) if period is not None else False
    with closing(store.connect()):
        pass
    # Serialize collection and projection without holding a database transaction during HTTP.
    with store.DATABASE.open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if period is None:
            for metadata in store.load_snapshots():
                print(render_rankings(metadata))
        else:
            collect_ranking(period, expand=period.pop("_expand", False))


if __name__ == "__main__":
    main()
