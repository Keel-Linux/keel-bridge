"""The credentials of the two bots.

GitHub: a GitHub App. The bridge signs a short JSON Web Token with the
private key of the App, and trades it for an installation token of one
hour. The App has issues and pull requests write and metadata read, and
nothing else. A fine-grained token in a file is the fallback.

Forgejo: the token of the user keel-bridge, in a file.
"""

import time
from datetime import datetime

from keelbridge.http import Http

GITHUB_API = "https://api.github.com"
PERMISSIONS = {"issues": "write", "pull_requests": "write", "metadata": "read"}


def read_secret(path):
    with open(path, encoding="utf-8") as fh:
        value = fh.read().strip()
    if not value:
        raise ValueError(f"{path} is empty")
    return value


class StaticToken:
    def __init__(self, token, scheme="token"):
        self.header = f"{scheme} {token}"

    def __call__(self):
        return self.header


class GitHubApp:
    def __init__(self, app_id, private_key, org, send=None, now=time.time, encode=None):
        self.app_id = str(app_id)
        self.key = private_key
        self.org = org
        self.now = now
        self.encode = encode or _jwt_encode
        kwargs = {"send": send} if send else {}
        self.api = Http(GITHUB_API, lambda: "Bearer " + self.jwt(), **kwargs)
        self._token = None
        self._expires = 0.0

    def jwt(self):
        now = int(self.now())
        return self.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.key)

    def __call__(self):
        if self._token is None or self._expires - self.now() < 300:
            inst = self.api.get(f"/orgs/{self.org}/installation")
            tok = self.api.post(f"/app/installations/{inst['id']}/access_tokens",
                                {"permissions": PERMISSIONS})
            self._token = tok["token"]
            self._expires = datetime.fromisoformat(tok["expires_at"].replace("Z", "+00:00")).timestamp()
        return f"token {self._token}"

    def bot_login(self):
        return self.api.get("/app")["slug"] + "[bot]"


def _jwt_encode(claims, key):  # pragma: no cover - needs python3-jwt
    import jwt
    return jwt.encode(claims, key, algorithm="RS256")
