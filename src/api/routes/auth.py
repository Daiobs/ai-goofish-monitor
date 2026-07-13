"""Web UI session authentication endpoints."""

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from src.api.auth import SESSION_COOKIE_NAME, SessionManager, get_session_manager


router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(
    payload: LoginRequest,
    response: Response,
    manager: SessionManager = Depends(get_session_manager),
):
    if not manager.credentials_are_valid(payload.username, payload.password):
        response.status_code = 401
        return {"authenticated": False}

    manager.set_cookie(response, manager.create_session())
    return {"authenticated": True, "username": payload.username}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    manager: SessionManager = Depends(get_session_manager),
):
    manager.revoke_session(request.cookies.get(SESSION_COOKIE_NAME))
    manager.clear_cookie(response)
    return {"authenticated": False}


@router.get("/session")
async def get_session(
    request: Request,
    response: Response,
    manager: SessionManager = Depends(get_session_manager),
):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    session = manager.read_session(token)
    if session is None:
        if token:
            manager.clear_cookie(response)
        return {"authenticated": False}
    return {"authenticated": True, "username": session.username}
