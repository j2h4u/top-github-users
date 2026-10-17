"""Typed shapes shared by the ranking collector and its SQLite store."""

from typing import NotRequired, TypedDict

Period = TypedDict(
    "Period",
    {
        "from": str,
        "to": str,
        "_expand": NotRequired[bool],
        "_refresh": NotRequired[bool],
    },
)


class FollowersCount(TypedDict):
    totalCount: int


class Profile(TypedDict):
    id: str
    login: str
    location: NotRequired[str | None]
    followers: FollowersCount
    _profileFetchedAt: NotRequired[float]


class ContributionCalendar(TypedDict):
    totalContributions: int


class ContributionsCollection(TypedDict):
    contributionCalendar: ContributionCalendar
    restrictedContributionsCount: int


class ApiUser(TypedDict):
    id: str
    login: str
    contributionsCollection: ContributionsCollection


class ObservedUser(Profile):
    totalContributions: int
    restrictedContributionsCount: int
    _observation: int


class RankingUser(TypedDict):
    id: str
    login: str
    followers: int
    totalContributions: int
    restrictedContributionsCount: int
    _observation: NotRequired[int]
    location: NotRequired[str | None]
    _profileFetchedAt: NotRequired[float]


class UnavailableUser(TypedDict):
    login: str
    id: NotRequired[str]
    location: NotRequired[str | None]
    followers: NotRequired[FollowersCount]


class SearchPageInfo(TypedDict):
    hasNextPage: bool
    endCursor: str | None


class SearchResult(TypedDict):
    userCount: int
    pageInfo: SearchPageInfo
    nodes: list[Profile]


class SearchData(TypedDict):
    search: SearchResult


class MaximumSearchResult(TypedDict):
    nodes: list["FollowerNode"]


class FollowerNode(TypedDict):
    followers: FollowersCount


class MaximumSearchData(TypedDict):
    search: MaximumSearchResult


class GraphQLError(TypedDict, total=False):
    type: NotRequired[str]
    message: NotRequired[str]
    path: NotRequired[list[str | int]]


class GraphQLResponse(TypedDict, total=False):
    data: dict[str, object] | None
    errors: list[GraphQLError]


SnapshotMetadata = TypedDict(
    "SnapshotMetadata",
    {
        "from": str,
        "to": str,
        "generatedAt": str,
        "users": list[RankingUser],
        "countsProvisional": NotRequired[bool],
        "selectionMode": NotRequired[str],
        "periodSemantics": NotRequired[str],
        "observerPolicy": NotRequired[str],
        "credentialScope": NotRequired[str],
        "searchPartitions": NotRequired[list[dict[str, str | int]]],
        "unavailableUsers": NotRequired[list[UnavailableUser]],
        "missingUsers": NotRequired[list[str]],
    },
)


class LocationConfig(TypedDict):
    country: str
    cities: list[str]
    locationAliases: NotRequired[list[str]]
    excludedLocationFragments: NotRequired[list[str]]


class Config(TypedDict):
    locations: list[LocationConfig]


class CliArguments(TypedDict):
    start: str | None
    end: str | None
    rebuild_reports: bool
    keep_saved_counts: bool
    refresh: bool
