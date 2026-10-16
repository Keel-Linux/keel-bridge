"""Tests of keelbridge.engine against in-memory servers."""

from datetime import datetime, timedelta, timezone

import pytest

from fakes import Clock, FakeForge, FakeGit
from keelbridge import text
from keelbridge.engine import Bridge
from keelbridge.store import Store

R = "keel-keys"
T0 = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    clock = Clock(T0)
    gh = FakeForge("gh", "GitHub", "https://github.com", "kb[bot]", clock,
                   labels={"bug", "docs", "gh-only"})
    fj = FakeForge("fj", "git.pop.coop", "https://git.pop.coop", "keel-bridge", clock,
                   labels={"bug", "docs", "fj-only"})
    store = Store(str(tmp_path / "s.db"))
    git = FakeGit(gh)
    bridge = Bridge(gh, fj, store, git, [R], now=clock)
    bridge.run_once()  # sets the start time
    clock.tick()

    class Env:
        pass
    e = Env()
    e.clock, e.gh, e.fj, e.store, e.git, e.bridge = clock, gh, fj, store, git, bridge

    def cycle():
        clock.tick()
        errors = bridge.run_once()
        clock.tick()
        return errors
    e.cycle = cycle
    yield e
    store.close()


def only_copy(forge, repo=R):
    copies = [d for d in forge.items.get(repo, {}).values() if d["login"] == forge.bot]
    assert len(copies) == 1, copies
    return copies[0]


def test_github_issue_is_copied_once_with_credit_and_backlink(env):
    n = env.gh.add(R, "Broken key", "see #3 @dave", labels={"bug", "gh-only"})
    env.cycle()
    env.cycle()
    copy = only_copy(env.fj)
    assert copy["title"] == "Broken key"
    assert "[Alice (alice)](https://github.com/alice)" in copy["body"]
    assert text.find_marker(copy["body"]) == ("gh", R, str(n))
    assert copy["labels"] == {"bug"}
    xref = [c for c in env.gh.comments_on(R, n) if c["login"] == "kb[bot]"]
    assert len(xref) == 1
    assert f"https://git.pop.coop/Keel-Linux/{R}/issues/{copy['number']}" in xref[0]["body"]
    link = env.store.link_by(R, "gh", n)
    assert link.fj == copy["number"] and link.origin == "gh"


def test_forgejo_issue_is_copied_to_github(env):
    n = env.fj.add(R, "Native", "body", login="marcos", name="Marcos")
    env.cycle()
    copy = only_copy(env.gh)
    assert "Mirrored from git.pop.coop" in copy["body"]
    assert env.store.link_by(R, "fj", n).gh == copy["number"]


def test_forgejo_pull_requests_stay_on_forgejo(env):
    env.fj.add(R, "native PR", is_pr=True, login="marcos")
    env.cycle()
    assert not env.gh.items.get(R)


def test_items_from_before_the_start_are_left_alone(env):
    env.gh.add(R, "old", created=T0 - timedelta(days=3))
    env.cycle()
    assert not env.fj.items.get(R)


def test_comments_sync_both_ways_without_loops(env):
    n = env.gh.add(R, "Issue")
    env.cycle()
    fjn = env.store.link_by(R, "gh", n).fj
    env.gh.say(R, n, "from github @eve", login="bob", name="Bob")
    env.fj.say(R, fjn, "from forgejo", login="marcos", name="Marcos")
    env.cycle()
    env.cycle()
    env.cycle()
    fj_copies = [c for c in env.fj.comments_on(R, fjn) if c["login"] == "keel-bridge"]
    gh_copies = [c for c in env.gh.comments_on(R, n) if c["login"] == "kb[bot]"]
    assert len(fj_copies) == 1 and "from github" in fj_copies[0]["body"]
    assert "@eve" not in fj_copies[0]["body"]
    # one xref note plus one comment copy on GitHub
    assert len(gh_copies) == 2
    assert any("[Marcos (marcos)](https://git.pop.coop/marcos) commented" in c["body"]
               for c in gh_copies)


def test_existing_comments_of_an_original_are_copied_with_it(env):
    n = env.gh.add(R, "Issue", created=T0 + timedelta(seconds=5))
    env.gh.say(R, n, "early comment")
    env.cycle()
    fjn = env.store.link_by(R, "gh", n).fj
    assert [c for c in env.fj.comments_on(R, fjn) if "early comment" in c["body"]]


def test_comment_edit_updates_the_copy(env):
    n = env.gh.add(R, "Issue")
    env.cycle()
    cid = env.gh.say(R, n, "first text")
    env.cycle()
    env.gh.comments[R][cid]["body"] = "second text"
    env.gh.comments[R][cid]["updated"] = env.clock()
    env.cycle()
    fjn = env.store.link_by(R, "gh", n).fj
    bodies = [c["body"] for c in env.fj.comments_on(R, fjn)]
    assert any("second text" in b for b in bodies)
    assert not any("first text" in b for b in bodies)


def test_comment_on_an_unlinked_item_is_ignored(env):
    n = env.gh.add(R, "old", created=T0 - timedelta(days=3))
    env.gh.say(R, n, "hello")
    env.cycle()
    assert not env.fj.items.get(R)


def test_edit_of_the_original_updates_the_copy(env):
    n = env.gh.add(R, "Title", "v1")
    env.cycle()
    env.gh.touch(R, n, title="Title 2", body="v2")
    env.cycle()
    copy = only_copy(env.fj)
    assert copy["title"] == "Title 2" and "v2" in copy["body"]


def test_close_on_github_closes_forgejo_and_back(env):
    n = env.gh.add(R, "Issue")
    env.cycle()
    fjn = env.store.link_by(R, "gh", n).fj
    env.gh.touch(R, n, state="closed")
    env.cycle()
    assert env.fj.items[R][fjn]["state"] == "closed"
    env.fj.touch(R, fjn, state="open")
    env.cycle()
    assert env.gh.items[R][n]["state"] == "open"
    assert env.store.link_by(R, "gh", n).state == "open"


def test_forgejo_wins_when_both_sides_change(env):
    n = env.gh.add(R, "Issue")
    env.cycle()
    link = env.store.link_by(R, "gh", n)
    env.store.update_link(link, state="closed")  # both now differ from the last state
    env.gh.touch(R, n, state="open")
    env.fj.touch(R, link.fj, state="open")
    env.cycle()
    assert env.store.link_by(R, "gh", n).state == "open"
    env.store.update_link(env.store.link_by(R, "gh", n), state="x")
    env.gh.touch(R, n, state="closed")
    env.fj.touch(R, link.fj, state="open")
    env.cycle()
    assert env.gh.items[R][n]["state"] == "open"


def test_labels_sync_three_way(env):
    n = env.gh.add(R, "Issue", labels={"bug"})
    env.cycle()
    fjn = env.store.link_by(R, "gh", n).fj
    env.fj.touch(R, fjn, labels={"bug", "docs", "fj-only"})
    env.cycle()
    assert env.gh.items[R][n]["labels"] == {"bug", "docs"}
    env.gh.touch(R, n, labels={"docs", "gh-only"})
    env.cycle()
    assert env.fj.items[R][fjn]["labels"] == {"docs", "fj-only"}
    assert env.store.link_by(R, "gh", n).labels == {"docs"}


def test_pending_copy_is_found_by_its_marker_after_a_crash(env):
    n = env.gh.add(R, "Issue")
    env.fj.fail_on = "create_comment"  # never reached: the xref goes to GitHub
    env.gh.fail_on = "create_comment"  # fails after the copy and the map row
    assert env.cycle() == 0  # one item fails, the pass goes on
    assert env.store.meta(f"fail:{R}:gh:item{n}").startswith("1|")
    env.gh.fail_on = None
    env.cycle()
    env.cycle()
    only_copy(env.fj)
    assert env.store.link_by(R, "gh", n) is not None
    assert not env.store.is_pending(R, f"issue:gh:{n}")
    assert env.store.meta(f"fail:{R}:gh:item{n}") is None


def test_crash_between_the_copy_and_the_map_row(env):
    n = env.gh.add(R, "Issue")
    real = env.store.add_link

    def crash(*a, **k):
        raise RuntimeError("power cut")
    env.store.add_link = crash
    env.cycle()
    assert env.store.is_pending(R, f"issue:gh:{n}")
    env.store.add_link = real
    env.cycle()
    only_copy(env.fj)
    assert env.store.link_by(R, "gh", n) is not None


def test_a_poison_item_is_dropped_after_three_tries(env):
    env.gh.add(R, "Issue")
    env.fj.fail_on = "create_issue"
    for _ in range(4):
        env.cycle()
    assert env.fj.calls.count("create_issue") == 3
    env.gh.add(R, "Next")
    env.fj.fail_on = None
    env.cycle()
    assert [d["title"] for d in env.fj.items[R].values()] == ["Next"]
    first = min(env.gh.items[R])
    env.gh.touch(R, first, body="edited")  # a change gives it new tries
    env.cycle()
    assert len(env.fj.items[R]) == 2


def test_rate_limit_stops_the_pass(env):
    from keelbridge.http import RateLimited
    env.bridge.repos = ["a", "b"]
    seen = []

    def limited(repo, since):
        seen.append(repo)
        raise RateLimited(403, "GET", "u", "")
    env.gh.list_updated = limited
    assert env.cycle() == 1
    assert seen == ["a"]


def test_rate_limit_inside_an_item_stops_the_pass(env):
    from keelbridge.http import RateLimited
    env.gh.add(R, "Issue")

    def limited(*a):
        raise RateLimited(429, "POST", "u", "")
    env.fj.create_issue = limited
    assert env.cycle() == 1


def test_a_failing_pull_request_does_not_stop_the_pass(env):
    n, link = bridged_pr(env)
    env.fj.get_pull = lambda repo, number: 1 / 0
    assert env.cycle() == 0


def test_closed_pull_requests_are_not_bridged(env):
    env.gh.add(R, "closed PR", is_pr=True, state="closed")
    env.cycle()
    assert not env.fj.items.get(R)
    assert env.git.pushes == []


def test_a_github_user_with_the_forgejo_bot_name_is_not_skipped(env):
    env.gh.add(R, "Issue", login="keel-bridge")
    env.cycle()
    only_copy(env.fj)


def test_pending_comment_is_found_by_its_marker(env):
    n = env.gh.add(R, "Issue")
    env.cycle()
    fjn = env.store.link_by(R, "gh", n).fj
    cid = env.gh.say(R, n, "hello")
    env.store.add_pending(R, f"c:gh:c{cid}")
    env.fj.say(R, fjn, text.comment_copy(env.gh.side, R, f"c{cid}", "u", "bob", "", "hello"),
               login="keel-bridge")
    env.cycle()
    assert len([c for c in env.fj.comments_on(R, fjn) if "hello" in c["body"]]) == 1


def test_creation_cap_defers_and_the_cursor_waits(env):
    env.bridge.max_creates = 1
    env.gh.add(R, "one")
    env.gh.add(R, "two")
    env.cycle()
    assert len(env.fj.items[R]) == 1
    env.cycle()
    assert len(env.fj.items[R]) == 2


def test_one_failing_repository_does_not_stop_the_pass(env):
    env.bridge.repos = ["broken", R]
    env.gh.add(R, "Issue")
    env.gh.fail_on = None

    def boom(repo, since, _orig=env.gh.list_updated):
        if repo == "broken":
            raise RuntimeError("down")
        return _orig(repo, since)
    env.gh.list_updated = boom
    assert env.cycle() == 1
    only_copy(env.fj)


# -- pull requests -----------------------------------------------------------

def bridged_pr(env, head="aaa"):
    n = env.gh.add(R, "Fix", "Fixes #31", is_pr=True, login="contrib", head=head,
                   labels={"docs"})
    env.cycle()
    link = env.store.link_by(R, "gh", n)
    return n, link


def test_pull_request_is_bridged_from_the_exact_head(env):
    n, link = bridged_pr(env)
    assert env.git.pushes == [(R, n, "aaa", f"https://git.pop.coop/keel-bridge/{R}.git")]
    copy = env.fj.items[R][link.fj]
    assert copy["is_pr"] and "pull request" in copy["body"]
    assert "(https://github.com/Keel-Linux/keel-keys/issues/31)" in copy["body"]
    assert "#31" not in copy["body"]
    assert copy["labels"] == {"docs"}
    assert link.kind == "pr" and link.head == "aaa" and R in env.fj.forks


def test_new_commits_are_pushed_again(env):
    n, link = bridged_pr(env)
    env.gh.pulls[R][n]["head"] = "bbb"
    env.cycle()
    assert env.git.pushes[-1][2] == "bbb"
    assert env.store.link_by(R, "gh", n).head == "bbb"
    env.cycle()
    assert len(env.git.pushes) == 2


def test_merge_on_forgejo_that_github_detects(env):
    n, link = bridged_pr(env)
    env.fj.pulls[R][link.fj].update(merged=True, state="closed", merge_sha="m1")
    env.gh.pulls[R][n].update(merged=True, state="closed")
    env.cycle()
    link = env.store.link_by(R, "gh", n)
    assert link.done and (R, f"gh-pr/{n}") in env.fj.deleted_branches
    assert not [c for c in env.gh.comments_on(R, n) if "merged on git.pop.coop" in c["body"]]


def test_merge_on_forgejo_that_github_misses_is_closed_after_the_grace(env):
    n, link = bridged_pr(env)
    env.fj.pulls[R][link.fj].update(merged=True, state="closed", merge_sha="m1")
    env.cycle()
    assert env.store.link_by(R, "gh", n).merge_seen
    assert env.gh.pulls[R][n]["state"] == "open"
    env.clock.tick(16 * 60)
    env.cycle()
    assert env.gh.pulls[R][n]["state"] == "closed"
    assert [c for c in env.gh.comments_on(R, n) if "m1" in c["body"]]
    assert env.store.link_by(R, "gh", n).done


def test_close_without_merge_on_forgejo_closes_github(env):
    n, link = bridged_pr(env)
    env.fj.close_pull(R, link.fj)
    env.cycle()
    assert env.gh.pulls[R][n]["state"] == "closed"
    assert env.store.link_by(R, "gh", n).done


def test_close_on_github_closes_forgejo(env):
    n, link = bridged_pr(env)
    env.gh.close_pull(R, n)
    env.cycle()
    assert env.fj.pulls[R][link.fj]["state"] == "closed"
    assert env.store.link_by(R, "gh", n).done


def test_both_closed_finishes(env):
    n, link = bridged_pr(env)
    env.store.update_link(link, head="aaa")
    env.gh.pulls[R][n]["state"] = "closed"
    env.fj.pulls[R][link.fj]["state"] = "closed"
    env.cycle()
    assert env.store.link_by(R, "gh", n).done


def test_reviews_sync_both_ways_once(env):
    from keelbridge.forges import ReviewComment
    n, link = bridged_pr(env)
    rc = ReviewComment("7", "README.md", 3, "typo", "carol", "", "u")
    env.gh.add_review(R, n, "r1", "APPROVED", "LGTM", comments=[rc])
    env.fj.add_review(R, link.fj, "r2", "REQUEST_CHANGES", "please fix", login="marcos")
    env.fj.add_review(R, link.fj, "r3", "COMMENT", text.marker("gh", R, "rvX"),
                      login="keel-bridge")
    env.cycle()
    env.cycle()
    fj_bodies = [c["body"] for c in env.fj.comments_on(R, link.fj)]
    gh_bodies = [c["body"] for c in env.gh.comments_on(R, n)]
    assert sum("approved this pull request on GitHub" in b for b in fj_bodies) == 1
    assert sum("`README.md` line 3" in b for b in fj_bodies) == 1
    assert sum("requested changes on git.pop.coop" in b for b in gh_bodies) == 1


# -- one-off commands ------------------------------------------------------------

def test_seed_links_imported_items_and_forgejo_wins(env):
    old = T0 - timedelta(days=5)
    g = env.gh.add(R, "Imported", created=old, state="closed", labels={"bug"})
    f = env.fj.add(R, "Imported", created=old, state="open", labels={"bug", "docs"})
    env.gh.add(R, "Different", created=old)
    env.fj.add(R, "Other title", created=old)
    gp = env.gh.add(R, "Old PR", created=old, is_pr=True)
    env.fj.add(R, "Old PR", created=old, is_pr=True)
    assert env.bridge.seed(R, T0) == 2
    assert env.bridge.seed(R, T0) == 0
    assert env.store.link_by(R, "gh", gp).done
    env.gh.touch(R, g)
    env.cycle()
    assert env.gh.items[R][g]["state"] == "open"
    assert env.gh.items[R][g]["labels"] == {"bug", "docs"}
    assert env.store.link_by(R, "gh", g).fj == f
    assert not [d for d in env.fj.items[R].values() if d["login"] == "keel-bridge"]


def test_backfill_copies_open_issues_on_both_sides(env):
    old = T0 - timedelta(days=30)
    env.gh.add(R, "archive open", created=old)
    env.gh.add(R, "archive closed", created=old, state="closed")
    env.fj.add(R, "native open", created=old, login="marcos")
    env.fj.add(R, "native PR", created=old, is_pr=True)
    env.bridge.pace = 0.5
    slept = []
    env.bridge.sleep = slept.append
    assert env.bridge.backfill(R) == 2
    assert env.bridge.backfill(R) == 0
    assert slept == [0.5, 0.5]
    assert [d["title"] for d in env.fj.items[R].values() if d["login"] == "keel-bridge"] == [
        "archive open"]
    assert [d["title"] for d in env.gh.items[R].values() if d["login"] == "kb[bot]"] == [
        "native open"]


def test_closed_original_gets_a_closed_copy(env):
    n = env.gh.add(R, "x", state="closed")
    env.bridge.copy_item(R, "gh", env.gh.get_item(R, n))
    assert only_copy(env.fj)["state"] == "closed"
