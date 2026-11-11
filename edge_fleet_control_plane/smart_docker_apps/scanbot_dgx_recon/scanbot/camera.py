"""Camera backends. Selected by [camera].type in config.toml.

  gopro       : GoPro HERO9+ (incl. HERO13) via the official Open GoPro HTTP API. Over USB the camera
                shows up as a network interface and is found automatically; over WiFi set ip = "10.5.5.9".
  gopro_hero4 : GoPro HERO4 over its own WiFi (camera at 10.5.5.9, legacy gpControl HTTP API).
                The Jetson must be on the GoPro's WiFi (second WiFi adapter recommended, or
                set wifi_switch so we hop onto the GoPro network for capture and back afterwards).
  stream      : record any RTSP/HTTP/UDP video stream with ffmpeg, e.g. an iPhone streaming app.
  file        : use an existing video file (testing / manual upload).

Every backend: start() -> begin recording, stop() -> end, fetch(dest_dir) -> Path to the video.
"""
import ipaddress
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import requests


class Camera:
    def __init__(self, cfg):
        self.cfg = cfg

    def prepare(self):
        pass

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def fetch(self, dest_dir):
        raise NotImplementedError

    def finish(self):
        pass


class FileCamera(Camera):
    def start(self):
        print(f"[camera/file] (pretend recording) will use {self.cfg['path']}")

    def stop(self):
        pass

    def fetch(self, dest_dir):
        dest = Path(dest_dir) / Path(self.cfg["path"]).name
        shutil.copy(Path(self.cfg["path"]).expanduser(), dest)
        return dest


class StreamCamera(Camera):
    """Records a network stream with ffmpeg (stream copy, no re-encode)."""

    def start(self):
        import imageio_ffmpeg
        self.tmp = Path(self.cfg.get("tmp_dir", "/tmp")) / f"scan_{int(time.time())}.mp4"
        cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-y"]
        if self.cfg["url"].startswith("rtsp"):
            cmd += ["-rtsp_transport", "tcp"]
        cmd += ["-i", self.cfg["url"], "-c", "copy", str(self.tmp)]
        print(f"[camera/stream] recording {self.cfg['url']}")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        time.sleep(2)
        if self.proc.poll() is not None:
            raise RuntimeError(f"ffmpeg could not open stream {self.cfg['url']}")

    def stop(self):
        self.proc.communicate(b"q", timeout=30)  # 'q' makes ffmpeg finalize the MP4

    def fetch(self, dest_dir):
        dest = Path(dest_dir) / self.tmp.name
        shutil.move(self.tmp, dest)
        return dest


class GoProHero4(Camera):
    """GoPro HERO4 Black/Silver via WiFi. Unofficial but long-stable gpControl API."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.ip = cfg.get("ip", "10.5.5.9")
        self.base = f"http://{self.ip}"

    # -- WiFi handling -------------------------------------------------------
    def _nmcli_up(self, conn):
        print(f"[camera/gopro] switching WiFi to '{conn}'")
        subprocess.run(["nmcli", "connection", "up", conn], check=True, capture_output=True)

    def prepare(self):
        if self.cfg.get("wifi_switch"):
            self._nmcli_up(self.cfg["gopro_wifi"])
        self._wake()
        self._cmd("/gp/gpControl/command/mode?p=0")  # video mode
        time.sleep(1)

    def finish(self):
        if self.cfg.get("wifi_switch"):
            self._nmcli_up(self.cfg["home_wifi"])
            time.sleep(5)

    # -- camera control ------------------------------------------------------
    def _cmd(self, path, timeout=10):
        r = requests.get(self.base + path, timeout=timeout)
        r.raise_for_status()
        return r

    def _wake(self):
        """HERO4 sleeps its WiFi API; a Wake-on-LAN packet to its MAC wakes it."""
        for _ in range(10):
            try:
                self._cmd("/gp/gpControl/status", timeout=3)
                return
            except requests.RequestException:
                if self.cfg.get("mac"):
                    mac = bytes.fromhex(self.cfg["mac"].replace(":", ""))
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                        s.sendto(b"\xff" * 6 + mac * 16, (self.ip, 9))
                time.sleep(2)
        raise RuntimeError(f"GoPro not reachable at {self.ip}: is the Jetson on the GoPro WiFi?")

    def _media(self):
        data = self._cmd("/gp/gpMediaList").json()
        return {f"{d['d']}/{f['n']}" for d in data.get("media", []) for f in d.get("fs", [])}

    def start(self):
        self.before = self._media()
        self._cmd("/gp/gpControl/command/shutter?p=1")
        print("[camera/gopro] recording")

    def stop(self):
        self._cmd("/gp/gpControl/command/shutter?p=0")
        print("[camera/gopro] stopped")

    def fetch(self, dest_dir):
        new = []
        for _ in range(15):  # camera needs a moment to close the file
            time.sleep(2)
            new = sorted(f for f in self._media() - self.before if f.upper().endswith(".MP4"))
            if new:
                break
        if not new:
            raise RuntimeError("GoPro: no new video file found after recording")
        # Long recordings are split into chapters (GOPR0001, GP010001, ...): download all of them.
        paths = []
        for name in new:
            dest = Path(dest_dir) / name.split("/")[-1]
            print(f"[camera/gopro] downloading {name}")
            with requests.get(f"http://{self.ip}:8080/videos/DCIM/{name}", stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            paths.append(dest)
            if self.cfg.get("delete_after_download"):
                self._cmd(f"/gp/gpControl/command/storage/delete?p=/{name}")
        return paths[0] if len(paths) == 1 else concat_videos(paths, Path(dest_dir) / "joined.mp4")


class GoPro(Camera):
    """GoPro HERO9+ via Open GoPro (https://gopro.github.io/OpenGoPro/). Everything is HTTP on port 8080.

    USB: camera sets USB Connection = GoPro Connect, then acts as a DHCP server on a USB network
    interface. The Jetson gets 172.2X.1YZ.N and the camera is 172.2X.1YZ.51 (XYZ = last serial digits).
    """

    def __init__(self, cfg):
        super().__init__(cfg)
        self.ip = cfg.get("ip") or find_gopro_usb_ip()
        self.base = f"http://{self.ip}:8080"

    def _get(self, path, timeout=10, **kw):
        r = requests.get(self.base + path, timeout=timeout, **kw)
        r.raise_for_status()
        return r

    def state(self):
        return self._get("/gopro/camera/state").json()

    def _wait_idle(self, timeout=60):
        """Status 8 = busy, 10 = encoding. Commands sent while either is set are rejected."""
        end = time.time() + timeout
        while time.time() < end:
            st = self.state()["status"]
            if not st.get("8") and not st.get("10"):
                return
            time.sleep(0.5)
        raise RuntimeError("GoPro stayed busy/encoding")

    def prepare(self):
        for _ in range(10):
            try:
                self._get("/gopro/camera/control/wired_usb?p=1", timeout=3)  # take control over USB
                break
            except requests.RequestException:
                time.sleep(2)
        else:
            raise RuntimeError(f"GoPro not reachable at {self.ip}:8080")
        self._get("/gopro/camera/keep_alive")
        self._wait_idle()
        self._get("/gopro/camera/presets/set_group?id=1000")  # video preset group
        time.sleep(1)

    def _media(self):
        data = self._get("/gopro/media/list").json()
        return {f"{d['d']}/{f['n']}" for d in data.get("media", []) for f in d.get("fs", [])}

    def start(self):
        self._wait_idle()
        self.before = self._media()
        self._get("/gopro/camera/shutter/start")
        print("[camera/gopro] recording")

    def stop(self):
        self._get("/gopro/camera/shutter/stop")
        print("[camera/gopro] stopped")

    def fetch(self, dest_dir):
        self._wait_idle()
        new = []
        for _ in range(15):  # media list can lag a moment behind the file being closed
            new = sorted(f for f in self._media() - self.before if f.upper().endswith(".MP4"))
            if new:
                break
            time.sleep(2)
        if not new:
            raise RuntimeError("GoPro: no new video file found after recording")
        # Long recordings are split into chapters (GX010001, GX020001, ...), which sort in order.
        paths = []
        for name in new:
            dest = Path(dest_dir) / name.split("/")[-1]
            print(f"[camera/gopro] downloading {name}")
            with self._get(f"/videos/DCIM/{name}", stream=True, timeout=60) as r:
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            paths.append(dest)
            if self.cfg.get("delete_after_download"):
                self._get(f"/gopro/media/delete/file?path={name}")
        return paths[0] if len(paths) == 1 else concat_videos(paths, Path(dest_dir) / "joined.mp4")


def find_gopro_usb_ip():
    """The GoPro USB network is 172.2X.1YZ.0/24; return .51 on whichever local interface is in it."""
    out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True).stdout
    for m in re.finditer(r"inet (172\.2\d\.1\d\d\.\d+)/", out):
        return str(ipaddress.ip_address(m.group(1).rsplit(".", 1)[0] + ".51"))
    raise RuntimeError("No GoPro USB network found: is the camera on, plugged in with a data cable, "
                       "and set to Preferences > Connections > USB Connection > GoPro Connect?")


def concat_videos(paths, dest):
    """Join GoPro chapter files losslessly (ffmpeg concat demuxer, stream copy)."""
    import imageio_ffmpeg
    lst = Path(dest).with_suffix(".txt")
    lst.write_text("".join(f"file '{Path(p).resolve()}'\n" for p in paths))
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(dest)], check=True)
    return Path(dest)


def make_camera(cfg):
    kind = cfg.get("type", "file")
    return {"file": FileCamera, "stream": StreamCamera, "gopro": GoPro, "gopro_hero4": GoProHero4}[kind](cfg)
