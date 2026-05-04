from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlparse

from .models import CreatedIssue, DiscussionComment, Issue
from .privacy import sanitize_user_paths
from .shell import run_cmd


class GHError(RuntimeError):
    pass


class GHClient:
    def __init__(self, repo: str):
        self.repo = repo

    def _api_repo_args(self) -> tuple[list[str], str]:
        parts = self.repo.split("/")
        if len(parts) == 2:
            owner, repo = parts
            return [], f"repos/{owner}/{repo}"
        if len(parts) == 3:
            host, owner, repo = parts
            return ["--hostname", host], f"repos/{owner}/{repo}"
        raise GHError("repo must be in owner/repo or host/owner/repo form")

    def _run(self, args: list[str]):
        result = run_cmd(args)
        if result.exit_code != 0:
            raise GHError(
                result.stderr.strip()
                or result.stdout.strip()
                or f"gh command failed: {result.command}"
            )
        return result

    def _paginated_items(self, args: list[str]) -> list[dict]:
        result = self._run([*args, "--paginate", "--slurp"])
        data = json.loads(result.stdout or "[]")
        if data and all(isinstance(page, list) for page in data):
            return [item for page in data for item in page]
        if isinstance(data, list):
            return data
        return []

    @staticmethod
    def _pr_number_from_url(pr_url: str) -> int:
        path = urlparse(pr_url).path.rstrip("/")
        parts = [part for part in path.split("/") if part]
        if len(parts) < 2 or parts[-2] != "pull":
            raise GHError(f"could not parse pull request number from URL: {pr_url}")
        try:
            return int(parts[-1])
        except ValueError as exc:
            raise GHError(
                f"could not parse pull request number from URL: {pr_url}"
            ) from exc

    @staticmethod
    def _issue_number_from_url(issue_url: str) -> int:
        path = urlparse(issue_url).path.rstrip("/")
        parts = [part for part in path.split("/") if part]
        if len(parts) < 2 or parts[-2] != "issues":
            raise GHError(f"could not parse issue number from URL: {issue_url}")
        try:
            return int(parts[-1])
        except ValueError as exc:
            raise GHError(
                f"could not parse issue number from URL: {issue_url}"
            ) from exc

    @contextmanager
    def _sanitized_body_file(self, body_file: Path):
        body = body_file.read_text(encoding="utf-8", errors="replace")
        sanitized = sanitize_user_paths(body)
        if sanitized == body:
            yield body_file
            return

        temp_path: Path | None = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
                handle.write(sanitized)
                temp_path = Path(handle.name)
            yield temp_path
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)

    def validate(self) -> None:
        self._run(["gh", "auth", "status"])
        self._run(["gh", "repo", "view", self.repo])

    def list_issues(self, labels: str | list[str]) -> list[Issue]:
        requested = [labels] if isinstance(labels, str) else labels
        items_by_number: dict[int, Issue] = {}
        for label in requested:
            result = self._run(
                [
                    "gh",
                    "issue",
                    "list",
                    "--repo",
                    self.repo,
                    "--state",
                    "open",
                    "--label",
                    label,
                    "--json",
                    "number,title,body,labels,state,url,updatedAt",
                ]
            )
            for item in json.loads(result.stdout or "[]"):
                issue = Issue.from_gh(item)
                items_by_number[issue.number] = issue
        return list(items_by_number.values())

    def view_issue(self, number: int) -> Issue:
        result = self._run(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "--repo",
                self.repo,
                "--json",
                "number,title,body,labels,state,url,updatedAt",
            ]
        )
        return Issue.from_gh(json.loads(result.stdout))

    def blocked_by(self, number: int) -> list[Issue]:
        hostname_args, repo_path = self._api_repo_args()
        items = self._paginated_items(
            [
                "gh",
                "api",
                *hostname_args,
                f"{repo_path}/issues/{number}/dependencies/blocked_by",
            ]
        )
        return [Issue.from_gh(item) for item in items]

    def sub_issues(self, number: int) -> list[Issue]:
        hostname_args, repo_path = self._api_repo_args()
        items = self._paginated_items(
            [
                "gh",
                "api",
                *hostname_args,
                f"{repo_path}/issues/{number}/sub_issues",
            ]
        )
        return [Issue.from_gh(item) for item in items]

    def _issue_api_record(self, number: int) -> dict:
        hostname_args, repo_path = self._api_repo_args()
        result = self._run(
            ["gh", "api", *hostname_args, f"{repo_path}/issues/{number}"]
        )
        data = json.loads(result.stdout or "{}")
        if not isinstance(data, dict) or not data.get("id"):
            raise GHError(f"could not load GitHub issue API id for issue #{number}")
        return data

    def add_sub_issue(self, parent_number: int, child_issue_id: int) -> None:
        hostname_args, repo_path = self._api_repo_args()
        self._run(
            [
                "gh",
                "api",
                *hostname_args,
                "-X",
                "POST",
                f"{repo_path}/issues/{parent_number}/sub_issues",
                "-f",
                f"sub_issue_id={child_issue_id}",
            ]
        )

    def add_blocked_by(self, issue_number: int, blocking_issue_id: int) -> None:
        hostname_args, repo_path = self._api_repo_args()
        self._run(
            [
                "gh",
                "api",
                *hostname_args,
                "-X",
                "POST",
                f"{repo_path}/issues/{issue_number}/dependencies/blocked_by",
                "-f",
                f"issue_id={blocking_issue_id}",
            ]
        )

    def issue_comments(self, number: int) -> list[DiscussionComment]:
        hostname_args, repo_path = self._api_repo_args()
        items = self._paginated_items(
            [
                "gh",
                "api",
                *hostname_args,
                f"{repo_path}/issues/{number}/comments",
            ]
        )
        return [DiscussionComment.from_gh(item, "issue comment") for item in items]

    def pr_comments(self, pr_url: str) -> list[DiscussionComment]:
        hostname_args, repo_path = self._api_repo_args()
        pr_number = self._pr_number_from_url(pr_url)
        items = self._paginated_items(
            [
                "gh",
                "api",
                *hostname_args,
                f"{repo_path}/pulls/{pr_number}/comments",
            ]
        )
        return [
            DiscussionComment.from_gh(item, "pull request comment") for item in items
        ]

    def pr_reviews(self, pr_url: str) -> list[DiscussionComment]:
        hostname_args, repo_path = self._api_repo_args()
        pr_number = self._pr_number_from_url(pr_url)
        items = self._paginated_items(
            [
                "gh",
                "api",
                *hostname_args,
                f"{repo_path}/pulls/{pr_number}/reviews",
            ]
        )
        return [
            DiscussionComment.from_gh(item, "pull request review")
            for item in items
            if item.get("body")
        ]

    def add_label(self, number: int, label: str) -> None:
        self._run(
            [
                "gh",
                "issue",
                "edit",
                str(number),
                "--repo",
                self.repo,
                "--add-label",
                label,
            ]
        )

    def remove_label(self, number: int, label: str) -> None:
        self._run(
            [
                "gh",
                "issue",
                "edit",
                str(number),
                "--repo",
                self.repo,
                "--remove-label",
                label,
            ]
        )

    def ensure_label(self, name: str, color: str, description: str) -> None:
        self._run(
            [
                "gh",
                "label",
                "create",
                name,
                "--repo",
                self.repo,
                "--color",
                color,
                "--description",
                description,
                "--force",
            ]
        )

    def ensure_labels(self, labels: Mapping[str, tuple[str, str]]) -> None:
        for name, (color, description) in labels.items():
            self.ensure_label(name, color, description)

    def comment(self, number: int, body_file: Path) -> None:
        with self._sanitized_body_file(body_file) as sanitized:
            self._run(
                [
                    "gh",
                    "issue",
                    "comment",
                    str(number),
                    "--repo",
                    self.repo,
                    "--body-file",
                    str(sanitized),
                ]
            )

    def create_issue(
        self, title: str, body_file: Path, labels: list[str] | None = None
    ) -> str:
        return self.create_issue_record(title, body_file, labels).url

    def create_issue_record(
        self, title: str, body_file: Path, labels: list[str] | None = None
    ) -> CreatedIssue:
        args = [
            "gh",
            "issue",
            "create",
            "--repo",
            self.repo,
            "--title",
            sanitize_user_paths(title),
        ]
        body_index = len(args)
        args.extend(["--body-file", ""])
        for label in labels or []:
            args.extend(["--label", label])
        with self._sanitized_body_file(body_file) as sanitized:
            args[body_index + 1] = str(sanitized)
            result = self._run(args)
        url = result.stdout.strip().splitlines()[-1]
        number = self._issue_number_from_url(url)
        data = self._issue_api_record(number)
        issue = Issue.from_gh(data)
        return CreatedIssue(
            number=number,
            title=issue.title or title,
            url=url,
            id=issue.id or int(data["id"]),
        )

    def create_pr(
        self,
        base: str,
        head: str,
        title: str,
        body_file: Path,
        draft: bool = True,
    ) -> str:
        args = [
            "gh",
            "pr",
            "create",
            "--repo",
            self.repo,
            "--base",
            base,
            "--head",
            head,
            "--title",
            sanitize_user_paths(title),
        ]
        body_index = len(args)
        args.extend(["--body-file", ""])
        if draft:
            args.append("--draft")
        with self._sanitized_body_file(body_file) as sanitized:
            args[body_index + 1] = str(sanitized)
            result = self._run(args)
        return result.stdout.strip().splitlines()[-1]

    def update_pr(self, pr_url: str, title: str, body_file: Path) -> None:
        args = [
            "gh",
            "pr",
            "edit",
            pr_url,
            "--repo",
            self.repo,
            "--title",
            sanitize_user_paths(title),
        ]
        body_index = len(args)
        args.extend(["--body-file", ""])
        with self._sanitized_body_file(body_file) as sanitized:
            args[body_index + 1] = str(sanitized)
            self._run(args)
