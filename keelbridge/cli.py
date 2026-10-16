"""keel-bridge: sync issues and pull requests between GitHub and Forgejo.

  keel-bridge [-c FILE] run                 one pass over all repositories
  keel-bridge [-c FILE] seed [--before T]   link the items of the import
  keel-bridge [-c FILE] backfill [REPO...]  copy open issues with no copy

The configuration file is INI (deploy/bridge.ini.example). The service
starts as root, reads the token files (root only, mode 0600), and then
changes to the user of the [bridge] section before it touches the network.
"""

import argparse
import configparser
import fcntl
import logging
import os
import pwd
import sys
from datetime import timedelta

from keelbridge.auth import GitHubApp, StaticToken, read_secret
from keelbridge.engine import Bridge
from keelbridge.forges import Forgejo, GitHub, parse_time
from keelbridge.gitops import GitOps
from keelbridge.http import Http
from keelbridge.store import Store

DEFAULT_CONFIG = "/etc/keel-bridge/bridge.ini"
log = logging.getLogger("keel-bridge")


def load_config(path):
    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        raise SystemExit(f"keel-bridge: cannot read {path}")
    for section in ("bridge", "forgejo", "github"):
        if section not in cfg:
            raise SystemExit(f"keel-bridge: [{section}] is missing in {path}")
    return cfg


def read_repos(cfg):
    value = cfg["bridge"].get("repos", "")
    if value.startswith("@"):
        with open(value[1:], encoding="utf-8") as fh:
            value = fh.read()
    repos = [r for line in value.splitlines() for r in line.split("#", 1)[0].split()]
    if not repos:
        raise SystemExit("keel-bridge: [bridge] repos is empty")
    return repos


def read_secrets(cfg):
    secrets = {"forgejo": read_secret(cfg["forgejo"]["token_file"])}
    ghc = cfg["github"]
    if ghc.get("key_file"):
        secrets["github_key"] = read_secret(ghc["key_file"])
        secrets["github_app_id"] = ghc.get("app_id") or read_secret(ghc["app_id_file"])
    else:
        secrets["github_token"] = read_secret(ghc["token_file"])
    return secrets


def drop_privileges(user):
    """Change to user when root. The token values stay in this process only."""
    if os.getuid() != 0:
        return
    if not user:
        raise SystemExit("keel-bridge: started as root, and [bridge] user is empty")
    pw = pwd.getpwnam(user)
    os.initgroups(user, pw.pw_gid)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    os.environ["HOME"] = pw.pw_dir
    try:
        os.setuid(0)
    except PermissionError:
        return
    raise SystemExit("keel-bridge: could not leave root")


def build(cfg, secrets):
    b, f, g = cfg["bridge"], cfg["forgejo"], cfg["github"]
    store = Store(b.get("db", "/var/lib/keel-bridge/state.db"))
    fj_http = Http(f["url"] + "/api/v1", StaticToken(secrets["forgejo"]), cache=store)
    fj = Forgejo(fj_http, f.get("org", "Keel-Linux"), f.get("bot", "keel-bridge"), f["url"])
    if "github_key" in secrets:
        auth = GitHubApp(secrets["github_app_id"], secrets["github_key"], g.get("org", "Keel-Linux"))
        bot = g.get("bot") or auth.bot_login()
    else:
        auth = StaticToken(secrets["github_token"])
        bot = g["bot"]
    gh_http = Http("https://api.github.com", auth, cache=store)
    gh = GitHub(gh_http, g.get("org", "Keel-Linux"), bot)
    git = GitOps(b.get("workdir", "/var/lib/keel-bridge/git"), fj.bot, secrets["forgejo"])
    bridge = Bridge(gh, fj, store, git, read_repos(cfg),
                    max_creates=b.getint("max_creates", 30),
                    merge_grace=timedelta(minutes=b.getint("merge_grace_minutes", 15)))
    if b.get("start") and store.meta("start") is None:
        store.set_meta("start", b["start"])
    return bridge


def main(argv=None, build_fn=build):
    parser = argparse.ArgumentParser(prog="keel-bridge")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    seed = sub.add_parser("seed")
    seed.add_argument("--before", default="2026-10-15T00:00:00Z")
    seed.add_argument("repos", nargs="*")
    back = sub.add_parser("backfill")
    back.add_argument("repos", nargs="*")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)

    cfg = load_config(args.config)
    secrets = read_secrets(cfg)
    cfg["bridge"]["repos"] = " ".join(read_repos(cfg))
    drop_privileges(cfg["bridge"].get("user", "keel-bridge"))
    workdir = cfg["bridge"].get("workdir", "/var/lib/keel-bridge/git")
    os.makedirs(workdir, mode=0o700, exist_ok=True)
    lock = open(os.path.join(workdir, ".lock"), "w")  # noqa: SIM115 held until exit
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another pass is running")
        return 0
    bridge = build_fn(cfg, secrets)
    if args.cmd == "run":
        return 1 if bridge.run_once() else 0
    repos = args.repos or bridge.repos
    if args.cmd == "seed":
        before = parse_time(args.before)
        for repo in repos:
            log.info("%s: %d imported items linked", repo, bridge.seed(repo, before))
        return 0
    bridge.start_time()
    bridge.pace = 2.0
    bridge.max_creates = 10_000
    for repo in repos:
        log.info("%s: %d open issues copied", repo, bridge.backfill(repo))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
