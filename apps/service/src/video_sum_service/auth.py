from __future__ import annotations

import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

ACCESS_TOKEN_ENV = "VIDEO_SUM_ACCESS_TOKEN"
SESSION_COOKIE_NAME = "vidmind_session"


class AccessTokenManager:
    def __init__(self, data_dir: Path, *, env: dict[str, str] | None = None) -> None:
        self._data_dir = Path(data_dir)
        self._env = env if env is not None else os.environ
        self._token: str | None = None

    @property
    def token_file(self) -> Path:
        return self._data_dir / "auth.json"

    def get_token(self) -> str:
        env_token = str(self._env.get(ACCESS_TOKEN_ENV) or "").strip()
        if env_token:
            return env_token
        if self._token is None:
            self._token = self._load_or_create_token()
        return self._token

    def is_configured_from_env(self) -> bool:
        return bool(str(self._env.get(ACCESS_TOKEN_ENV) or "").strip())

    def verify(self, candidate: str | None) -> bool:
        expected = self.get_token()
        provided = str(candidate or "").strip()
        return bool(provided) and hmac.compare_digest(provided, expected)

    def _load_or_create_token(self) -> str:
        token_path = self.token_file
        try:
            if token_path.exists():
                payload = json.loads(token_path.read_text(encoding="utf-8"))
                token = str(payload.get("access_token") or "").strip()
                if token:
                    return token
        except (OSError, ValueError, TypeError):
            pass

        token = secrets.token_urlsafe(32)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(
            json.dumps(
                {
                    "access_token": token,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return token


def extract_bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return None


def is_auth_exempt_path(path: str, method: str = "GET") -> bool:
    if path in {"/health", "/api/v1/auth/status", "/api/v1/auth/session"}:
        return True
    if path == "/" or path.startswith("/static/") or path.startswith("/media/"):
        return True
    if method.upper() == "GET" and path.startswith("/api/v1/tasks/") and "/visual-evidence/media/" in path:
        return True
    if method.upper() == "GET" and (
        path.startswith("/videos")
        or path.startswith("/settings")
        or path.startswith("/knowledge")
    ):
        return True
    return False


def request_is_authorized(request: Request, manager: AccessTokenManager) -> bool:
    bearer = extract_bearer_token(request)
    if manager.verify(bearer):
        return True
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    return manager.verify(cookie_token)


def unauthorized_response() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": "需要输入 VidMind 访问密钥。"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def set_session_cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
        max_age=60 * 60 * 24 * 30,
    )


def require_access_token(request: Request, manager: AccessTokenManager) -> None:
    if not request_is_authorized(request, manager):
        raise HTTPException(status_code=401, detail="访问密钥无效。")


def describe_token_source(manager: AccessTokenManager) -> dict[str, object]:
    return {
        "required": True,
        "configuredFromEnv": manager.is_configured_from_env(),
        "tokenFile": str(manager.token_file),
    }
