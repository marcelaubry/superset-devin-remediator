import hmac

from fastapi import Depends, HTTPException, Request

from ..config import Settings, get_settings


class OperatorAuthRequired(Exception):
    pass


def _is_json_request(request: Request) -> bool:
    return request.headers.get(
        "HX-Request"
    ) != "true" and "application/json" in request.headers.get("accept", "")


async def require_operator(request: Request, settings: Settings = Depends(get_settings)) -> str:
    token = request.cookies.get("operator_token")
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
    if token and hmac.compare_digest(token, settings.operator_token):
        return token
    if (
        _is_json_request(request)
        or request.url.path.startswith("/api/")
        or request.url.path == "/metrics"
    ):
        raise HTTPException(status_code=401, detail="operator authentication required")
    raise OperatorAuthRequired
