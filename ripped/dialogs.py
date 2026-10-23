"""The "Advanced settings" and "Hidden options" dialogs."""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .jobs import AdvancedSettings, HiddenOptions  # noqa: E402

X264_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                "medium", "slow", "slower", "veryslow"]


def section(title: str, orientation=Gtk.Orientation.VERTICAL) -> tuple[Gtk.Frame, Gtk.Box]:
    frame = Gtk.Frame()
    label = Gtk.Label(label=title, xalign=0)
    label.add_css_class("section-title")
    frame.set_label_widget(label)
    box = Gtk.Box(orientation=orientation, spacing=6)
    box.set_margin_top(6)
    box.set_margin_bottom(10)
    box.set_margin_start(10)
    box.set_margin_end(10)
    frame.set_child(box)
    return frame, box


def radio_group(labels: list[str]) -> list[Gtk.CheckButton]:
    buttons: list[Gtk.CheckButton] = []
    for text in labels:
        b = Gtk.CheckButton(label=text)
        if buttons:
            b.set_group(buttons[0])
        buttons.append(b)
    return buttons


def spin(lo: float, hi: float, step: float, value: float, digits: int = 0) -> Gtk.SpinButton:
    s = Gtk.SpinButton.new_with_range(lo, hi, step)
    s.set_digits(digits)
    s.set_value(value)
    s.set_width_chars(max(2, len(str(int(hi)))))
    s.set_hexpand(False)
    s.set_halign(Gtk.Align.START)
    return s


class TimeEntry(Gtk.Box):
    """h : m : s – replaces AutoGK's "Frame Calculator"."""

    def __init__(self, seconds: float):
        super().__init__(spacing=4)
        sec = int(seconds)
        self.h = spin(0, 9, 1, sec // 3600)
        self.m = spin(0, 59, 1, sec % 3600 // 60)
        self.s = spin(0, 59, 1, sec % 60)
        for i, w in enumerate((self.h, self.m, self.s)):
            if i:
                self.append(Gtk.Label(label=":"))
            self.append(w)

    @property
    def seconds(self) -> float:
        return self.h.get_value() * 3600 + self.m.get_value() * 60 + self.s.get_value()

    def set_sensitive_all(self, on: bool) -> None:
        for w in (self.h, self.m, self.s):
            w.set_sensitive(on)


class BaseDialog(Gtk.Window):
    def __init__(self, parent: Gtk.Window, title: str):
        super().__init__(title=title, transient_for=parent, modal=True,
                         resizable=False)
        self.add_css_class("ripped-dialog")
        header = Gtk.HeaderBar()
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        ok = Gtk.Button(label="OK")
        ok.add_css_class("suggested-action")
        ok.connect("clicked", self._on_ok)
        header.pack_start(cancel)
        header.pack_end(ok)
        self.set_titlebar(header)
        self.set_default_widget(ok)

    def _on_ok(self, *_):
        self.apply()
        self.close()

    def apply(self) -> None:
        raise NotImplementedError


class AdvancedDialog(BaseDialog):
    def __init__(self, parent: Gtk.Window, adv: AdvancedSettings,
                 on_apply: Callable[[], None]):
        super().__init__(parent, "Advanced Settings")
        self.adv = adv
        self.on_apply = on_apply

        grid = Gtk.Grid(column_spacing=12, row_spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(grid, f"set_margin_{m}")(16)

        # Resolution
        f, box = section("Resolution")
        self.res = radio_group(["Automatic width", "Minimum width", "Fixed width", "Maximum width"])
        self.width = spin(160, 3840, 16, adv.width)
        modes = ["auto", "min", "fixed", "max"]
        self.res[modes.index(adv.res_mode)].set_active(True)
        for b in self.res:
            box.append(b)
            b.connect("toggled", self._sync)
        row = Gtk.Box(spacing=6)
        row.append(Gtk.Label(label="Width (px):"))
        row.append(self.width)
        box.append(row)
        grid.attach(f, 0, 0, 1, 1)

        # Audio
        f, box = section("Audio format")
        self.audio = radio_group(["Automatic", "Original (copy)", "MP3, VBR/ABR",
                                  "MP3, CBR", "AAC (MKV/MP4)", "AC3"])
        self.audio_modes = ["auto", "copy", "mp3_abr", "mp3_cbr", "aac", "ac3"]
        self.audio[self.audio_modes.index(adv.audio_mode)].set_active(True)
        for b in self.audio:
            box.append(b)
            b.connect("toggled", self._sync)
        self.bitrate = spin(32, 640, 16, adv.audio_bitrate)
        row = Gtk.Box(spacing=6)
        row.append(Gtk.Label(label="Bitrate (kbps):"))
        row.append(self.bitrate)
        box.append(row)
        grid.attach(f, 1, 0, 1, 1)

        # Codec
        f, box = section("Video codec")
        self.codec = radio_group(["XviD (MPEG-4 ASP)", "x264 (H.264/AVC)"])
        self.codec[0 if adv.codec == "xvid" else 1].set_active(True)
        for b in self.codec:
            box.append(b)
            b.connect("toggled", self._sync)
        row = Gtk.Box(spacing=6)
        row.append(Gtk.Label(label="x264-preset:"))
        self.preset = Gtk.DropDown.new_from_strings(X264_PRESETS)
        self.preset.set_hexpand(True)
        self.preset.set_selected(X264_PRESETS.index(adv.x264_preset)
                                 if adv.x264_preset in X264_PRESETS else 6)
        row.append(self.preset)
        box.append(row)
        grid.attach(f, 0, 1, 1, 1)

        # Container
        f, box = section("Output format")
        self.container = radio_group(["AVI", "MKV (Matroska)", "MP4"])
        self.containers = ["avi", "mkv", "mp4"]
        self.container[self.containers.index(adv.container)].set_active(True)
        for b in self.container:
            box.append(b)
            b.connect("toggled", self._sync)
        self.container_hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=34)
        self.container_hint.add_css_class("dim-label")
        box.append(self.container_hint)
        grid.attach(f, 1, 1, 1, 1)

        # Subtitles
        f, box = section("Subtitles", Gtk.Orientation.HORIZONTAL)
        self.forced = Gtk.CheckButton(label="Show forced subtitles only",
                                      active=adv.forced_subs_only)
        self.external = Gtk.CheckButton(label="Use external (soft) subtitles",
                                        active=adv.external_subs)
        box.set_spacing(24)
        box.append(self.forced)
        box.append(self.external)
        grid.attach(f, 0, 2, 2, 1)

        self.set_child(grid)
        self._sync()

    def _sync(self, *_):
        self.width.set_sensitive(not self.res[0].get_active())
        mode = self.audio_modes[[b.get_active() for b in self.audio].index(True)]
        self.bitrate.set_sensitive(mode not in ("auto", "copy"))
        x264 = self.codec[1].get_active()
        self.preset.set_sensitive(x264)
        # x264 with B-frames in AVI is a bad idea – only allow MKV/MP4.
        self.container[0].set_sensitive(not x264)
        if x264 and self.container[0].get_active():
            self.container[1].set_active(True)
        hints = {
            "avi": "Classic and compatible with older DVD/DivX players.",
            "mkv": "Recommended: chapters, multiple audio tracks, soft subtitles.",
            "mp4": "Best for phones, TV apps and web browsers.",
        }
        c = self.containers[[b.get_active() for b in self.container].index(True)]
        self.container_hint.set_label(hints[c])

    def apply(self) -> None:
        a = self.adv
        a.res_mode = ["auto", "min", "fixed", "max"][[b.get_active() for b in self.res].index(True)]
        a.width = int(self.width.get_value()) // 16 * 16
        a.audio_mode = self.audio_modes[[b.get_active() for b in self.audio].index(True)]
        a.audio_bitrate = int(self.bitrate.get_value())
        a.codec = "xvid" if self.codec[0].get_active() else "x264"
        a.x264_preset = X264_PRESETS[self.preset.get_selected()]
        a.container = self.containers[[b.get_active() for b in self.container].index(True)]
        a.forced_subs_only = self.forced.get_active()
        a.external_subs = self.external.get_active()
        self.on_apply()


class HiddenDialog(BaseDialog):
    def __init__(self, parent: Gtk.Window, hid: HiddenOptions, on_apply: Callable[[], None]):
        super().__init__(parent, "Hidden Options")
        self.hid = hid
        self.on_apply = on_apply
        h = hid

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{m}")(16)

        wd = Gtk.Box(spacing=8)
        wd.append(Gtk.Label(label="Working directory:"))
        self.work_dir = Gtk.Entry(text=h.work_dir, hexpand=True,
                                  placeholder_text="Same as the output file")
        wd.append(self.work_dir)
        outer.append(wd)

        cols = Gtk.Box(spacing=12, homogeneous=True)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        cols.append(left)
        cols.append(right)
        outer.append(cols)

        def check(box, label, value):
            b = Gtk.CheckButton(label=label, active=value)
            box.append(b)
            return b

        f, b = section(".mono")
        self.mono = check(b, "Force mono audio when encoding MP3", h.mono)
        left.append(f)

        f, b = section(".cartoon (x264)")
        self.cartoon = check(b, "Cartoon mode (x264 --tune animation)", h.cartoon)
        left.append(f)

        f, b = section(".ivtc / .deint")
        self.ivtc = check(b, "Force IVTC (telecine → 23.976 fps)", h.force_ivtc)
        self.deint = check(b, "Force deinterlacing (bwdif)", h.force_deint)
        left.append(f)

        f, b = section(".credits (x264)")
        self.credits = check(b, "Encode end credits at a separate quality", h.credits)
        row = Gtk.Box(spacing=6)
        row.append(Gtk.Label(label="Start:"))
        self.credits_start = TimeEntry(h.credits_start)
        row.append(self.credits_start)
        b.append(row)
        row = Gtk.Box(spacing=6)
        row.append(Gtk.Label(label="Quality (%):"))
        self.credits_q = spin(5, 100, 1, h.credits_quality)
        row.append(self.credits_q)
        b.append(row)
        left.append(f)

        f, b = section(".autocrop")
        self.ac_tune = check(b, "Tune autocrop parameters", h.autocrop_tune)
        g = Gtk.Grid(column_spacing=8, row_spacing=4)
        self.ac_thr = spin(1, 255, 1, h.autocrop_threshold)
        self.ac_n = spin(2, 50, 1, h.autocrop_samples)
        self.ac_start = TimeEntry(h.autocrop_start)
        for r, (lab, w) in enumerate([("Threshold (0–255):", self.ac_thr),
                                      ("Sample points:", self.ac_n),
                                      ("Start from:", self.ac_start)]):
            g.attach(Gtk.Label(label=lab, xalign=0), 0, r, 1, 1)
            g.attach(w, 1, r, 1, 1)
        b.append(g)
        self.crop_manual = check(b, "Force crop (px):", h.crop_manual)
        cg = Gtk.Grid(column_spacing=4, row_spacing=4, halign=Gtk.Align.CENTER)
        self.crop_t = spin(0, 2000, 2, h.crop_top)
        self.crop_l = spin(0, 2000, 2, h.crop_left)
        self.crop_r = spin(0, 2000, 2, h.crop_right)
        self.crop_b = spin(0, 2000, 2, h.crop_bottom)
        cg.attach(self.crop_t, 1, 0, 1, 1)
        cg.attach(self.crop_l, 0, 1, 1, 1)
        cg.attach(self.crop_r, 2, 1, 1, 1)
        cg.attach(self.crop_b, 1, 2, 1, 1)
        b.append(cg)
        left.append(f)

        f, b = section(".aspect")
        self.aspect_override = check(b, "Override source aspect ratio", h.aspect_override)
        row = Gtk.Box(spacing=12)
        self.aspect = radio_group(["16:9", "4:3", "Original"])
        for x in self.aspect:
            row.append(x)
        self.aspect[{"16:9": 0, "4:3": 1}.get(h.aspect, 2)].set_active(True)
        b.append(row)
        right.append(f)

        f, b = section(".fps")
        self.reduce_fps = check(b, "Reduce frame rate for 50/60p HDTV sources", h.reduce_fps)
        row = Gtk.Box(spacing=12)
        self.fps_rates = ["25", "24000/1001", "30000/1001"]
        self.fps = radio_group(["25.000", "23.976", "29.970"])
        for x in self.fps:
            row.append(x)
        self.fps[self.fps_rates.index(h.reduce_fps_rate)
                 if h.reduce_fps_rate in self.fps_rates else 2].set_active(True)
        b.append(row)
        right.append(f)

        f, b = section(".sharpmatrix (XviD)")
        self.sharp = check(b, "Force MPEG matrix regardless of the compressibility test", h.sharp_matrix)
        right.append(f)

        f, b = section(".normalize")
        self.normalize = check(b, "Normalize volume when re-encoding audio", h.normalize)
        right.append(f)

        f, b = section(".adjustsubs")
        self.adjust_subs = check(b, "Scale and position burned-in subtitles inside the picture",
                                 h.adjust_subs)
        right.append(f)

        f, b = section(".colorcorrection")
        self.colorcorr = check(b, "Correct colors (Rec.709 → Rec.601 for XviD)",
                               h.color_correction)
        right.append(f)

        f, b = section(".detect43")
        self.detect43 = check(b, "Detect and force 4:3 when close", h.detect_43)
        right.append(f)

        f, b = section(".compatibility")
        self.standalone = check(b, "Standalone player compatibility", h.standalone)
        row = Gtk.Box(spacing=12)
        self.chip = radio_group(["MTK/Sigma", "ESS"])
        for x in self.chip:
            row.append(x)
        self.chip[0 if h.standalone_chip == "mtk" else 1].set_active(True)
        b.append(row)
        right.append(f)

        f, b = section(".debug")
        self.keep_temp = check(b, "Keep temporary files", h.keep_temp)
        right.append(f)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      propagate_natural_height=True,
                                      max_content_height=760)
        scroller.set_child(outer)
        self.set_child(scroller)

        for w in (self.credits, self.ac_tune, self.crop_manual, self.aspect_override,
                  self.reduce_fps, self.standalone):
            w.connect("toggled", self._sync)
        self._sync()

    def _sync(self, *_):
        self.credits_start.set_sensitive_all(self.credits.get_active())
        self.credits_q.set_sensitive(self.credits.get_active())
        for w in (self.ac_thr, self.ac_n):
            w.set_sensitive(self.ac_tune.get_active())
        self.ac_start.set_sensitive_all(self.ac_tune.get_active())
        for w in (self.crop_t, self.crop_l, self.crop_r, self.crop_b):
            w.set_sensitive(self.crop_manual.get_active())
        for w in self.aspect:
            w.set_sensitive(self.aspect_override.get_active())
        for w in self.fps:
            w.set_sensitive(self.reduce_fps.get_active())
        for w in self.chip:
            w.set_sensitive(self.standalone.get_active())

    def apply(self) -> None:
        h = self.hid
        h.work_dir = self.work_dir.get_text().strip()
        h.mono = self.mono.get_active()
        h.cartoon = self.cartoon.get_active()
        h.force_ivtc = self.ivtc.get_active()
        h.force_deint = self.deint.get_active()
        h.credits = self.credits.get_active()
        h.credits_start = self.credits_start.seconds
        h.credits_quality = int(self.credits_q.get_value())
        h.autocrop_tune = self.ac_tune.get_active()
        h.autocrop_threshold = int(self.ac_thr.get_value())
        h.autocrop_samples = int(self.ac_n.get_value())
        h.autocrop_start = self.ac_start.seconds
        h.crop_manual = self.crop_manual.get_active()
        h.crop_top = int(self.crop_t.get_value())
        h.crop_left = int(self.crop_l.get_value())
        h.crop_right = int(self.crop_r.get_value())
        h.crop_bottom = int(self.crop_b.get_value())
        h.aspect_override = self.aspect_override.get_active()
        h.aspect = ["16:9", "4:3", "original"][[b.get_active() for b in self.aspect].index(True)]
        h.reduce_fps = self.reduce_fps.get_active()
        h.reduce_fps_rate = self.fps_rates[[b.get_active() for b in self.fps].index(True)]
        h.sharp_matrix = self.sharp.get_active()
        h.normalize = self.normalize.get_active()
        h.adjust_subs = self.adjust_subs.get_active()
        h.color_correction = self.colorcorr.get_active()
        h.detect_43 = self.detect43.get_active()
        h.standalone = self.standalone.get_active()
        h.standalone_chip = "mtk" if self.chip[0].get_active() else "ess"
        h.keep_temp = self.keep_temp.get_active()
        self.on_apply()
