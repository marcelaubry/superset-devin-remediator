from ..config import Settings
from .client import DevinClient
from .fake import FakeDevinClient


def build_devin_client(settings: Settings) -> DevinClient:
    if settings.devin_client == "fake":
        return FakeDevinClient()
    raise NotImplementedError("real Devin client is Phase 2")
