import contextlib
import io
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from star import (
    GitHubAPIError, LINK_REGEX, STAR_REGEX, get_github_repo_path, get_star_count,
    main, parse_args, process_files, sort_secondary_list_lines, update_markdown,
    update_star_text,
)


class StarUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_get_github_repo_path_accepts_repository_links(self):
        self.assertEqual(
            get_github_repo_path("https://github.com/Owner/Repo/tree/main"),
            "/owner/repo",
        )
        self.assertEqual(get_github_repo_path("https://www.github.com/Owner/Repo.git"), "/owner/repo")
        self.assertIsNone(get_github_repo_path("https://example.com/Owner/Repo"))
        self.assertIsNone(get_github_repo_path("https://github.com/Owner"))
        self.assertIsNone(get_github_repo_path("https://github.com.evil.test/Owner/Repo"))
        self.assertEqual(get_github_repo_path("https://github.com/Owner/Repo?tab=readme#section"), "/owner/repo")

    def test_default_files_and_legacy_token(self):
        with patch.dict(os.environ, {"github_access_token": "legacy"}, clear=True), patch("sys.argv", ["star.py"]):
            args = parse_args()
        self.assertEqual(args.paths, ["README.md", "README_zh-CN.md"])
        self.assertEqual(args.token, "legacy")

    def test_explicit_files_and_token_take_precedence(self):
        with patch.dict(os.environ, {"GITHUB_ACCESS_TOKEN": "env"}, clear=True), patch("sys.argv", ["star.py", "README.md", "-t", "cli"]):
            args = parse_args()
        self.assertEqual(args.paths, ["README.md"])
        self.assertEqual(args.token, "cli")

    def test_standard_token_takes_precedence_over_legacy(self):
        with patch.dict(os.environ, {"GITHUB_ACCESS_TOKEN": "standard", "github_access_token": "legacy"}, clear=True), patch("sys.argv", ["star.py"]):
            self.assertEqual(parse_args().token, "standard")

    def test_only_replaces_project_star_count(self):
        line = "- [repo](https://github.com/owner/repo) ★ 1,234 - description ★ 99\n"
        self.assertEqual(update_star_text(line, 0), "- [repo](https://github.com/owner/repo) ★ 0 - description ★ 99\n")
        incidental = "- [repo](https://github.com/owner/repo) - description ★ 99\n"
        self.assertEqual(update_star_text(incidental, 0), incidental)

    def test_update_markdown_updates_and_sorts_items(self):
        text = (
            "- Category\n"
            "  - [low](https://github.com/example/low) ★ 1 - low\n"
            "  - [high](https://github.com/example/high) ★ 2 - high\n"
            "    - [child](https://github.com/example/child) no star\n"
            "  - [missing](https://github.com/example/missing) ★ 3 - missing\n"
            "- [external](https://example.com/project) ★ 99 - external\n"
        )
        session = Mock()
        cache = {
            "/example/low": 10,
            "/example/high": 20,
            "/example/missing": None,
        }

        updated = update_markdown(text, session, token="secret", star_cache=cache)

        self.assertIn("[high](https://github.com/example/high) ★ 20", updated)
        self.assertIn("[low](https://github.com/example/low) ★ 10", updated)
        self.assertIn("[missing](https://github.com/example/missing) ★ 3", updated)
        self.assertLess(updated.index("[high]"), updated.index("[low]"))
        self.assertLess(updated.index("[child]"), updated.index("[missing]"))
        self.assertIn("[external](https://example.com/project) ★ 99", updated)
        session.get.assert_not_called()

    def test_sort_keeps_children_and_continuations_with_parent(self):
        low = "  - low ★ 1\n"
        child = "    - child\n"
        continuation = "    more text\n"
        high = "  - high ★ 1,000\n"
        other = "  - other ★ 5\n"
        lines = ["- First\n", low, child, continuation, high, "- Second\n", other]
        self.assertEqual(sort_secondary_list_lines(lines), ["- First\n", high, low, child, continuation, "- Second\n", other])

    def test_sort_handles_last_line_without_newline_and_stable_ties(self):
        self.assertEqual(
            "".join(sort_secondary_list_lines(["  - low ★ 1\n", "  - high ★ 2"])),
            "  - high ★ 2\n  - low ★ 1",
        )
        tied = ["  - first ★ 1\n", "  - second ★ 1\n"]
        self.assertEqual(sort_secondary_list_lines(tied), tied)

    def test_get_star_count_authentication_and_zero(self):
        session = Mock()
        session.get.return_value = Mock(status_code=200, json=Mock(return_value={"stargazers_count": 0}))
        self.assertEqual(get_star_count(session, "/owner/repo", "secret"), 0)
        self.assertEqual(session.get.call_args.kwargs["headers"]["Authorization"], "token secret")

    def test_404_keeps_old_stars_and_is_cached(self):
        session = Mock()
        session.get.return_value = Mock(status_code=404)
        text = "- [repo](https://github.com/owner/repo) ★ 42\n" * 2
        with contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(update_markdown(text, session, None, {}), text)
        session.get.assert_called_once()
        self.assertIn("404", output.getvalue())

    def test_api_errors_are_not_silently_accepted(self):
        for status in [401, 403, 429, 500, 503]:
            with self.subTest(status=status):
                session = Mock()
                session.get.return_value = Mock(status_code=status, reason="API error")
                with self.assertRaises(GitHubAPIError):
                    get_star_count(session, "/owner/repo")

        session = Mock()
        session.get.side_effect = requests.Timeout("timed out")
        with self.assertRaises(GitHubAPIError):
            get_star_count(session, "/owner/repo")

    def test_get_star_count_rejects_invalid_response(self):
        for data in [{}, [], {"stargazers_count": "42"}, {"stargazers_count": -1}, {"stargazers_count": True}]:
            with self.subTest(data=data):
                session = Mock()
                session.get.return_value = Mock(status_code=200, json=Mock(return_value=data))
                with self.assertRaises(GitHubAPIError):
                    get_star_count(session, "/owner/repo")

        session = Mock()
        session.get.return_value = Mock(status_code=200, json=Mock(side_effect=ValueError("invalid JSON")))
        with self.assertRaises(GitHubAPIError):
            get_star_count(session, "/owner/repo")

    def test_batch_failure_leaves_both_files_untouched_and_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ["README.md", "README_zh-CN.md"]]
            originals = [f"- [repo](https://github.com/owner/repo{i}) ★ 1\n" for i in range(2)]
            for path, text in zip(paths, originals):
                path.write_text(text, encoding="utf-8")
            with patch("star.get_star_count", side_effect=[100, GitHubAPIError("Rate limited")]), patch("sys.argv", ["star.py"] + [str(path) for path in paths]):
                with self.assertRaises(SystemExit) as error:
                    main()
            self.assertIn("Rate limited", str(error.exception))
            self.assertNotEqual(error.exception.code, 0)
            self.assertEqual([path.read_text(encoding="utf-8") for path in paths], originals)

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "README.md"
            original = "- [repo](https://github.com/owner/repo) ★ 1\n"
            path.write_text(original, encoding="utf-8")
            with patch("star.get_star_count", return_value=100):
                process_files([path], None, dry_run=True)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_both_real_readmes_share_counts_and_preserve_content(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ["README.md", "README_zh-CN.md"]]
            originals = [(root / path.name).read_text(encoding="utf-8") for path in paths]
            for path, text in zip(paths, originals):
                path.write_text(text, encoding="utf-8")
            session = Mock()
            # Different values on every request expose accidental duplicate fetches.
            session.get.side_effect = lambda *args, **kwargs: Mock(
                status_code=200, json=Mock(return_value={"stargazers_count": session.get.call_count * 100}),
            )
            with patch("star.create_session") as factory:
                factory.return_value.__enter__.return_value = session
                process_files(paths, "secret")

            counts = {}
            for path, original in zip(paths, originals):
                updated = path.read_text(encoding="utf-8")
                self.assertEqual(
                    Counter(STAR_REGEX.sub("★ COUNT", original).splitlines()),
                    Counter(STAR_REGEX.sub("★ COUNT", updated).splitlines()),
                )
                for line in updated.splitlines():
                    link = LINK_REGEX.match(line)
                    stars = STAR_REGEX.match(line, link.end()) if link else None
                    if not stars:
                        continue
                    repo = get_github_repo_path(link.group(1))
                    if repo in counts:
                        self.assertEqual(counts[repo], stars.group(1))
                    counts[repo] = stars.group(1)

                def children(text):
                    parent = None
                    result = {}
                    for line in text.splitlines():
                        if line.startswith("  - "):
                            parent = STAR_REGEX.sub("★ COUNT", line)
                        elif line.startswith("    "):
                            result[line] = parent
                    return result

                self.assertEqual(children(updated), children(original))
            self.assertEqual(session.get.call_count, len(counts))


if __name__ == "__main__":
    unittest.main()
