#!/usr/bin/env python3
"""Container entry — runs UNet++ multi-GigE live streaming."""

import sys

sys.path.insert(0, "/workspace")

from app.main import main

if __name__ == "__main__":
    main()
