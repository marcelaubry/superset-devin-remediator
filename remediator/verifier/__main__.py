import os

import uvicorn

from . import create_app


def main() -> None:
    uvicorn.run(
        create_app(),
        host=os.environ.get("VERIFIER_HOST", "0.0.0.0"),
        port=int(os.environ.get("VERIFIER_PORT", "8080")),
        log_level="info",
        # Stay on the stdlib loop: under uvloop a probe that leaves any detached descendant
        # (even one with stdio redirected) keeps the child's stdio socketpair alive, so
        # `Process.wait()` never returns and every such probe is misreported as a timeout.
        loop="asyncio",
    )


if __name__ == "__main__":
    main()
