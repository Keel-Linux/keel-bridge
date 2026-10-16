"""The sync engine: one pass over all repositories.

Loop rules (handbook decision 0056, amendment of 2026-10-16):
1. The bridge skips every item and comment that a bridge bot wrote.
2. The bridge skips every item and comment with a bridge marker.
3. The ID map links an original and its copy. The bridge writes a
   "pending" row before it creates a copy. If a pending row is still
   there at the next pass, the bridge looks for the marker on the other
   side before it creates the copy again. Thus each operation is
   idempotent.
4. The open or closed state and the labels are compared with the last
   synced value. A side that changed wins. If both changed, Forgejo wins,
   because git.pop.coop is the source.
5. Edits of the title and the body flow from the original to the copy
   only.
6. Pull requests go from GitHub to Forgejo only, and merge on Forgejo
   only. Forgejo pull requests stay on Forgejo.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from keelbridge import text
from keelbridge.forges import fmt_time, parse_time
from keelbridge.http import RateLimited

log = logging.getLogger("keel-bridge")
MAX_TRIES = 3


def utcnow():
    return datetime.now(timezone.utc)


class Bridge:
    def __init__(self, gh, fj, store, git, repos, now=utcnow, max_creates=30,
                 merge_grace=timedelta(minutes=15), overlap=timedelta(minutes=1),
                 pace=0.0, sleep=time.sleep):
        self.gh = gh
        self.fj = fj
        self.store = store
        self.git = git
        self.repos = list(repos)
        self.now = now
        self.max_creates = max_creates
        self.merge_grace = merge_grace
        self.overlap = overlap
        self.pace = pace
        self.sleep = sleep
        self.creates = 0
        self.deferred = {}

    # -- helpers -----------------------------------------------------------

    def forge(self, side):
        return self.gh if side == "gh" else self.fj

    @staticmethod
    def other(side):
        return "fj" if side == "gh" else "gh"

    @staticmethod
    def num(link, side):
        return link.gh if side == "gh" else link.fj

    def common_labels(self, repo):
        return set(self.gh.repo_labels(repo)) & set(self.fj.repo_labels(repo))

    def item_url(self, side, repo, number):
        s = self.forge(side).side
        return f"{s.web}/{s.org}/{repo}/issues/{number}"

    def start_time(self):
        value = self.store.meta("start")
        if value is None:
            value = fmt_time(self.now())
            self.store.set_meta("start", value)
        return parse_time(value)

    # -- the pass ----------------------------------------------------------

    def run_once(self):
        start = self.start_time()
        self.creates = 0
        errors = 0
        for repo in self.repos:
            try:
                self.sync_repo(repo, start)
            except RateLimited:
                log.warning("rate limit reached at %s: the pass stops here", repo)
                return errors + 1
            except Exception:  # noqa: BLE001 one repository must not stop the others
                log.exception("sync of %s failed", repo)
                errors += 1
        return errors

    def _guarded(self, repo, side, key, updated, func, *args):
        """Run func for one item or comment. A failure does not stop the pass.

        The cursor waits for a failed item, so the next pass tries it again.
        After MAX_TRIES failures the bridge logs it and lets it go until the
        item changes again, so one bad item (for example a text that the
        other server refuses) does not block the repository.
        """
        fkey = f"fail:{repo}:{side}:{key}"
        stamp = fmt_time(updated)
        tries, _, seen = (self.store.meta(fkey) or "0|").partition("|")
        tries = int(tries) if seen == stamp else 0
        if tries >= MAX_TRIES:
            return  # given up until the item changes again
        try:
            func(*args)
        except RateLimited:
            raise
        except Exception:  # noqa: BLE001
            tries += 1
            self.store.set_meta(fkey, f"{tries}|{stamp}")
            log.exception("%s: %s %s failed (try %d of %d)", repo, side, key, tries, MAX_TRIES)
            if tries < MAX_TRIES:
                self.deferred[side] = min(self.deferred.get(side, updated), updated)
            return
        if self.store.meta(fkey) is not None:
            self.store.del_meta(fkey)

    def _since(self, repo, side, start):
        value = self.store.cursor(repo, side)
        return parse_time(value) if value else start - self.overlap

    def sync_repo(self, repo, start):
        self.deferred = {}
        since = {s: self._since(repo, s, start) for s in ("gh", "fj")}
        newest = dict(since)
        for side in ("gh", "fj"):
            for item in self.forge(side).list_updated(repo, since[side]):
                newest[side] = max(newest[side], item.updated)
                self._guarded(repo, side, f"item{item.number}", item.updated,
                              self.on_item, repo, side, item, start)
        for side in ("gh", "fj"):
            for comment in self.forge(side).list_comments_since(repo, since[side]):
                newest[side] = max(newest[side], comment.updated)
                self._guarded(repo, side, f"c{comment.id}", comment.updated,
                              self.on_comment, repo, side, comment)
        for link in self.store.open_pull_links(repo):
            try:
                self.sync_pull(repo, link)
                if not link.done:
                    self.sync_reviews(repo, link)
            except RateLimited:
                raise
            except Exception:  # noqa: BLE001 the next pass tries this pull request again
                log.exception("%s: pull request gh#%s failed", repo, link.gh)
        for side in ("gh", "fj"):
            cursor = max(since[side], newest[side] - self.overlap)
            if side in self.deferred:
                cursor = min(cursor, self.deferred[side] - timedelta(seconds=1))
            self.store.set_cursor(repo, side, fmt_time(cursor))

    # -- items ---------------------------------------------------------------

    def is_foreign(self, side, login, body):
        """True for what the bridge bot of this side wrote or what carries a marker."""
        return login == self.forge(side).bot or text.has_marker(body)

    def on_item(self, repo, side, item, start):
        link = self.store.link_by(repo, side, item.number)
        if link:
            self.reconcile(repo, link, **{side: item})
            return
        if self.is_foreign(side, item.login, item.body):
            return
        if item.created < start:
            return
        if side == "fj" and item.is_pr:
            return
        if item.is_pr and item.state != "open":
            return
        self.copy_item(repo, side, item)

    def copy_item(self, repo, side, item):
        if self.creates >= self.max_creates:
            old = self.deferred.get(side, item.updated)
            self.deferred[side] = min(old, item.updated)
            return None
        src, dst, to = self.forge(side), self.forge(self.other(side)), self.other(side)
        kind = "pr" if item.is_pr else "issue"
        key = f"{kind}:{side}:{item.number}"
        number = None
        if self.store.is_pending(repo, key):
            number = dst.find_marked(repo, side, item.number)
        else:
            self.store.add_pending(repo, key)
        head = None
        if kind == "pr":
            pull = self.gh.get_pull(repo, item.number)
            head = pull.head_sha
        if number is None:
            body = text.issue_copy(src.side, repo, item.number, item.url, item.login,
                                   item.name, item.body, kind)
            if kind == "pr":
                push_url = self.fj.ensure_fork(repo)
                branch = self.git.push_pull(repo, self.gh.fetch_url(repo), item.number, push_url)
                copy = self.fj.create_pull(repo, branch, pull.base_ref,
                                           text.safe_title(item.title), body)
                if item.labels:
                    self.fj.set_labels(repo, copy.number, item.labels)
            else:
                copy = dst.create_issue(repo, text.safe_title(item.title), body, item.labels)
            number = copy.number
            self.creates += 1
        common = self.common_labels(repo)
        nums = {side: item.number, to: number}
        link = self.store.add_link(repo, kind, side, gh=nums["gh"], fj=nums["fj"],
                                   state=item.state, labels=set(item.labels) & common,
                                   src_hash=text.body_hash(item.title, item.body), head=head)
        self.store.clear_pending(repo, key)
        log.info("%s: %s %s#%s copied to %s#%s", repo, kind, side, item.number, to, number)
        src.create_comment(repo, item.number,
                           text.xref(dst.side, self.item_url(to, repo, number), repo, number))
        for comment in src.item_comments(repo, item.number):
            self.on_comment(repo, side, comment)
        if item.state == "closed" and kind == "issue":
            dst.update_item(repo, number, state="closed")
        if self.pace:
            self.sleep(self.pace)
        return link

    def reconcile(self, repo, link, gh=None, fj=None):
        g = gh or self.gh.get_item(repo, link.gh)
        f = fj or self.fj.get_item(repo, link.fj)
        if link.origin in ("gh", "fj"):
            orig = g if link.origin == "gh" else f
            digest = text.body_hash(orig.title, orig.body)
            if digest != link.src_hash:
                to = self.other(link.origin)
                body = text.issue_copy(self.forge(link.origin).side, repo, orig.number, orig.url,
                                       orig.login, orig.name, orig.body, link.kind)
                self.forge(to).update_item(repo, self.num(link, to),
                                           title=text.safe_title(orig.title), body=body)
                self.store.update_link(link, src_hash=digest)
        self.sync_labels(repo, link, g, f)
        if link.kind == "issue":
            self.sync_state(repo, link, g, f)

    def sync_state(self, repo, link, g, f):
        last, gs, fs = link.state, g.state, f.state
        if gs == fs:
            if last != gs:
                self.store.update_link(link, state=gs)
            return
        if fs != last:  # Forgejo changed (or both changed): Forgejo wins
            self.gh.update_item(repo, link.gh, state=fs)
            self.store.update_link(link, state=fs)
        else:
            self.fj.update_item(repo, link.fj, state=gs)
            self.store.update_link(link, state=gs)

    def sync_labels(self, repo, link, g, f):
        common = self.common_labels(repo)
        last = link.labels & common
        gl, fl = set(g.labels) & common, set(f.labels) & common
        new = (last | (gl - last) | (fl - last)) - ((last - gl) | (last - fl))
        if gl != new:
            self.gh.set_labels(repo, link.gh, (set(g.labels) - common) | new)
        if fl != new:
            self.fj.set_labels(repo, link.fj, (set(f.labels) - common) | new)
        if link.labels != new:
            self.store.update_link(link, labels=new)

    # -- pull requests -------------------------------------------------------

    def finish_pull(self, repo, link):
        self.fj.delete_fork_branch(repo, f"gh-pr/{link.gh}")
        self.store.update_link(link, done=1, state="closed")

    def sync_pull(self, repo, link):
        gp = self.gh.get_pull(repo, link.gh)
        fp = self.fj.get_pull(repo, link.fj)
        if fp.merged:
            if gp.merged or gp.state == "closed":
                log.info("%s: gh#%s shows merged=%s after the Forgejo merge", repo, link.gh,
                         gp.merged)
                self.finish_pull(repo, link)
            elif not link.merge_seen:
                self.store.update_link(link, merge_seen=fmt_time(self.now()))
            elif self.now() - parse_time(link.merge_seen) >= self.merge_grace:
                self.gh.create_comment(repo, link.gh,
                                       text.merged_elsewhere(fp.url, fp.merge_sha, repo))
                self.gh.close_pull(repo, link.gh)
                self.finish_pull(repo, link)
            return
        if fp.state == "closed" and gp.state == "open":
            self.gh.create_comment(repo, link.gh,
                                   text.closed_elsewhere(self.fj.side, fp.url, repo, f"pr{link.fj}"))
            self.gh.close_pull(repo, link.gh)
            self.finish_pull(repo, link)
            log.info("%s: fj#%s closed, gh#%s closed", repo, link.fj, link.gh)
        elif gp.merged and fp.state == "open":
            # The commits reached main by another pull request (a maintainer
            # pushed them into the repository for a trusted run), and the
            # mirror made GitHub show the original as merged.
            self.fj.create_comment(repo, link.fj,
                                   text.merged_on_main(gp.url, repo, f"pr{link.gh}"))
            self.fj.close_pull(repo, link.fj)
            self.finish_pull(repo, link)
            log.info("%s: gh#%s merged through main, fj#%s closed", repo, link.gh, link.fj)
        elif gp.state == "closed" and fp.state == "open":
            self.fj.create_comment(repo, link.fj,
                                   text.closed_elsewhere(self.gh.side, gp.url, repo, f"pr{link.gh}"))
            self.fj.close_pull(repo, link.fj)
            self.finish_pull(repo, link)
            log.info("%s: gh#%s closed, fj#%s closed", repo, link.gh, link.fj)
        elif gp.state == "closed":
            self.finish_pull(repo, link)
        elif gp.head_sha != link.head:
            push_url = self.fj.ensure_fork(repo)
            self.git.push_pull(repo, self.gh.fetch_url(repo), link.gh, push_url)
            self.store.update_link(link, head=gp.head_sha)
            log.info("%s: gh#%s new head %s pushed", repo, link.gh, gp.head_sha)

    def sync_reviews(self, repo, link):
        for side in ("gh", "fj"):
            src, to = self.forge(side), self.other(side)
            dst, number = self.forge(to), self.num(link, to)
            for rev in src.list_reviews(repo, self.num(link, side)):
                if self.is_foreign(side, rev.login, rev.body):
                    continue
                key = f"rv{rev.id}"
                if self.store.comment(repo, side, key) is None:
                    body = text.review_copy(src.side, repo, key, rev.url, rev.login, rev.name,
                                            rev.state, rev.body)
                    self._post_once(repo, side, key, dst, number, body, "")
                for rc in rev.comments:
                    rkey = f"rc{rc.id}"
                    if self.store.comment(repo, side, rkey) is None:
                        body = text.review_comment_copy(src.side, repo, rkey, rc.url, rc.login,
                                                        rc.name, rc.path, rc.line, rc.body)
                        self._post_once(repo, side, rkey, dst, number, body, rc.body)

    # -- comments ------------------------------------------------------------

    def _post_once(self, repo, side, key, dst, number, body, source_body):
        pkey = f"c:{side}:{key}"
        cid = None
        if self.store.is_pending(repo, pkey):
            cid = dst.find_marked_comment(repo, number, side, key)
        else:
            self.store.add_pending(repo, pkey)
        if cid is None:
            cid = dst.create_comment(repo, number, body).id
        self.store.add_comment(repo, side, key, cid, text.body_hash("", source_body))
        self.store.clear_pending(repo, pkey)

    def on_comment(self, repo, side, comment):
        src = self.forge(side)
        if self.is_foreign(side, comment.login, comment.body):
            return
        link = self.store.link_by(repo, side, comment.number)
        if link is None:
            return
        to = self.other(side)
        dst, number = self.forge(to), self.num(link, to)
        key = f"c{comment.id}"
        body = text.comment_copy(src.side, repo, key, comment.url, comment.login,
                                 comment.name, comment.body)
        known = self.store.comment(repo, side, key)
        if known:
            digest = text.body_hash("", comment.body)
            if known[1] != digest:
                dst.edit_comment(repo, known[0], body)
                self.store.update_comment(repo, side, key, digest)
            return
        self._post_once(repo, side, key, dst, number, body, comment.body)

    # -- one-off commands ------------------------------------------------------

    def seed(self, repo, before):
        """Link the items that the 2026-10 import copied with the same number."""
        epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
        fj_items = {i.number: i for i in self.fj.list_updated(repo, epoch)}
        common = self.common_labels(repo)
        count = 0
        for g in self.gh.list_updated(repo, epoch):
            f = fj_items.get(g.number)
            if (f is None or g.created >= before or f.title != g.title
                    or f.is_pr != g.is_pr or self.store.link_by(repo, "gh", g.number)
                    or self.store.link_by(repo, "fj", f.number)):
                continue
            self.store.add_link(repo, "pr" if g.is_pr else "issue", "import", gh=g.number,
                                fj=f.number, state=g.state, labels=set(g.labels) & common,
                                src_hash="", head=None)
            if g.is_pr:
                self.store.update_link(self.store.link_by(repo, "gh", g.number), done=1)
            count += 1
        return count

    def backfill(self, repo):
        """Copy the open issues that no copy exists for yet, on both sides."""
        epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
        made = 0
        for side in ("gh", "fj"):
            for item in self.forge(side).list_updated(repo, epoch):
                if (item.state != "open" or item.is_pr
                        or self.store.link_by(repo, side, item.number)
                        or self.is_foreign(side, item.login, item.body)):
                    continue
                if self.copy_item(repo, side, item):
                    made += 1
        return made
