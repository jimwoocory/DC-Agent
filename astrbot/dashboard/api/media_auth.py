"""Administrator-only OAuth controls for NAS media providers."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from astrbot.dashboard.responses import ApiError, ok
from astrbot.dashboard.services.media_auth_service import MediaAuthService

from .auth import AuthContext, require_scope

router = APIRouter(prefix="/media-auth", tags=["Media Authentication"])
media_auth_service = MediaAuthService()

MediaProvider = Literal["codex", "dreamina"]


class LoginCheckRequest(BaseModel):
    """Device-flow state submitted by the dashboard.

    Args:
        session_id: Server-generated Codex login session identifier.
        device_code: Dreamina device code returned by its headless login flow.
        poll_seconds: Bounded provider-side polling duration.
    """

    session_id: str = Field(default="", max_length=128)
    device_code: str = Field(default="", max_length=512)
    poll_seconds: int = Field(default=0, ge=0, le=30)


async def require_provider_scope(request: Request) -> AuthContext:
    """Require provider administration permission.

    Args:
        request: Current FastAPI request.

    Returns:
        Authenticated dashboard context.
    """
    return await require_scope(request, "provider")


@router.get("/status")
async def get_media_auth_status(
    _auth: AuthContext = Depends(require_provider_scope),
) -> dict:
    """Return redacted Image2 and Dreamina authentication status.

    Args:
        _auth: Authenticated provider administrator.

    Returns:
        Standard API envelope containing both provider states.
    """
    return ok(await media_auth_service.status())


@router.post("/{provider}/login/start")
async def start_media_login(
    provider: MediaProvider,
    _auth: AuthContext = Depends(require_provider_scope),
) -> dict:
    """Start a provider OAuth Device Flow inside the NAS runtime.

    Args:
        provider: Codex OAuth or Dreamina.
        _auth: Authenticated provider administrator.

    Returns:
        Verification URL and one-time user code.

    Raises:
        ApiError: If the provider CLI is unavailable or login cannot start.
    """
    try:
        return ok(await media_auth_service.start_login(provider))
    except (RuntimeError, ValueError) as exc:
        raise ApiError(str(exc), status_code=503) from exc


@router.post("/{provider}/login/check")
async def check_media_login(
    provider: MediaProvider,
    payload: LoginCheckRequest,
    _auth: AuthContext = Depends(require_provider_scope),
) -> dict:
    """Poll one provider OAuth Device Flow.

    Args:
        provider: Codex OAuth or Dreamina.
        payload: Opaque login session state.
        _auth: Authenticated provider administrator.

    Returns:
        Pending, authenticated, or failed login state.

    Raises:
        ApiError: If required session data is missing or invalid.
    """
    try:
        return ok(
            await media_auth_service.check_login(
                provider,
                session_id=payload.session_id,
                device_code=payload.device_code,
                poll_seconds=payload.poll_seconds,
            )
        )
    except ValueError as exc:
        raise ApiError(str(exc), status_code=400) from exc
    except RuntimeError as exc:
        raise ApiError(str(exc), status_code=503) from exc


@router.post("/{provider}/test")
async def test_media_auth(
    provider: MediaProvider,
    _auth: AuthContext = Depends(require_provider_scope),
) -> dict:
    """Run a non-generating provider authentication probe.

    Args:
        provider: Codex OAuth or Dreamina.
        _auth: Authenticated provider administrator.

    Returns:
        Redacted probe result.
    """
    return ok(await media_auth_service.test(provider))


@router.post("/{provider}/logout")
async def logout_media_auth(
    provider: MediaProvider,
    _auth: AuthContext = Depends(require_provider_scope),
) -> dict:
    """Remove one provider login from the NAS credential volume.

    Args:
        provider: Codex OAuth or Dreamina.
        _auth: Authenticated provider administrator.

    Returns:
        Redacted logged-out state.

    Raises:
        ApiError: If the provider CLI cannot perform logout.
    """
    try:
        return ok(await media_auth_service.logout(provider))
    except RuntimeError as exc:
        raise ApiError(str(exc), status_code=503) from exc


__all__ = [
    "LoginCheckRequest",
    "check_media_login",
    "get_media_auth_status",
    "logout_media_auth",
    "media_auth_service",
    "router",
    "start_media_login",
    "test_media_auth",
]
