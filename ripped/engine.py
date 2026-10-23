"""The encoding engine – AutoGK's workflow rebuilt on top of ffmpeg.

Equivalents of the original tool chain:
  DGIndex/VobSub demux   -> ffmpeg dvdvideo demuxer, remuxed to Matroska
  Decomb IsCombed        -> the idet filter
  Telecide/Decimate      -> fieldmatch + decimate
  KernelDeint            -> bwdif
  autocrop.dll           -> cropdetect
  SelectRangeEvery test  -> select filter + encode at a reference quantizer
  XviD VfW / VirtualDub  -> libxvid / libx264 via ffmpeg (1 or 2 pass)
  LAME / Azid / Normalize-> libmp3lame, downmix, volumedetect + volume
"""

from __future__ import annotations

import copy
import math
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable

from . import media
from .jobs import Job
from .media import MediaInfo, Stream, fmt_duration, fmt_size

# Compressibility test: encode 15 frames out of every 300 (5 % of the movie).
SAMPLE_EVERY = 300
SAMPLE_LEN = 15
# Desired compressibility for automatic width (AutoGK: "60–80 % is normal").
COMP_TARGET = {"xvid": 70.0, "x264": 65.0}
# Reference quality = "100 %": XviD quantizer 2, x264 CRF 18.
XVID_REF_QUANT = 2
X264_REF_CRF = 18.0
# How the bit demand scales with the pixel count at constant quality.
PIXEL_EXPONENT = 0.8

AVI_COPY = {"ac3", "dts", "mp3", "mp2", "pcm_s16le"}
MP4_COPY = {"aac", "ac3", "eac3", "mp3", "alac", "opus", "flac"}


class Aborted(Exception):
    pass


class JobError(Exception):
    pass


LogFn = Callable[[str], None]
ProgressFn = Callable[[str, float, str], None]


def quality_to_quant(q: int) -> int:
    """AutoGK: quality % = 200 / quantizer (67 % ≈ Q3)."""
    return max(2, min(31, round(200 / max(q, 1))))


def quality_to_crf(q: float) -> float:
    """Same size scale for x264: half the quality ≈ +6 CRF."""
    return max(0.0, min(51.0, X264_REF_CRF + 6 * math.log2(100 / max(q, 1))))


def fps_fraction(fps: float) -> Fraction:
    for std in (Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001),
                Fraction(24), Fraction(25), Fraction(30), Fraction(50), Fraction(60)):
        if abs(float(std) - fps) < 0.01:
            return std
    return Fraction(fps).limit_denominator(1001)


def filter_escape(path: str) -> str:
    """Escape a path for both the filter option and the filtergraph level."""
    lvl1 = path.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    out = []
    for ch in lvl1:
        if ch in "\\'[],;":
            out.append("\\")
        out.append(ch)
    return "".join(out)


@dataclass
class Crop:
    w: int
    h: int
    x: int = 0
    y: int = 0

    def filter(self) -> str:
        return f"crop={self.w}:{self.h}:{self.x}:{self.y}"


@dataclass
class AudioPlan:
    stream: Stream
    mode: str           # copy | mp3_abr | mp3_cbr | aac | ac3
    bitrate: int = 128  # kbps
    channels: int = 0   # 0 = same as source
    gain_db: float = 0.0

    def est_bytes(self, duration: float) -> float:
        if self.mode == "copy":
            br = self.stream.bit_rate or _guess_bitrate(self.stream)
            return br * duration / 8
        return self.bitrate * 1000 * duration / 8

    def describe(self) -> str:
        if self.mode == "copy":
            return f"original ({self.stream.codec.upper()})"
        name = {"mp3_abr": "VBR/ABR MP3", "mp3_cbr": "CBR MP3", "aac": "AAC", "ac3": "AC3"}[self.mode]
        ch = {1: ", mono", 2: ", stereo"}.get(self.channels, "")
        gain = f", normalized {self.gain_db:+.1f} dB" if self.gain_db else ""
        return f"{name} {self.bitrate} kbps{ch}{gain}"

    def codec_args(self, i: int) -> list[str]:
        if self.mode == "copy":
            return [f"-c:a:{i}", "copy"]
        enc = {"mp3_abr": "libmp3lame", "mp3_cbr": "libmp3lame", "aac": "aac", "ac3": "ac3"}[self.mode]
        args = [f"-c:a:{i}", enc, f"-b:a:{i}", f"{self.bitrate}k"]
        if self.mode == "mp3_abr":
            args += [f"-abr:a:{i}", "1"]
        if self.channels:
            args += [f"-ac:a:{i}", str(self.channels)]
        if self.gain_db:
            args += [f"-filter:a:{i}", f"volume={self.gain_db:.2f}dB"]
        return args


def _guess_bitrate(s: Stream) -> int:
    if s.codec == "dts":
        return 1_536_000 if s.channels > 2 else 768_000
    if s.codec in ("ac3", "eac3"):
        return 448_000 if s.channels > 2 else 192_000
    if s.codec.startswith("pcm"):
        return s.sample_rate * s.channels * 16 or 1_536_000
    return 192_000 if s.channels <= 2 else 640_000


@dataclass
class Analysis:
    video: Stream
    deint: list[str]
    fps_out: Fraction
    crop: Crop
    ar: float
    mode: str


@dataclass
class VideoPlan:
    analysis: Analysis
    out_w: int
    out_h: int
    rate_mode: str              # size | quality
    bitrate_kbps: int = 0
    quality: int = 75
    resizer: str = "lanczos"    # lanczos | bicubic
    mpeg_matrix: bool = False
    colormatrix: bool = False
    hd_tags: bool = False
    video_bytes: float = 0.0    # target for the video part (size mode)
    other_bytes: float = 0.0    # estimated audio + overhead


@dataclass
class SubPlan:
    burn_stream: Stream | None = None   # bitmap or text stream from the source
    burn_file: str = ""                 # external srt/ass/idx
    soft_streams: list[Stream] = field(default_factory=list)


def can_copy(container: str, s: Stream) -> bool:
    if container == "avi":
        return s.codec in AVI_COPY
    if container == "mp4":
        return s.codec in MP4_COPY
    return True


def plan_audio(job: Job, info: MediaInfo) -> tuple[list[AudioPlan], list[str]]:
    """Choose codec, bitrate and channels for the selected tracks (no I/O).

    Returns the plans and notes worth logging. Normalization gain is measured later.
    """
    adv, hid = job.advanced, job.hidden
    ids = [i for i in (job.audio1, job.audio2) if i is not None]
    streams = [info.stream(i) for i in ids if i in [s.index for s in info.audio]]
    big = job.size_mode == "quality" or job.size_mb > 700
    plans, notes = [], []
    for n, s in enumerate(streams):
        mode, br = adv.audio_mode, adv.audio_bitrate
        if mode == "auto":
            mode, br = ("copy", 0) if (n == 0 and big) else ("mp3_abr", 128)
        if mode == "copy" and not can_copy(adv.container, s):
            fallback = "aac" if adv.container == "mp4" else "mp3_abr"
            notes.append(f"{s.codec.upper()} cannot be stored in {adv.container.upper()} – "
                         f"converting to {fallback.upper()}.")
            mode, br = fallback, max(br, 128)
        if mode in ("mp3_abr", "mp3_cbr") and adv.container == "mp4":
            mode = "aac"
        if mode == "aac" and adv.container == "avi":
            mode = "mp3_abr"
        p = AudioPlan(s, mode, br)
        if mode != "copy":
            if hid.mono:
                p.channels = 1
                if mode.startswith("mp3"):
                    p.bitrate = max(32, br // 2)
            elif mode.startswith("mp3"):
                p.channels = 2
            elif s.channels > 6:
                p.channels = 6
        plans.append(p)
    return plans, notes


def container_overhead(container: str, frames: float, n_audio: int, duration: float) -> float:
    if container == "avi":
        # 24 bytes per video chunk + one audio chunk per frame and track, plus index.
        return frames * 24 * (1 + n_audio) + 64 * 1024
    if container == "mkv":
        return frames * 12 * (1 + n_audio) + duration * 40 + 512 * 1024
    return frames * 16 * (1 + n_audio) + 512 * 1024


@dataclass
class Estimate:
    video_kbps: int
    audio_kbps: int
    overhead_kbps: int
    fps: float


def estimate_bitrate(job: Job, info: MediaInfo) -> Estimate | None:
    """The video bitrate a size-mode job will get, before any analysis.

    Uses the same budget as the encoder. The frame rate is a guess until the interlace
    analysis has run (IVTC can lower it), so the real value may differ slightly.
    None in quality mode.
    """
    if job.size_mode == "quality" or not info.duration or not info.video:
        return None
    ids = [s.index for s in info.video]
    v = info.stream(job.video_index) if job.video_index in ids else info.video[0]
    hid = job.hidden
    if hid.reduce_fps:
        fps = float(Fraction(hid.reduce_fps_rate))
    elif hid.force_ivtc or (abs(v.fps - 29.97) < 0.05 and abs(v.avg_fps - 23.976) < 0.05
                            and not hid.force_deint):
        fps = 24000 / 1001
    else:
        fps = v.fps or 25.0
    duration = info.duration
    audio, _ = plan_audio(job, info)
    audio_bytes = sum(a.est_bytes(duration) for a in audio)
    overhead = container_overhead(job.advanced.container, duration * fps, len(audio), duration)
    video_bytes = job.size_mb * 1048576 - audio_bytes - overhead

    def kbps(nbytes: float) -> int:
        return int(nbytes * 8 / duration / 1000)

    return Estimate(kbps(video_bytes), kbps(audio_bytes), kbps(overhead), fps)


def _inp(src: str | list[str]) -> list[str]:
    """A path, or ready-made input arguments (e.g. from media.input_args)."""
    return list(src) if isinstance(src, list) else ["-i", src]


class Runner:
    def __init__(self, job: Job, log: LogFn, progress: ProgressFn):
        self.job = job
        self.log = log
        self.progress = progress
        self.adv = job.advanced
        self.hid = job.hidden
        self._proc: subprocess.Popen | None = None
        self._aborted = False
        out = Path(self._output_path())
        base = Path(self.hid.work_dir) if self.hid.work_dir else out.parent
        if base.exists() and not os.access(base, os.W_OK):
            # Read-only folder (e.g. a root-owned mount point): use the cache instead.
            base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ripped"
            base.mkdir(parents=True, exist_ok=True)
        self.work = base / f".{out.stem}.ripped"

    # ------------------------------------------------------------ helpers

    def abort(self) -> None:
        self._aborted = True
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()

    def _check(self) -> None:
        if self._aborted:
            raise Aborted()

    def _output_path(self) -> str:
        p = Path(self.job.output)
        ext = "." + self.adv.container
        return str(p.with_suffix(ext)) if p.suffix.lower() != ext else str(p)

    def _run(self, args: list[str], stage: str | None = None, duration: float = 0,
             quiet: bool = False) -> tuple[list[str], dict]:
        """Run ffmpeg. Returns (stderr lines, last progress values)."""
        self._check()
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-y"]
        if stage:
            cmd += ["-progress", "pipe:1", "-nostats", "-loglevel", "error"]
        else:
            cmd += ["-loglevel", "info"]
        cmd += args
        if not quiet:
            self.log("$ " + shlex.join(cmd))
        self._proc = proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            errors="replace", bufsize=1)
        err: deque[str] = deque(maxlen=4000)

        def read_err():
            assert proc.stderr
            for line in proc.stderr:
                err.append(line.rstrip())

        t = threading.Thread(target=read_err, daemon=True)
        t.start()
        last: dict = {}
        started = time.monotonic()
        assert proc.stdout
        for line in proc.stdout:
            if "=" not in line:
                continue
            k, v = line.strip().split("=", 1)
            last[k] = v
            if k == "progress" and stage:
                self._report(stage, last, duration, started)
        proc.wait()
        t.join(timeout=5)
        self._proc = None
        if self._aborted:
            raise Aborted()
        if proc.returncode != 0:
            tail = "\n".join(list(err)[-15:])
            raise JobError(f"ffmpeg exited with code {proc.returncode}:\n{tail}")
        return list(err), last

    def _report(self, stage: str, p: dict, duration: float, started: float) -> None:
        try:
            t = int(p.get("out_time_us", "0")) / 1e6
        except ValueError:
            t = 0.0
        frac = min(1.0, t / duration) if duration > 0 else 0.0
        info = []
        fps = p.get("fps", "")
        if fps and fps != "0.00":
            info.append(f"{float(fps):.0f} fps")
        elapsed = time.monotonic() - started
        if frac > 0.01:
            remain = elapsed * (1 - frac) / frac
            info.append(f"about {fmt_duration(remain)} left")
        self.progress(stage, frac, "  ·  ".join(info))

    # ---------------------------------------------------------- pipeline

    def run(self) -> list[str]:
        t0 = time.monotonic()
        job = self.job
        self.log("=" * 60)
        self.log(f"Starting job: {job.input}")
        self.work.mkdir(parents=True, exist_ok=True)
        try:
            src, info = self._prepare_source()
            ana = self._analyze(src, info)
            audio = self._plan_audio(src, info)
            subs = self._plan_subs(info)
            plan = self._plan_video(src, info, ana, audio)
            out = self._encode(src, info, plan, audio, subs)
            outputs = self._finish(src, out, subs, info)
        except Aborted:
            self.log("The job was aborted.")
            raise
        finally:
            if not self.hid.keep_temp and not self._aborted:
                shutil.rmtree(self.work, ignore_errors=True)
        self.log(f"Job finished. Total time: {fmt_duration(time.monotonic() - t0)}")
        return outputs

    def _prepare_source(self) -> tuple[str, MediaInfo]:
        job = self.job
        if job.dvd_title is None:
            self.log("Analyzing source.")
            return job.input, media.probe(job.input)
        cached = self.work / "source.mkv"
        marker = self.work / "source.ok"
        tag = f"{media.dvd_root(job.input)}#{job.dvd_title}"
        if cached.exists() and marker.exists() and marker.read_text() == tag:
            self.log("Reusing previously demuxed DVD title.")
        else:
            self.log(f"Demuxing DVD title {job.dvd_title}.")
            dvd = media.probe(job.input, job.dvd_title)
            self._run([*media.input_args(job.input, job.dvd_title),
                       "-map", "0", "-c", "copy", "-f", "matroska", str(cached)],
                      stage="Demuxing DVD", duration=dvd.duration)
            marker.write_text(tag)
        return str(cached), media.probe(str(cached))

    # --------------------------------------------------------- analysis

    def _sample_points(self, duration: float, n: int, start: float = 0.0) -> list[float]:
        lo = max(start, duration * 0.05)
        hi = duration * 0.95
        if n <= 1 or hi <= lo:
            return [lo]
        return [lo + (hi - lo) * i / (n - 1) for i in range(n)]

    def _analyze(self, src: str, info: MediaInfo) -> Analysis:
        v = info.stream(self.job.video_index) if self.job.video_index in \
            [s.index for s in info.video] else info.video[0]
        self.log(f"Source resolution: {v.width}×{v.height}")
        self.log(f"Source aspect ratio: {v.dar:.3f}")
        self.log(f"Source frame rate: {v.fps:.3f} fps (average {v.avg_fps:.3f})")
        self.log(f"Source duration: {fmt_duration(info.duration)}")
        deint, fps_out, mode = self._detect_interlace(src, info, v)
        crop = self._detect_crop(src, info, v)
        sar = v.sar
        if self.hid.aspect_override and self.hid.aspect != "original":
            dar_full = Fraction(16, 9) if self.hid.aspect == "16:9" else Fraction(4, 3)
            sar = dar_full * v.height / v.width
            self.log(f"Manual aspect ratio: {self.hid.aspect}")
        ar = float(crop.w * sar / crop.h)
        if self.hid.detect_43 and abs(ar - 4 / 3) / (4 / 3) < 0.04 and ar != 4 / 3:
            self.log(f"Aspect ratio {ar:.3f} is close to 4:3 – forcing 4:3.")
            ar = 4 / 3
        return Analysis(v, deint, fps_out, crop, ar, mode)

    def _detect_interlace(self, src: str, info: MediaInfo, v: Stream
                          ) -> tuple[list[str], Fraction, str]:
        hid = self.hid
        fps = fps_fraction(v.fps)
        if hid.reduce_fps:
            rate = Fraction(hid.reduce_fps_rate)
            self.log(f"Reducing frame rate to {float(rate):.3f} fps (analysis skipped).")
            return [f"fps={rate}"], rate, "fps"
        ntsc = abs(v.fps - 29.97) < 0.05
        if ntsc and abs(v.avg_fps - 23.976) < 0.05 and not hid.force_deint:
            self.log("The source looks like pure FILM (soft telecine).")
            return ["fps=24000/1001"], Fraction(24000, 1001), "film"
        if hid.force_ivtc:
            self.log("Forcing normal IVTC.")
            return ["fieldmatch=combmatch=full", "bwdif=deint=interlaced", "decimate"], \
                Fraction(24000, 1001), "film"
        if hid.force_deint:
            self.log("Forcing deinterlacing.")
            return ["bwdif=mode=send_frame:deint=all"], fps, "interlaced"

        self.log("Analyzing interlacing.")
        tff = bff = prog = rep = rep_total = 0
        points = self._sample_points(info.duration, 12)
        for i, pos in enumerate(points):
            self.progress("Analyzing interlacing", i / len(points), "")
            lines, _ = self._run(["-ss", f"{pos:.2f}", *_inp(src), "-map", f"0:{v.index}",
                                  "-frames:v", "200", "-vf", "idet", "-an", "-sn",
                                  "-f", "null", "-"], quiet=True)
            text = "\n".join(lines)
            # idet prints one line per filter instance; the last one is the real one.
            m = re.findall(r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*"
                           r"Progressive:\s*(\d+)", text)
            if m:
                tff += int(m[-1][0]); bff += int(m[-1][1]); prog += int(m[-1][2])
            m = re.findall(r"Repeated Fields:\s*Neither:\s*(\d+)\s*Top:\s*(\d+)\s*Bottom:\s*(\d+)", text)
            if m:
                rep += int(m[-1][1]) + int(m[-1][2])
                rep_total += sum(map(int, m[-1]))
        total = tff + bff + prog
        if not total:
            self.log("Could not analyze interlacing – treating the source as progressive.")
            return [], fps, "progressive"
        inter = (tff + bff) / total
        repeated = rep / rep_total if rep_total else 0.0
        self.log(f"Progressive patterns: {100 * prog / total:.2f} %")
        self.log(f"Interlaced patterns: {100 * inter:.2f} %")
        self.log(f"Repeated fields (telecine): {100 * repeated:.2f} %")
        if inter < 0.10:
            self.log("The source is considered progressive.")
            return [], fps, "progressive"
        if ntsc and inter <= 0.60 and repeated >= 0.08:
            kind = "FILM" if inter <= 0.45 else "hybrid (mostly FILM)"
            self.log(f"The source is considered {kind} – applying IVTC.")
            return ["fieldmatch=combmatch=full", "bwdif=deint=interlaced", "decimate"], \
                Fraction(24000, 1001), "film"
        self.log("The source is considered interlaced – deinterlacing with bwdif.")
        return ["bwdif=mode=send_frame:deint=all"], fps, "interlaced"

    def _detect_crop(self, src: str | list[str], info: MediaInfo, v: Stream) -> Crop:
        hid = self.hid
        W, H = v.width, v.height
        if hid.crop_manual:
            c = Crop(W - hid.crop_left - hid.crop_right, H - hid.crop_top - hid.crop_bottom,
                     hid.crop_left, hid.crop_top)
            self.log(f"Manual crop: {c.filter()}")
            return c
        tune = hid.autocrop_tune
        n = hid.autocrop_samples if tune else 10
        limit = hid.autocrop_threshold if tune else 24
        start = hid.autocrop_start if tune else 0.0
        self.log("Looking for black borders (autocrop).")
        x1, y1, x2, y2 = W, H, 0, 0
        found = False
        points = self._sample_points(info.duration, n, start)
        for i, pos in enumerate(points):
            self.progress("Autocrop", i / len(points), "")
            lines, _ = self._run(["-ss", f"{pos:.2f}", *_inp(src), "-map", f"0:{v.index}",
                                  "-frames:v", "8", "-vf",
                                  f"cropdetect=limit={limit}:round=2:reset=0",
                                  "-an", "-sn", "-f", "null", "-"], quiet=True)
            crops = re.findall(r"crop=(-?\d+):(-?\d+):(-?\d+):(-?\d+)", "\n".join(lines))
            if not crops:
                continue
            w, h, x, y = map(int, crops[-1])
            if w < W * 0.3 or h < H * 0.3 or x < 0 or y < 0:
                continue  # black frame or nonsense
            found = True
            x1, y1 = min(x1, x), min(y1, y)
            x2, y2 = max(x2, x + w), max(y2, y + h)
        if not found:
            self.log("No black borders found.")
            return Crop(W, H)
        # Even position (required for 4:2:0) and dimensions divisible by 4, as in AutoGK.
        x1 += x1 % 2
        y1 += y1 % 2
        c = Crop((x2 - x1) // 4 * 4, (y2 - y1) // 4 * 4, x1, y1)
        self.log(f"Crop: left {c.x}, right {W - c.w - c.x}, top {c.y}, "
                 f"bottom {H - c.h - c.y} → {c.w}×{c.h}")
        return c

    # ------------------------------------------------------------ audio

    def _plan_audio(self, src: str, info: MediaInfo) -> list[AudioPlan]:
        plans, notes = plan_audio(self.job, info)
        for note in notes:
            self.log(note)
        for n, p in enumerate(plans):
            if p.mode != "copy" and self.hid.normalize:
                p.gain_db = self._peak_gain(src, p.stream, p.channels)
            self.log(f"Audio track {n + 1}: {p.stream.label()} → {p.describe()}")
        if not plans:
            self.log("No audio track selected – the output will have no sound.")
        return plans

    def _peak_gain(self, src: str, s: Stream, channels: int) -> float:
        layout = {1: "mono", 2: "stereo"}.get(channels)
        af = (f"aformat=channel_layouts={layout}," if layout else "") + "volumedetect"
        self.log(f"Measuring audio level for track #{s.index}.")
        lines, _ = self._run(["-i", src, "-map", f"0:{s.index}", "-vn", "-sn",
                              "-af", af, "-f", "null", "-"], quiet=True)
        m = re.search(r"max_volume:\s*(-?[\d.]+) dB", "\n".join(lines))
        if not m:
            return 0.0
        gain = -float(m[1]) - 0.5
        return round(gain, 2) if gain >= 0.2 else 0.0

    # -------------------------------------------------------- subtitles

    def _plan_subs(self, info: MediaInfo) -> SubPlan:
        job, adv = self.job, self.adv
        plan = SubPlan()
        if job.ext_subs_file:
            plan.burn_file = job.ext_subs_file
            self.log(f"Burning in external subtitles: {job.ext_subs_file}")
            return plan
        if job.subtitle is None:
            return plan
        subs = info.subtitles
        if adv.external_subs:
            plan.soft_streams = subs if job.subtitle == -1 else \
                [s for s in subs if s.index == job.subtitle]
            for s in plan.soft_streams:
                self.log(f"External subtitle: {s.label()}")
        else:
            match = [s for s in subs if s.index == job.subtitle]
            if match:
                plan.burn_stream = match[0]
                forced = " (forced only)" if adv.forced_subs_only else ""
                self.log(f"Burning in subtitle: {match[0].label()}{forced}")
        return plan

    # ------------------------------------------------------------ video

    def _height_mod(self) -> int:
        return 16 if self.hid.standalone else 8

    def _dims(self, ana: Analysis, w: int) -> tuple[int, int]:
        m = self._height_mod()
        return w, max(m, round(w / ana.ar / m) * m)

    def _plan_video(self, src: str, info: MediaInfo, ana: Analysis,
                    audio: list[AudioPlan]) -> VideoPlan:
        job, adv = self.job, self.adv
        cap = max(160, ana.crop.w // 16 * 16)
        hd = ana.video.height > 576
        plan = VideoPlan(ana, 0, 0, "quality" if job.size_mode == "quality" else "size")
        plan.colormatrix = hd and adv.codec == "xvid" and self.hid.color_correction
        plan.hd_tags = hd and adv.codec == "x264"
        if plan.colormatrix:
            self.log("Color correction: Rec.709 → Rec.601.")
        self.log(f"Output codec: {'XviD' if adv.codec == 'xvid' else 'x264'}")
        if adv.res_mode != "auto":
            label = {"min": "minimum", "fixed": "fixed", "max": "maximum"}[adv.res_mode]
            self.log(f"Custom resolution: {label} width {adv.width} pixels")

        if plan.rate_mode == "quality":
            w = {"auto": cap, "fixed": adv.width, "max": min(cap, adv.width),
                 "min": max(cap, adv.width)}[adv.res_mode] // 16 * 16
            plan.out_w, plan.out_h = self._dims(ana, w)
            plan.quality = job.quality
            plan.mpeg_matrix = adv.codec == "xvid" and (self.hid.sharp_matrix or job.quality >= 75)
            q = (f"quantizer {quality_to_quant(job.quality)}" if adv.codec == "xvid"
                 else f"CRF {quality_to_crf(job.quality):.1f}")
            self.log(f"Single pass with target quality {job.quality} % ({q}).")
            self.log(f"Chosen resolution: {plan.out_w}×{plan.out_h} (AR: {ana.ar:.3f})")
            return plan

        duration = info.duration
        frames = duration * float(ana.fps_out)
        target = job.size_mb * 1048576
        audio_bytes = sum(a.est_bytes(duration) for a in audio)
        overhead = container_overhead(self.adv.container, frames, len(audio), duration)
        video_bytes = target - audio_bytes - overhead
        self.log(f"Target size: {fmt_size(target)}, audio: {fmt_size(audio_bytes)}, "
                 f"overhead: {fmt_size(overhead)}")
        if video_bytes <= 0:
            raise JobError("Audio and overhead are larger than the target size!")
        plan.bitrate_kbps = int(video_bytes * 8 / duration / 1000)
        plan.video_bytes, plan.other_bytes = video_bytes, audio_bytes + overhead
        self.log(f"Target bitrate: {plan.bitrate_kbps} kbps")

        if adv.res_mode == "fixed":
            top = adv.width // 16 * 16
        elif adv.res_mode == "max":
            top = min(cap, adv.width // 16 * 16)
        else:
            top = cap
        lo = max(160, min(320, top))
        if adv.res_mode == "min":
            lo = min(top, max(lo, adv.width // 16 * 16))

        comp0 = self._comptest(src, info, ana, top, video_bytes, frames)
        pix0 = top * self._dims(ana, top)[1]

        def predict(w: int) -> float:
            return comp0 * (pix0 / (w * self._dims(ana, w)[1])) ** PIXEL_EXPONENT

        if adv.res_mode == "fixed":
            w = top
        else:
            target_comp = COMP_TARGET[adv.codec]
            w = lo
            for cand in range(top, lo - 1, -16):
                if predict(cand) >= target_comp:
                    w = cand
                    break
        plan.out_w, plan.out_h = self._dims(ana, w)
        comp = predict(w)
        self.log(f"Chosen resolution: {plan.out_w}×{plan.out_h} (AR: {ana.ar:.3f})")
        self.log(f"Expected compressibility: {comp:.1f} %")
        if comp > 100 and w == top:
            self.log("Warning: the final file will probably be smaller than the target size.")
        if comp < 50:
            self.log("Warning: low compressibility – consider a larger target size or MP3 audio.")
        if comp < 60:
            plan.resizer = "bicubic"
            self.log("Using softer resizing.")
        plan.mpeg_matrix = adv.codec == "xvid" and (self.hid.sharp_matrix or comp >= 75)
        if plan.mpeg_matrix:
            self.log("XviD uses the MPEG quantization matrix.")
        return plan

    def _comptest(self, src: str, info: MediaInfo, ana: Analysis, width: int,
                  video_bytes: float, frames: float) -> float:
        self.log("Running compressibility test.")
        out_w, out_h = self._dims(ana, width)
        plan = VideoPlan(ana, out_w, out_h, "ref")
        graph, _, _ = self._graph(src, info, plan, SubPlan(), sample=True)
        ext = "m4v" if self.adv.codec == "xvid" else "264"
        out = self.work / f"comptest.{ext}"
        _, last = self._run(["-i", src, "-filter_complex", graph, "-map", "[vout]",
                             "-an", "-sn", *self._video_args(plan, frames),
                             "-f", "m4v" if ext == "m4v" else "h264", str(out)],
                            stage="Compressibility test",
                            duration=info.duration * SAMPLE_LEN / SAMPLE_EVERY)
        sampled = int(last.get("frame", "0") or 0)
        size = out.stat().st_size if out.exists() else 0
        if not sampled or not size:
            raise JobError("The compressibility test produced no result.")
        full = size * frames / sampled
        comp = 100 * video_bytes / full
        self.log(f"Test encode {out_w}×{out_h}: {sampled} frames, {fmt_size(size)} "
                 f"→ estimated {fmt_size(full)} at reference quality.")
        self.log(f"Compressibility: {comp:.1f} %")
        return comp

    # ------------------------------------------------------- ffmpeg args

    def _graph(self, src: str, info: MediaInfo, plan: VideoPlan, subs: SubPlan,
               sample: bool = False) -> tuple[str, list[str], list[str]]:
        """Returns (filtergraph, extra input arguments, decoder options)."""
        ana = plan.analysis
        chains: list[str] = []
        extra_in: list[str] = []
        dec_opts: list[str] = []
        cur = f"[0:{ana.video.index}]"
        n = 0

        def push(filters: list[str], label: str | None = None) -> None:
            nonlocal cur, n
            lab = label or f"[v{n}]"
            n += 1
            chains.append(f"{cur}{','.join(filters) or 'null'}{lab}")
            cur = lab

        pre = list(ana.deint)
        if sample:
            pre += [f"select='lt(mod(n,{SAMPLE_EVERY}),{SAMPLE_LEN})'",
                    "setpts=N/FRAME_RATE/TB"]
        if pre:
            push(pre)

        bitmap_in = None
        text_filter = None
        if not sample:
            if subs.burn_file:
                f = subs.burn_file
                low = f.lower()
                if low.endswith(".sub") and Path(f).with_suffix(".idx").exists():
                    f, low = str(Path(f).with_suffix(".idx")), ".idx"
                if low.endswith(".idx"):
                    extra_in = ["-i", f]
                    bitmap_in = "[1:s:0]"
                else:
                    flt = "ass" if low.endswith((".ass", ".ssa")) else "subtitles"
                    text_filter = f"{flt}=filename='{filter_escape(f)}'"
            elif subs.burn_stream:
                s = subs.burn_stream
                if s.is_text_sub:
                    si = [x.index for x in info.subtitles].index(s.index)
                    text_filter = f"subtitles=filename='{filter_escape(src)}':si={si}"
                else:
                    bitmap_in = f"[0:{s.index}]"
                    if self.adv.forced_subs_only:
                        dec_opts = [f"-forced_subs_only:{s.index}", "1"]

        if bitmap_in and not self.hid.adjust_subs:
            chains.append(f"{cur}{bitmap_in}overlay=eof_action=pass[v{n}]")
            cur = f"[v{n}]"
            n += 1
        push([ana.crop.filter()])
        if bitmap_in and self.hid.adjust_subs:
            chains.append(f"{bitmap_in}scale={ana.crop.w}:{ana.crop.h}[subs]")
            chains.append(f"{cur}[subs]overlay=eof_action=pass[v{n}]")
            cur = f"[v{n}]"
            n += 1
        post = []
        if plan.colormatrix:
            post.append("colormatrix=bt709:bt601")
        if plan.resizer == "bicubic":
            post.append(f"scale={plan.out_w}:{plan.out_h}:flags=bicubic:param0=0:param1=0.5")
        else:
            post.append(f"scale={plan.out_w}:{plan.out_h}:flags=lanczos")
        post.append("setsar=1")
        if text_filter:
            post.append(text_filter)
        post.append("format=yuv420p")
        push(post, "[vout]")
        return ";".join(chains), extra_in, dec_opts

    def _video_args(self, plan: VideoPlan, frames: float, pass_no: int | None = None) -> list[str]:
        adv, hid = self.adv, self.hid
        fps = float(plan.analysis.fps_out)
        keyint = str(max(25, round(fps * 10)))
        if adv.codec == "xvid":
            bf = "1" if hid.standalone and hid.standalone_chip == "ess" else "2"
            args = ["-c:v", "libxvid", "-vtag", "XVID", "-g", keyint, "-bf", bf,
                    "-trellis", "1", "-me_quality", "6", "-variance_aq", "1",
                    "-flags", "+mv4+aic"]
            if plan.mpeg_matrix:
                args += ["-mpeg_quant", "1"]
            if plan.rate_mode == "ref":
                args += ["-qscale:v", str(XVID_REF_QUANT)]
            elif plan.rate_mode == "quality":
                args += ["-qscale:v", str(quality_to_quant(plan.quality))]
            else:
                args += ["-b:v", f"{plan.bitrate_kbps}k"]
        else:
            args = ["-c:v", "libx264", "-preset", adv.x264_preset, "-g", keyint]
            if hid.cartoon:
                args += ["-tune", "animation"]
            if hid.standalone:
                args += ["-profile:v", "high", "-level:v", "4.1",
                         "-maxrate", "20M", "-bufsize", "25M"]
            if plan.rate_mode == "ref":
                args += ["-crf", f"{X264_REF_CRF:.1f}"]
            elif plan.rate_mode == "quality":
                args += ["-crf", f"{quality_to_crf(plan.quality):.1f}"]
            else:
                args += ["-b:v", f"{plan.bitrate_kbps}k"]
            if hid.credits and plan.rate_mode != "ref" and hid.credits_start > 0:
                start = int(hid.credits_start * fps)
                qp = round(quality_to_crf(hid.credits_quality))
                if start < frames:
                    args += ["-x264-params", f"zones={start},{int(frames)},q={qp}"]
            if plan.hd_tags:
                args += ["-colorspace", "bt709", "-color_primaries", "bt709",
                         "-color_trc", "bt709"]
        if pass_no:
            args += ["-pass", str(pass_no), "-passlogfile", str(self.work / "pass")]
        return args

    def _container_args(self, plan: VideoPlan) -> list[str]:
        c = self.adv.container
        if c == "avi":
            return ["-fps_mode", "cfr", "-r", str(plan.analysis.fps_out),
                    "-map_chapters", "-1", "-f", "avi"]
        if c == "mkv":
            return ["-f", "matroska"]
        return ["-movflags", "+faststart", "-f", "mp4"]

    # ----------------------------------------------------------- encode

    def _encode(self, src: str, info: MediaInfo, plan: VideoPlan,
                audio: list[AudioPlan], subs: SubPlan) -> str:
        frames = info.duration * float(plan.analysis.fps_out)
        graph, extra_in, dec_opts = self._graph(src, info, plan, subs)
        inputs = [*dec_opts, "-i", src, *extra_in]
        out = self._output_path()
        tmp_out = str(self.work / ("encode" + Path(out).suffix))

        maps = ["-map", "[vout]"]
        codec = self._video_args(plan, frames, 2 if plan.rate_mode == "size" else None)
        for i, a in enumerate(audio):
            maps += ["-map", f"0:{a.stream.index}"]
            codec += a.codec_args(i)
        if audio:
            codec += ["-disposition:a:0", "default"]
        soft = self._soft_sub_args(subs)
        maps += soft[0]
        codec += soft[1]
        tail = [*self._container_args(plan), "-max_muxing_queue_size", "9999", tmp_out]

        self.log("Encoding starts.")
        if plan.rate_mode == "size":
            self.log("Running first pass.")
            self._run([*inputs, "-filter_complex", graph, "-map", "[vout]",
                       *self._video_args(plan, frames, 1), "-an", "-sn", "-f", "null", "-"],
                      stage="Pass 1 of 2", duration=info.duration)
            self.log("Running second pass.")
            self._run([*inputs, "-filter_complex", graph, *maps, *codec, *tail],
                      stage="Pass 2 of 2", duration=info.duration)
            # Like AutoGK: adjust and re-encode if the result misses the target by too much.
            # Over the target is worst (the file won't fit); slightly under is harmless.
            video = os.path.getsize(tmp_out) - plan.other_bytes
            ratio = plan.video_bytes / video if video > 0 else 1.0
            if ratio < 0.995 or ratio > 1.07:
                old = plan.bitrate_kbps
                plan.bitrate_kbps = max(50, int(old * ratio * (0.99 if ratio < 1 else 1.0)))
                self.log(f"The video part came out at {100 / ratio:.1f} % of the target – adjusting "
                         f"{old} → {plan.bitrate_kbps} kbps and running the second pass again.")
                codec = self._video_args(plan, frames, 2)
                for i, a in enumerate(audio):
                    codec += a.codec_args(i)
                if audio:
                    codec += ["-disposition:a:0", "default"]
                codec += soft[1]
                self._run([*inputs, "-filter_complex", graph, *maps, *codec, *tail],
                          stage="Pass 2 (adjusted)", duration=info.duration)
        else:
            self.log("Running single-pass encode.")
            self._run([*inputs, "-filter_complex", graph, *maps, *codec, *tail],
                      stage="Encoding (single pass)", duration=info.duration)
        return tmp_out

    def _soft_sub_args(self, subs: SubPlan) -> tuple[list[str], list[str]]:
        c = self.adv.container
        maps: list[str] = []
        codec: list[str] = []
        if c == "avi":
            return maps, codec  # sidecar files are written afterwards
        i = 0
        for s in subs.soft_streams:
            if c == "mp4":
                if not s.is_text_sub:
                    self.log(f"Bitmap subtitle #{s.index} is not supported in MP4 – skipping.")
                    continue
                codec += [f"-c:s:{i}", "mov_text"]
            else:
                codec += [f"-c:s:{i}", "copy" if s.codec != "mov_text" else "srt"]
            maps += ["-map", f"0:{s.index}"]
            i += 1
        return maps, codec

    # ----------------------------------------------------------- finish

    def _finish(self, src: str, tmp_out: str, subs: SubPlan, info: MediaInfo) -> list[str]:
        job = self.job
        out = Path(self._output_path())
        out.parent.mkdir(parents=True, exist_ok=True)
        size = os.path.getsize(tmp_out)
        self.log(f"Final size: {fmt_size(size)}"
                 + (f" (target {job.size_mb} MB)" if job.size_mode != "quality" else ""))
        parts = job.split_parts
        outputs: list[str]
        part_cap = job.size_mb / parts * 1048576
        if parts > 1 and size > part_cap:
            outputs = self._split(tmp_out, out, parts, part_cap)
        else:
            shutil.move(tmp_out, out)
            outputs = [str(out)]
        if self.adv.container == "avi" and subs.soft_streams:
            outputs += self._write_sidecars(src, out, subs.soft_streams)
        for o in outputs:
            self.log(f"Wrote {o}")
        return outputs

    def _split(self, path: str, out: Path, parts: int, cap: float) -> list[str]:
        """Split at the last keyframe before each part limit (e.g. 700 MB per CD).

        Every part except the last ends up just below `cap`, so all of them fit as
        long as the whole fits in parts × cap. Bytes are counted in file order (= the
        actual file size per part) and the cuts are passed to the segment muxer as
        video packet indices; time-based cuts are unreliable in AVI with packed
        B-frames.
        """
        self.log(f"Splitting into {parts} parts.")
        res = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "packet=stream_index,size,flags", "-of", "csv=p=0", path],
                             capture_output=True, text=True)
        vs = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                             "-show_entries", "stream=index", "-of", "csv=p=0", path],
                            capture_output=True, text=True).stdout.strip()
        limit = cap * 0.99  # margin for container header and index per part
        cuts: list[int] = []
        cum = part_start = key_cum = 0
        nvid = last_key = 0
        for line in res.stdout.splitlines():
            f = line.split(",")
            if len(f) < 3:
                continue
            if f[0] == vs:
                if "K" in f[2]:
                    last_key, key_cum = nvid, cum
                nvid += 1
            cum += int(f[1])
            if cum - part_start >= limit and len(cuts) < parts - 1 \
                    and last_key > (cuts[-1] if cuts else 0):
                cuts.append(last_key)
                part_start = key_cum
        if not cuts:
            shutil.move(path, out)
            return [str(out)]
        fmt = {"mkv": "matroska"}.get(self.adv.container, self.adv.container)
        pattern = str(out.with_name(f"{out.stem}.CD%d{out.suffix}"))
        self._run(["-i", path, "-map", "0", "-c", "copy", "-f", "segment",
                   "-segment_frames", ",".join(map(str, cuts)),
                   "-segment_start_number", "1", "-reset_timestamps", "1",
                   "-segment_format", fmt, pattern])
        os.remove(path)
        return [pattern % i for i in range(1, len(cuts) + 2)]

    def _write_sidecars(self, src: str, out: Path, streams: list[Stream]) -> list[str]:
        written = []
        for s in streams:
            lang = f".{s.lang}" if s.lang and s.lang != "und" else ""
            if s.is_text_sub:
                dst = out.with_name(f"{out.stem}{lang}.{s.index}.srt")
                self._run(["-i", src, "-map", f"0:{s.index}", "-c:s", "srt", str(dst)])
            else:
                # ffmpeg cannot write VobSub idx/sub – a Matroska subtitle file works
                # in mpv/VLC and can be converted further with e.g. mkvextract.
                dst = out.with_name(f"{out.stem}{lang}.{s.index}.mks")
                self._run(["-i", src, "-map", f"0:{s.index}", "-c:s", "copy",
                           "-f", "matroska", str(dst)])
            written.append(str(dst))
        return written

    # ---------------------------------------------------------- preview

    def preview_command(self) -> list[str]:
        """Analyze (crop/interlacing) and build an mpv/ffplay command."""
        inp, info, _ = self.preview_source()
        ana = self._analyze(inp, info)
        adv = self.adv
        cap = max(160, ana.crop.w // 16 * 16)
        w = {"auto": cap, "fixed": adv.width, "max": min(cap, adv.width),
             "min": max(cap, adv.width)}[adv.res_mode] // 16 * 16
        out_w, out_h = self._dims(ana, w)
        self.log(f"Previewing at {out_w}×{out_h} "
                 f"(automatic width may differ when encoding).")
        vf = [*ana.deint, ana.crop.filter(), f"scale={out_w}:{out_h}:flags=lanczos", "setsar=1"]
        job = self.job
        if media.which("mpv"):
            cmd = ["mpv", "--force-window=yes", "--title=Ripped – preview",
                   "--aid=no", f"--vf=lavfi=[{','.join(vf)}]"]
            # Straight from the DVD: mpv's own dvdnav input. Seeking through ffmpeg's
            # dvdvideo demuxer crashes mpv, and dvdnav numbers titles from 0.
            dvd = inp[0] == "-f"
            if job.ext_subs_file:
                cmd.append(f"--sub-file={job.ext_subs_file}")
            elif job.subtitle is not None and job.subtitle >= 0:
                subs = info.subtitles
                ids = [s.index for s in subs]
                if job.subtitle in ids:
                    s = subs[ids.index(job.subtitle)]
                    # dvdnav lists subpicture streams in another order than ffmpeg.
                    if dvd and s.lang and s.lang != "und":
                        cmd.append(f"--slang={s.lang}")
                    else:
                        cmd.append(f"--sid={ids.index(job.subtitle) + 1}")
                if adv.forced_subs_only:
                    cmd.append("--sub-forced-events-only=yes")
            else:
                cmd.append("--sid=no")
            if dvd:
                return [*cmd, f"--dvd-device={inp[-1]}", f"dvd://{job.dvd_title - 1}"]
            return [*cmd, inp[-1]]
        return ["ffplay", "-hide_banner", "-an", "-sn", "-window_title",
                "Ripped – preview", "-vf", ",".join(vf), *inp]

    # ------------------------------------------------- crop preview

    def preview_source(self) -> tuple[list[str], MediaInfo, Stream]:
        """Input for the previews, without demuxing the whole DVD title."""
        job = self.job
        cached = self.work / "source.mkv"
        marker = self.work / "source.ok"
        if job.dvd_title is None:
            inp, info = ["-i", job.input], media.probe(job.input)
        elif cached.exists() and marker.exists() and \
                marker.read_text() == f"{media.dvd_root(job.input)}#{job.dvd_title}":
            inp, info = ["-i", str(cached)], media.probe(str(cached))
        else:
            inp = media.input_args(job.input, job.dvd_title)
            info = media.probe(job.input, job.dvd_title)
        if not info.video:
            raise JobError("The source has no video.")
        ids = [s.index for s in info.video]
        v = info.stream(job.video_index) if job.video_index in ids else info.video[0]
        return inp, info, v

    def detect_crop(self, inp: list[str], info: MediaInfo, v: Stream) -> Crop:
        hid = copy.copy(self.hid)
        hid.crop_manual = False
        self.hid = hid
        return self._detect_crop(inp, info, v)

    @staticmethod
    def grab_frame(inp: list[str], v: Stream, pos: float, display_width: int) -> bytes:
        """One frame as PNG, scaled to square pixels (display width × source height)."""
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-ss", f"{pos:.2f}", *inp, "-map", f"0:{v.index}", "-frames:v", "1",
               "-vf", f"scale={display_width}:{v.height},setsar=1",
               "-c:v", "png", "-f", "image2pipe", "-"]
        res = subprocess.run(cmd, capture_output=True, timeout=60)
        if res.returncode != 0 or not res.stdout:
            msg = res.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["unknown error"]
            raise JobError(f"Could not grab frame: {msg[0]}")
        return res.stdout
