"""
auth.py - simple single-admin JWT auth for the web terminal.
Does not touch any of the trading bot's own files.
"""
import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext

COOKIE_NAME = "access_token"
# Set SESSION_COOKIE_SECURE=true once you're serving over HTTPS (behind Nginx+SSL).
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"

JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "insecure-dev-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MINUTES", "720"))

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)


def authenticate(username: str, password: str) -> bool:
    if username != ADMIN_USERNAME:
        return False
    if not ADMIN_PASSWORD_HASH:
        # No hash configured yet - refuse rather than silently allow.
        return False
    return pwd_context.verify(password, ADMIN_PASSWORD_HASH)


def verify_admin_password(password: str) -> bool:
    """Used to unlock action buttons in the frontend's locked/view-only mode -
    checks the password alone against the same admin hash, independent of login."""
    if not ADMIN_PASSWORD_HASH:
        return False
    return pwd_context.verify(password, ADMIN_PASSWORD_HASH)


def create_access_token(username: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {"sub": username, "exp": expire}
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> str:
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        username = payload.get("sub")
        if username is None:
            raise JWTError("missing subject")
        return username
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")


def get_current_user(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> str:
    """Accepts either the httpOnly session cookie (browser UI) or a Bearer
    header (useful for curl/scripts). Cookie is checked first."""
    token = request.cookies.get(COOKIE_NAME)
    if not token and creds is not None:
        token = creds.credentials
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return decode_token(token)


def get_user_from_ws_token(token: str) -> str:
    """Same verification path, used for the websocket query-param token."""
    return decode_token(token)

