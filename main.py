from __future__ import annotations

import sys
from pathlib import Path


def _ensure_src_on_path() -> None:
    src_path = Path(__file__).resolve().parent / "src"
    src_text = str(src_path)
    if src_text not in sys.path:
        sys.path.insert(0, src_text)


def main() -> None:
    _ensure_src_on_path()
    from voicebridge.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
