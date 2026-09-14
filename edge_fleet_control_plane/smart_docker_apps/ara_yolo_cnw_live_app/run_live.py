#!/usr/bin/env python3
"""Container entry — YOLO CNW live annotated RTSPS stream."""

import sys

sys.path.insert(0, "/workspace")

from app.detect_and_stream import main

if __name__ == "__main__":
    raise SystemExit(main())
