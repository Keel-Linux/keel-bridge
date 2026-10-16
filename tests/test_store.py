"""Tests of keelbridge.store: the ID map in SQLite."""

import pytest

from keelbridge.store import Store


@pytest.fixture
def st(tmp_path):
    s = Store(str(tmp_path / "state.db"))
    yield s
    s.close()


def test_link_add_and_find_on_both_sides(st):
    link = st.add_link("r", "issue", "gh", gh=12, fj=40, state="open",
                       labels={"bug"}, src_hash="h", head=None)
    assert st.link_by("r", "gh", 12).fj == 40
    assert st.link_by("r", "fj", 40).gh == 12
    assert st.link_by("r", "gh", 13) is None
    assert link.labels == {"bug"}
    assert link.origin == "gh" and link.kind == "issue" and not link.done


def test_link_update(st):
    link = st.add_link("r", "pr", "gh", gh=1, fj=2, state="open", labels=set(),
                       src_hash="a", head="s1")
    st.update_link(link, state="closed", labels={"x"}, head="s2", done=1,
                   merge_seen="2026-10-16T00:00:00+00:00", src_hash="b")
    again = st.link_by("r", "gh", 1)
    assert again.state == "closed" and again.labels == {"x"} and again.head == "s2"
    assert again.done and again.merge_seen.startswith("2026") and again.src_hash == "b"


def test_update_link_rejects_unknown_fields(st):
    link = st.add_link("r", "issue", "gh", gh=1, fj=2, state="open", labels=set(),
                       src_hash="", head=None)
    with pytest.raises(ValueError):
        st.update_link(link, number=3)


def test_open_pull_links(st):
    st.add_link("r", "pr", "gh", gh=1, fj=2, state="open", labels=set(), src_hash="", head="a")
    done = st.add_link("r", "pr", "gh", gh=3, fj=4, state="open", labels=set(),
                       src_hash="", head="b")
    st.update_link(done, done=1)
    st.add_link("r", "issue", "gh", gh=5, fj=6, state="open", labels=set(), src_hash="", head=None)
    assert [link.gh for link in st.open_pull_links("r")] == [1]


def test_comments_map(st):
    st.add_comment("r", "gh", "c1", "d1", "h1")
    assert st.comment("r", "gh", "c1") == ("d1", "h1")
    assert st.comment("r", "fj", "c1") is None
    st.update_comment("r", "gh", "c1", "h2")
    assert st.comment("r", "gh", "c1") == ("d1", "h2")
    assert st.comment_by_copy("r", "gh", "d1") == "c1"
    assert st.comment_by_copy("r", "gh", "zz") is None


def test_cursors(st):
    assert st.cursor("r", "gh") is None
    st.set_cursor("r", "gh", "2026-10-16T00:00:00+00:00")
    assert st.cursor("r", "gh") == "2026-10-16T00:00:00+00:00"
    st.set_cursor("r", "gh", "2026-10-17T00:00:00+00:00")
    assert st.cursor("r", "gh") == "2026-10-17T00:00:00+00:00"


def test_pending(st):
    st.add_pending("r", "issue:gh:3")
    st.add_pending("r", "issue:gh:3")
    assert st.is_pending("r", "issue:gh:3")
    st.clear_pending("r", "issue:gh:3")
    assert not st.is_pending("r", "issue:gh:3")


def test_meta_and_http_cache(st):
    assert st.meta("start") is None
    st.set_meta("start", "x")
    assert st.meta("start") == "x"
    assert st.http_get("u") is None
    st.http_put("u", "etag1", "[1]")
    assert st.http_get("u") == ("etag1", "[1]")
    st.http_put("u", "etag2", "[2]")
    assert st.http_get("u") == ("etag2", "[2]")


def test_persists_across_opens(tmp_path):
    path = str(tmp_path / "s.db")
    s = Store(path)
    s.add_link("r", "issue", "fj", gh=1, fj=2, state="open", labels=set(), src_hash="", head=None)
    s.close()
    s2 = Store(path)
    assert s2.link_by("r", "fj", 2).gh == 1
    s2.close()
