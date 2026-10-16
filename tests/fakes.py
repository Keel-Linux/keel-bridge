"""In-memory servers with the interface of keelbridge.forges."""

from datetime import timedelta

from keelbridge.forges import Comment, Item, Pull, Review
from keelbridge.text import Side, find_marker


class Clock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def tick(self, seconds=60):
        self.t += timedelta(seconds=seconds)
        return self.t


class FakeForge:
    def __init__(self, key, label, web, bot, clock, labels=()):
        self.side = Side(key, label, web, "Keel-Linux")
        self.bot = bot
        self.clock = clock
        self.items = {}  # repo -> number -> dict
        self.comments = {}  # repo -> id -> dict
        self.pulls = {}  # repo -> number -> dict
        self.reviews = {}  # repo -> number -> [Review]
        self.labels = set(labels)
        self.next_number = 1
        self.next_comment = 100
        self.calls = []
        self.forks = set()
        self.deleted_branches = []
        self.fail_on = None

    # -- test helpers --------------------------------------------------------

    def add(self, repo, title, body="", login="alice", name="Alice", state="open",
            labels=(), is_pr=False, created=None, head="sha1", base="main"):
        n = self.next_number
        self.next_number += 1
        t = created or self.clock()
        self.items.setdefault(repo, {})[n] = dict(
            number=n, title=title, body=body, state=state, labels=set(labels), login=login,
            name=name, is_pr=is_pr, created=t, updated=self.clock())
        if is_pr:
            self.pulls.setdefault(repo, {})[n] = dict(state=state, merged=False, head=head,
                                                      base=base, merge_sha="")
        return n

    def touch(self, repo, number, **fields):
        it = self.items[repo][number]
        it.update(fields)
        it["updated"] = self.clock()

    def say(self, repo, number, body, login="bob", name="Bob"):
        cid = str(self.next_comment)
        self.next_comment += 1
        self.comments.setdefault(repo, {})[cid] = dict(
            id=cid, number=number, body=body, login=login, name=name, updated=self.clock())
        self.touch(repo, number)
        return cid

    def comments_on(self, repo, number):
        return [c for c in self.comments.get(repo, {}).values() if c["number"] == number]

    # -- the interface ---------------------------------------------------------

    def _check(self, op):
        self.calls.append(op)
        if self.fail_on == op:
            raise RuntimeError(f"injected failure in {op}")

    def _item(self, repo, d):
        return Item(d["number"], d["title"], d["body"], d["state"], frozenset(d["labels"]),
                    d["login"], d["name"], f"{self.side.web}/Keel-Linux/{repo}/issues/{d['number']}",
                    d["is_pr"], d["created"], d["updated"])

    def _comment(self, repo, c):
        return Comment(c["id"], c["number"], c["body"], c["login"], c["name"],
                       f"{self.side.web}/c/{c['id']}", c["updated"])

    def repo_labels(self, repo):
        return {lb: lb for lb in self.labels}

    def list_updated(self, repo, since):
        self._check("list_updated")
        return sorted((self._item(repo, d) for d in self.items.get(repo, {}).values()
                       if d["updated"] >= since), key=lambda i: i.updated)

    def list_comments_since(self, repo, since):
        return [self._comment(repo, c) for c in self.comments.get(repo, {}).values()
                if c["updated"] >= since]

    def get_item(self, repo, number):
        return self._item(repo, self.items[repo][number])

    def create_issue(self, repo, title, body, labels):
        self._check("create_issue")
        n = self.add(repo, title, body, login=self.bot, name="",
                     labels=[lb for lb in labels if lb in self.labels])
        return self.get_item(repo, n)

    def update_item(self, repo, number, title=None, body=None, state=None):
        fields = {k: v for k, v in dict(title=title, body=body, state=state).items() if v is not None}
        self.touch(repo, number, **fields)
        if state and number in self.pulls.get(repo, {}):
            self.pulls[repo][number]["state"] = state

    def set_labels(self, repo, number, labels):
        self.touch(repo, number, labels={lb for lb in labels if lb in self.labels})

    def item_comments(self, repo, number):
        return [self._comment(repo, c) for c in self.comments_on(repo, number)]

    def create_comment(self, repo, number, body):
        self._check("create_comment")
        cid = self.say(repo, number, body, login=self.bot, name="")
        return self._comment(repo, self.comments[repo][cid])

    def edit_comment(self, repo, ident, body):
        c = self.comments[repo][ident]
        c["body"] = body
        c["updated"] = self.clock()

    def find_marked(self, repo, origin, ident):
        for d in self.items.get(repo, {}).values():
            if d["login"] == self.bot and find_marker(d["body"]) == (origin, repo, str(ident)):
                return d["number"]
        return None

    def find_marked_comment(self, repo, number, origin, ident):
        for c in self.comments_on(repo, number):
            if c["login"] == self.bot and find_marker(c["body"]) == (origin, repo, str(ident)):
                return c["id"]
        return None

    def get_pull(self, repo, number):
        p = self.pulls[repo][number]
        return Pull(number, p["state"], p["merged"], p["head"], p["base"], p["merge_sha"],
                    f"{self.side.web}/Keel-Linux/{repo}/pulls/{number}")

    def close_pull(self, repo, number):
        self.pulls[repo][number]["state"] = "closed"
        self.touch(repo, number, state="closed")

    def list_reviews(self, repo, number):
        return list(self.reviews.get(repo, {}).get(number, []))

    def add_review(self, repo, number, rid, state, body, login="carol", comments=()):
        rev = Review(rid, state, body, login, "", f"{self.side.web}/r/{rid}", list(comments))
        self.reviews.setdefault(repo, {}).setdefault(number, []).append(rev)

    def fetch_url(self, repo):
        return f"{self.side.web}/Keel-Linux/{repo}.git"

    def ensure_fork(self, repo):
        self.forks.add(repo)
        return f"{self.side.web}/{self.bot}/{repo}.git"

    def create_pull(self, repo, branch, base, title, body):
        self._check("create_pull")
        n = self.add(repo, title, body, login=self.bot, name="", is_pr=True, head=branch,
                     base=base)
        return self.get_item(repo, n)

    def delete_fork_branch(self, repo, branch):
        self.deleted_branches.append((repo, branch))


class FakeGit:
    def __init__(self, gh):
        self.gh = gh
        self.pushes = []

    def push_pull(self, repo, fetch_url, number, push_url):
        self.pushes.append((repo, number, self.gh.pulls[repo][number]["head"], push_url))
        return f"gh-pr/{number}"
