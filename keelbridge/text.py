"""The text of the copies that the bridge posts.

Each copy starts with a note that names the origin and credits the author
by name and link, because the bot posts it. Each copy ends with a hidden
marker. The marker holds the origin side, the repository and the id of
the original, so the bridge can find a copy again after a crash and never
copies a copy.

Two changes keep a copy from doing harm on the other side:
- An @mention gets a word joiner after the @, so it does not notify an
  unrelated user of the same name on the other server.
- A bare #N, GH-N or "Fixes #N" becomes a link to the origin side. The
  numbers of the two servers are different, and a bare reference would
  point to (or close) the wrong item.
"""

import hashlib
import re
import urllib.parse
from collections import namedtuple

Side = namedtuple("Side", "key label web org")

MARKER_RE = re.compile(r"<!-- keel-bridge:v1 o=(\w+) r=([\w.-]+) id=([\w-]+) -->")
WORD_JOINER = "⁠"
_CODE_RE = re.compile(r"(```.*?```|`[^`\n]*`)", re.S)
_MENTION_RE = re.compile(r"(?<![\w.@/`])@(?=[A-Za-z0-9][A-Za-z0-9-]*)")
_REF_RE = re.compile(r"(?<![\w/&#])(?:#|GH-)(\d+)\b")
_ANY_REF_RE = re.compile(r"(?<!&)([#!])(?=\d)")
MAX_BODY = 60000
MAX_TITLE = 255


def marker(origin, repo, ident):
    """The hidden marker of a copy: origin side, repository, original id."""
    return f"<!-- keel-bridge:v1 o={origin} r={repo} id={ident} -->"


def find_marker(body):
    """(origin, repo, id) of the last marker in body, or None.

    The bridge writes its marker last, so a marker that a user typed
    higher up in the text does not count.
    """
    found = MARKER_RE.findall(body or "")
    return tuple(found[-1]) if found else None


def has_marker(body):
    return find_marker(body) is not None


def _outside_code(body, func):
    parts = _CODE_RE.split(body)
    return "".join(p if i % 2 else func(p) for i, p in enumerate(parts))


def neutralize_mentions(body):
    return _outside_code(body, lambda s: _MENTION_RE.sub("@" + WORD_JOINER, s))


def defang_refs(text):
    """A word joiner after each # or ! before a digit: no server reads a reference."""
    return _ANY_REF_RE.sub(lambda m: m[1] + WORD_JOINER, text)


def link_refs(body, origin, repo):
    """Bare references become links to the origin; all others are defanged.

    A closing keyword ("Fixes #12") must not act on the other server, also
    not in code or in a heading, where a server can still read it.
    """
    def one(s):
        out = []
        for line in s.split("\n"):
            if not re.match(r"\s*#+\s", line):  # not a markdown heading
                line = _REF_RE.sub(
                    lambda m: f"[{repo}#{m[1]}]({origin.web}/{origin.org}/{repo}/issues/{m[1]})",
                    line)
            out.append(defang_refs(line))
        return "\n".join(out)
    parts = _CODE_RE.split(body)
    return "".join(defang_refs(p) if i % 2 else one(p) for i, p in enumerate(parts))


def clean(body, origin, repo):
    body = (body or "").replace("<!--", "&lt;!--")
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "\n\n(The bridge cut the text here. See the original.)"
    return link_refs(neutralize_mentions(body), origin, repo)


def safe_title(title):
    """The title of a copy: one line, no reference, at most 255 characters."""
    return defang_refs(" ".join((title or "").split()))[:MAX_TITLE] or "(no title)"


def _plain(value, limit):
    return re.sub(r"[^\w .'-]", "", value or "")[:limit].strip()


def person(side, login, name):
    shown_login = _plain(login, 60)
    shown_name = _plain(name, 60)
    shown = f"{shown_name} ({shown_login})" if shown_name and shown_name != shown_login else shown_login
    return f"[{shown}]({side.web}/{urllib.parse.quote(login or '')})"


def issue_copy(origin, repo, number, url, login, name, body, kind):
    what = "pull request" if kind == "pr" else "issue"
    head = (f"> Mirrored from {origin.label}: {url}\n"
            f"> Opened there by {person(origin, login, name)}. "
            f"The bridge bot posts this {what} for the author. "
            f"Comments sync both ways.")
    if kind == "pr":
        head += ("\n> The commits are the exact commits of the original. "
                 "Merge it here, with a merge commit.")
    return f"{head}\n\n{clean(body, origin, repo)}\n\n{marker(origin.key, repo, number)}"


def comment_copy(origin, repo, ident, url, login, name, body):
    return (f"{person(origin, login, name)} commented on {origin.label} ([link]({url})):\n\n"
            f"{clean(body, origin, repo)}\n\n{marker(origin.key, repo, ident)}")


_REVIEW_WORDS = {
    "APPROVED": "approved this pull request",
    "REQUEST_CHANGES": "requested changes",
    "CHANGES_REQUESTED": "requested changes",
}


def review_copy(origin, repo, ident, url, login, name, state, body):
    verb = _REVIEW_WORDS.get(state, "reviewed this pull request")
    text = f"{person(origin, login, name)} {verb} on {origin.label} ([link]({url}))."
    if body:
        text += "\n\n" + clean(body, origin, repo)
    return f"{text}\n\n{marker(origin.key, repo, ident)}"


def review_comment_copy(origin, repo, ident, url, login, name, path, line, body):
    path = re.sub(r"[`<>\s]", "", path or "")[:200]
    where = f"`{path}` line {line}" if line else f"`{path}`"
    return (f"{person(origin, login, name)} commented on {where} on {origin.label} "
            f"([link]({url})):\n\n{clean(body, origin, repo)}\n\n"
            f"{marker(origin.key, repo, ident)}")


def xref(other, url, repo, ident):
    """The note on an original that links to its copy."""
    return (f"The bridge mirrored this item to {other.label}: {url}\n"
            f"Comments, labels and the open or closed state sync both ways.\n\n"
            f"{marker('x', repo, ident)}")


def merged_elsewhere(url, sha, repo):
    return (f"This pull request was merged on git.pop.coop, the source of the "
            f"project: {url} (commit {sha}). The bridge closes it here.\n\n"
            f"{marker('x', repo, 'merged')}")


def merged_on_main(url, repo, ident):
    return (f"The commits of this pull request are on the main branch now, and "
            f"GitHub shows the original as merged: {url}\n"
            f"A maintainer merged them through a pull request of this repository. "
            f"The bridge closes this copy.\n\n{marker('x', repo, ident)}")


def closed_elsewhere(side, url, repo, ident):
    return (f"The copy of this item was closed on {side.label}: {url}\n\n"
            f"{marker('x', repo, ident)}")


def body_hash(title, body):
    return hashlib.sha256(f"{title or ''}\0{body or ''}".encode()).hexdigest()
