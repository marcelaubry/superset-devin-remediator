from ..config import Settings
from .client import DevinClient
from .fake import FakeDevinClient
from .live import LiveDevinClient


def build_devin_client(settings: Settings) -> DevinClient:
    if not settings.live_mode:
        return FakeDevinClient()
    if settings.devin_api_key is None or not settings.devin_org_id:
        raise RuntimeError("DEVIN_CLIENT_MODE=live requires DEVIN_API_KEY and DEVIN_ORG_ID")
    return LiveDevinClient(
        api_key=settings.devin_api_key.get_secret_value(),
        org_id=settings.devin_org_id,
        base_url=settings.devin_api_base_url,
        repos_format=settings.devin_repos_format,
        request_timeout_seconds=settings.devin_http_timeout_seconds,
        max_retries=settings.devin_http_max_retries,
    )
