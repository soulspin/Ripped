"""Media analysis via ffprobe, including DVD structures (VIDEO_TS / ISO)."""

from __future__ import annotations

import json
import os
import struct
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

VIDEO_EXTS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mpg", ".mpeg", ".vob", ".vro", ".m2v",
    ".m1v", ".dat", ".ts", ".tp", ".trp", ".m2t", ".m2ts", ".mts", ".mov",
    ".wmv", ".flv", ".webm", ".ogm", ".divx", ".3gp", ".iso", ".ifo",
}

TEXT_SUB_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text", "srt"}
BITMAP_SUB_CODECS = {"dvd_subtitle", "hdmv_pgs_subtitle", "dvb_subtitle", "xsub"}


class ProbeError(RuntimeError):
    pass


def parse_ratio(value: str | None) -> Fraction:
    if not value:
        return Fraction(0)
    for sep in (":", "/"):
        if sep in value:
            a, b = value.split(sep, 1)
            try:
                a, b = int(a), int(b)
            except ValueError:
                return Fraction(0)
            return Fraction(a, b) if b else Fraction(0)
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError):
        return Fraction(0)


@dataclass
class Stream:
    index: int
    kind: str  # video | audio | subtitle
    codec: str
    lang: str = ""
    title: str = ""
    channels: int = 0
    bit_rate: int = 0
    sample_rate: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    avg_fps: float = 0.0
    sar: Fraction = field(default_factory=lambda: Fraction(1))
    field_order: str = ""
    forced: bool = False
    color_space: str = ""

    @property
    def is_text_sub(self) -> bool:
        return self.codec in TEXT_SUB_CODECS

    @property
    def is_bitmap_sub(self) -> bool:
        return self.codec in BITMAP_SUB_CODECS

    @property
    def dar(self) -> float:
        if not self.height:
            return 0.0
        sar = self.sar if self.sar > 0 else Fraction(1)
        return float(self.width * sar / self.height)

    def label(self) -> str:
        lang = f"[{self.lang}] " if self.lang and self.lang != "und" else ""
        title = f" – {self.title}" if self.title else ""
        if self.kind == "video":
            return (f"#{self.index} {self.codec.upper()} {self.width}×{self.height} "
                    f"{self.fps:.3f} fps, DAR {self.dar:.2f}")
        if self.kind == "audio":
            ch = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(self.channels, f"{self.channels} ch")
            br = f", {self.bit_rate // 1000} kbps" if self.bit_rate else ""
            return f"#{self.index} {lang}{self.codec.upper()} {ch}{br}{title}"
        forced = " (forced)" if self.forced else ""
        return f"#{self.index} {lang}{self.codec}{forced}{title}"


@dataclass
class MediaInfo:
    path: str
    dvd_title: int | None
    duration: float
    format_name: str
    streams: list[Stream]
    chapters: int = 0

    @property
    def video(self) -> list[Stream]:
        return [s for s in self.streams if s.kind == "video"]

    @property
    def audio(self) -> list[Stream]:
        return [s for s in self.streams if s.kind == "audio"]

    @property
    def subtitles(self) -> list[Stream]:
        return [s for s in self.streams if s.kind == "subtitle"]

    def stream(self, index: int) -> Stream:
        for s in self.streams:
            if s.index == index:
                return s
        raise KeyError(index)


# --------------------------------------------------------------------- DVD

def is_dvd(path: str) -> bool:
    p = Path(path)
    if p.suffix.lower() in (".iso", ".ifo"):
        return True
    if p.is_dir():
        return p.name.upper() == "VIDEO_TS" or any(
            c.name.upper() == "VIDEO_TS" for c in p.iterdir() if c.is_dir())
    return False


def dvd_root(path: str) -> str:
    """Normalise to what libdvdread wants: an ISO file or the disc root."""
    p = Path(path)
    if p.suffix.lower() == ".ifo":
        p = p.parent
    if p.is_dir() and p.name.upper() == "VIDEO_TS":
        p = p.parent
    return str(p)


def _find_ci(directory: Path, name: str) -> Path | None:
    if not directory.is_dir():
        return None
    for c in directory.iterdir():
        if c.name.upper() == name.upper():
            return c
    return None


def dvd_title_count(root: str) -> int:
    """Read the title count from VIDEO_TS.IFO (TT_SRPT). Returns 0 if unknown."""
    r = Path(root)
    if r.suffix.lower() == ".iso":
        return 0
    vts = _find_ci(r, "VIDEO_TS") or r
    ifo = _find_ci(vts, "VIDEO_TS.IFO")
    if not ifo:
        return 0
    try:
        with open(ifo, "rb") as f:
            f.seek(0xC4)
            sector = struct.unpack(">I", f.read(4))[0]
            f.seek(sector * 2048)
            return struct.unpack(">H", f.read(2))[0]
    except (OSError, struct.error):
        return 0


def input_args(path: str, dvd_title: int | None) -> list[str]:
    if dvd_title is not None:
        return ["-f", "dvdvideo", "-title", str(dvd_title), "-i", dvd_root(path)]
    return ["-i", path]


# ------------------------------------------------------------------- probe

def _num(d: dict, key: str) -> int:
    try:
        return int(d.get(key) or 0)
    except ValueError:
        return 0


def probe(path: str, dvd_title: int | None = None, timeout: int = 120) -> MediaInfo:
    cmd = ["ffprobe", "-v", "error", "-show_streams", "-show_format",
           "-show_chapters", "-of", "json", *input_args(path, dvd_title)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ProbeError(f"ffprobe timed out: {path}") from e
    if res.returncode != 0:
        raise ProbeError(res.stderr.strip() or f"ffprobe failed: {path}")
    data = json.loads(res.stdout or "{}")
    fmt = data.get("format", {})
    streams: list[Stream] = []
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        if kind not in ("video", "audio", "subtitle"):
            continue
        if kind == "video" and s.get("disposition", {}).get("attached_pic"):
            continue
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        sar = parse_ratio(s.get("sample_aspect_ratio"))
        streams.append(Stream(
            index=s["index"],
            kind=kind,
            codec=s.get("codec_name", "?"),
            lang=tags.get("language", ""),
            title=tags.get("title", ""),
            channels=_num(s, "channels"),
            bit_rate=_num(s, "bit_rate") or _num(tags, "bps"),
            sample_rate=_num(s, "sample_rate"),
            width=_num(s, "width"),
            height=_num(s, "height"),
            fps=float(parse_ratio(s.get("r_frame_rate"))),
            avg_fps=float(parse_ratio(s.get("avg_frame_rate"))),
            sar=sar if sar > 0 else Fraction(1),
            field_order=s.get("field_order", ""),
            forced=bool(s.get("disposition", {}).get("forced")),
            color_space=s.get("color_space", ""),
        ))
    duration = float(fmt.get("duration") or 0)
    if not duration:
        for s in data.get("streams", []):
            duration = max(duration, float(s.get("duration") or 0))
    if not any(s.kind == "video" for s in streams):
        raise ProbeError("No video stream found in the source.")
    return MediaInfo(path=path, dvd_title=dvd_title, duration=duration,
                     format_name=fmt.get("format_name", ""), streams=streams,
                     chapters=len(data.get("chapters", [])))


@dataclass
class DvdTitle:
    number: int
    duration: float
    info: MediaInfo

    def label(self) -> str:
        return f"Title {self.number} – {fmt_duration(self.duration)}"


def list_dvd_titles(path: str, max_titles: int = 99) -> list[DvdTitle]:
    known = dvd_title_count(dvd_root(path))
    titles: list[DvdTitle] = []
    misses = 0
    for n in range(1, (known or max_titles) + 1):
        try:
            info = probe(path, n, timeout=60)
        except ProbeError:
            misses += 1
            # ISO files have no readable title count: stop after three misses in a row.
            if not known and misses >= 3:
                break
            continue
        misses = 0
        titles.append(DvdTitle(n, info.duration, info))
    if not titles:
        raise ProbeError("Could not read any DVD titles (encrypted disc?).")
    return titles


def fmt_duration(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def fmt_size(nbytes: float) -> str:
    return f"{nbytes / 1048576:.1f} MB"


def available_encoders() -> set[str]:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6:
            names.add(parts[1])
    return names


def which(name: str) -> str | None:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return None
