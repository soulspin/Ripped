"""Crop preview: a video frame with the crop rectangle drawn on top."""

from __future__ import annotations

import threading
from typing import Callable

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .dialogs import spin  # noqa: E402
from .engine import Aborted, Crop, Runner  # noqa: E402
from .jobs import HiddenOptions, Job  # noqa: E402
from .media import fmt_duration  # noqa: E402

EDGES = ("top", "bottom", "left", "right")
GRAB_PX = 14        # how close to an edge (in screen pixels) the pointer must be to drag it
MIN_SIDE = 32       # minimum width/height after cropping


def _ui(fn, *args):
    GLib.idle_add(lambda: (fn(*args), False)[1])


class CropPreviewDialog(Gtk.Window):
    def __init__(self, parent: Gtk.Window, job: Job, hidden: HiddenOptions,
                 log: Callable[[str], None], on_apply: Callable[[], None]):
        super().__init__(title="Crop Preview", transient_for=parent, modal=True,
                         default_width=900, default_height=720)
        self.hidden = hidden
        self.log = log
        self.on_apply = on_apply
        self.runner = Runner(job, lambda m: _ui(log, m), self._on_progress)
        self.inp: list[str] = []
        self.info = None
        self.v = None
        self.W = self.H = self.disp_w = 0
        self.auto: Crop | None = None
        self._closed = False
        self._busy = False
        self._frame_serial = 0
        self._seek_source = 0
        self._drag_edge: str | None = None
        self._drag_start = 0.0
        self._syncing = False

        header = Gtk.HeaderBar()
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        self.ok = Gtk.Button(label="Apply", sensitive=False)
        self.ok.add_css_class("suggested-action")
        self.ok.connect("clicked", self._on_ok)
        header.pack_start(cancel)
        header.pack_end(self.ok)
        self.set_titlebar(header)
        self.connect("close-request", self._on_close)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for m in ("top", "bottom", "start", "end"):
            getattr(root, f"set_margin_{m}")(14)
        self.set_child(root)

        # ---- Picture + frame
        overlay = Gtk.Overlay(vexpand=True, hexpand=True)
        overlay.add_css_class("crop-stage")
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.picture.set_size_request(640, 360)
        overlay.set_child(self.picture)
        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_draw_func(self._draw)
        overlay.add_overlay(self.area)
        self.status = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.status.add_css_class("osd")
        self.status.add_css_class("crop-status")
        self.spinner = Gtk.Spinner(spinning=True)
        self.status_label = Gtk.Label(label="Reading source …")
        self.status.append(self.spinner)
        self.status.append(self.status_label)
        overlay.add_overlay(self.status)
        root.append(overlay)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", lambda *_: setattr(self, "_drag_edge", None))
        self.area.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.area.add_controller(motion)

        # ---- Position
        pos = Gtk.Box(spacing=10)
        lab = Gtk.Label(label="Position:")
        lab.add_css_class("field-label")
        pos.append(lab)
        self.scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                               draw_value=False, sensitive=False)
        self.scale.set_range(0, 1)
        self.scale.connect("value-changed", self._on_seek)
        pos.append(self.scale)
        self.time_label = Gtk.Label(label="0:00:00", width_chars=8)
        self.time_label.add_css_class("numeric")
        pos.append(self.time_label)
        root.append(pos)

        # ---- Crop
        row = Gtk.Box(spacing=16)
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.spins: dict[str, Gtk.SpinButton] = {}
        for i, (edge, text) in enumerate((("top", "Top:"), ("bottom", "Bottom:"),
                                          ("left", "Left:"), ("right", "Right:"))):
            s = spin(0, 4000, 2, 0)
            s.set_sensitive(False)
            s.connect("value-changed", self._on_spin)
            self.spins[edge] = s
            grid.attach(Gtk.Label(label=text, xalign=1), (i // 2) * 2, i % 2, 1, 1)
            grid.attach(s, (i // 2) * 2 + 1, i % 2, 1, 1)
        row.append(grid)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                       valign=Gtk.Align.CENTER)
        self.result_label = Gtk.Label(xalign=0)
        self.result_label.add_css_class("field-label")
        self.source_label = Gtk.Label(xalign=0)
        self.source_label.add_css_class("dim-label")
        info.append(self.result_label)
        info.append(self.source_label)
        row.append(info)

        btns = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.auto_btn = Gtk.Button(label="Auto-detect", sensitive=False,
                                   tooltip_text="Run autocrop with the settings from Hidden options")
        self.auto_btn.connect("clicked", lambda *_: self._detect())
        self.reset_btn = Gtk.Button(label="No crop", sensitive=False)
        self.reset_btn.connect("clicked", lambda *_: self._set_crop(0, 0, 0, 0))
        btns.append(self.auto_btn)
        btns.append(self.reset_btn)
        row.append(btns)
        root.append(row)

        hint = Gtk.Label(xalign=0, wrap=True,
                         label="Drag the edges of the frame or change the values. A changed crop "
                               "is saved as \"Force crop\" and applies until you choose "
                               "a new source.")
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        root.append(hint)

        self._busy = True
        threading.Thread(target=self._load, daemon=True).start()

    # ============================================================== work

    def _load(self) -> None:
        try:
            inp, info, v = self.runner.preview_source()
            _ui(self._on_source, inp, info, v)
            if self.hidden.crop_manual:
                return
            auto = self.runner.detect_crop(inp, info, v)
            _ui(self._on_detected, auto)
        except Aborted:
            pass
        except Exception as e:  # noqa: BLE001
            _ui(self._on_error, str(e))

    def _on_source(self, inp, info, v) -> None:
        if self._closed:
            return
        self.inp, self.info, self.v = inp, info, v
        self.W, self.H = v.width, v.height
        self.disp_w = max(2, round(v.width * float(v.sar or 1) / 2) * 2)
        self.spins["top"].set_range(0, self.H - MIN_SIDE)
        self.spins["bottom"].set_range(0, self.H - MIN_SIDE)
        self.spins["left"].set_range(0, self.W - MIN_SIDE)
        self.spins["right"].set_range(0, self.W - MIN_SIDE)
        self.source_label.set_label(f"Source: {v.width}×{v.height}, "
                                    f"aspect ratio {v.dar:.3f}")
        self.scale.set_range(0, max(1.0, info.duration))
        self._syncing = True
        self.scale.set_value(info.duration * 0.33)
        self._syncing = False
        self.scale.set_sensitive(True)
        h = self.hidden
        if h.crop_manual:
            self._set_crop(h.crop_top, h.crop_bottom, h.crop_left, h.crop_right)
            self._idle()
            self.result_label.set_label(self.result_label.get_label() + "  (manual)")
        else:
            self.status_label.set_label("Looking for black borders …")
        self._grab()

    def _on_detected(self, c: Crop) -> None:
        if self._closed:
            return
        self.auto = c
        self._set_crop(c.y, self.H - c.h - c.y, c.x, self.W - c.w - c.x)
        self._idle()

    def _on_error(self, msg: str) -> None:
        if self._closed:
            return
        self._busy = False
        self.spinner.set_spinning(False)
        self.status_label.set_label(f"Error: {msg}")
        self.status.set_visible(True)

    def _idle(self) -> None:
        self._busy = False
        self.status.set_visible(False)
        for w in (*self.spins.values(), self.auto_btn, self.reset_btn, self.ok):
            w.set_sensitive(True)

    def _detect(self) -> None:
        if self._busy or not self.v:
            return
        self._busy = True
        self.auto_btn.set_sensitive(False)
        self.status_label.set_label("Looking for black borders …")
        self.spinner.set_spinning(True)
        self.status.set_visible(True)

        def work():
            try:
                c = self.runner.detect_crop(self.inp, self.info, self.v)
                _ui(self._on_detected, c)
            except Aborted:
                pass
            except Exception as e:  # noqa: BLE001
                _ui(self._on_error, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, stage: str, frac: float, _info: str) -> None:
        _ui(lambda: self._closed or self.status_label.set_label(
            f"Looking for black borders … {frac * 100:.0f} %"))

    def _on_seek(self, *_) -> None:
        self.time_label.set_label(fmt_duration(self.scale.get_value()))
        if self._syncing:
            return
        if self._seek_source:
            GLib.source_remove(self._seek_source)
        self._seek_source = GLib.timeout_add(250, self._seek_timeout)

    def _seek_timeout(self) -> bool:
        self._seek_source = 0
        self._grab()
        return False

    def _grab(self) -> None:
        if not self.v:
            return
        self._frame_serial += 1
        serial = self._frame_serial
        pos = self.scale.get_value()
        inp, v, dw = self.inp, self.v, self.disp_w

        def work():
            try:
                png = Runner.grab_frame(inp, v, pos, dw)
                _ui(self._on_frame, serial, png)
            except Exception as e:  # noqa: BLE001
                _ui(self.log, f"Crop preview: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _on_frame(self, serial: int, png: bytes) -> None:
        if self._closed or serial != self._frame_serial:
            return
        try:
            tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
        except GLib.Error as e:
            self.log(f"Crop preview: {e.message}")
            return
        self.picture.set_paintable(tex)
        self.area.queue_draw()

    # ================================================================ crop

    def _crop(self) -> tuple[int, int, int, int]:
        return tuple(int(self.spins[e].get_value()) for e in EDGES)  # type: ignore[return-value]

    def _set_crop(self, t: int, b: int, l: int, r: int) -> None:
        self._syncing = True
        for edge, val in zip(EDGES, (t, b, l, r)):
            self.spins[edge].set_value(val)
        self._syncing = False
        self._on_spin(None)

    def _on_spin(self, changed: Gtk.SpinButton | None) -> None:
        if self._syncing or not self.v:
            return
        t, b, l, r = self._crop()
        # Keep at least MIN_SIDE pixels – adjust the edge that was just changed.
        if changed is not None:
            edge = next(e for e, s in self.spins.items() if s is changed)
            over_h = t + b - (self.H - MIN_SIDE)
            over_w = l + r - (self.W - MIN_SIDE)
            if edge in ("top", "bottom") and over_h > 0:
                self._syncing = True
                changed.set_value(changed.get_value() - over_h)
                self._syncing = False
            elif edge in ("left", "right") and over_w > 0:
                self._syncing = True
                changed.set_value(changed.get_value() - over_w)
                self._syncing = False
            t, b, l, r = self._crop()
        w, h = self.W - l - r, self.H - t - b
        ar = w * float(self.v.sar or 1) / h
        text = f"Result: {w}×{h}  ·  aspect ratio {ar:.3f}"
        if self.auto and (t, b, l, r) == self._auto_tuple():
            text += "  (automatic)"
        self.result_label.set_label(text)
        self.area.queue_draw()

    def _auto_tuple(self) -> tuple[int, int, int, int]:
        c = self.auto
        assert c
        return c.y, self.H - c.h - c.y, c.x, self.W - c.w - c.x

    # ============================================================= drawing

    def _image_rect(self, width: int, height: int) -> tuple[float, float, float]:
        """(x, y, scale) of the picture inside the area – same as Gtk.ContentFit.CONTAIN."""
        if not self.disp_w or not self.H:
            return 0, 0, 0
        scale = min(width / self.disp_w, height / self.H)
        return (width - self.disp_w * scale) / 2, (height - self.H * scale) / 2, scale

    def _crop_rect(self, width: int, height: int) -> tuple[float, float, float, float]:
        ox, oy, sc = self._image_rect(width, height)
        fx = self.disp_w / self.W * sc
        t, b, l, r = self._crop()
        return ox + l * fx, oy + t * sc, ox + (self.W - r) * fx, oy + (self.H - b) * sc

    def _draw(self, _area, cr, width: int, height: int) -> None:
        if not self.picture.get_paintable() or not self.v:
            return
        ox, oy, sc = self._image_rect(width, height)
        x0, y0, x1, y1 = self._crop_rect(width, height)
        # Darken what gets cropped away.
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.rectangle(ox, oy, self.disp_w * sc, self.H * sc)
        cr.rectangle(x0, y0, x1 - x0, y1 - y0)
        cr.set_source_rgba(0, 0, 0, 0.62)
        cr.fill()
        # Frame and handles.
        cr.set_source_rgba(0.21, 0.52, 0.89, 1)
        cr.set_line_width(2)
        cr.rectangle(x0, y0, x1 - x0, y1 - y0)
        cr.stroke()
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        for hx, hy in ((mx, y0), (mx, y1), (x0, my), (x1, my)):
            cr.rectangle(hx - 5, hy - 5, 10, 10)
        cr.fill()
        # Rule-of-thirds lines to judge the composition.
        cr.set_source_rgba(1, 1, 1, 0.25)
        cr.set_line_width(1)
        for k in (1, 2):
            x = x0 + (x1 - x0) * k / 3
            y = y0 + (y1 - y0) * k / 3
            cr.move_to(x, y0)
            cr.line_to(x, y1)
            cr.move_to(x0, y)
            cr.line_to(x1, y)
        cr.stroke()

    # ============================================================ dragging

    def _edge_at(self, x: float, y: float) -> str | None:
        if not self.v or not self.spins["top"].get_sensitive():
            return None
        x0, y0, x1, y1 = self._crop_rect(self.area.get_width(), self.area.get_height())
        cands = []
        if x0 - GRAB_PX <= x <= x1 + GRAB_PX:
            cands += [(abs(y - y0), "top"), (abs(y - y1), "bottom")]
        if y0 - GRAB_PX <= y <= y1 + GRAB_PX:
            cands += [(abs(x - x0), "left"), (abs(x - x1), "right")]
        cands = [c for c in cands if c[0] <= GRAB_PX]
        return min(cands)[1] if cands else None

    def _on_motion(self, _ctl, x: float, y: float) -> None:
        if self._drag_edge:
            return
        edge = self._edge_at(x, y)
        self.area.set_cursor_from_name(
            {"top": "ns-resize", "bottom": "ns-resize",
             "left": "ew-resize", "right": "ew-resize"}.get(edge or "", "default"))

    def _on_drag_begin(self, _g, x: float, y: float) -> None:
        self._drag_edge = self._edge_at(x, y)
        if self._drag_edge:
            x0, y0, x1, y1 = self._crop_rect(self.area.get_width(), self.area.get_height())
            self._drag_start = {"top": y0, "bottom": y1, "left": x0, "right": x1}[self._drag_edge]

    def _on_drag_update(self, _g, dx: float, dy: float) -> None:
        edge = self._drag_edge
        if not edge:
            return
        ox, oy, sc = self._image_rect(self.area.get_width(), self.area.get_height())
        if not sc:
            return
        fx = self.disp_w / self.W * sc
        pos = self._drag_start + (dy if edge in ("top", "bottom") else dx)
        val = {"top": (pos - oy) / sc, "bottom": self.H - (pos - oy) / sc,
               "left": (pos - ox) / fx, "right": self.W - (pos - ox) / fx}[edge]
        self.spins[edge].set_value(max(0, round(val / 2) * 2))

    # =============================================================== close

    def _on_ok(self, *_) -> None:
        t, b, l, r = self._crop()
        h = self.hidden
        if self.auto and (t, b, l, r) == self._auto_tuple():
            h.crop_manual = False
            self.log("Crop: automatic (as in the preview).")
        else:
            h.crop_manual = True
            h.crop_top, h.crop_bottom, h.crop_left, h.crop_right = t, b, l, r
            self.log(f"Crop: manual – top {t}, bottom {b}, left {l}, right {r} "
                     f"→ {self.W - l - r}×{self.H - t - b}.")
        self.on_apply()
        self.close()

    def _on_close(self, *_) -> bool:
        self._closed = True
        if self._seek_source:
            GLib.source_remove(self._seek_source)
            self._seek_source = 0
        self.runner.abort()
        return False
