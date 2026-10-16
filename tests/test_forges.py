"""Tests of keelbridge.forges: the REST calls of each server."""

from datetime import datetime, timezone

import pytest

from keelbridge.forges import Forgejo, GitHub, fmt_time, parse_time
from keelbridge.http import HttpError
from keelbridge.text import marker

T = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes  # list of (method, path prefix, answer or exception)
        self.calls = []

    def _answer(self, method, path, body=None):
        self.calls.append((method, path, body))
        for i, (m, prefix, ans) in enumerate(self.routes):
            if m == method and path.startswith(prefix):
                if isinstance(ans, list) and ans and isinstance(ans[0], Exception):
                    err = ans.pop(0)
                    if not ans:
                        self.routes.pop(i)
                    raise err
                if isinstance(ans, Exception):
                    raise ans
                return ans
        raise AssertionError(f"no route for {method} {path}")

    def get(self, path):
        return self._answer("GET", path)

    def get_list(self, path, cache=True):
        return self._answer("GET", path)

    def post(self, path, body):
        return self._answer("POST", path, body)

    def patch(self, path, body):
        return self._answer("PATCH", path, body)

    def put(self, path, body):
        return self._answer("PUT", path, body)

    def delete(self, path):
        return self._answer("DELETE", path)


def gh_issue(n, body="b", pr=False, labels=("bug",)):
    raw = {"number": n, "title": f"t{n}", "body": body, "state": "open",
           "labels": [{"name": lb} for lb in labels], "user": {"login": "alice"},
           "html_url": f"https://github.com/Keel-Linux/r/issues/{n}",
           "created_at": "2026-10-16T12:00:00Z", "updated_at": "2026-10-16T12:05:00Z"}
    if pr:
        raw["pull_request"] = {}
    return raw


def fj_issue(n, body="b", pr=False):
    return {"number": n, "title": f"t{n}", "body": body, "state": "open",
            "labels": [{"name": "bug", "id": 5}],
            "user": {"login": "marcos", "full_name": "Marcos"},
            "html_url": f"https://git.pop.coop/Keel-Linux/r/issues/{n}",
            "pull_request": {"merged": False} if pr else None,
            "created_at": "2026-10-16T09:00:00-03:00", "updated_at": "2026-10-16T09:05:00-03:00"}


def test_time_helpers():
    assert parse_time("2026-10-16T09:00:00-03:00") == T
    assert fmt_time(T) == "2026-10-16T12:00:00Z"


def test_github_lists_items_and_names_users():
    http = FakeHttp([("GET", "/users/alice", {"name": "Alice"}),
                     ("GET", "/repos/Keel-Linux/r/issues?", [gh_issue(1), gh_issue(2, pr=True)])])
    gh = GitHub(http, "Keel-Linux", "kb[bot]")
    items = gh.list_updated("r", T)
    assert [i.number for i in items] == [1, 2]
    assert items[0].name == "Alice" and not items[0].is_pr and items[1].is_pr
    assert items[0].labels == {"bug"}
    assert "since=2026-10-16T12:00:00Z" in http.calls[0][1]
    gh.user_name("alice")
    assert sum(1 for c in http.calls if c[1] == "/users/alice") == 1


def test_github_user_name_failure_is_empty():
    http = FakeHttp([("GET", "/users/", HttpError(404, "GET", "u", "x"))])
    assert GitHub(http, "O", "b").user_name("ghost") == ""


def test_github_comments_and_writes():
    comment = {"id": 9, "issue_url": "https://api.github.com/repos/O/r/issues/4", "body": "hi",
               "user": {"login": "bob"}, "html_url": "h", "updated_at": "2026-10-16T12:00:00Z"}
    http = FakeHttp([
        ("GET", "/users/", {"name": ""}),
        ("GET", "/repos/O/r/issues/comments?", [comment]),
        ("GET", "/repos/O/r/issues/4/comments", [comment]),
        ("POST", "/repos/O/r/issues/4/comments", comment),
        ("PATCH", "/repos/O/r/issues/comments/9", {}),
        ("GET", "/repos/O/r/labels", [{"name": "bug"}, {"name": "docs"}]),
        ("POST", "/repos/O/r/issues", gh_issue(7)),
        ("PUT", "/repos/O/r/issues/7/labels", []),
        ("GET", "/repos/O/r/issues/7", gh_issue(7)),
        ("PATCH", "/repos/O/r/issues/7", {}),
    ])
    gh = GitHub(http, "O", "kb[bot]")
    assert gh.list_comments_since("r", T)[0].number == 4
    assert gh.item_comments("r", 4)[0].id == "9"
    assert gh.create_comment("r", 4, "x").id == "9"
    gh.edit_comment("r", "9", "y")
    assert gh.create_issue("r", "t", "b", {"bug", "nope"}).number == 7
    assert ("POST", "/repos/O/r/issues", {"title": "t", "body": "b", "labels": ["bug"]}) in http.calls
    gh.set_labels("r", 7, {"docs", "nope"})
    assert ("PUT", "/repos/O/r/issues/7/labels", {"labels": ["docs"]}) in http.calls
    assert gh.get_item("r", 7).number == 7
    gh.update_item("r", 7, state="closed", title=None)
    assert ("PATCH", "/repos/O/r/issues/7", {"state": "closed"}) in http.calls
    n = len(http.calls)
    gh.update_item("r", 7)
    assert len(http.calls) == n


def test_github_find_marked_items_and_comments():
    mine = gh_issue(3, body="x " + marker("fj", "r", "12"))
    mine["user"] = {"login": "kb[bot]"}
    spoof = gh_issue(5, body=marker("fj", "r", "13"))
    other = gh_issue(4, body="no marker")
    note = {"id": 5, "issue_url": "https://api/x/issues/3", "body": marker("fj", "r", "c8"),
            "user": {"login": "kb[bot]"}, "html_url": "h", "updated_at": "2026-10-16T12:00:00Z"}
    http = FakeHttp([("GET", "/users/", {"name": ""}),
                     ("GET", "/repos/O/r/issues?creator=kb%5Bbot%5D", [other, spoof, mine]),
                     ("GET", "/repos/O/r/issues/3/comments", [note])])
    gh = GitHub(http, "O", "kb[bot]")
    assert gh.find_marked("r", "fj", 12) == 3
    assert gh.find_marked("r", "fj", 99) is None
    assert gh.find_marked("r", "fj", 13) is None
    assert gh.find_marked_comment("r", 3, "fj", "c8") == "5"
    assert gh.find_marked_comment("r", 3, "fj", "c9") is None


def test_github_pulls_and_reviews():
    pull = {"number": 2, "state": "open", "merged": False, "head": {"sha": "abc"},
            "base": {"ref": "main"}, "merge_commit_sha": None, "html_url": "p"}
    reviews = [
        {"id": 1, "state": "APPROVED", "body": "ok", "user": {"login": "carol"}, "html_url": "r1"},
        {"id": 2, "state": "PENDING", "body": "", "user": {"login": "carol"}, "html_url": "r2"},
        {"id": 3, "state": "COMMENTED", "body": "", "user": None, "html_url": "r3"},
    ]
    rcs = [{"id": 11, "pull_request_review_id": 1, "path": "a.py", "line": None,
            "original_line": 4, "body": "nit", "user": {"login": "carol"}, "html_url": "c"},
           {"id": 12, "pull_request_review_id": 99, "path": "b", "line": 1, "body": "",
            "user": {"login": "x"}, "html_url": "c"}]
    http = FakeHttp([("GET", "/users/", {"name": "Carol"}),
                     ("GET", "/repos/O/r/pulls/2/comments", rcs),
                     ("GET", "/repos/O/r/pulls/2/reviews", reviews),
                     ("GET", "/repos/O/r/pulls/2", pull),
                     ("PATCH", "/repos/O/r/pulls/2", {})])
    gh = GitHub(http, "O", "b")
    p = gh.get_pull("r", 2)
    assert (p.head_sha, p.base_ref, p.merged, p.merge_sha) == ("abc", "main", False, "")
    gh.close_pull("r", 2)
    revs = gh.list_reviews("r", 2)
    assert len(revs) == 1 and revs[0].name == "Carol"
    assert revs[0].comments[0].line == 4 and revs[0].comments[0].path == "a.py"
    assert gh.fetch_url("r") == "https://github.com/O/r.git"


def test_forgejo_items_comments_and_labels():
    comment = {"id": 3, "issue_url": "", "pull_request_url": "https://git/O/r/pulls/6",
               "body": "c", "user": {"login": "m", "full_name": ""}, "html_url": "h",
               "updated_at": "2026-10-16T12:00:00Z"}
    bare = dict(comment, pull_request_url="")
    http = FakeHttp([
        ("GET", "/repos/O/r/issues?state=all", [fj_issue(1), fj_issue(6, pr=True)]),
        ("GET", "/repos/O/r/issues/comments?", [comment, bare]),
        ("GET", "/orgs/O/labels", [{"name": "org", "id": 1}]),
        ("GET", "/repos/O/r/labels", [{"name": "bug", "id": 5}]),
        ("POST", "/repos/O/r/issues", fj_issue(8)),
        ("PUT", "/repos/O/r/issues/8/labels", []),
    ])
    fj = Forgejo(http, "O", "keel-bridge", "https://git.pop.coop/")
    items = fj.list_updated("r", T)
    assert items[0].name == "Marcos" and items[1].is_pr and not items[0].is_pr
    assert items[0].created == T
    cs = fj.list_comments_since("r", T)
    assert cs[0].number == 6 and cs[1].number == 0
    fj.create_issue("r", "t", "b", {"bug", "org", "x"})
    assert ("POST", "/repos/O/r/issues", {"title": "t", "body": "b", "labels": [1, 5]}) in http.calls
    fj.set_labels("r", 8, {"bug"})
    assert ("PUT", "/repos/O/r/issues/8/labels", {"labels": [5]}) in http.calls
    assert fj.side.web == "https://git.pop.coop"
    assert "created_by=keel-bridge" in fj._bot_items_path("r")


def test_forgejo_pulls_reviews_and_fork():
    pull = {"number": 6, "state": "closed", "merged": True, "head": {"sha": "abc"},
            "base": {"ref": "main"}, "merge_commit_sha": "m", "html_url": "p"}
    reviews = [
        {"id": 1, "state": "APPROVED", "body": "ok", "user": {"login": "m", "full_name": "M"},
         "html_url": "r1", "comments_count": 1},
        {"id": 2, "state": "REQUEST_REVIEW", "body": "", "user": {"login": "m"}, "html_url": "r2"},
        {"id": 3, "state": "COMMENT", "body": "x", "user": {"login": "m"}, "html_url": "r3",
         "comments_count": 0},
    ]
    rc = [{"id": 9, "path": "a", "position": 0, "original_position": 7, "body": "nit",
           "user": {"login": "m"}, "html_url": "c"}]
    http = FakeHttp([
        ("GET", "/repos/O/r/pulls/6/reviews/1/comments", rc),
        ("GET", "/repos/O/r/pulls/6/reviews", reviews),
        ("GET", "/repos/O/r/pulls/6", pull),
        ("PATCH", "/repos/O/r/pulls/6", {}),
        ("GET", "/repos/keel-bridge/r", {}),
        ("PATCH", "/repos/keel-bridge/r", {}),
        ("POST", "/repos/O/r/pulls", fj_issue(6, pr=True)),
        ("DELETE", "/repos/keel-bridge/r/branches/gh-pr%2F3", None),
    ])
    fj = Forgejo(http, "O", "keel-bridge", "https://git.pop.coop")
    p = fj.get_pull("r", 6)
    assert p.merged and p.merge_sha == "m"
    fj.close_pull("r", 6)
    revs = fj.list_reviews("r", 6)
    assert [r.id for r in revs] == ["1", "3"]
    assert revs[0].comments[0].line is None and revs[0].comments[0].path == "a"
    assert fj.ensure_fork("r") == "https://git.pop.coop/keel-bridge/r.git"
    assert not [c for c in http.calls if c[0] == "PATCH" and c[1] == "/repos/keel-bridge/r"]
    assert fj.create_pull("r", "gh-pr/3", "main", "t", "b").is_pr
    assert ("POST", "/repos/O/r/pulls",
            {"head": "keel-bridge:gh-pr/3", "base": "main", "title": "t", "body": "b"}) in http.calls
    fj.delete_fork_branch("r", "gh-pr/3")


def test_forgejo_fork_is_created_when_missing():
    missing = HttpError(404, "GET", "u", "")
    http = FakeHttp([
        ("GET", "/repos/keel-bridge/r", [missing, missing]),
        ("GET", "/repos/keel-bridge/r", {}),
        ("POST", "/repos/O/r/forks", {}),
        ("PATCH", "/repos/keel-bridge/r", {}),
    ])
    fj = Forgejo(http, "O", "keel-bridge", "https://git.pop.coop")
    slept = []
    fj.sleep = slept.append
    fj.ensure_fork("r")
    assert ("POST", "/repos/O/r/forks", {}) in http.calls
    assert slept == [3]
    patch = [c for c in http.calls if c[0] == "PATCH" and c[1] == "/repos/keel-bridge/r"][0]
    assert patch[2]["has_actions"] is False


def test_forgejo_fork_check_raises_other_errors():
    http = FakeHttp([("GET", "/repos/keel-bridge/r", HttpError(500, "GET", "u", ""))])
    with pytest.raises(HttpError):
        Forgejo(http, "O", "keel-bridge", "https://g").ensure_fork("r")


def test_forgejo_delete_branch_ignores_404_only():
    http = FakeHttp([("DELETE", "/repos/keel-bridge/r/branches/", HttpError(404, "D", "u", ""))])
    fj = Forgejo(http, "O", "keel-bridge", "https://g")
    fj.delete_fork_branch("r", "gh-pr/1")
    http.routes = [("DELETE", "/repos/keel-bridge/r/branches/", HttpError(403, "D", "u", ""))]
    with pytest.raises(HttpError):
        fj.delete_fork_branch("r", "gh-pr/1")
