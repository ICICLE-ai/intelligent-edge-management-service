#!/usr/bin/env bash
# Fixed-camera turntable capture -> masks -> COLMAP -> pose video -> nerfacto -> orbit video.
#
#   scripts/recon_turntable.sh RAW_FRAMES_DIR OUT_DIR
#
# Produces:
#   OUT/proc/{images,masks,masks_hull}     1920px frames + masks (tight for COLMAP, hull for NeRF)
#   OUT/colmap/sparse/0                    COLMAP model (single RADIAL camera)
#   OUT/nerf/                              ns-process-data output (transforms.json with mask_path)
#   OUT/nerf/outputs/.../config.yml + nerfstudio_models/*.ckpt
#   OUT/videos/colmap_poses.mp4, OUT/videos/nerf_orbit.mp4
# frames.csv (from extract_frames.py) is read from RAW/ or RAW/.. unless FRAMES_CSV is set.
# Env knobs: MAX_ITERS (default 30000), STAGES (default "masks colmap posevid nerf render").
set -euo pipefail
RAW=$(realpath "$1"); OUT=$(mkdir -p "$2" && realpath "$2")
HERE=$(dirname "$(realpath "$0")")
MAX_ITERS=${MAX_ITERS:-30000}
STAGES=${STAGES:-"masks colmap posevid nerf render"}
# The DGX host sources a micromamba env here. Inside the IEMS image the tools
# are already on PATH, and a missing activate.sh must not abort the job.
if [ -f "$HERE/../activate.sh" ]; then
  # shellcheck disable=SC1091
  source "$HERE/../activate.sh" >/dev/null 2>&1 || true
fi
has() { [[ " $STAGES " == *" $1 "* ]]; }
log() { echo "[recon $(date +%H:%M:%S)] $*"; }
mkdir -p "$OUT/videos"

if has masks; then
  log "masks"
  python "$HERE/turntable_masks.py" "$RAW" "$OUT/proc" --dilate 15
fi

if has colmap; then
  log "colmap"
  C=$OUT/colmap; rm -rf "$C"; mkdir -p "$C/cmasks" "$C/sparse_all" "$C/sparse"
  for f in "$OUT"/proc/masks/*.png; do b=$(basename "$f" .png); ln -sf "$f" "$C/cmasks/$b.jpg.png"; done
  # One RADIAL camera (fx=fy) with the focal length FIXED (FOCAL, default 900 px at 1920 wide =
  # GoPro HERO13 Wide, HyperSmooth off). A free focal length lets the small turntable scene trade focal
  # for rotation: the loop failed to close by ~40 deg. A fixed 900 px closes it to ~5 deg at the lowest
  # reprojection error. A lower init triangulation angle is needed: neighboring frames are ~4 deg apart.
  colmap feature_extractor --database_path "$C/database.db" --image_path "$OUT/proc/images" \
    --ImageReader.mask_path "$C/cmasks" --ImageReader.single_camera 1 \
    --ImageReader.camera_model RADIAL --ImageReader.camera_params "${FOCAL:-900},960,540,0,0" >"$C/log.txt" 2>&1
  colmap exhaustive_matcher --database_path "$C/database.db" --SiftMatching.guided_matching 1 >>"$C/log.txt" 2>&1
  colmap mapper --database_path "$C/database.db" --image_path "$OUT/proc/images" --output_path "$C/sparse_all" \
    --Mapper.init_min_tri_angle 4 --Mapper.ba_refine_principal_point 0 --Mapper.ba_refine_focal_length 0 >>"$C/log.txt" 2>&1
  # The mapper can leave small partial models next to the full one; keep the largest as sparse/0.
  best=$(for m in "$C"/sparse_all/*/; do
    echo "$(colmap model_analyzer --path "$m" 2>&1 | grep -oP 'Registered images: \K\d+') $m"; done | sort -n | tail -1)
  log "largest model: $best of $(ls "$OUT/proc/images" | wc -l) images"
  mkdir -p "$C/raw_model" && cp -r "${best#* }"/. "$C/raw_model/"
  colmap model_converter --input_path "$C/raw_model" --output_path "$C/raw_model" --output_type TXT >/dev/null 2>&1
  # Drop still duplicates at the ends and frames off the smooth angle-vs-time curve, then re-adjust.
  CSV=${FRAMES_CSV:-$( [ -f "$RAW/frames.csv" ] && echo "$RAW/frames.csv" || echo "$RAW/../frames.csv")}
  python "$HERE/filter_turntable_poses.py" "$C/raw_model" "$CSV" "$C/drop.txt"
  mkdir -p "$C/filtered" "$C/sparse/0"
  colmap image_deleter --input_path "$C/raw_model" --output_path "$C/filtered" --image_names_path "$C/drop.txt" >>"$C/log.txt" 2>&1
  colmap bundle_adjuster --input_path "$C/filtered" --output_path "$C/sparse/0" \
    --BundleAdjustment.refine_principal_point 0 --BundleAdjustment.refine_focal_length 0 >>"$C/log.txt" 2>&1
  colmap model_converter --input_path "$C/sparse/0" --output_path "$C/sparse/0" --output_type TXT >/dev/null 2>&1
  colmap model_analyzer --path "$C/sparse/0" 2>&1 | grep -E "Registered images|Points|Mean reprojection" | sed 's/^.*\] /[recon] /'
fi

if has posevid; then
  log "pose video"
  python "$HERE/colmap_pose_video.py" "$OUT/colmap/sparse/0" "$OUT/proc/images" "$OUT/videos/colmap_poses.mp4" \
    --masks "$OUT/proc/masks"
fi

if has nerf; then
  log "nerf: process"
  N=$OUT/nerf; rm -rf "$N"; mkdir -p "$N"
  ns-process-data images --data "$OUT/proc/images" --output-dir "$N" --skip-colmap \
    --colmap-model-path "$OUT/colmap/sparse/0" --num-downscales 0 >"$N/process.log" 2>&1
  python "$HERE/add_masks_to_transforms.py" "$N" "$OUT/proc/masks_hull"
  log "nerf: train nerfacto ($MAX_ITERS iters)"
  # max-num-iterations: with ~10% mask coverage the default 100 rejection-sampling tries occasionally run
  # out, and nerfstudio's fallback path then crashes (IndexError in collate_image_dataset_batch_list).
  (cd "$N" && ns-train nerfacto --data . --output-dir outputs --experiment-name scan \
    --max-num-iterations "$MAX_ITERS" --vis tensorboard --viewer.quit-on-train-completion True \
    --pipeline.model.background-color random \
    --pipeline.datamanager.pixel-sampler.max-num-iterations 1000 \
    nerfstudio-data --orientation-method up --center-method poses --auto-scale-poses True >train.log 2>&1)
fi

if has render; then
  log "nerf: orbit video"
  # The config stores data paths relative to $OUT/nerf (ns-train ran there), so render from there too.
  cfg=$(cd "$OUT/nerf" && ls -t outputs/scan/nerfacto/*/config.yml | head -1)
  (cd "$OUT/nerf" && python "$HERE/nerf_orbit_video.py" "$cfg" "$OUT/videos/nerf_orbit.mp4")
fi
log "done -> $OUT"
