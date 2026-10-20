"""JWT authenticates identity; live database membership authorizes each request."""

from datetime import UTC, datetime, timedelta

import jwt
from fastapi import HTTPException

ISSUER = "tenant-access-lab"
AUDIENCE = "tenant-documents-api"


def issue_token(secret: str, user_id: str, minutes: int = 10) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user_id,
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + timedelta(minutes=minutes),
        },
        secret,
        algorithm="HS256",
    )


def identity(secret: str, authorization: str | None) -> str:
    unauthorized = HTTPException(
        401, "Valid bearer token required", headers={"WWW-Authenticate": "Bearer"}
    )
    if not authorization or not authorization.startswith("Bearer "):
        raise unauthorized
    try:
        claims = jwt.decode(
            authorization[7:],
            secret,
            algorithms=["HS256"],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": ["sub", "iss", "aud", "exp", "iat"]},
        )
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise unauthorized
        return claims["sub"]
    except jwt.InvalidTokenError:
        raise unauthorized from None
