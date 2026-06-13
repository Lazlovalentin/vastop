"""Entry point: ``python -m vastai_operator`` runs kopf with our handlers."""

from __future__ import annotations

import logging

import kopf

from . import handlers  # noqa: F401 — import for handler registration side effects


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    kopf.run(standalone=True, clusterwide=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
