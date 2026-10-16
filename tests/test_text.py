"""Tests of keelbridge.text: the bodies of the copies and their markers."""

from keelbridge import text

GH = text.Side("gh", "GitHub", "https://github.com", "Keel-Linux")
FJ = text.Side("fj", "git.pop.coop", "https://git.pop.coop", "Keel-Linux")


def test_marker_round_trip():
    body = "hello\n\n" + text.marker("gh", "keel-keys", "12")
    assert text.find_marker(body) == ("gh", "keel-keys", "12")


def test_find_marker_returns_none_without_a_marker():
    assert text.find_marker("plain text") is None
    assert text.find_marker(None) is None


def test_has_marker():
    assert text.has_marker("x " + text.marker("fj", "r", "c3"))
    assert not text.has_marker("x")


def test_mentions_are_neutralized():
    out = text.neutralize_mentions("ping @alice and @bob-2, mail a@b.com")
    assert "@alice" not in out
    assert "@bob-2" not in out
    assert "a@b.com" in out
    assert "alice" in out


def test_mentions_in_code_are_kept():
    out = text.neutralize_mentions("see `@alice` and\n```\n@bob\n```\nthen @carol")
    assert "`@alice`" in out
    assert "\n@bob\n" in out
    assert "@carol" not in out


J = text.WORD_JOINER


def test_refs_become_links_to_the_origin():
    out = text.link_refs("Fixes #31, see #4 and GH-7", GH, "inithooks")
    assert f"Fixes [inithooks#{J}31](https://github.com/Keel-Linux/inithooks/issues/31)" in out
    assert f"[inithooks#{J}4](https://github.com/Keel-Linux/inithooks/issues/4)" in out
    assert f"[inithooks#{J}7](https://github.com/Keel-Linux/inithooks/issues/7)" in out
    assert "#3" not in out and "#4" not in out


def test_refs_in_code_headings_and_other_repos_are_defanged():
    src = "x/y#3 http://a/b#x `Fixes #9`\n# Fixes #1 heading\n```\nCloses #8 !5\n```"
    out = text.link_refs(src, FJ, "r")
    for raw in ("#3", "#9", "#1", "#8", "!5"):
        assert raw not in out
    assert f"x/y#{J}3" in out and "http://a/b#x" in out
    assert text.link_refs("it&#39;s", FJ, "r") == "it&#39;s"
    assert f"`Fixes #{J}9`" in out


def test_safe_title():
    assert text.safe_title("Fix #12\nnow") == f"Fix #{J}12 now"
    assert len(text.safe_title("x" * 300)) == 255
    assert text.safe_title("") == "(no title)"


def test_clean_escapes_markers_and_cuts_long_bodies():
    spoof = text.marker("gh", "r", "1")
    assert not text.has_marker(text.clean(spoof, GH, "r"))
    long = text.clean("a" * (text.MAX_BODY + 10), GH, "r")
    assert len(long) < text.MAX_BODY + 100 and "cut the text" in long


def test_last_marker_wins():
    body = text.marker("gh", "r", "1") + " x " + text.marker("fj", "r", "2")
    assert text.find_marker(body) == ("fj", "r", "2")


def test_person_removes_markdown_and_mentions_from_the_name():
    out = text.person(GH, "dependabot[bot]", "@victim](http://evil) <!--")
    assert "@" not in out and "evil)" not in out and "<!--" not in out
    assert out.endswith("(https://github.com/dependabot%5Bbot%5D)")


def test_clean_applies_both():
    out = text.clean("@a fixes #2", GH, "r")
    assert "@a" not in out and f"[r#{J}2]" in out


def test_clean_of_empty_body():
    assert text.clean(None, GH, "r") == ""


def test_person_link_with_and_without_a_name():
    assert text.person(GH, "alice", "Alice A") == "[Alice A (alice)](https://github.com/alice)"
    assert text.person(FJ, "bob", "") == "[bob](https://git.pop.coop/bob)"
    assert text.person(FJ, "bob", "bob") == "[bob](https://git.pop.coop/bob)"


def test_issue_copy_body_credits_the_author_and_links_back():
    body = text.issue_copy(
        GH, "keel-keys", 12, "https://github.com/Keel-Linux/keel-keys/issues/12",
        "alice", "Alice", "Broken @bob, see #3", kind="issue")
    assert "Mirrored from GitHub" in body
    assert "https://github.com/Keel-Linux/keel-keys/issues/12" in body
    assert "[Alice (alice)](https://github.com/alice)" in body
    assert "@bob" not in body
    assert f"[keel-keys#{J}3](https://github.com/Keel-Linux/keel-keys/issues/3)" in body
    assert text.find_marker(body) == ("gh", "keel-keys", "12")


def test_pull_copy_body_names_the_branch_rule():
    body = text.issue_copy(GH, "r", 5, "u", "a", "", "", kind="pr")
    assert "pull request" in body
    assert "Merge it here" in body
    assert text.find_marker(body) == ("gh", "r", "5")


def test_forgejo_issue_copy_on_github():
    body = text.issue_copy(FJ, "keel", 180, "https://git.pop.coop/Keel-Linux/keel/issues/180",
                           "marcos", "Marcos", "text", kind="issue")
    assert "Mirrored from git.pop.coop" in body
    assert text.find_marker(body) == ("fj", "keel", "180")


def test_comment_copy():
    body = text.comment_copy(GH, "r", "c99", "https://x/c99", "alice", "Alice", "ok @bob #1")
    assert "[Alice (alice)](https://github.com/alice) commented on GitHub" in body
    assert "@bob" not in body
    assert text.find_marker(body) == ("gh", "r", "c99")


def test_review_copy_states():
    approved = text.review_copy(GH, "r", "rv1", "u", "a", "", "APPROVED", "LGTM")
    assert "approved this pull request on GitHub" in approved
    changes = text.review_copy(FJ, "r", "rv2", "u", "a", "", "REQUEST_CHANGES", "")
    assert "requested changes on git.pop.coop" in changes
    other = text.review_copy(GH, "r", "rv3", "u", "a", "", "COMMENTED", "hm")
    assert "reviewed this pull request on GitHub" in other
    assert text.find_marker(other) == ("gh", "r", "rv3")


def test_review_comment_copy_names_file_and_line():
    body = text.review_comment_copy(GH, "r", "rc1", "u", "a", "", "src/x.py", 12, "nit")
    assert "`src/x.py` line 12" in body
    no_line = text.review_comment_copy(GH, "r", "rc2", "u", "a", "", "f", None, "x")
    assert "`f`" in no_line and "line" not in no_line.split("\n")[0].split("`f`")[1]
    evil = text.review_comment_copy(GH, "r", "rc3", "u", "a", "", "a`b <!--c", 1, "x")
    assert "`ab!--c`" in evil


def test_xref_note():
    note = text.xref(FJ, "https://git.pop.coop/Keel-Linux/r/issues/4", "r", "4")
    assert "https://git.pop.coop/Keel-Linux/r/issues/4" in note
    assert text.find_marker(note) == ("x", "r", "4")


def test_closing_notes():
    merged = text.merged_elsewhere("https://git.pop.coop/Keel-Linux/r/pulls/9", "abc123", "r")
    assert "merged on git.pop.coop" in merged and "abc123" in merged
    closed = text.closed_elsewhere(FJ, "https://x", "r", "9")
    assert "closed on git.pop.coop" in closed
    assert text.has_marker(merged) and text.has_marker(closed)


def test_body_hash_is_stable_and_sensitive():
    assert text.body_hash("t", "b") == text.body_hash("t", "b")
    assert text.body_hash("t", "b") != text.body_hash("t", "c")
    assert text.body_hash(None, None) == text.body_hash("", "")
