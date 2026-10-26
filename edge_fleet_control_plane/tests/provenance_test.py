"""Provenance classification: one case per row of the decision table.

Run with:
    python tests/provenance_test.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import provenance_service as p  # noqa: E402

A = "aa" * 32
B = "bb" * 32
C = "cc" * 32
D = "dd" * 32

CASES = [
    ("both declared digests present and equal",
     dict(declared_sha256=A, observed_sha256=A, declared_digest="sha256:" + C, observed_digest=C),
     p.MATCHED),
    ("repo@sha256: and sha256: prefixes are the same digest",
     dict(declared_sha256=A, observed_sha256=A.upper(),
          declared_digest="sha256:" + C, observed_digest="habg21/app@sha256:" + C),
     p.MATCHED),
    ("only the file hash declared, and it matches",
     dict(declared_sha256=A, observed_sha256=A, observed_digest=C),
     p.MATCHED),
    ("artifact hash differs",
     dict(declared_sha256=A, observed_sha256=B, declared_digest=C, observed_digest=C),
     p.DRIFT),
    ("image digest differs",
     dict(declared_sha256=A, observed_sha256=A, declared_digest=C, observed_digest=D),
     p.DRIFT),
    ("one digest matches, the other declared field was not observed",
     dict(declared_sha256=A, observed_sha256=A, declared_digest=C),
     p.PARTIAL),
    ("declared and observed records hold different, noncomparable fields",
     dict(declared_sha256=A, observed_digest=C),
     p.UNVERIFIED),
    ("observations exist but nothing was declared",
     dict(observed_sha256=A, observed_digest=C),
     p.UNDECLARED),
    ("observations absent",
     dict(declared_sha256=A, declared_digest=C),
     p.UNKNOWN),
    ("observations absent and nothing declared",
     dict(),
     p.UNKNOWN),
    ("observed digest is malformed",
     dict(declared_sha256=A, observed_sha256="not-a-hash"),
     p.INVALID),
    ("declared digest is malformed",
     dict(declared_sha256=A[:40], observed_sha256=A),
     p.INVALID),
    ("malformed field alongside a matching field is not MATCHED",
     dict(declared_sha256=A, observed_sha256=A, declared_digest=C, observed_digest="sha256:xyz"),
     p.INVALID),
    ("a proven mismatch outranks a malformed field",
     dict(declared_sha256=A, observed_sha256=B, declared_digest=C, observed_digest="sha256:xyz"),
     p.DRIFT),
]

failures = []
for name, kwargs, expected in CASES:
    status, reasons = p.compare(**kwargs)
    if status != expected:
        failures.append("%s: expected %s, got %s %s" % (name, expected, status, reasons))
    elif status != p.MATCHED and not reasons:
        failures.append("%s: %s without a reason" % (name, status))

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("provenance_test OK (%d cases)" % len(CASES))
