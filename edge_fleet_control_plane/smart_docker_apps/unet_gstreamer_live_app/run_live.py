#!/usr/bin/env python3
"""Container entry — UNet++ CSI live stream (single or multi-camera)."""

import sys

sys.path.insert(0, "/workspace")

from app.main import main

if __name__ == "__main__":
    main()
