"""Password authentication and revocable PostgreSQL-backed browser sessions."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pwdlib import PasswordHash
from sqlalchemy import delete, select

from app.config import config
from app.db.models import User, WebSession
from app.db.session import database_manager


class AuthenticationError(RuntimeError):
    """Credentials are invalid, disabled, or temporarily locked."""


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    user_id: int
    username: str
    display_name: str
    role: str
    expires_at: datetime
    csrf_hash: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
        }


@dataclass(frozen=True, slots=True)
class CreatedSession:
    token: str
    csrf_token: str
    principal: SessionPrincipal


class AuthService:
    def __init__(self) -> None:
        self.password_hash = PasswordHash.recommended()
        self._dummy_hash = self.password_hash.hash(secrets.token_urlsafe(24))

    @staticmethod
    def hash_secret(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_username(username: str) -> str:
        return username.strip().casefold()

    async def ensure_bootstrap_user(self) -> None:
        """Create the configured local administrator exactly once."""
        if not config.web_login_enabled:
            return

        username = self._normalize_username(config.admin_username)
        async with database_manager.session_factory() as session:
            existing = await session.scalar(select(User).where(User.username == username))
            if existing is not None:
                return
            session.add(
                User(
                    username=username,
                    display_name="平台管理员",
                    password_hash=self.password_hash.hash(config.admin_password),
                    role="admin",
                    is_active=True,
                )
            )
            await session.commit()

    async def login(self, username: str, password: str, remember: bool) -> CreatedSession:
        normalized = self._normalize_username(username)
        now = datetime.now(UTC)
        user: User | None

        async with database_manager.session_factory() as session:
            user = await session.scalar(
                select(User).where(User.username == normalized).with_for_update()
            )
            if user is None:
                self.password_hash.verify(password, self._dummy_hash)
                raise AuthenticationError("Invalid username or password")

            locked = user.locked_until is not None and user.locked_until > now
            password_valid = self.password_hash.verify(password, user.password_hash)
            if locked or not user.is_active or not password_valid:
                if not locked and user.is_active:
                    user.failed_login_count += 1
                    if user.failed_login_count >= config.login_max_failures:
                        user.locked_until = now + timedelta(minutes=config.login_lock_minutes)
                await session.commit()
                raise AuthenticationError("Invalid username or password")

            user.failed_login_count = 0
            user.locked_until = None
            user.last_login_at = now
            await session.execute(delete(WebSession).where(WebSession.expires_at <= now))

            token = secrets.token_urlsafe(48)
            csrf_token = secrets.token_urlsafe(32)
            duration = (
                timedelta(days=config.session_remember_days)
                if remember
                else timedelta(hours=config.session_ttl_hours)
            )
            expires_at = now + duration
            session.add(
                WebSession(
                    token_hash=self.hash_secret(token),
                    csrf_hash=self.hash_secret(csrf_token),
                    user_id=user.id,
                    expires_at=expires_at,
                    last_seen_at=now,
                )
            )
            await session.commit()

            principal = SessionPrincipal(
                user_id=user.id,
                username=user.username,
                display_name=user.display_name,
                role=user.role,
                expires_at=expires_at,
                csrf_hash=self.hash_secret(csrf_token),
            )
            return CreatedSession(token=token, csrf_token=csrf_token, principal=principal)

    async def get_principal(self, token: str) -> SessionPrincipal | None:
        if not token:
            return None
        now = datetime.now(UTC)
        async with database_manager.session_factory() as session:
            row = (
                await session.execute(
                    select(WebSession, User)
                    .join(User, User.id == WebSession.user_id)
                    .where(
                        WebSession.token_hash == self.hash_secret(token),
                        WebSession.expires_at > now,
                        User.is_active.is_(True),
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            web_session, user = row
            return SessionPrincipal(
                user_id=user.id,
                username=user.username,
                display_name=user.display_name,
                role=user.role,
                expires_at=web_session.expires_at,
                csrf_hash=web_session.csrf_hash,
            )

    async def csrf_is_valid(self, token: str, csrf_token: str) -> bool:
        principal = await self.get_principal(token)
        return bool(
            principal
            and csrf_token
            and secrets.compare_digest(principal.csrf_hash, self.hash_secret(csrf_token))
        )

    async def logout(self, token: str) -> None:
        if not token:
            return
        async with database_manager.session_factory() as session:
            await session.execute(
                delete(WebSession).where(WebSession.token_hash == self.hash_secret(token))
            )
            await session.commit()


auth_service = AuthService()
