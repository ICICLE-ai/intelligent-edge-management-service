#!/bin/sh
# Installed to /etc/X11/Xsession.d/ by install.sh
# Runs once at each graphical login so Docker containers can open the host display.
if command -v xhost >/dev/null 2>&1; then
  xhost +local:root 2>/dev/null || true
  xhost +local:docker 2>/dev/null || true
fi
