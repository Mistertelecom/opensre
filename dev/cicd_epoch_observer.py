"""Dev-only: watch one PR and write a red→green epoch trace to .tmp/cicd_analytics/.

    uv run python dev/cicd_epoch_observer.py owner/repo#123

Polls the PR head and its check rollup. An epoch opens when a head settles red and
closes when the PR merges, closes, or the observer stops; a new red after green opens
the next epoch. Every head is attributed to its remote commit author, so commits made by
the OpenSRE agent are told apart from everyone else's. Delete this file before merging.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from config.constants.git import OPENSRE_COMMIT_COAUTHOR_EMAIL, OPENSRE_COMMIT_COAUTHOR_NAME
from integrations.github.tools.ci_fix.gh import run_gh_json
from integrations.github.tools.ci_repair_loop.credentials import configured_token

OPENSRE = "opensre"
# GitHub's own verdicts: a skipped or neutral check does not turn the PR red.
_FAILED = {
    "FAILURE",
    "ERROR",
    "CANCELLED",
    "TIMED_OUT",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
    "STALE",
}
_TERMINAL = _FAILED | {"SUCCESS", "SKIPPED", "NEUTRAL"}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Commit:
    sha: str
    author: str  # "opensre" or the GitHub login / name
    committer: str
    observed_at: str
    settled_at: str = ""
    result: str = ""  # red | green | superseded


@dataclass
class Epoch:
    number: int
    commits: list[Commit] = field(default_factory=list)
    merge_sha: str = ""
    closed_reason: str = ""

    @property
    def fixing_commit(self) -> Commit | None:
        return next((c for c in self.commits if c.result == "green"), None)

    @property
    def open(self) -> bool:
        return not self.closed_reason


class Observer:
    def __init__(self, owner: str, repo: str, number: int, out_dir: Path) -> None:
        self.repo = f"{owner}/{repo}"
        self.number = number
        self.token = _github_token()
        self.path = out_dir / f"{owner}__{repo}__pr{number}.txt"
        self.epochs: list[Epoch] = []
        self.head: Commit | None = None  # latest head, whether or not an epoch is open

    # -- polling -----------------------------------------------------------------

    def tick(self) -> bool:
        """Observe once; return False when the PR reached a terminal state."""
        pr = run_gh_json(
            [
                "pr",
                "view",
                str(self.number),
                "--json",
                "state,headRefOid,mergeCommit,statusCheckRollup",
            ],
            repo=self.repo,
            github_token=self.token,
        )
        sha = str(pr.get("headRefOid") or "")
        if sha and (self.head is None or self.head.sha != sha):
            self._new_head(sha)
        rows = [r for r in pr.get("statusCheckRollup") or [] if isinstance(r, dict)]
        if self.head and not self.head.result and rows and all(_terminal(r) for r in rows):
            self._settle(any(_verdict(r) in _FAILED for r in rows))
        state = str(pr.get("state") or "").upper()
        if state == "MERGED":
            merge = pr.get("mergeCommit") or {}
            self.close("merged", str(merge.get("oid") or "") if isinstance(merge, dict) else "")
        elif state == "CLOSED":
            self.close("closed without merge")
        self.write()
        return state == "OPEN"

    def _new_head(self, sha: str) -> None:
        if self.head and not self.head.result:
            self.head.result = "superseded"
            self.head.settled_at = _now()
        self.head = Commit(sha=sha, observed_at=_now(), **self._author(sha))
        if self.epochs and self.epochs[-1].open:
            self.epochs[-1].commits.append(self.head)

    def _settle(self, red: bool) -> None:
        assert self.head is not None
        self.head.result = "red" if red else "green"
        self.head.settled_at = _now()
        current = self.epochs[-1] if self.epochs else None
        if red and (current is None or not current.open or current.fixing_commit):
            if current and current.open:
                self.close("went red again")
            self.epochs.append(Epoch(number=len(self.epochs) + 1, commits=[self.head]))

    def close(self, reason: str, merge_sha: str = "") -> None:
        """Finish the open epoch, if any."""
        if self.epochs and self.epochs[-1].open:
            self.epochs[-1].closed_reason = reason
            self.epochs[-1].merge_sha = merge_sha

    def _author(self, sha: str) -> dict[str, str]:
        data = run_gh_json(
            ["api", f"repos/{self.repo}/commits/{sha}"],
            repo=self.repo,
            github_token=self.token,
            repo_flag=False,
        )
        commit = data.get("commit") or {}
        return {
            "author": _label(commit.get("author") or {}, data.get("author") or {}),
            "committer": _label(commit.get("committer") or {}, data.get("committer") or {}),
        }

    # -- rendering ---------------------------------------------------------------

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = f"# {self.repo}#{self.number}  (updated {_now()})\n"
        body = "\n\n".join(_render(e) for e in self.epochs) or "No red epoch observed yet.\n"
        self.path.write_text(header + "\n" + body, encoding="utf-8")


def _github_token() -> str:
    """OpenSRE's configured GitHub token, else whatever ``gh auth`` holds."""
    try:
        return configured_token()
    except ValueError:
        return subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        ).stdout.strip()


def _verdict(row: dict) -> str:
    return str(row.get("conclusion") or row.get("state") or "").upper()


def _terminal(row: dict) -> bool:
    return _verdict(row) in _TERMINAL


def _label(git_identity: dict, github_user: dict) -> str:
    name, email = str(git_identity.get("name") or ""), str(git_identity.get("email") or "")
    if email == OPENSRE_COMMIT_COAUTHOR_EMAIL or name == OPENSRE_COMMIT_COAUTHOR_NAME:
        return OPENSRE
    return str(github_user.get("login") or name or "unknown")


def _render(epoch: Epoch) -> str:
    fixing = epoch.fixing_commit
    lines = [f"Epoch #{epoch.number}", ""]
    for index, c in enumerate(epoch.commits):
        title = (
            "CI becomes RED"
            if index == 0
            else ("OpenSRE agent commit" if c.author == OPENSRE else "user commit")
        )
        lines += [
            f"t{index}  {title}",
            f"    commit: {c.sha[:12]}",
            f"    author: {c.author}"
            + (f"  (committer: {c.committer})" if c.committer != c.author else ""),
            f"    observed: {c.observed_at}"
            + (f"  settled: {c.settled_at}" if c.settled_at else ""),
            f"    result: {c.result or 'pending'}",
        ]
        if c is fixing:
            lines.append("    role: fixing_commit")
        lines.append("")
    if epoch.merge_sha:
        lines += [
            f"t{len(epoch.commits)}  PR merged",
            f"    merge_commit: {epoch.merge_sha[:12]}",
            "",
        ]
    status = "resolved" if fixing else ("abandoned" if epoch.closed_reason else "unresolved")
    lines += ["Epoch outcome:", f"    status: {status}"]
    if fixing:
        lines += [f"    resolver: {fixing.author}", f"    fixing_commit: {fixing.sha[:12]}"]
    if epoch.closed_reason:
        lines.append(f"    closed: {epoch.closed_reason}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("pr", help="owner/repo#number")
    parser.add_argument(
        "--red-interval", type=float, default=30, help="seconds between polls while red/pending"
    )
    parser.add_argument(
        "--green-interval", type=float, default=180, help="seconds between polls once green"
    )
    parser.add_argument("--max-hours", type=float, default=8)
    parser.add_argument("--out", type=Path, default=Path(".tmp/cicd_analytics"))
    args = parser.parse_args()

    match = re.fullmatch(r"([^/\s]+)/([^#\s]+)#(\d+)", args.pr)
    if not match:
        parser.error("expected owner/repo#number")
    observer = Observer(match[1], match[2], int(match[3]), args.out)
    deadline = time.monotonic() + args.max_hours * 3600
    print(f"Writing {observer.path}")
    try:
        while observer.tick():
            if time.monotonic() >= deadline:
                observer.close("observer deadline reached")
                break
            green = observer.head is not None and observer.head.result == "green"
            time.sleep(args.green_interval if green else args.red_interval)
    except KeyboardInterrupt:
        observer.close("observer stopped")
    observer.write()
    print(f"Done: {observer.path}")


if __name__ == "__main__":
    main()
