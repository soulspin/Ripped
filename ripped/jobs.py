"""Settings, jobs and persistence (XDG)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ripped"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "ripped"

# (label, MiB). Same MB definition as AutoGK (1 CD = 700 MiB).
PRESETS: list[tuple[str, int]] = [
    ("1/4 CD (175 MB)", 175),
    ("1/3 CD (233 MB)", 233),
    ("1/2 CD (350 MB)", 350),
    ("1 CD (700 MB)", 700),
    ("2 CD (1400 MB)", 1400),
    ("3 CD (2100 MB)", 2100),
    ("1/6 DVD-R (746 MB)", 746),
    ("1/5 DVD-R (896 MB)", 896),
    ("1/4 DVD-R (1120 MB)", 1120),
    ("1/3 DVD-R (1493 MB)", 1493),
    ("1/2 DVD-R (2240 MB)", 2240),
    ("2 GB", 2048),
    ("FAT32 max (4095 MB)", 4095),
    ("DVD-R (4480 MB)", 4480),
    ("DVD-R DL (8140 MB)", 8140),
]
DEFAULT_PRESET = 4  # 2 CD, just like AutoGK

# Presets that are split into 700 MB parts (as AutoGK does for 2/3 CD).
SPLIT_SIZES = {1400: 2, 2100: 3}


def _from_dict(cls, data: dict | None):
    data = data or {}
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class AdvancedSettings:
    res_mode: str = "auto"         # auto | min | fixed | max
    width: int = 640
    audio_mode: str = "auto"       # auto | copy | mp3_abr | mp3_cbr | aac | ac3
    audio_bitrate: int = 128
    codec: str = "xvid"            # xvid | x264
    container: str = "avi"         # avi | mkv | mp4
    x264_preset: str = "slow"
    forced_subs_only: bool = False
    external_subs: bool = False


@dataclass
class HiddenOptions:
    mono: bool = False
    cartoon: bool = False
    force_ivtc: bool = False
    force_deint: bool = False
    credits: bool = False
    credits_start: float = 0.0     # seconds
    credits_quality: int = 20
    autocrop_tune: bool = False
    autocrop_threshold: int = 24
    autocrop_samples: int = 10
    autocrop_start: float = 0.0    # seconds
    crop_manual: bool = False
    crop_top: int = 0
    crop_bottom: int = 0
    crop_left: int = 0
    crop_right: int = 0
    aspect_override: bool = False
    aspect: str = "16:9"           # 16:9 | 4:3 | original
    reduce_fps: bool = False
    reduce_fps_rate: str = "30000/1001"
    sharp_matrix: bool = False
    normalize: bool = True
    adjust_subs: bool = False
    color_correction: bool = True
    detect_43: bool = True
    standalone: bool = False
    standalone_chip: str = "mtk"   # mtk | ess
    keep_temp: bool = False
    work_dir: str = ""


@dataclass
class Settings:
    advanced: AdvancedSettings = field(default_factory=AdvancedSettings)
    hidden: HiddenOptions = field(default_factory=HiddenOptions)
    shutdown_when_done: bool = False
    last_dir: str = ""

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads((CONFIG_DIR / "settings.json").read_text())
        except (OSError, ValueError):
            return cls()
        return cls(advanced=_from_dict(AdvancedSettings, data.get("advanced")),
                   hidden=_from_dict(HiddenOptions, data.get("hidden")),
                   shutdown_when_done=False,
                   last_dir=data.get("last_dir", ""))

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("shutdown_when_done", None)
        (CONFIG_DIR / "settings.json").write_text(json.dumps(data, indent=2))


@dataclass
class Job:
    input: str
    output: str
    dvd_title: int | None = None
    video_index: int = 0
    audio1: int | None = None
    audio2: int | None = None
    subtitle: int | None = None    # stream index, or -1 = all (external only)
    ext_subs_file: str = ""
    size_mode: str = "preset"      # preset | custom | quality
    size_mb: int = 1400
    quality: int = 75
    advanced: AdvancedSettings = field(default_factory=AdvancedSettings)
    hidden: HiddenOptions = field(default_factory=HiddenOptions)
    enabled: bool = True
    status: str = "pending"        # pending | running | done | failed | aborted

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        job = _from_dict(cls, d)
        job.advanced = _from_dict(AdvancedSettings, d.get("advanced"))
        job.hidden = _from_dict(HiddenOptions, d.get("hidden"))
        return job

    @property
    def split_parts(self) -> int:
        # Only the 2 CD / 3 CD presets are split; a custom size is always one file.
        if self.size_mode != "preset":
            return 1
        return SPLIT_SIZES.get(self.size_mb, 1)


def summary(adv: AdvancedSettings) -> str:
    codec = "XviD" if adv.codec == "xvid" else "x264"
    audio = {"auto": "Auto", "copy": "Original", "mp3_abr": f"MP3 ABR {adv.audio_bitrate}",
             "mp3_cbr": f"MP3 CBR {adv.audio_bitrate}", "aac": f"AAC {adv.audio_bitrate}",
             "ac3": f"AC3 {adv.audio_bitrate}"}[adv.audio_mode]
    res = {"auto": "Auto width", "min": f"Min. width {adv.width}",
           "fixed": f"Fixed width {adv.width}", "max": f"Max. width {adv.width}"}[adv.res_mode]
    return f"Codec: {codec}  ·  Audio: {audio}  ·  {res}  ·  .{adv.container.upper()}"


# Status names written by early development builds (Swedish UI).
_OLD_STATUS = {"väntar": "pending", "kör": "running", "klar": "done", "fel": "failed",
               "avbruten": "aborted"}


def save_queue(jobs: list[Job]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "queue.json").write_text(json.dumps([j.to_dict() for j in jobs], indent=2))


def load_queue() -> list[Job]:
    try:
        data = json.loads((DATA_DIR / "queue.json").read_text())
    except (OSError, ValueError):
        return []
    jobs = []
    for d in data:
        try:
            job = Job.from_dict(d)
        except TypeError:
            continue
        job.status = _OLD_STATUS.get(job.status, job.status)
        if job.status == "running":
            job.status = "aborted"
        jobs.append(job)
    return jobs
