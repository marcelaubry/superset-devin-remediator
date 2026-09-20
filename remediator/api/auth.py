import hashlib
import hmac
import time

from fastapi import Depends, HTTPException, Request

from ..config import Settings, get_settings


class OperatorAuthRequired(Exception):
    pass


COOKIE_NAME = "operator_session"


def _cookie_value(token: str, exp: str) -> str:
    signature = hmac.new(token.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{signature}"


def _is_json_request(request: Request) -> bool:
    return request.headers.get(
        "HX-Request"
    ) != "true" and "application/json" in request.headers.get("accept", "")


async def require_operator(request: Request, settings: Settings = Depends(get_settings)) -> str:
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
        if hmac.compare_digest(token, settings.operator_token):
            return token
    cookie = request.cookies.get(COOKIE_NAME, "")
    try:
        exp, signature = cookie.split(".", 1)
        expected = _cookie_value(settings.operator_token, exp).split(".", 1)[1]
        valid = int(exp) >= int(time.time()) and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        valid = False
    if valid:
        return settings.operator_token
    if request.headers.get("HX-Request") == "true":
        # An expired cookie on a polling partial must not swap the login page into the
        # panel: tell HTMX to navigate the whole window instead.
        raise HTTPException(
            status_code=401,
            detail="operator authentication required",
            headers={"HX-Redirect": "/login"},
        )
    if (
        _is_json_request(request)
        or request.url.path.startswith("/api/")
        or request.url.path == "/metrics"
    ):
        raise HTTPException(status_code=401, detail="operator authentication required")
    raise OperatorAuthRequired
