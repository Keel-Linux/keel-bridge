"""The two servers behind one interface: GitHub and Forgejo.

The engine sees only Item, Comment, Review and ReviewComment, and the
methods below. Each class turns them into the REST calls of its server.
"""

import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone

from keelbridge.text import Side, find_marker


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def fmt_time(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Item:
    number: int
    title: str
    body: str
    state: str
    labels: frozenset
    login: str
    name: str
    url: str
    is_pr: bool
    created: datetime
    updated: datetime


@dataclass
class Pull:
    number: int
    state: str
    merged: bool
    head_sha: str
    base_ref: str
    merge_sha: str
    url: str


@dataclass
class Comment:
    id: str
    number: int
    body: str
    login: str
    name: str
    url: str
    updated: datetime


@dataclass
class Review:
    id: str
    state: str
    body: str
    login: str
    name: str
    url: str
    comments: list = field(default_factory=list)


@dataclass
class ReviewComment:
    id: str
    path: str
    line: object
    body: str
    login: str
    name: str
    url: str


def _number_from_url(url):
    return int(url.rstrip("/").rsplit("/", 1)[1])


class Forge:
    """The parts that are the same for both servers."""

    side: Side
    bot: str

    def __init__(self, http, org):
        self.http = http
        self.org = org
        self._labels = {}

    def _r(self, repo):
        return f"/repos/{self.org}/{repo}"

    def repo_labels(self, repo):
        if repo not in self._labels:
            self._labels[repo] = self._load_labels(repo)
        return self._labels[repo]

    def find_marked(self, repo, origin, ident):
        """The number of an item the bot made with this marker, or None."""
        for raw in self.http.get_list(self._bot_items_path(repo), cache=False):
            if (raw["user"]["login"] == self.bot
                    and find_marker(raw.get("body")) == (origin, repo, str(ident))):
                return raw["number"]
        return None

    def find_marked_comment(self, repo, number, origin, ident):
        for c in self.item_comments(repo, number):
            if c.login == self.bot and find_marker(c.body) == (origin, repo, str(ident)):
                return c.id
        return None

    def item_comments(self, repo, number):
        return [self._comment(c) for c in
                self.http.get_list(f"{self._r(repo)}/issues/{number}/comments", cache=False)]

    def create_comment(self, repo, number, body):
        return self._comment(self.http.post(f"{self._r(repo)}/issues/{number}/comments",
                                            {"body": body}))

    def edit_comment(self, repo, ident, body):
        self.http.patch(f"{self._r(repo)}/issues/comments/{ident}", {"body": body})

    def get_item(self, repo, number):
        return self._item(self.http.get(f"{self._r(repo)}/issues/{number}"))

    def update_item(self, repo, number, **fields):
        body = {k: v for k, v in fields.items() if v is not None}
        if body:
            self.http.patch(f"{self._r(repo)}/issues/{number}", body)


class GitHub(Forge):
    side = Side("gh", "GitHub", "https://github.com", "")

    def __init__(self, http, org, bot):
        super().__init__(http, org)
        self.side = Side("gh", "GitHub", "https://github.com", org)
        self.bot = bot
        self._names = {}

    def user_name(self, login):
        if login not in self._names:
            try:
                self._names[login] = self.http.get(f"/users/{login}").get("name") or ""
            except Exception:  # noqa: BLE001 a name is a courtesy, not a need
                self._names[login] = ""
        return self._names[login]

    def _item(self, raw):
        login = raw["user"]["login"]
        return Item(raw["number"], raw["title"], raw.get("body") or "", raw["state"],
                    frozenset(lb["name"] for lb in raw.get("labels", [])), login,
                    self.user_name(login), raw["html_url"], "pull_request" in raw,
                    parse_time(raw["created_at"]), parse_time(raw["updated_at"]))

    def _comment(self, raw):
        login = raw["user"]["login"]
        return Comment(str(raw["id"]), _number_from_url(raw["issue_url"]), raw.get("body") or "",
                       login, self.user_name(login), raw["html_url"],
                       parse_time(raw["updated_at"]))

    def _load_labels(self, repo):
        return {lb["name"]: lb["name"] for lb in self.http.get_list(f"{self._r(repo)}/labels?per_page=100")}

    def _bot_items_path(self, repo):
        bot = urllib.parse.quote(self.bot)
        return f"{self._r(repo)}/issues?creator={bot}&state=all&sort=created&direction=desc&per_page=100"

    def list_updated(self, repo, since):
        return [self._item(r) for r in self.http.get_list(
            f"{self._r(repo)}/issues?state=all&sort=updated&direction=asc&per_page=100"
            f"&since={fmt_time(since)}")]

    def list_comments_since(self, repo, since):
        return [self._comment(r) for r in self.http.get_list(
            f"{self._r(repo)}/issues/comments?sort=updated&direction=asc&per_page=100"
            f"&since={fmt_time(since)}")]

    def create_issue(self, repo, title, body, labels):
        names = sorted(lb for lb in labels if lb in self.repo_labels(repo))
        return self._item(self.http.post(f"{self._r(repo)}/issues",
                                         {"title": title, "body": body, "labels": names}))

    def set_labels(self, repo, number, labels):
        self.http.put(f"{self._r(repo)}/issues/{number}/labels",
                      {"labels": sorted(lb for lb in labels if lb in self.repo_labels(repo))})

    def get_pull(self, repo, number):
        raw = self.http.get(f"{self._r(repo)}/pulls/{number}")
        return Pull(raw["number"], raw["state"], bool(raw.get("merged")), raw["head"]["sha"],
                    raw["base"]["ref"], raw.get("merge_commit_sha") or "", raw["html_url"])

    def close_pull(self, repo, number):
        self.http.patch(f"{self._r(repo)}/pulls/{number}", {"state": "closed"})

    def list_reviews(self, repo, number):
        out = []
        comments = self.http.get_list(f"{self._r(repo)}/pulls/{number}/comments?per_page=100")
        for raw in self.http.get_list(f"{self._r(repo)}/pulls/{number}/reviews?per_page=100"):
            if raw["state"] == "PENDING" or raw.get("user") is None:
                continue
            login = raw["user"]["login"]
            rev = Review(str(raw["id"]), raw["state"], raw.get("body") or "", login,
                         self.user_name(login), raw["html_url"])
            for c in comments:
                if c.get("pull_request_review_id") == raw["id"]:
                    cl = c["user"]["login"]
                    rev.comments.append(ReviewComment(
                        str(c["id"]), c["path"], c.get("line") or c.get("original_line"),
                        c.get("body") or "", cl, self.user_name(cl), c["html_url"]))
            out.append(rev)
        return out

    def fetch_url(self, repo):
        return f"https://github.com/{self.org}/{repo}.git"


class Forgejo(Forge):
    def __init__(self, http, org, bot, web):
        super().__init__(http, org)
        self.side = Side("fj", "git.pop.coop", web.rstrip("/"), org)
        self.bot = bot

    def _item(self, raw):
        user = raw["user"]
        return Item(raw["number"], raw["title"], raw.get("body") or "", raw["state"],
                    frozenset(lb["name"] for lb in raw.get("labels") or []), user["login"],
                    user.get("full_name") or "", raw["html_url"],
                    raw.get("pull_request") is not None,
                    parse_time(raw["created_at"]), parse_time(raw["updated_at"]))

    def _comment(self, raw):
        user = raw["user"]
        url = raw.get("issue_url") or raw.get("pull_request_url") or ""
        return Comment(str(raw["id"]), _number_from_url(url) if url else 0, raw.get("body") or "",
                       user["login"], user.get("full_name") or "", raw["html_url"],
                       parse_time(raw["updated_at"]))

    def _load_labels(self, repo):
        labels = {lb["name"]: lb["id"] for lb in self.http.get_list(f"/orgs/{self.org}/labels?limit=50")}
        labels.update({lb["name"]: lb["id"] for lb in self.http.get_list(f"{self._r(repo)}/labels?limit=50")})
        return labels

    def _bot_items_path(self, repo):
        return f"{self._r(repo)}/issues?created_by={self.bot}&state=all&limit=50"

    def list_updated(self, repo, since):
        return [self._item(r) for r in self.http.get_list(
            f"{self._r(repo)}/issues?state=all&limit=50&since={fmt_time(since)}")]

    def list_comments_since(self, repo, since):
        return [self._comment(r) for r in self.http.get_list(
            f"{self._r(repo)}/issues/comments?limit=50&since={fmt_time(since)}")]

    def _label_ids(self, repo, labels):
        known = self.repo_labels(repo)
        return sorted(known[lb] for lb in labels if lb in known)

    def create_issue(self, repo, title, body, labels):
        return self._item(self.http.post(f"{self._r(repo)}/issues", {
            "title": title, "body": body, "labels": self._label_ids(repo, labels)}))

    def set_labels(self, repo, number, labels):
        self.http.put(f"{self._r(repo)}/issues/{number}/labels",
                      {"labels": self._label_ids(repo, labels)})

    def get_pull(self, repo, number):
        raw = self.http.get(f"{self._r(repo)}/pulls/{number}")
        return Pull(raw["number"], raw["state"], bool(raw.get("merged")), raw["head"]["sha"],
                    raw["base"]["ref"], raw.get("merge_commit_sha") or "", raw["html_url"])

    def close_pull(self, repo, number):
        self.http.patch(f"{self._r(repo)}/pulls/{number}", {"state": "closed"})

    def list_reviews(self, repo, number):
        out = []
        for raw in self.http.get_list(f"{self._r(repo)}/pulls/{number}/reviews?limit=50"):
            if raw["state"] in ("PENDING", "REQUEST_REVIEW") or raw.get("user") is None:
                continue
            user = raw["user"]
            rev = Review(str(raw["id"]), raw["state"], raw.get("body") or "", user["login"],
                         user.get("full_name") or "", raw["html_url"])
            if raw.get("comments_count"):
                for c in self.http.get_list(
                        f"{self._r(repo)}/pulls/{number}/reviews/{raw['id']}/comments"):
                    cu = c["user"]
                    # Forgejo gives a diff position, not a line: show the path only.
                    rev.comments.append(ReviewComment(
                        str(c["id"]), c["path"], None, c.get("body") or "", cu["login"],
                        cu.get("full_name") or "", c["html_url"]))
            out.append(rev)
        return out

    sleep = staticmethod(time.sleep)

    def ensure_fork(self, repo):
        """The bot's fork of repo. A new fork gets Actions, issues and wiki off."""
        try:
            self.http.get(f"/repos/{self.bot}/{repo}")
        except Exception as err:  # noqa: BLE001
            if getattr(err, "status", None) != 404:
                raise
            self.http.post(f"{self._r(repo)}/forks", {})
            for _ in range(10):
                try:
                    self.http.get(f"/repos/{self.bot}/{repo}")
                    break
                except Exception:  # noqa: BLE001 the fork is still on its way
                    self.sleep(3)
            self.http.patch(f"/repos/{self.bot}/{repo}",
                            {"has_actions": False, "has_issues": False, "has_wiki": False,
                             "has_projects": False, "has_releases": False,
                             "has_packages": False})
        return f"{self.side.web}/{self.bot}/{repo}.git"

    def create_pull(self, repo, branch, base, title, body):
        raw = self.http.post(f"{self._r(repo)}/pulls", {
            "head": f"{self.bot}:{branch}", "base": base, "title": title, "body": body})
        return self._item(raw)

    def delete_fork_branch(self, repo, branch):
        try:
            self.http.delete(f"/repos/{self.bot}/{repo}/branches/{urllib.parse.quote(branch, safe='')}")
        except Exception as err:  # noqa: BLE001
            if getattr(err, "status", None) != 404:
                raise
