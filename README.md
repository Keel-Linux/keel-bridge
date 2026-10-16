# keel-bridge

keel-bridge syncs issues and pull requests between the GitHub mirror
<https://github.com/Keel-Linux> and the source <https://git.pop.coop/Keel-Linux>
(handbook decision 0056 and its amendment of 2026-10-16).

## What it does

- A GitHub issue gets a copy on git.pop.coop. A git.pop.coop issue gets a
  copy on GitHub. The copy starts with "Mirrored from ..." and credits the
  author by name and link. The original gets a note with a link to the copy.
- Comments, labels (the names that exist on both sides) and the open or
  closed state sync both ways. Edits of the title and the body go from the
  original to the copy.
- A GitHub pull request gets a copy on git.pop.coop. The bot pushes the
  exact head commit to the branch `gh-pr/<n>` of its own fork
  (`keel-bridge/<repo>`) and opens a pull request from there. Forgejo holds
  the CI of that pull request until a maintainer approves the run.
  Merge on git.pop.coop only, with a merge commit. The push mirror then
  makes GitHub show the original as merged. If GitHub does not, the bridge
  closes the original with a link to the merge.
- Reviews sync both ways as comments that name the review state.
- Pull requests that start on git.pop.coop stay there.

## Loop rules

1. Skip everything that a bridge bot wrote.
2. Skip everything that carries a bridge marker
   (`<!-- keel-bridge:v1 ... -->`).
3. The SQLite ID map links an original and its copy. A "pending" row is
   written before each copy. After a crash, the bridge looks for the
   marker before it makes the copy again.
4. State and labels are compared with the last synced value. A side that
   changed wins. If both changed, git.pop.coop wins.

A copy neutralizes @mentions and turns `#N` into a link to the origin
side, because the numbers on the two servers are different. Other
references (in code, in headings, `owner/repo#N`) get a word joiner, so
no closing keyword acts on the wrong server.

## Known gaps

- The commits of a bridged pull request are the exact commits of the
  original. A `Fixes #N` in a commit message is not changed, and on merge
  it can act on item N of git.pop.coop. Read the commit messages before
  the merge.
- After a bridged pull request is closed or merged, a reopen on either
  side is not synced.
- A deleted comment is not deleted on the other side. Comment edits sync
  from the original to the copy only.
- Reviews sync as comments that name the state. They are not real reviews
  on the other server.
- A change of the base branch of a GitHub pull request is not synced.

## Commands

```
keel-bridge -c /etc/keel-bridge/bridge.ini run         # one pass (timer: every 2 min)
keel-bridge -c /etc/keel-bridge/bridge.ini seed        # link the items of the 2026-10 import
keel-bridge -c /etc/keel-bridge/bridge.ini backfill    # copy open issues that have no copy
```

## Where it runs

In the unprivileged container `keel-bridge` on its own bridge `kbr0`
of the runner VM, with outbound HTTPS only. Nothing can open a connection
to it (handbook docs/infra/keel-lxc-2.md). The token files are in
`/etc/keel-bridge`, root only. The service reads them as root, then
changes to the user `keel-bridge`.

Deploy: `deploy/` has the systemd units, an example configuration, the
GitHub App manifest page and the script that trades the manifest code for
the App key inside the container.

## Tests

```
python3 -m pytest -q tests
```

The tests use in-memory servers and local git repositories. They need no
network and no token. The gate is 95 percent coverage.
