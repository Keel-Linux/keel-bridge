"""The state of the bridge in SQLite: the ID map and the poll cursors.

The issue numbers of the two servers are different, so the map is the
only link between an original and its copy (with the marker in the body
of the copy as the second record). Each write commits at once: after a
crash, the bridge restarts from the last committed state.
"""

import json
import sqlite3
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY,
  repo TEXT NOT NULL, kind TEXT NOT NULL, origin TEXT NOT NULL,
  gh INTEGER NOT NULL, fj INTEGER NOT NULL,
  state TEXT NOT NULL, labels TEXT NOT NULL, src_hash TEXT NOT NULL,
  head TEXT, merge_seen TEXT, done INTEGER NOT NULL DEFAULT 0,
  UNIQUE (repo, gh), UNIQUE (repo, fj));
CREATE TABLE IF NOT EXISTS comments (
  repo TEXT NOT NULL, origin TEXT NOT NULL, src TEXT NOT NULL,
  copy TEXT NOT NULL, src_hash TEXT NOT NULL,
  PRIMARY KEY (repo, origin, src));
CREATE TABLE IF NOT EXISTS cursors (
  repo TEXT NOT NULL, side TEXT NOT NULL, since TEXT NOT NULL,
  PRIMARY KEY (repo, side));
CREATE TABLE IF NOT EXISTS pending (
  repo TEXT NOT NULL, key TEXT NOT NULL, PRIMARY KEY (repo, key));
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS http_cache (
  url TEXT PRIMARY KEY, etag TEXT NOT NULL, body TEXT NOT NULL);
"""

_LINK_BY = {
    "gh": "SELECT * FROM links WHERE repo = ? AND gh = ?",
    "fj": "SELECT * FROM links WHERE repo = ? AND fj = ?",
}
# One fixed statement per field: no SQL is built from values.
_LINK_UPDATE = {
    "state": "UPDATE links SET state = ? WHERE id = ?",
    "labels": "UPDATE links SET labels = ? WHERE id = ?",
    "src_hash": "UPDATE links SET src_hash = ? WHERE id = ?",
    "head": "UPDATE links SET head = ? WHERE id = ?",
    "merge_seen": "UPDATE links SET merge_seen = ? WHERE id = ?",
    "done": "UPDATE links SET done = ? WHERE id = ?",
}


@dataclass
class Link:
    id: int
    repo: str
    kind: str
    origin: str
    gh: int
    fj: int
    state: str
    labels: set
    src_hash: str
    head: str
    merge_seen: str
    done: int


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    def _link(self, row):
        if row is None:
            return None
        row = list(row)
        row[7] = set(json.loads(row[7]))
        return Link(*row)

    def add_link(self, repo, kind, origin, gh, fj, state, labels, src_hash, head):
        cur = self.db.execute(
            "INSERT INTO links (repo, kind, origin, gh, fj, state, labels, src_hash, head)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repo, kind, origin, gh, fj, state, json.dumps(sorted(labels)), src_hash, head))
        self.db.commit()
        return self.link_by_id(cur.lastrowid)

    def link_by_id(self, ident):
        return self._link(self.db.execute(
            "SELECT * FROM links WHERE id = ?", (ident,)).fetchone())

    def link_by(self, repo, side, number):
        query = _LINK_BY["gh" if side == "gh" else "fj"]
        return self._link(self.db.execute(query, (repo, number)).fetchone())

    def update_link(self, link, **fields):
        bad = set(fields) - set(_LINK_UPDATE)
        if bad:
            raise ValueError(f"unknown link fields: {sorted(bad)}")
        for key, value in fields.items():
            setattr(link, key, value)
            if key == "labels":
                value = json.dumps(sorted(value))
            self.db.execute(_LINK_UPDATE[key], (value, link.id))
        self.db.commit()

    def open_pull_links(self, repo):
        rows = self.db.execute(
            "SELECT * FROM links WHERE repo = ? AND kind = 'pr' AND done = 0 ORDER BY gh",
            (repo,)).fetchall()
        return [self._link(r) for r in rows]

    def add_comment(self, repo, origin, src, copy, src_hash):
        self.db.execute("INSERT INTO comments VALUES (?, ?, ?, ?, ?)",
                        (repo, origin, str(src), str(copy), src_hash))
        self.db.commit()

    def comment(self, repo, origin, src):
        row = self.db.execute(
            "SELECT copy, src_hash FROM comments WHERE repo = ? AND origin = ? AND src = ?",
            (repo, origin, str(src))).fetchone()
        return tuple(row) if row else None

    def comment_by_copy(self, repo, origin, copy):
        row = self.db.execute(
            "SELECT src FROM comments WHERE repo = ? AND origin = ? AND copy = ?",
            (repo, origin, str(copy))).fetchone()
        return row[0] if row else None

    def update_comment(self, repo, origin, src, src_hash):
        self.db.execute(
            "UPDATE comments SET src_hash = ? WHERE repo = ? AND origin = ? AND src = ?",
            (src_hash, repo, origin, str(src)))
        self.db.commit()

    def cursor(self, repo, side):
        row = self.db.execute("SELECT since FROM cursors WHERE repo = ? AND side = ?",
                              (repo, side)).fetchone()
        return row[0] if row else None

    def set_cursor(self, repo, side, since):
        self.db.execute("INSERT OR REPLACE INTO cursors VALUES (?, ?, ?)", (repo, side, since))
        self.db.commit()

    def add_pending(self, repo, key):
        self.db.execute("INSERT OR IGNORE INTO pending VALUES (?, ?)", (repo, key))
        self.db.commit()

    def is_pending(self, repo, key):
        return self.db.execute("SELECT 1 FROM pending WHERE repo = ? AND key = ?",
                               (repo, key)).fetchone() is not None

    def clear_pending(self, repo, key):
        self.db.execute("DELETE FROM pending WHERE repo = ? AND key = ?", (repo, key))
        self.db.commit()

    def meta(self, key):
        row = self.db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.db.commit()

    def del_meta(self, key):
        self.db.execute("DELETE FROM meta WHERE k = ?", (key,))
        self.db.commit()

    def http_get(self, url):
        row = self.db.execute("SELECT etag, body FROM http_cache WHERE url = ?",
                              (url,)).fetchone()
        return tuple(row) if row else None

    def http_put(self, url, etag, body):
        self.db.execute("INSERT OR REPLACE INTO http_cache VALUES (?, ?, ?)", (url, etag, body))
        self.db.commit()
