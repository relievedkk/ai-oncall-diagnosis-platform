"""Browser login, session introspection, and logout endpoints."""

import secrets
import time

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.config import config
from app.models.auth import LoginRequest, LoginResponse, UserInfo
from app.services.auth_service import AuthenticationError, SessionPrincipal, auth_service

router = APIRouter()


def _set_auth_cookies(response: Response, token: str, csrf_token: str, max_age: int) -> None:
    common = {
        "max_age": max_age,
        "path": "/",
        "secure": config.session_cookie_secure,
        "samesite": "lax",
    }
    response.set_cookie(
        config.session_cookie_name,
        token,
        httponly=True,
        **common,
    )
    response.set_cookie(
        config.csrf_cookie_name,
        csrf_token,
        httponly=False,
        **common,
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(config.session_cookie_name, path="/")
    response.delete_cookie(config.csrf_cookie_name, path="/")


@router.post("/auth/login", response_model=LoginResponse)
async def login(payload: LoginRequest, response: Response) -> LoginResponse:
    if not config.web_login_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    try:
        created = await auth_service.login(
            payload.username,
            payload.password.get_secret_value(),
            payload.remember,
        )
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误，连续失败后账号会被暂时锁定",
        ) from exc

    max_age = max(1, int(created.principal.expires_at.timestamp() - time.time()))
    _set_auth_cookies(response, created.token, created.csrf_token, max_age)
    return LoginResponse(
        user=UserInfo(**created.principal.as_dict()),
        expires_at=created.principal.expires_at,
    )


@router.get("/auth/me", response_model=UserInfo)
async def current_user(request: Request) -> UserInfo:
    principal: SessionPrincipal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return UserInfo(**principal.as_dict())


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request) -> Response:
    token = request.cookies.get(config.session_cookie_name, "")
    csrf_cookie = request.cookies.get(config.csrf_cookie_name, "")
    csrf_header = request.headers.get("X-CSRF-Token", "")
    if token and (
        not csrf_cookie
        or not secrets.compare_digest(csrf_cookie, csrf_header)
        or not await auth_service.csrf_is_valid(token, csrf_header)
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    await auth_service.logout(token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_auth_cookies(response)
    return response
