"""Create Sidq's receipt schema and its two visible DataHub tags."""

from __future__ import annotations

import json

from sidq.receipt.bootstrap import ensure_sidq_properties


def main() -> None:
    print(json.dumps(ensure_sidq_properties(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
