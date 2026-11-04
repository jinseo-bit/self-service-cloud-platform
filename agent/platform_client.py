"""Platform API client for the agent (D-181).

Signs in with the agent's own Cognito client, so the API lets it draft and read only. The password is typed by the
developer in their own terminal (python -m agent.login) and never reaches the MCP server or the chat; only the refresh
token is kept, in a file readable by the user only. Stdlib only: the agent runs on the developer's machine.
"""

from __future__ import annotations

import base64
import io
import json
import os
import stat
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

CONFIG = Path(os.environ.get("SSCP_AGENT_CONFIG") or Path.home() / ".sscp" / "agent.json")  # tests point this elsewhere
TIMEOUT = 30


class PlatformError(Exception):
    def __init__(self, status: int, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.detail = status, message, detail or {}


class NotLoggedIn(PlatformError):
    def __init__(self):
        super().__init__(401, "로그인이 필요해요. 터미널에서 `python -m agent.login`을 먼저 실행해 주세요.")


def _http(method: str, url: str, body: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310 - URLs come from the user's own config
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _cognito(region: str, action: str, payload: dict) -> dict:
    status, raw = _http("POST", f"https://cognito-idp.{region}.amazonaws.com/", json.dumps(payload).encode(), {
        "Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": f"AWSCognitoIdentityProviderService.{action}"})
    data = json.loads(raw or b"{}")
    if status != 200:
        raise PlatformError(status, data.get("message") or data.get("__type") or "로그인에 실패했어요.")
    return data["AuthenticationResult"]


def _exp(token: str) -> int:
    try:
        part = token.split(".")[1]
        return int(json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("exp") or 0)
    except (IndexError, ValueError):
        return 0


def save_config(cfg: dict, path: Path | None = None) -> None:
    path = path or CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600 where the OS supports it
    except OSError:
        pass


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG
    if not path.exists():
        raise NotLoggedIn()
    return json.loads(path.read_text(encoding="utf-8"))


def login(*, email: str, password: str | None = None, api_url: str = "", ui_url: str = "", region: str = "",
          client_id: str = "", local: str = "", path: Path | None = None) -> dict:
    """Cognito sign-in with the agent client, or the local dev server (`local` = its base URL)."""
    if local:
        base = local.rstrip("/")
        status, raw = _http("POST", f"{base}/local/auth", json.dumps({"email": email, "client": "agent"}).encode(),
                            {"Content-Type": "application/json"})
        if status != 200:
            raise PlatformError(status, json.loads(raw or b"{}").get("error", "local login failed"))
        tokens = json.loads(raw)
        cfg = {"mode": "local", "api_url": f"{base}/api", "ui_url": base, "email": email, "id_token": tokens["IdToken"],
               "id_token_exp": 0}
    else:
        if not (api_url and region and client_id and password):
            raise PlatformError(400, "api_url, region, client_id와 비밀번호가 필요해요.")
        r = _cognito(region, "InitiateAuth", {"AuthFlow": "USER_PASSWORD_AUTH", "ClientId": client_id,
                                              "AuthParameters": {"USERNAME": email, "PASSWORD": password}})
        cfg = {"mode": "cognito", "api_url": api_url.rstrip("/"), "ui_url": ui_url.rstrip("/"), "region": region,
               "client_id": client_id, "email": email, "id_token": r["IdToken"], "id_token_exp": _exp(r["IdToken"]),
               "refresh_token": r["RefreshToken"]}
    save_config(cfg, path)
    return {k: v for k, v in cfg.items() if "token" not in k}


class PlatformClient:
    def __init__(self, path: Path | None = None):
        self.path = path or CONFIG
        self.cfg = load_config(self.path)

    @property
    def ui_url(self) -> str:
        return self.cfg.get("ui_url", "")

    def env_url(self, env_id: str) -> str:
        return f"{self.ui_url}/#/env/{env_id}" if self.ui_url else env_id

    def _token(self) -> str:
        c = self.cfg
        if c.get("mode") == "cognito" and c.get("id_token_exp", 0) - 60 < time.time():
            try:
                r = _cognito(c["region"], "InitiateAuth", {"AuthFlow": "REFRESH_TOKEN_AUTH", "ClientId": c["client_id"],
                                                           "AuthParameters": {"REFRESH_TOKEN": c["refresh_token"]}})
            except PlatformError as e:  # the agent client's refresh token lives 1 day: sign in again, do not show Cognito's text
                raise NotLoggedIn() from e
            c["id_token"], c["id_token_exp"] = r["IdToken"], _exp(r["IdToken"])
            save_config(c, self.path)
        return c["id_token"]

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": self._token(), **({"Content-Type": "application/json"} if data else {})}
        status, raw = _http(method, f"{self.cfg['api_url']}{path}", data, headers)
        payload = json.loads(raw or b"{}") if raw else {}
        if status >= 400:
            if status == 401:
                raise NotLoggedIn()
            raise PlatformError(status, payload.get("error", f"HTTP {status}"), payload)
        return payload

    def upload_zip(self, form: dict, data: bytes) -> None:
        """Presigned S3 POST (the same form the UI uses): fields first, the file last."""
        boundary = uuid.uuid4().hex
        buf = io.BytesIO()
        for k, v in form["fields"].items():
            buf.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        buf.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="site.zip"\r\n'
                  f"Content-Type: application/zip\r\n\r\n".encode())
        buf.write(data + f"\r\n--{boundary}--\r\n".encode())
        url = form["url"] if form["url"].startswith("http") else f"{self.ui_url}{form['url']}"
        status, raw = _http("POST", url, buf.getvalue(), {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        if status >= 300:
            raise PlatformError(status, f"업로드 실패 (HTTP {status})", {"body": raw[:300].decode(errors="replace")})


def zip_dir(path: Path) -> bytes:
    """A build output folder (index.html inside) as a zip, the same shape the UI asks for."""
    if path.is_file():
        return path.read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(path.rglob("*")):
            if p.is_file() and not any(part in ("node_modules", ".git") for part in p.relative_to(path).parts):
                z.write(p, p.relative_to(path).as_posix())
    return buf.getvalue()
