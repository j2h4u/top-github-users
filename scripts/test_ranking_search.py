import datetime
import json
import os
import re
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest.mock import patch

import kazakhstan_ranking as ranking


def search_page(selected: Sequence[Mapping[str, object]], variables: dict[str, str | int | None]) -> dict[str, object]:
    cursor = int(variables["cursor"] or 0)
    end = cursor + 100
    return {
        "search": {
            "userCount": len(selected),
            "nodes": list(selected[cursor:end]),
            "pageInfo": {"hasNextPage": end < len(selected), "endCursor": str(end)},
        }
    }


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.previous = Path.cwd()
        self.directory = tempfile.TemporaryDirectory()
        os.chdir(self.directory.name)
        Path("config.json").write_text(json.dumps({"locations": [{"country": "Kazakhstan", "cities": ["Astana"]}]}))
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(os.chdir, self.previous)

    def test_complete_partitioned_search_over_1000_without_option(self):
        profiles = [{"id": f"ID-{i}", "login": f"u{i}", "followers": {"totalCount": i % 2}} for i in range(1101)]

        def api(query: str, variables: dict[str, str | int | None]) -> dict[str, object]:
            q = variables["q"]
            if not isinstance(q, str):
                raise AssertionError("expected search query")
            self.assertIn("type:user", q)
            self.assertIn("location:astana", q)
            if "first: 1)" in query:
                return {"search": {"nodes": [{"followers": {"totalCount": 1}}]}}
            match = re.search(r"followers:(\d+)\.\.(\d+)", q)
            if match is None:
                raise AssertionError("expected follower partition")
            low, high = map(int, match.groups())
            selected = [p for p in profiles if low <= p["followers"]["totalCount"] <= high]
            return search_page(selected, variables)

        with patch.object(ranking, "api", side_effect=api), patch("builtins.print"):
            users, partitions = ranking.collect_profiles()
        self.assertEqual(len(users), 1101)
        self.assertEqual(len(partitions), 2)
        self.assertTrue(all(isinstance(p["userCount"], int) and p["userCount"] <= 1000 for p in partitions))

    def test_creation_dates_split_a_single_follower_bucket(self):
        profiles = [
            {
                "id": f"ID-{i}",
                "login": f"u{i}",
                "followers": {"totalCount": 0},
                "created": datetime.date(2010 if i < 600 else 2020, 1, 1),
            }
            for i in range(1101)
        ]

        def api(query: str, variables: dict[str, str | int | None]) -> dict[str, object]:
            if "first: 1)" in query:
                return {"search": {"nodes": [{"followers": {"totalCount": 0}}]}}
            query_text = variables["q"]
            if not isinstance(query_text, str):
                raise AssertionError("expected search query")
            match = re.search(r"created:([\d-]+)\.\.([\d-]+)", query_text)
            selected = profiles
            if match:
                start, end = map(datetime.date.fromisoformat, match.groups())
                selected = [p for p in profiles if start <= p["created"] <= end]
            return search_page(selected, variables)

        with patch.object(ranking, "api", side_effect=api), patch("builtins.print"):
            users, partitions = ranking.collect_profiles()
        self.assertEqual(len(users), 1101)
        self.assertTrue(
            all(
                isinstance(p["query"], str)
                and "created:" in p["query"]
                and isinstance(p["userCount"], int)
                and p["userCount"] <= 1000
                for p in partitions
            )
        )

    def test_alias_groups_merge_unicode_and_spaced_locations_by_identity(self):
        aliases = [f"place{i}" for i in range(11)] + ["Қазақстан", "Алматы", "Qazaqstan", "Nur Sultan"]
        Path("config.json").write_text(
            json.dumps({"locations": [{"country": "Kazakhstan", "cities": ["Astana"], "locationAliases": aliases}]})
        )
        alice = {"id": "ID-A", "login": "alice", "followers": {"totalCount": 1}}
        bob = {"id": "ID-B", "login": "bob", "followers": {"totalCount": 1}}
        searches = []

        def api(query: str, variables: dict[str, str | int | None]) -> dict[str, object]:
            q = variables["q"]
            if not isinstance(q, str):
                raise AssertionError("expected search query")
            searches.append(q)
            nodes = [alice, bob] if "location:алматы" in q else [alice]
            if "first: 1)" in query:
                return {"search": {"nodes": nodes[:1]}}
            return {
                "search": {
                    "userCount": len(nodes),
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }

        with patch.object(ranking, "api", side_effect=api), patch("builtins.print"):
            users, partitions = ranking.collect_profiles()
        self.assertEqual({u["id"] for u in users}, {"ID-A", "ID-B"})
        self.assertEqual(len(partitions), 2)
        self.assertTrue(any("location:қазақстан" in q and "location:qazaqstan" in q for q in searches))
        self.assertTrue(any('location:"nur sultan"' in q for q in searches))
        self.assertTrue(all(q.count("location:") <= 12 for q in searches))

    def test_known_foreign_locations_are_excluded(self):
        Path("config.json").write_text(
            json.dumps(
                {
                    "locations": [
                        {
                            "country": "Kazakhstan",
                            "cities": ["Petropavlovsk"],
                            "excludedLocationFragments": ["kamchat", "камчат", "oral drug"],
                        }
                    ]
                }
            )
        )
        profiles = [
            {"id": f"ID-{i}", "login": f"u{i}", "location": location, "followers": {"totalCount": 1}}
            for i, location in enumerate(
                ["Petropavlovsk", "Petropavlovsk-Kamchatsky", "Петропавловск-Камчатский", "Oral Drug Delivery"]
            )
        ]
        replies = [
            {"search": {"nodes": profiles[:1]}},
            {"search": {"userCount": 4, "nodes": profiles, "pageInfo": {"hasNextPage": False}}},
        ]
        with patch.object(ranking, "api", side_effect=replies), patch("builtins.print"):
            users, _ = ranking.collect_profiles()
        self.assertEqual([u["id"] for u in users], ["ID-0"])

    def test_overlapping_groups_keep_the_newest_search_profile(self):
        Path("config.json").write_text(
            json.dumps(
                {
                    "locations": [
                        {
                            "country": "Kazakhstan",
                            "cities": ["Astana"],
                            "locationAliases": [f"place{i}" for i in range(11)],
                        }
                    ]
                }
            )
        )

        def api(_query: str, variables: dict[str, str | int | None]) -> dict[str, object]:
            q = variables["q"]
            if not isinstance(q, str):
                raise AssertionError("expected search query")
            stale = "location:place10" in q
            user = {"id": "ID-A", "login": "OldName" if stale else "NewName", "followers": {"totalCount": 1}}
            search = {"nodes": [user], "userCount": 1, "pageInfo": {"hasNextPage": False}}
            return {"search": search, "_fetchedAt": 100 if stale else 300}

        with patch.object(ranking, "api", side_effect=api), patch("builtins.print"):
            users, partitions = ranking.collect_profiles()
        self.assertEqual(len(partitions), 2)
        self.assertEqual([(u["login"], u.get("_profileFetchedAt")) for u in users], [("NewName", 300)])

    def test_missing_repeated_and_cyclic_cursors_fail(self):
        for cursors in ([None], ["same", "same"], ["a", "b", "a"]):
            replies = [{"search": {"nodes": [{"followers": {"totalCount": 1}}]}}]
            replies += [
                {"search": {"userCount": 10, "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": c}}}
                for c in cursors
            ]
            with (
                patch.object(ranking, "api", side_effect=replies),
                patch("builtins.print"),
                self.assertRaisesRegex(RuntimeError, "pagination"),
            ):
                ranking.collect_profiles()

    def test_empty_or_organization_maximum_fails_clearly(self):
        for nodes in ([], [{}]):
            with (
                patch.object(ranking, "api", return_value={"search": {"nodes": nodes}}),
                self.assertRaisesRegex(RuntimeError, "country search"),
            ):
                ranking.collect_profiles()


if __name__ == "__main__":
    unittest.main()
