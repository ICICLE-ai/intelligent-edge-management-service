"""Export per-device deployment timings from the control-plane database.

One CSV row per (command, device) for ``deploy_model`` and
``restart_deployment``. Each row combines:

* server-clock timestamps: command recorded, MQTT published, and the arrival
  of each agent ack (DOWNLOADING, PULLING, STARTING, RUNNING / FAILED);
* device-clock phase durations reported by agent v2.1.0+ in ``timings``
  (queue, resolve, download, pull, run, total, cache hits).

Server and device clocks are never subtracted from each other, so the
results are not affected by clock skew between the device and the server.

Run from ``edge_fleet_control_plane/`` against whatever database the app is
configured for (``DATABASE_URL`` / ``DB_*`` for PostgreSQL, otherwise the
local SQLite file):

    python eval/export_deployment_timings.py --since 2026-10-05 \\
        --out eval/results/deployment_timings.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.time import parse_iso  # noqa: E402
from app.db.session import fetch_all  # noqa: E402

OPERATIONS = ("deploy_model", "restart_deployment")
ACK_PHASES = ("DOWNLOADING", "PULLING", "STARTING")
TERMINAL = ("RUNNING", "FAILED")
AGENT_FIELDS = (
    "agent_version", "queue_s", "resolve_s", "download_s", "pull_s", "run_s",
    "total_s", "artifact_cached", "image_cached", "artifact_bytes",
)

COLUMNS = [
    "request_id", "operation", "deployment_uid", "device_uid", "generation_uid",
    "model_card_uid", "target_type", "final_status", "error",
    "command_created_at", "mqtt_sent_at",
    "ack_downloading_at", "ack_pulling_at", "ack_starting_at", "ack_terminal_at",
    "dispatch_s", "end_to_end_s", "overhead_s",
    *(f"agent_{f}" for f in AGENT_FIELDS),
]


def _seconds(start: Optional[str], end: Optional[str]) -> Optional[float]:
    a, b = parse_iso(start), parse_iso(end)
    if not a or not b:
        return None
    return round((b - a).total_seconds(), 4)


def _load_commands(since: Optional[str], until: Optional[str]) -> List[dict]:
    sql = """
        SELECT c.request_id, c.operation, c.deployment_uid, c.device_uid,
               c.target_type, c.created_at, c.sent_at, dep.model_card_uid
          FROM device_commands c
          LEFT JOIN deployments dep ON dep.deployment_uid = c.deployment_uid
         WHERE c.operation IN (?, ?)
    """
    params: List[Any] = list(OPERATIONS)
    if since:
        sql += " AND c.created_at >= ?"
        params.append(since)
    if until:
        sql += " AND c.created_at < ?"
        params.append(until)
    sql += " ORDER BY c.created_at"
    return [dict(r) for r in fetch_all(sql, params)]


def _load_acks(deployment_uids: Iterable[str]) -> Dict[Tuple[str, str], List[dict]]:
    """Ack events keyed by (request_id, device_uid), oldest first."""
    acks: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    uids = sorted({u for u in deployment_uids if u})
    for i in range(0, len(uids), 200):
        chunk = uids[i:i + 200]
        rows = fetch_all(
            f"""
            SELECT device_uid, event_type, payload_json, created_at
              FROM events
             WHERE event_type LIKE 'AGENT_ACK_%'
               AND deployment_uid IN ({", ".join("?" for _ in chunk)})
             ORDER BY created_at
            """,
            chunk,
        )
        for r in rows:
            r = dict(r)
            try:
                payload = json.loads(r.get("payload_json") or "{}")
            except ValueError:
                continue
            request_id = payload.get("request_id")
            device_uid = r.get("device_uid") or payload.get("device_id")
            if request_id and device_uid:
                acks[(request_id, device_uid)].append(
                    {"at": r["created_at"], "payload": payload}
                )
    return acks


def _generations() -> Dict[str, str]:
    rows = fetch_all("SELECT device_uid, generation_uid FROM devices")
    return {r["device_uid"]: r["generation_uid"] for r in (dict(x) for x in rows)}


def build_rows(since: Optional[str] = None, until: Optional[str] = None) -> List[dict]:
    commands = _load_commands(since, until)
    acks = _load_acks(c["deployment_uid"] for c in commands)
    gens = _generations()
    by_request: Dict[str, List[Tuple[str, List[dict]]]] = defaultdict(list)
    for (request_id, device_uid), events in acks.items():
        by_request[request_id].append((device_uid, events))

    rows: List[dict] = []
    for cmd in commands:
        # Group broadcasts have no device_uid on the command; one row per acking device.
        devices = by_request.get(cmd["request_id"]) or [(cmd["device_uid"], [])]
        for device_uid, events in devices:
            if cmd["device_uid"] and device_uid != cmd["device_uid"]:
                continue
            seen: Dict[str, dict] = {}
            for ev in events:
                status = (ev["payload"].get("status") or "").upper()
                seen.setdefault(status, ev)
            terminal = next((seen[s] for s in TERMINAL if s in seen), None)
            final_status = (terminal["payload"].get("status") or "").upper() if terminal else "INCOMPLETE"
            last = terminal or (events[-1] if events else None)
            timings = (last or {}).get("payload", {}).get("timings") or {}

            row = {
                "request_id": cmd["request_id"],
                "operation": cmd["operation"],
                "deployment_uid": cmd["deployment_uid"],
                "device_uid": device_uid,
                "generation_uid": gens.get(device_uid or ""),
                "model_card_uid": cmd.get("model_card_uid"),
                "target_type": cmd["target_type"],
                "final_status": final_status,
                "error": (terminal or {}).get("payload", {}).get("error"),
                "command_created_at": cmd["created_at"],
                "mqtt_sent_at": cmd["sent_at"],
                "ack_terminal_at": terminal["at"] if terminal else None,
            }
            for phase in ACK_PHASES:
                row[f"ack_{phase.lower()}_at"] = seen[phase]["at"] if phase in seen else None
            row["dispatch_s"] = _seconds(cmd["created_at"], cmd["sent_at"])
            row["end_to_end_s"] = _seconds(cmd["created_at"], row["ack_terminal_at"])
            for field in AGENT_FIELDS:
                row[f"agent_{field}"] = timings.get(field)
            # Time not spent executing on the device: control-plane dispatch,
            # MQTT delivery, and the final ack's HTTPS round trip.
            if row["end_to_end_s"] is not None and timings.get("total_s") is not None:
                row["overhead_s"] = round(row["end_to_end_s"] - timings["total_s"], 4)
            else:
                row["overhead_s"] = None
            rows.append(row)
    return rows


def _fmt(values: List[float]) -> str:
    if not values:
        return "-"
    values = sorted(values)
    p90 = values[min(len(values) - 1, int(round(0.9 * (len(values) - 1))))]
    return f"{statistics.median(values):8.2f} {p90:8.2f}"


def print_summary(rows: List[dict]) -> None:
    groups: Dict[tuple, List[dict]] = defaultdict(list)
    for r in rows:
        if r["final_status"] != "RUNNING":
            continue
        key = (r["model_card_uid"], r["generation_uid"], r["operation"],
               r["agent_artifact_cached"], r["agent_image_cached"])
        groups[key].append(r)
    failed = sum(1 for r in rows if r["final_status"] == "FAILED")
    incomplete = sum(1 for r in rows if r["final_status"] == "INCOMPLETE")
    print(f"{len(rows)} rows: {len(rows) - failed - incomplete} RUNNING, "
          f"{failed} FAILED, {incomplete} INCOMPLETE")
    if not groups:
        return
    print("\nmedian / p90 seconds, successful runs only")
    header = (f"{'model':34} {'generation':24} {'operation':19} {'art$':>5} {'img$':>5} {'n':>3} "
              f"{'end_to_end':>17} {'download':>17} {'pull':>17} {'run':>17}")
    print(header)
    print("-" * len(header))
    for key in sorted(groups, key=lambda k: tuple(str(x) for x in k)):
        rs = groups[key]

        def col(name: str) -> List[float]:
            return [r[name] for r in rs if isinstance(r[name], (int, float))]

        model, gen, op, art, img = key
        print(f"{str(model)[:34]:34} {str(gen)[:24]:24} {op:19} {str(art):>5} {str(img):>5} {len(rs):>3} "
              f"{_fmt(col('end_to_end_s')):>17} {_fmt(col('agent_download_s')):>17} "
              f"{_fmt(col('agent_pull_s')):>17} {_fmt(col('agent_run_s')):>17}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since", help="ISO date/time; only commands created at or after this")
    parser.add_argument("--until", help="ISO date/time; only commands created before this")
    parser.add_argument("--out", default=str(ROOT / "eval" / "results" / "deployment_timings.csv"))
    args = parser.parse_args()

    rows = build_rows(args.since, args.until)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {out}")
    print_summary(rows)


if __name__ == "__main__":
    main()
