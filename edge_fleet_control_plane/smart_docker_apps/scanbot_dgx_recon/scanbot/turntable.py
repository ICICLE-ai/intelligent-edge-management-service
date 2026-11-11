"""Turntable backends. Selected by [turntable].type in config.toml.

  continuous : turntable spins on its own (power/remote). We only time the recording.
  comxim_ascii: ComXim over USB serial (CH340, 115200), ASCII protocol found by probing
               (tools/capture_test.py): CT+START(1,1,1,<angle>,<pause>,<times>); -> ... CR+EVENT=TB_END;
  comxim     : ComXim with raw hex start/stop commands from config (legacy, before the protocol was known).
  sim        : prints only, for testing. time_scale = 0.05 makes a "rotation" 20x faster.

The agent calls rotate(rotations), which blocks until the turn is finished.
"""
import time


class Turntable:
    def __init__(self, cfg):
        self.cfg = cfg
        self.seconds_per_rev = float(cfg.get("seconds_per_rev", 30))

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass

    def rotate(self, rotations, seconds=None):
        """Turn `rotations` times (or for `seconds`) and return when done."""
        self.start()
        try:
            time.sleep(seconds or rotations * self.seconds_per_rev)
        finally:
            self.stop()


class SimTurntable(Turntable):
    def start(self):
        print("[turntable/sim] start")

    def stop(self):
        print("[turntable/sim] stop")

    def rotate(self, rotations, seconds=None):
        t = (seconds or rotations * self.seconds_per_rev) * float(self.cfg.get("time_scale", 1))
        print(f"[turntable/sim] rotating {rotations} rev ({t:.1f}s simulated)")
        self.start()
        time.sleep(t)
        self.stop()


class ContinuousTurntable(Turntable):
    """The table is already spinning (or the operator starts it); nothing to send."""

    def start(self):
        if self.cfg.get("prompt_operator", False):
            input("[turntable] Make sure the turntable is spinning, then press Enter... ")


class ComXimTurntable(Turntable):
    """ComXim over USB serial. Commands are hex strings in config, e.g. start_cmd = "AA 01 55"."""

    def __init__(self, cfg):
        super().__init__(cfg)
        import serial  # pyserial
        for key in ("start_cmd", "stop_cmd"):
            if not cfg.get(key):
                raise RuntimeError(f"[turntable] comxim needs '{key}' in config; protocol not known yet. "
                                   "Use type = \"continuous\" until it is.")
        self.port = serial.Serial(cfg.get("port", "/dev/ttyUSB0"), int(cfg.get("baud", 9600)), timeout=1)
        if cfg.get("speed_cmd"):
            self._send(cfg["speed_cmd"])

    def _send(self, hexcmd):
        data = bytes.fromhex(hexcmd)
        self.port.write(data)
        reply = self.port.read(64)
        print(f"[turntable] sent {data.hex(' ')} reply {reply.hex(' ') or '-'}")

    def start(self):
        self._send(self.cfg["start_cmd"])

    def stop(self):
        self._send(self.cfg["stop_cmd"])

    def close(self):
        self.port.close()


class ComXimAscii(Turntable):
    """ComXim ASCII protocol (115200 8N1). One command turns `angle` degrees `times` times with `pause` s
    between, then the table reports TB_END. Command template is configurable as rotate_cmd."""

    def __init__(self, cfg):
        super().__init__(cfg)
        import serial  # pyserial
        self.port = serial.Serial(cfg.get("port", "/dev/ttyUSB0"), int(cfg.get("baud", 115200)), timeout=0.5)

    def rotate(self, rotations, seconds=None):
        times = max(1, round(rotations))
        angle = round(360 * rotations / times)
        cmd = self.cfg.get("rotate_cmd", "CT+START(1,1,1,{angle},{pause},{times});").format(
            angle=angle, pause=int(self.cfg.get("pause", 1)), times=times)
        timeout = float(self.cfg.get("timeout_s", 30 + 2 * times * self.seconds_per_rev))
        self.port.reset_input_buffer()
        self.port.write(cmd.encode())
        print(f"[turntable] sent {cmd}")
        t0, buf = time.time(), b""
        while time.time() - t0 < timeout:
            buf += self.port.read(64)
            if b"ERR" in buf:
                raise RuntimeError(f"turntable error: {buf.replace(b'#', b'').decode(errors='replace')}")
            if b"TB_END" in buf:
                print(f"[turntable] done in {time.time() - t0:.1f}s")
                return
        raise RuntimeError(f"turntable: no TB_END after {timeout:.0f}s (got {buf[-80:]!r})")

    def close(self):
        self.port.close()


def make_turntable(cfg):
    kind = cfg.get("type", "continuous")
    return {"sim": SimTurntable, "continuous": ContinuousTurntable, "comxim": ComXimTurntable,
            "comxim_ascii": ComXimAscii}[kind](cfg)
