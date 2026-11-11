"""Render a synthetic orbit video of a textured box-in-a-room scene (pipeline smoke test)."""
import numpy as np, torch, torch.nn.functional as F, subprocess, sys, os
out = sys.argv[1] if len(sys.argv) > 1 else "data/synthetic/orbit.mp4"
os.makedirs(os.path.dirname(out), exist_ok=True)
d = "cuda"; g = torch.Generator(device=d).manual_seed(0)
def tex(n=512):  # multi-scale colorful noise -> rich SIFT features
    t = sum(F.interpolate(torch.rand(1, 3, s, s, device=d, generator=g), size=n, mode="bilinear") for s in (8, 32, 128, 512))
    return (t / 4).clamp(0, 1)[0]
# axis-aligned boxes: (min, max, texture); inside of the room + a box in the middle
boxes = [(torch.tensor([-4., -4., -1.]), torch.tensor([4., 4., 5.]), tex()),
         (torch.tensor([-0.7, -0.7, -1.]), torch.tensor([0.7, 0.7, 0.6]), tex()),
         (torch.tensor([1.2, -0.3, -1.]), torch.tensor([1.8, 0.3, 1.2]), tex())]
W, H, f = 640, 480, 500.
ys, xs = torch.meshgrid(torch.arange(H, device=d), torch.arange(W, device=d), indexing="ij")
dirs_cam = torch.stack([(xs - W / 2) / f, -(ys - H / 2) / f, -torch.ones_like(xs, dtype=torch.float)], -1)
frames = []
for i in range(120):
    a = 2 * np.pi * i / 120
    eye = torch.tensor([2.8 * np.cos(a), 2.8 * np.sin(a), 1.2 + 0.4 * np.sin(2 * a)], device=d, dtype=torch.float32)
    fwd = -eye / eye.norm(); right = torch.linalg.cross(fwd, torch.tensor([0., 0., 1.], device=d)); right /= right.norm()
    up = torch.linalg.cross(right, fwd)
    rd = dirs_cam[..., :1] * right + dirs_cam[..., 1:2] * up - dirs_cam[..., 2:3] * fwd
    rd = rd / rd.norm(dim=-1, keepdim=True)
    best_t = torch.full((H, W), 1e9, device=d); img = torch.zeros(H, W, 3, device=d)
    for k, (lo, hi, T) in enumerate(boxes):
        lo, hi = lo.to(d), hi.to(d)
        t0 = (lo - eye) / rd; t1 = (hi - eye) / rd
        tmin = torch.minimum(t0, t1).amax(-1); tmax = torch.maximum(t0, t1).amin(-1)
        t = tmax if k == 0 else torch.where(tmin > 0, tmin, torch.full_like(tmin, 1e9))  # room: inside walls
        hit = (tmax >= tmin) & (t > 0) & (t < best_t)
        p = eye + rd * t[..., None]
        n = (p - (lo + hi) / 2) / ((hi - lo) / 2)
        ax = n.abs().argmax(-1)  # face axis -> pick the other two coordinates as UV
        uv_idx = torch.stack([(ax + 1) % 3, (ax + 2) % 3], -1)
        uv = torch.gather((p - lo) / (hi - lo), -1, uv_idx) * 2 - 1
        c = F.grid_sample(T[None], uv[None], align_corners=False)[0].permute(1, 2, 0)
        c = c * (0.7 + 0.3 * (ax[..., None] == 2).float())  # simple face shading
        img = torch.where(hit[..., None], c, img); best_t = torch.where(hit, t, best_t)
    frames.append((img.clamp(0, 1) * 255).byte().cpu().numpy())
p = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                      "-r", "24", "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "16", out], stdin=subprocess.PIPE)
p.communicate(np.stack(frames).tobytes()); print("wrote", out)
