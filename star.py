"""
Update star counts in markdown files.
Author: xiejiahe
@example python3 star.py README.md README_zh-CN.md
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

GITHUB_URL_HOSTS = {"github.com", "www.github.com"}
LINK_REGEX = re.compile(r'^[ \t]*- \[[^\]\n]+\]\(([^)\s]+)\)[ \t]*')
STAR_REGEX = re.compile(r'★[ \t]*([0-9][0-9,]*)')
REPO_PATH_REGEX = re.compile(r'^/([^/]+)/([^/]+)(?:/.*)?$')


class GitHubAPIError(RuntimeError):
    """Abort an update when GitHub cannot return reliable star counts."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update GitHub star counts in markdown files.")
    parser.add_argument(
        "paths", nargs="*", default=["README.md", "README_zh-CN.md"],
        help="Markdown files to update (default: both READMEs).",
    )
    parser.add_argument(
        "--token", "-t",
        default=(
            os.getenv("GITHUB_ACCESS_TOKEN")
            or os.getenv("github_access_token")
            or os.getenv("GITHUB_TOKEN")
        ),
        help="GitHub access token.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write changes to disk.")
    return parser.parse_args()


def get_github_repo_path(url: str) -> Optional[str]:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in GITHUB_URL_HOSTS:
        return None
    match = REPO_PATH_REGEX.match(parsed.path)
    if not match:
        return None
    owner, repo = match.groups()
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not re.fullmatch(r"[A-Za-z0-9-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        return None
    return f"/{owner}/{repo}".lower()


def create_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    session.mount("https://api.github.com/", HTTPAdapter(max_retries=retries))
    return session


def get_star_count(session: requests.Session, repo_path: str, token: Optional[str] = None) -> Optional[int]:
    api_url = f"https://api.github.com/repos{repo_path}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    print(f"Fetching {api_url}")
    try:
        resp = session.get(api_url, headers=headers, timeout=10)
    except requests.RequestException as exc:
        raise GitHubAPIError(f"Request failed for {api_url}: {exc}") from exc

    if resp.status_code == 404:
        print(f"Warning: {api_url} returned 404; keeping its existing star count.", file=sys.stderr)
        return None
    if resp.status_code != 200:
        raise GitHubAPIError(f"Failed to fetch {api_url}: {resp.status_code} {resp.reason}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise GitHubAPIError(f"Invalid JSON returned by {api_url}") from exc
    star_count = data.get("stargazers_count") if isinstance(data, dict) else None
    if type(star_count) is not int or star_count < 0:
        raise GitHubAPIError(f"Invalid stargazers_count returned by {api_url}")
    return star_count


def update_star_text(line: str, star_count: int) -> str:
    link = LINK_REGEX.match(line)
    match = STAR_REGEX.match(line, link.end()) if link else None
    if match:
        return line[:match.start()] + f"★ {star_count}" + line[match.end():]
    return line


def sort_secondary_list_lines(lines: List[str]) -> List[str]:
    sorted_lines: List[str] = []
    current_group: List[Tuple[int, List[str]]] = []

    # A last line without a newline may move into the middle of a sorted group.
    missing_newline = bool(lines) and not lines[-1].endswith(("\n", "\r"))
    if missing_newline:
        lines = lines[:-1] + [lines[-1] + "\n"]

    for line in lines:
        if line.startswith("  - "):
            star = 0
            match = STAR_REGEX.search(line)
            if match:
                try:
                    star = int(match.group(1).replace(",", ""))
                except ValueError:
                    star = 0
            current_group.append((star, [line]))
            continue

        # Keep nested lists and continuation lines attached to their parent item.
        if current_group and line.startswith("    "):
            current_group[-1][1].append(line)
            continue

        if current_group:
            current_group.sort(key=lambda item: item[0], reverse=True)
            sorted_lines.extend(child for _, block in current_group for child in block)
            current_group = []

        sorted_lines.append(line)

    if current_group:
        current_group.sort(key=lambda item: item[0], reverse=True)
        sorted_lines.extend(child for _, block in current_group for child in block)

    if missing_newline:
        sorted_lines[-1] = sorted_lines[-1].rstrip("\r\n")

    return sorted_lines


def update_markdown(
    text: str, session: requests.Session, token: Optional[str], star_cache: Dict[str, Optional[int]]
) -> str:
    lines = text.splitlines(keepends=True)
    updated_lines: List[str] = []
    for line in lines:
        link_match = LINK_REGEX.match(line)
        # Only fetch entries with an existing star marker, not incidental links.
        if not link_match or not STAR_REGEX.match(line, link_match.end()):
            updated_lines.append(line)
            continue

        repo_path = get_github_repo_path(link_match.group(1))
        if not repo_path:
            updated_lines.append(line)
            continue

        if repo_path not in star_cache:
            star_cache[repo_path] = get_star_count(session, repo_path, token=token)

        star_count = star_cache[repo_path]
        if star_count is not None:
            line = update_star_text(line, star_count)

        updated_lines.append(line)

    return "".join(sort_secondary_list_lines(updated_lines))


def process_files(paths: List[Path], token: Optional[str], dry_run: bool = False) -> None:
    originals = [(path, path.read_text(encoding="utf-8")) for path in paths]
    star_cache: Dict[str, Optional[int]] = {}
    # Finish fetching for all files before writing any changes.
    with create_session() as session:
        updates = [
            (path, text, update_markdown(text, session, token, star_cache))
            for path, text in originals
        ]

    if dry_run:
        print("Dry run complete. No file changes were written.")
        return

    for path, original, updated in updates:
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            print(f"Finished updating {path}")
        else:
            print(f"No changes to {path}")


def process_file(path: Path, token: Optional[str], dry_run: bool = False) -> None:
    process_files([path], token, dry_run=dry_run)


def main() -> None:
    args = parse_args()
    try:
        process_files([Path(path) for path in args.paths], token=args.token, dry_run=args.dry_run)
    except (GitHubAPIError, OSError) as exc:
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
