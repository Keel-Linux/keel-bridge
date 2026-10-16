"""Move the commits of a GitHub pull request to the bot's Forgejo fork.

The bridge keeps one bare repository per project. It fetches the exact
head of the pull request (refs/pull/N/head) and pushes that commit to the
branch gh-pr/N of the bot's fork. It never checks out a tree and never
runs a hook, so the code of a pull request does not run here. The commit
is the same object on both sides: when Forgejo merges it with a merge
commit and the push mirror updates GitHub, GitHub sees the pull request
as merged.

The Forgejo token reaches git through the environment and a credential
helper, never through the command line.
"""

import os
import subprocess
import urllib.parse

HELPER = '!f() { echo username="$KB_USER"; echo password="$KB_TOKEN"; }; f'


def run_git(args, env):
    try:
        subprocess.run(["git", *args], check=True, env=env, capture_output=True, timeout=600)
    except subprocess.CalledProcessError as err:
        stderr = err.stderr.decode(errors="replace")
        token = env.get("KB_TOKEN")
        if token:
            stderr = stderr.replace(token, "***")
        verb = next((a for a in args if a in ("init", "fetch", "push")), "command")
        raise RuntimeError(f"git {verb} failed: {stderr[-500:]}") from None


class GitOps:
    def __init__(self, workdir, user, token, run=run_git):
        self.workdir = workdir
        self.user = user
        self.token = token
        self.run = run

    def _env(self, with_token):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.workdir,
               "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
        if with_token:
            env.update({"KB_USER": self.user, "KB_TOKEN": self.token})
        return env

    def repo_dir(self, repo):
        return os.path.join(self.workdir, repo + ".git")

    def push_pull(self, repo, fetch_url, number, push_url):
        """Push refs/pull/<number>/head of fetch_url to gh-pr/<number>."""
        path = self.repo_dir(repo)
        if not os.path.isdir(path):
            self.run(["init", "--bare", "--quiet", path], self._env(False))
        base = ["-C", path, "-c", "core.hooksPath=/dev/null", "-c", "credential.helper="]
        branch = f"gh-pr/{number}"
        # The fetch reads data from GitHub: no token in its environment.
        self.run([*base, "-c", "transfer.fsckObjects=true", "fetch", "--quiet", "--no-tags",
                  fetch_url, f"+refs/pull/{number}/head:refs/heads/{branch}"], self._env(False))
        # The push has the helper for the host of push_url only.
        parts = urllib.parse.urlsplit(push_url)
        scope = f"{parts.scheme}://{parts.netloc}"
        self.run([*base, "-c", f"credential.{scope}.helper={HELPER}", "push", "--quiet",
                  "--force", push_url, f"refs/heads/{branch}:refs/heads/{branch}"],
                 self._env(True))
        return branch
