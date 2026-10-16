"""Tests of keelbridge.gitops, keelbridge.auth and keelbridge.cli."""

import os
from datetime import datetime, timezone
import subprocess

import pytest

from keelbridge import cli
from keelbridge.auth import GitHubApp, StaticToken, read_secret
from keelbridge.gitops import GitOps


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def test_push_pull_moves_the_exact_commit(tmp_path):
    origin = tmp_path / "origin"
    git("init", "-q", "-b", "main", str(origin))
    (origin / "f").write_text("1")
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "c1",
        cwd=origin)
    (origin / "f").write_text("2")
    git("add", "f", cwd=origin)
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "pr", cwd=origin)
    head = git("rev-parse", "HEAD", cwd=origin)
    git("update-ref", "refs/pull/5/head", head, cwd=origin)
    fork = tmp_path / "fork.git"
    git("init", "-q", "--bare", str(fork))
    ops = GitOps(str(tmp_path / "work"), "keel-bridge", "secret")
    os.makedirs(ops.workdir)
    assert ops.push_pull("r", f"file://{origin}", 5, f"file://{fork}") == "gh-pr/5"
    assert git("rev-parse", "refs/heads/gh-pr/5", cwd=fork) == head
    ops.push_pull("r", f"file://{origin}", 5, f"file://{fork}")  # a second push is fine


def test_token_is_not_on_the_command_line(tmp_path):
    seen = []
    ops = GitOps(str(tmp_path), "keel-bridge", "s3cret", run=lambda a, env: seen.append((a, env)))
    ops.push_pull("r", "https://github.com/O/r.git", 2, "https://git/keel-bridge/r.git")
    assert all("s3cret" not in " ".join(a) for a, _ in seen)
    fetch_env, push_args, push_env = seen[-2][1], seen[-1][0], seen[-1][1]
    assert "KB_TOKEN" not in fetch_env
    assert push_env["KB_TOKEN"] == "s3cret"
    assert "core.hooksPath=/dev/null" in push_args
    assert any(a.startswith("credential.https://git.helper=") for a in push_args)
    assert "transfer.fsckObjects=true" in seen[-2][0]


def test_read_secret(tmp_path):
    p = tmp_path / "t"
    p.write_text(" abc \n")
    assert read_secret(str(p)) == "abc"
    p.write_text("\n")
    with pytest.raises(ValueError):
        read_secret(str(p))


def test_static_token():
    assert StaticToken("x")() == "token x"
    assert StaticToken("x", "Bearer")() == "Bearer x"


def test_github_app_token_is_cached_until_near_expiry():
    sent = []

    def send(method, url, headers, data):
        sent.append((method, url, headers["Authorization"]))
        if url.endswith("/installation"):
            return 200, {}, '{"id": 77}'
        if url.endswith("/access_tokens"):
            return 201, {}, '{"token": "inst", "expires_at": "2026-10-16T13:00:00Z"}'
        return 200, {}, '{"slug": "keel-linux-bridge"}'
    clock = [datetime(2026, 10, 16, 12, tzinfo=timezone.utc).timestamp()]
    app = GitHubApp(1234, "KEY", "Keel-Linux", send=send, now=lambda: clock[0],
                    encode=lambda claims, key: f"jwt-{claims['iss']}-{key}")
    assert app() == "token inst"
    assert app() == "token inst"
    assert len(sent) == 2 and sent[0][2] == "Bearer jwt-1234-KEY"
    assert sent[1][1].endswith("/app/installations/77/access_tokens")
    clock[0] += 3500
    app()
    assert len(sent) == 4
    assert app.bot_login() == "keel-linux-bridge[bot]"


INI = """
[bridge]
db = {d}/state.db
workdir = {d}/git
user =
repos = @{d}/repos
start = 2026-10-16T12:00:00Z
[forgejo]
url = https://git.pop.coop
token_file = {d}/fj
[github]
{gh}
"""


def write_cfg(tmp_path, gh="token_file = {d}/gh\nbot = kb[bot]"):
    d = str(tmp_path)
    (tmp_path / "fj").write_text("FJTOKEN")
    (tmp_path / "gh").write_text("GHTOKEN")
    (tmp_path / "key").write_text("PEM")
    (tmp_path / "repos").write_text("# list\nkeel-keys\ninithooks\n")
    path = tmp_path / "bridge.ini"
    path.write_text(INI.format(d=d, gh=gh.format(d=d)))
    return str(path)


def test_load_config_errors(tmp_path):
    with pytest.raises(SystemExit):
        cli.load_config(str(tmp_path / "none.ini"))
    bad = tmp_path / "bad.ini"
    bad.write_text("[bridge]\n")
    with pytest.raises(SystemExit):
        cli.load_config(str(bad))


def test_repos_and_secrets(tmp_path):
    cfg = cli.load_config(write_cfg(tmp_path))
    assert cli.read_repos(cfg) == ["keel-keys", "inithooks"]
    assert cli.read_secrets(cfg) == {"forgejo": "FJTOKEN", "github_token": "GHTOKEN"}
    cfg["bridge"]["repos"] = " "
    with pytest.raises(SystemExit):
        cli.read_repos(cfg)
    cfg2 = cli.load_config(write_cfg(tmp_path, gh="key_file = {d}/key\napp_id = 42"))
    assert cli.read_secrets(cfg2)["github_app_id"] == "42"
    assert cli.read_secrets(cfg2)["github_key"] == "PEM"


def test_build_wires_the_parts(tmp_path):
    cfg = cli.load_config(write_cfg(tmp_path))
    bridge = cli.build(cfg, cli.read_secrets(cfg))
    assert bridge.repos == ["keel-keys", "inithooks"]
    assert bridge.gh.bot == "kb[bot]" and bridge.fj.bot == "keel-bridge"
    assert bridge.store.meta("start") == "2026-10-16T12:00:00Z"
    assert bridge.git.token == "FJTOKEN"
    cfg2 = cli.load_config(write_cfg(tmp_path, gh="key_file = {d}/key\napp_id = 42\nbot = a[bot]"))
    bridge2 = cli.build(cfg2, cli.read_secrets(cfg2))
    assert bridge2.gh.bot == "a[bot]"


class FakeBridge:
    def __init__(self, errors=0):
        self.errors = errors
        self.repos = ["a", "b"]
        self.calls = []

    def run_once(self):
        return self.errors

    def seed(self, repo, before):
        self.calls.append(("seed", repo, before.year))
        return 1

    def start_time(self):
        self.calls.append(("start",))

    def backfill(self, repo):
        self.calls.append(("backfill", repo))
        return 0


def test_main_commands(tmp_path):
    path = write_cfg(tmp_path)
    fb = FakeBridge()
    assert cli.main(["-c", path, "run"], build_fn=lambda c, s: fb) == 0
    assert cli.main(["-c", path, "run"], build_fn=lambda c, s: FakeBridge(errors=2)) == 1
    assert cli.main(["-c", path, "seed", "x"], build_fn=lambda c, s: fb) == 0
    assert cli.main(["-c", path, "backfill"], build_fn=lambda c, s: fb) == 0
    assert ("seed", "x", 2026) in fb.calls
    assert ("backfill", "a", ) in fb.calls and ("backfill", "b") in fb.calls
    assert fb.pace == 2.0


def test_main_skips_when_another_pass_holds_the_lock(tmp_path):
    import fcntl
    path = write_cfg(tmp_path)
    os.makedirs(tmp_path / "git")
    with open(tmp_path / "git" / ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert cli.main(["-c", path, "run"], build_fn=lambda c, s: 1 / 0) == 0


def test_drop_privileges_is_a_no_op_for_a_normal_user(monkeypatch):
    monkeypatch.setattr(cli.os, "getuid", lambda: 1000)
    cli.drop_privileges("keel-bridge")
    cli.drop_privileges("")


def test_run_git_reports_stderr_without_the_token(tmp_path):
    from keelbridge.gitops import run_git
    with pytest.raises(RuntimeError) as err:
        run_git(["-C", str(tmp_path), "fetch", "file:///nonexistent-s3cret"],
                {"PATH": os.environ["PATH"], "KB_TOKEN": "s3cret"})
    assert "s3cret" not in str(err.value) and "fetch" in str(err.value)


def test_drop_privileges_as_root(monkeypatch):
    calls = []
    uid = [0]

    class PW:
        pw_uid, pw_gid, pw_dir = 999, 998, "/var/lib/keel-bridge"
    monkeypatch.setattr(cli.os, "getuid", lambda: uid[0])
    monkeypatch.setattr(cli.pwd, "getpwnam", lambda u: PW)
    monkeypatch.setattr(cli.os, "initgroups", lambda u, g: calls.append(("groups", u, g)))
    monkeypatch.setattr(cli.os, "setgid", lambda g: calls.append(("gid", g)))
    can_return = [False]

    def setuid(u):
        calls.append(("uid", u))
        if u == 0 and uid[0] != 0 and not can_return[0]:
            raise PermissionError
        uid[0] = u
    monkeypatch.setattr(cli.os, "setuid", setuid)
    monkeypatch.setenv("HOME", "/root")
    cli.drop_privileges("keel-bridge")
    assert calls == [("groups", "keel-bridge", 998), ("gid", 998), ("uid", 999), ("uid", 0)]
    assert os.environ["HOME"] == "/var/lib/keel-bridge"
    # a setuid that can go back to root is a failure
    uid[0] = 0
    can_return[0] = True
    with pytest.raises(SystemExit):
        cli.drop_privileges("keel-bridge")


def test_drop_privileges_as_root_needs_a_user(monkeypatch):
    monkeypatch.setattr(cli.os, "getuid", lambda: 0)
    with pytest.raises(SystemExit):
        cli.drop_privileges("")
