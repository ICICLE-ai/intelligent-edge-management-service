#!/usr/bin/env python3
"""Container entry - runs YOLO faux-weed detection over MVS GigE cameras."""

import sys

sys.path.insert(0, "/workspace")

from app.main import main

if __name__ == "__main__":
    main()
