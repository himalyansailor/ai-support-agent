from fastapi import APIRouter, Cookie, Depends, Request, Response, status

from supportapi.deps import get_auth_service
from supportapi.schemas.auth import AuthResponse, LoginRequest, SignupRequest, UserOut
from supportcore.config import Settings
from supportcore.identity.service import AuthResult, AuthService, InvalidRefreshTokenError

REFRESH_COOKIE = "support_refresh"
REFRESH_COOKIE_PATH = "/auth"

router = APIRouter(prefix="/auth", tags=["auth"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def set_refresh_cookie(response: Response, settings: Settings, result: AuthResult) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        result.refresh_token,
        max_age=settings.refresh_token_ttl_seconds,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=settings.env != "dev",
    )


def _respond(response: Response, settings: Settings, result: AuthResult) -> AuthResponse:
    set_refresh_cookie(response, settings, result)
    return AuthResponse(access_token=result.access_token, user=UserOut.model_validate(result.user))


@router.post("/signup", status_code=status.HTTP_201_CREATED, response_model=AuthResponse)
async def signup(
    body: SignupRequest,
    request: Request,
    response: Response,
    auth: AuthService = Depends(get_auth_service),
) -> AuthResponse:
    result = await auth.signup(body.email, body.password, body.name)
    return _respond(response, _settings(request), result)


@router.post("/login", response_model=AuthResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    auth: AuthService = Depends(get_auth_service),
) -> AuthResponse:
    result = await auth.login(body.email, body.password)
    return _respond(response, _settings(request), result)


@router.post("/refresh", response_model=AuthResponse)
async def refresh(
    request: Request,
    response: Response,
    support_refresh: str | None = Cookie(default=None),
    auth: AuthService = Depends(get_auth_service),
) -> AuthResponse:
    if not support_refresh:
        raise InvalidRefreshTokenError()
    result = await auth.refresh(support_refresh)
    return _respond(response, _settings(request), result)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    support_refresh: str | None = Cookie(default=None),
    auth: AuthService = Depends(get_auth_service),
) -> Response:
    if support_refresh:
        await auth.logout(support_refresh)
    settings = _settings(request)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=settings.env != "dev",
    )
    return response
