import os

import uvicorn

from . import create_app


def main() -> None:
    uvicorn.run(
        create_app(),
        host=os.environ.get("VERIFIER_HOST", "0.0.0.0"),
        port=int(os.environ.get("VERIFIER_PORT", "8080")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
