"""Attach per-frame masks to a nerfstudio transforms.json (masked-out pixels are ignored in training).

  python scripts/add_masks_to_transforms.py NERF_DATA_DIR MASK_DIR

Copies MASK_DIR/<stem>.png to NERF_DATA_DIR/masks/ and sets "mask_path" on each frame.
"""
import json
import shutil
import sys
from pathlib import Path

data, masks = Path(sys.argv[1]), Path(sys.argv[2])
tf = json.loads((data / "transforms.json").read_text())
(data / "masks").mkdir(exist_ok=True)
n = 0
for fr in tf["frames"]:
    m = masks / (Path(fr["file_path"]).stem + ".png")
    if m.exists():
        shutil.copy(m, data / "masks" / m.name)
        fr["mask_path"] = f"masks/{m.name}"
        n += 1
(data / "transforms.json").write_text(json.dumps(tf, indent=2))
print(f"masks attached to {n}/{len(tf['frames'])} frames")
