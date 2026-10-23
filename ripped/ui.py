"""The main window – the same four-step flow as Auto Gordian Knot, in GTK4."""

from __future__ import annotations

import copy
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, VERSION, media  # noqa: E402
from .croppreview import CropPreviewDialog  # noqa: E402
from .dialogs import AdvancedDialog, HiddenDialog  # noqa: E402
from .engine import Aborted, Runner  # noqa: E402
from .jobs import (DATA_DIR, DEFAULT_PRESET, PRESETS, Job, Settings,  # noqa: E402
                   load_queue, save_queue, summary)

CSS = """
.step-card {
  border-radius: 12px;
  border: 1px solid alpha(currentColor, 0.12);
  background-color: alpha(currentColor, 0.035);
  padding: 12px 14px;
}
.step-badge {
  min-width: 24px; min-height: 24px;
  border-radius: 999px;
  background-color: @theme_selected_bg_color;
  color: @theme_selected_fg_color;
  font-weight: 800;
}
.step-title { font-weight: 700; font-size: 1.05em; }
.section-title { font-weight: 700; }
.field-label { font-weight: 600; }
.stream-info {
  font-family: monospace; font-size: 0.9em;
  padding: 4px 8px; border-radius: 6px;
  background-color: alpha(currentColor, 0.06);
}
.log-view { font-family: monospace; font-size: 0.88em; }
.log-view text { background-color: alpha(currentColor, 0.02); }
.pane-title { font-weight: 700; }
.status-pill {
  border-radius: 999px; padding: 1px 10px; font-size: 0.85em; font-weight: 600;
  background-color: alpha(currentColor, 0.10);
}
.status-pill.running { background-color: alpha(@theme_selected_bg_color, 0.85);
                       color: @theme_selected_fg_color; }
.status-pill.done { background-color: alpha(#2ec27e, 0.80); color: white; }
.status-pill.error { background-color: alpha(#e01b24, 0.80); color: white; }
.big-action { padding: 6px 18px; font-weight: 700; }
.summary-bar { padding: 4px 12px; font-size: 0.9em; }
.drop-hint { border: 2px dashed @theme_selected_bg_color; border-radius: 12px; }
.crop-stage { background-color: black; border-radius: 8px; }
.crop-status { padding: 8px 14px; border-radius: 8px; }
"""

STATUS_CLASS = {"running": "running", "done": "done", "failed": "error", "aborted": "error"}


def _ui(fn, *args):
    """Run fn on the GTK thread."""
    GLib.idle_add(lambda: (fn(*args), False)[1])


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: "RippedApp"):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1060, 820)
        self.app = app
        self.settings = Settings.load()
        self.jobs: list[Job] = load_queue()
        self.info: media.MediaInfo | None = None
        self.dvd_titles: list[media.DvdTitle] = []
        self.runner: Runner | None = None
        self.running = False
        self._log_file = None
        self._probe_serial = 0

        self._build_header()
        self._build_body()
        self._install_actions()
        self._install_drop()
        self._refresh_queue()
        self._update_summary()
        self._update_buttons()
        self.log(f"{APP_NAME} {VERSION} – XviD/x264 conversion for Linux.")
        if self.jobs:
            self.log(f"Restored {len(self.jobs)} job(s) from the previous session.")
        GLib.idle_add(self._check_tools)

    # ================================================================ UI

    def _build_header(self) -> None:
        header = Gtk.HeaderBar()
        title = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        t = Gtk.Label(label=APP_NAME)
        t.add_css_class("title")
        s = Gtk.Label(label="XviD and x264 conversion")
        s.add_css_class("subtitle")
        s.add_css_class("dim-label")
        title.append(t)
        title.append(s)
        header.set_title_widget(title)

        menu = Gio.Menu()
        menu.append("Advanced Settings", "win.advanced")
        menu.append("Hidden Options", "win.hidden")
        menu.append("Crop Preview", "win.crop-preview")
        menu.append("Open Log Folder", "win.open-logs")
        section = Gio.Menu()
        section.append("Keyboard Shortcuts", "win.shortcuts")
        section.append(f"About {APP_NAME}", "win.about")
        menu.append_section(None, section)
        mb = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                            tooltip_text="Menu")
        header.pack_end(mb)
        self.set_titlebar(header)

    def _card(self, number: int, title: str) -> tuple[Gtk.Box, Gtk.Box]:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.add_css_class("step-card")
        head = Gtk.Box(spacing=10)
        badge = Gtk.Label(label=str(number))
        badge.add_css_class("step-badge")
        lab = Gtk.Label(label=title, xalign=0)
        lab.add_css_class("step-title")
        head.append(badge)
        head.append(lab)
        card.append(head)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.append(body)
        return card, body

    def _field(self, text: str) -> Gtk.Label:
        lab = Gtk.Label(label=text, xalign=0)
        lab.add_css_class("field-label")
        return lab

    def _build_body(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(root)

        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "start", "end"):
            getattr(top, f"set_margin_{m}")(14)
        root.append(top)

        # ---- Step 1
        card, body = self._card(1, "Choose source and output file")
        self.drop_card = card
        g = Gtk.Grid(column_spacing=10, row_spacing=8)
        body.append(g)

        mode = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
        self.mode_file = Gtk.ToggleButton(label="File", active=True)
        self.mode_dvd = Gtk.ToggleButton(label="DVD", group=self.mode_file)
        self.mode_file.set_tooltip_text("Video file: MKV, MP4, AVI, MPG, VOB, TS …")
        self.mode_dvd.set_tooltip_text("DVD folder (VIDEO_TS) or ISO image – unencrypted")
        mode.append(self.mode_file)
        mode.append(self.mode_dvd)
        g.attach(mode, 0, 0, 1, 2)

        g.attach(self._field("Source:"), 1, 0, 1, 1)
        self.input_entry = Gtk.Entry(hexpand=True,
                                     placeholder_text="Choose or drop a file or DVD folder …")
        self.input_entry.connect("activate", lambda e: self._set_input(e.get_text()))
        g.attach(self.input_entry, 2, 0, 1, 1)
        b = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Browse (Ctrl+O)")
        b.connect("clicked", lambda *_: self._choose_input())
        g.attach(b, 3, 0, 1, 1)

        g.attach(self._field("Output:"), 1, 1, 1, 1)
        self.output_entry = Gtk.Entry(hexpand=True)
        g.attach(self.output_entry, 2, 1, 1, 1)
        b = Gtk.Button(icon_name="document-save-symbolic", tooltip_text="Save as (Ctrl+S)")
        b.connect("clicked", lambda *_: self._choose_output())
        g.attach(b, 3, 1, 1, 1)

        self.title_row = Gtk.Box(spacing=10, visible=False)
        self.title_row.append(self._field("DVD title:"))
        self.title_dd = Gtk.DropDown.new_from_strings([])
        self.title_dd.set_hexpand(True)
        self.title_dd.connect("notify::selected", self._on_title_changed)
        self.title_row.append(self.title_dd)
        body.append(self.title_row)

        self.video_row = Gtk.Box(spacing=10, visible=False)
        self.video_row.append(self._field("Video stream:"))
        self.video_dd = Gtk.DropDown.new_from_strings([])
        self.video_dd.set_hexpand(True)
        self.video_dd.connect("notify::selected", lambda *_: self._update_stream_info())
        self.video_row.append(self.video_dd)
        body.append(self.video_row)

        info_row = Gtk.Box(spacing=8)
        self.probe_spinner = Gtk.Spinner()
        self.stream_info = Gtk.Label(label="No source selected", xalign=0, hexpand=True,
                                     ellipsize=3)
        self.stream_info.add_css_class("stream-info")
        info_row.append(self.stream_info)
        info_row.append(self.probe_spinner)
        body.append(info_row)
        top.append(card)

        # ---- Step 2 + 3/4
        mid = Gtk.Box(spacing=12, homogeneous=True)
        top.append(mid)

        card, body = self._card(2, "Audio and subtitles")
        g = Gtk.Grid(column_spacing=10, row_spacing=8)
        body.append(g)
        g.attach(self._field("Audio track:"), 0, 0, 1, 1)
        self.audio1_dd = Gtk.DropDown.new_from_strings(["—"])
        self.audio1_dd.set_hexpand(True)
        g.attach(self.audio1_dd, 1, 0, 1, 1)
        self.audio2_dd = Gtk.DropDown.new_from_strings(["No second audio track"])
        g.attach(self.audio2_dd, 1, 1, 1, 1)
        g.attach(self._field("Subtitle:"), 0, 2, 1, 1)
        self.sub_dd = Gtk.DropDown.new_from_strings(["No subtitles"])
        g.attach(self.sub_dd, 1, 2, 1, 1)
        self.ext_sub_row = Gtk.Box(spacing=6, visible=False)
        self.ext_sub_entry = Gtk.Entry(hexpand=True,
                                       placeholder_text="External subtitle file to burn in (srt/ass/idx)")
        self.ext_sub_row.append(self.ext_sub_entry)
        b = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Choose subtitle file (Ctrl+U)")
        b.connect("clicked", lambda *_: self._choose_ext_subs())
        self.ext_sub_row.append(b)
        g.attach(self.ext_sub_row, 0, 3, 2, 1)
        mid.append(card)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        mid.append(right)

        card, body = self._card(3, "Output size")
        g = Gtk.Grid(column_spacing=10, row_spacing=8)
        body.append(g)
        self.rb_preset = Gtk.CheckButton(label="Predefined size:", active=True)
        self.rb_custom = Gtk.CheckButton(label="Custom size (MB):", group=self.rb_preset)
        self.rb_quality = Gtk.CheckButton(label="Target quality (%):", group=self.rb_preset)
        self.preset_dd = Gtk.DropDown.new_from_strings([p[0] for p in PRESETS])
        self.preset_dd.set_selected(DEFAULT_PRESET)
        self.preset_dd.set_hexpand(True)
        self.custom_spin = Gtk.SpinButton.new_with_range(10, 50000, 10)
        self.custom_spin.set_value(1400)
        self.quality_spin = Gtk.SpinButton.new_with_range(10, 100, 1)
        self.quality_spin.set_value(75)
        g.attach(self.rb_preset, 0, 0, 1, 1)
        g.attach(self.preset_dd, 1, 0, 1, 1)
        g.attach(self.rb_custom, 0, 1, 1, 1)
        g.attach(self.custom_spin, 1, 1, 1, 1)
        g.attach(self.rb_quality, 0, 2, 1, 1)
        g.attach(self.quality_spin, 1, 2, 1, 1)
        for rb in (self.rb_preset, self.rb_custom, self.rb_quality):
            rb.connect("toggled", lambda *_: self._sync_size())
        self._sync_size()
        right.append(card)

        card, body = self._card(4, "Advanced (optional)")
        row = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True)
        for i, (label, icon, action) in enumerate((
                ("Settings", "emblem-system-symbolic", "win.advanced"),
                ("Hidden options", "view-more-horizontal-symbolic", "win.hidden"),
                ("Crop", "zoom-fit-best-symbolic", "win.crop-preview"),
                ("Preview", "media-playback-start-symbolic", "win.preview"))):
            b = Gtk.Button(action_name=action)
            bx = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
            bx.append(Gtk.Image(icon_name=icon))
            bx.append(Gtk.Label(label=label))
            b.set_child(bx)
            if action == "win.preview":
                self.preview_btn = b
            row.attach(b, i % 2, i // 2, 1, 1)
        body.append(row)
        right.append(card)

        # ---- Action row
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.add_btn = Gtk.Button(label="Add job", action_name="win.add-job")
        self.add_btn.add_css_class("big-action")
        self.start_btn = Gtk.Button(label="Start", action_name="win.start")
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.add_css_class("big-action")
        self.abort_btn = Gtk.Button(label="Abort", action_name="win.abort")
        self.abort_btn.add_css_class("destructive-action")
        self.abort_btn.add_css_class("big-action")
        for b in (self.add_btn, self.start_btn, self.abort_btn):
            actions.append(b)
        top.append(actions)

        # ---- Log + queue
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True,
                          wide_handle=True, position=560)
        paned.set_margin_start(14)
        paned.set_margin_end(14)
        paned.set_margin_top(10)
        paned.set_margin_bottom(8)
        root.append(paned)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        lh = Gtk.Box(spacing=8)
        lt = Gtk.Label(label="Log", xalign=0, hexpand=True)
        lt.add_css_class("pane-title")
        lh.append(lt)
        self.shutdown_cb = Gtk.CheckButton(label="Shut down the computer when everything is done")
        lh.append(self.shutdown_cb)
        left.append(lh)
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_view.add_css_class("log-view")
        self.log_view.set_left_margin(8)
        self.log_view.set_right_margin(8)
        self.log_view.set_top_margin(6)
        sw = Gtk.ScrolledWindow(vexpand=True, child=self.log_view)
        sw.add_css_class("frame")
        left.append(sw)
        paned.set_start_child(left)
        paned.set_resize_start_child(True)

        qbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        qt = Gtk.Label(label="Job queue", xalign=0)
        qt.add_css_class("pane-title")
        qbox.append(qt)
        qrow = Gtk.Box(spacing=6, vexpand=True)
        self.queue = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.queue.set_placeholder(Gtk.Label(label="The queue is empty", css_classes=["dim-label"]))
        self.queue.connect("row-selected", lambda *_: self._update_buttons())
        qsw = Gtk.ScrolledWindow(vexpand=True, hexpand=True, child=self.queue)
        qsw.add_css_class("frame")
        qrow.append(qsw)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER)
        self.up_btn = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Move up (Ctrl+↑)",
                                 action_name="win.job-up")
        self.del_btn = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove (Ctrl+D)",
                                  action_name="win.job-delete")
        self.down_btn = Gtk.Button(icon_name="go-down-symbolic", tooltip_text="Move down (Ctrl+↓)",
                                   action_name="win.job-down")
        for b in (self.up_btn, self.del_btn, self.down_btn):
            b.add_css_class("circular")
            side.append(b)
        qrow.append(side)
        qbox.append(qrow)
        paned.set_end_child(qbox)
        paned.set_resize_end_child(True)

        # ---- Status bar
        status = Gtk.Box(spacing=12)
        status.add_css_class("summary-bar")
        status.set_margin_start(6)
        status.set_margin_end(6)
        status.set_margin_bottom(6)
        self.summary = Gtk.Label(xalign=0, hexpand=True)
        self.summary.add_css_class("dim-label")
        status.append(self.summary)
        self.progress = Gtk.ProgressBar(show_text=True, valign=Gtk.Align.CENTER)
        self.progress.set_size_request(380, -1)
        self.progress.set_text("Ready")
        status.append(self.progress)
        root.append(status)

    def _install_actions(self) -> None:
        acts = {
            "open-input": (self._choose_input, ["<Ctrl>o"]),
            "save-output": (self._choose_output, ["<Ctrl>s"]),
            "ext-subs": (self._choose_ext_subs, ["<Ctrl>u"]),
            "toggle-ext-subs": (self._toggle_ext_subs, ["<Ctrl>F8"]),
            "advanced": (self._show_advanced, ["<Ctrl>e"]),
            "hidden": (self._show_hidden, ["<Ctrl>F9"]),
            "preview": (self._preview, ["<Ctrl>p"]),
            "crop-preview": (self._crop_preview, ["<Ctrl>b"]),
            "add-job": (self._add_job, ["<Ctrl>Return"]),
            "start": (self._start, ["<Ctrl>r"]),
            "abort": (self._confirm_abort, ["<Ctrl>period"]),
            "job-up": (lambda: self._move_job(-1), ["<Ctrl>Up"]),
            "job-down": (lambda: self._move_job(1), ["<Ctrl>Down"]),
            "job-delete": (self._delete_job, ["<Ctrl>d"]),
            "open-logs": (self._open_logs, []),
            "shortcuts": (self._show_shortcuts, ["<Ctrl>question"]),
            "about": (self._show_about, []),
        }
        for name, (fn, accels) in acts.items():
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, f=fn: f())
            self.add_action(a)
            if accels:
                self.app.set_accels_for_action(f"win.{name}", accels)

    def _install_drop(self) -> None:
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.connect("drop", self._on_drop)
        target.connect("enter", lambda *_: (self.drop_card.add_css_class("drop-hint"),
                                            Gdk.DragAction.COPY)[1])
        target.connect("leave", lambda *_: self.drop_card.remove_css_class("drop-hint"))
        self.add_controller(target)

    def _on_drop(self, _t, value, _x, _y) -> bool:
        self.drop_card.remove_css_class("drop-hint")
        files = value.get_files()
        if not files:
            return False
        path = files[0].get_path()
        if path:
            self._set_input(path)
        return True

    # ================================================================ log

    def log(self, text: str) -> None:
        buf = self.log_view.get_buffer()
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {text}\n" if not text.startswith("$ ") else f"           {text}\n"
        buf.insert(buf.get_end_iter(), line)
        mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.log_view.scroll_mark_onscreen(mark)
        buf.delete_mark(mark)
        if self._log_file:
            self._log_file.write(line)
            self._log_file.flush()

    def _alert(self, message: str, detail: str = "") -> None:
        d = Gtk.AlertDialog()
        d.set_message(message)
        if detail:
            d.set_detail(detail)
        d.show(self)

    def _check_tools(self) -> bool:
        missing = [t for t in ("ffmpeg", "ffprobe") if not media.which(t)]
        if missing:
            self._alert("ffmpeg is missing", "Install ffmpeg (with libxvid and libx264).")
            return False
        enc = media.available_encoders()
        need = {"libxvid": "XviD", "libx264": "x264", "libmp3lame": "LAME MP3"}
        gone = [name for lib, name in need.items() if lib not in enc]
        if gone:
            self._alert("Codecs missing in ffmpeg",
                        "Your ffmpeg lacks: " + ", ".join(gone) +
                        ".\nInstall an ffmpeg build with --enable-libxvid --enable-libx264 "
                        "--enable-libmp3lame.")
            if "XviD" in gone and "x264" not in gone:
                self.settings.advanced.codec = "x264"
                if self.settings.advanced.container == "avi":
                    self.settings.advanced.container = "mkv"
                self._update_summary()
        else:
            self.log("Found ffmpeg with libxvid, libx264 and libmp3lame.")
        return False

    # ============================================================= input

    def _file_dialog(self, title: str) -> Gtk.FileDialog:
        d = Gtk.FileDialog(title=title, modal=True)
        if self.settings.last_dir and Path(self.settings.last_dir).is_dir():
            d.set_initial_folder(Gio.File.new_for_path(self.settings.last_dir))
        return d

    def _choose_input(self) -> None:
        if self.mode_dvd.get_active():
            d = self._file_dialog("Choose DVD folder (VIDEO_TS or the disc root)")
            d.select_folder(self, None, self._on_input_chosen, "folder")
            return
        d = self._file_dialog("Choose source file")
        f = Gtk.FileFilter(name="Video files and DVD images")
        for ext in sorted(media.VIDEO_EXTS):
            f.add_suffix(ext[1:])
        anyf = Gtk.FileFilter(name="All files")
        anyf.add_pattern("*")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        store.append(anyf)
        d.set_filters(store)
        d.open(self, None, self._on_input_chosen, "file")

    def _on_input_chosen(self, dialog: Gtk.FileDialog, result, kind: str) -> None:
        try:
            gfile = dialog.select_folder_finish(result) if kind == "folder" \
                else dialog.open_finish(result)
        except GLib.Error:
            return
        if gfile and gfile.get_path():
            self._set_input(gfile.get_path())

    def _set_input(self, path: str) -> None:
        path = path.strip()
        if not path:
            return
        p = Path(path)
        if not p.exists():
            self._alert("The source does not exist", path)
            return
        self.settings.last_dir = str(p.parent if p.is_file() else p)
        if self.settings.hidden.crop_manual and path != self.input_entry.get_text().strip():
            self.settings.hidden.crop_manual = False
            self.settings.save()
            self.log("New source – manual crop reset to automatic.")
        dvd = media.is_dvd(path)
        (self.mode_dvd if dvd else self.mode_file).set_active(True)
        self.input_entry.set_text(path)
        self._suggest_output(path, dvd)
        self._probe(path, dvd)

    def _suggest_output(self, path: str, dvd: bool) -> None:
        ext = "." + self.settings.advanced.container
        p = Path(path)
        if dvd:
            root = Path(media.dvd_root(path))
            name = root.stem if root.suffix.lower() == ".iso" else root.name
            out = root.parent / f"{name}{ext}"
        else:
            out = p.with_suffix(ext)
            if out == p:
                out = p.with_name(f"{p.stem}.ripped{ext}")
        self.output_entry.set_text(str(out))

    def _choose_output(self) -> None:
        d = self._file_dialog("Save as")
        cur = self.output_entry.get_text()
        if cur:
            d.set_initial_name(Path(cur).name)
            if Path(cur).parent.is_dir():
                d.set_initial_folder(Gio.File.new_for_path(str(Path(cur).parent)))
        d.save(self, None, self._on_output_chosen)

    def _on_output_chosen(self, dialog, result) -> None:
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return
        if gfile and gfile.get_path():
            p = Path(gfile.get_path())
            self.output_entry.set_text(str(p.with_suffix("." + self.settings.advanced.container)))

    def _choose_ext_subs(self) -> None:
        self.ext_sub_row.set_visible(True)
        d = self._file_dialog("Choose subtitle file")
        f = Gtk.FileFilter(name="Subtitles (srt, ass, ssa, idx, sub)")
        for ext in ("srt", "ass", "ssa", "idx", "sub", "txt"):
            f.add_suffix(ext)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        d.set_filters(store)
        d.open(self, None, self._on_ext_subs)

    def _on_ext_subs(self, dialog, result) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        if gfile and gfile.get_path():
            self.ext_sub_entry.set_text(gfile.get_path())

    def _toggle_ext_subs(self) -> None:
        on = not self.ext_sub_row.get_visible()
        self.ext_sub_row.set_visible(on)
        if not on:
            self.ext_sub_entry.set_text("")
        self.log("External input subtitles " + ("enabled." if on else "disabled."))

    # ============================================================= probe

    def _probe(self, path: str, dvd: bool) -> None:
        self._probe_serial += 1
        serial = self._probe_serial
        self.info = None
        self.dvd_titles = []
        self.probe_spinner.start()
        self.stream_info.set_label("Reading DVD titles …" if dvd else "Analyzing source …")
        self.title_row.set_visible(False)
        self._update_buttons()

        def work():
            try:
                if dvd:
                    titles = media.list_dvd_titles(path)
                    _ui(self._on_dvd_titles, serial, titles)
                else:
                    info = media.probe(path)
                    _ui(self._on_probed, serial, info)
            except Exception as e:  # noqa: BLE001
                _ui(self._on_probe_error, serial, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_probe_error(self, serial: int, msg: str) -> None:
        if serial != self._probe_serial:
            return
        self.probe_spinner.stop()
        self.stream_info.set_label("Could not read the source")
        self.log(f"Error: {msg}")
        self._alert("Could not read the source", msg)

    def _on_dvd_titles(self, serial: int, titles: list[media.DvdTitle]) -> None:
        if serial != self._probe_serial:
            return
        self.dvd_titles = titles
        longest = max(range(len(titles)), key=lambda i: titles[i].duration)
        self.title_dd.set_model(Gtk.StringList.new(
            [t.label() + ("  (main movie)" if i == longest else "") for i, t in enumerate(titles)]))
        self.title_row.set_visible(True)
        self.title_dd.set_selected(longest)
        self._on_probed(serial, titles[longest].info)
        self.log(f"Found {len(titles)} DVD titles; selected title {titles[longest].number} "
                 f"({media.fmt_duration(titles[longest].duration)}).")

    def _on_title_changed(self, *_):
        i = self.title_dd.get_selected()
        if 0 <= i < len(self.dvd_titles) and self.info is not self.dvd_titles[i].info:
            self._on_probed(self._probe_serial, self.dvd_titles[i].info)

    def _on_probed(self, serial: int, info: media.MediaInfo) -> None:
        if serial != self._probe_serial:
            return
        self.probe_spinner.stop()
        self.info = info
        videos = info.video
        self.video_dd.set_model(Gtk.StringList.new([v.label() for v in videos]))
        self.video_row.set_visible(len(videos) > 1)
        if len(videos) > 1:
            self.log("Several video streams found – choose which one to encode.")
        audio = [a.label() for a in info.audio]
        self.audio1_dd.set_model(Gtk.StringList.new(audio or ["No audio"]))
        self.audio2_dd.set_model(Gtk.StringList.new(["No second audio track", *audio]))
        self._fill_subs()
        self._update_stream_info()
        self._update_buttons()

    def _fill_subs(self) -> None:
        items = ["No subtitles"]
        if self.info:
            items += [s.label() for s in self.info.subtitles]
            if self.settings.advanced.external_subs and len(self.info.subtitles) > 1:
                items.append("All subtitles")
        self.sub_dd.set_model(Gtk.StringList.new(items))

    def _update_stream_info(self) -> None:
        if not self.info:
            return
        v = self._selected_video()
        extra = f"  ·  {self.info.chapters} chapters" if self.info.chapters else ""
        self.stream_info.set_label(f"{v.label()}  ·  {media.fmt_duration(self.info.duration)}{extra}")

    def _selected_video(self) -> media.Stream:
        assert self.info
        i = self.video_dd.get_selected()
        vids = self.info.video
        return vids[i] if 0 <= i < len(vids) else vids[0]

    def _sync_size(self) -> None:
        self.preset_dd.set_sensitive(self.rb_preset.get_active())
        self.custom_spin.set_sensitive(self.rb_custom.get_active())
        self.quality_spin.set_sensitive(self.rb_quality.get_active())

    # ============================================================== jobs

    def _job_from_ui(self) -> Job | None:
        if not self.info:
            self._alert("No source", "Choose a source file or DVD first.")
            return None
        out = self.output_entry.get_text().strip()
        if not out:
            self._alert("No output file", "Specify where the result should be saved.")
            return None
        adv = copy.deepcopy(self.settings.advanced)
        out = str(Path(out).with_suffix("." + adv.container))
        info = self.info
        a1 = self.audio1_dd.get_selected()
        a2 = self.audio2_dd.get_selected()
        s = self.sub_dd.get_selected()
        subs = info.subtitles
        subtitle = None
        if 1 <= s <= len(subs):
            subtitle = subs[s - 1].index
        elif s == len(subs) + 1:
            subtitle = -1
        job = Job(
            input=self.input_entry.get_text().strip(),
            output=out,
            dvd_title=info.dvd_title,
            video_index=self._selected_video().index,
            audio1=info.audio[a1].index if 0 <= a1 < len(info.audio) else None,
            audio2=info.audio[a2 - 1].index if 1 <= a2 <= len(info.audio) else None,
            subtitle=subtitle,
            ext_subs_file=self.ext_sub_entry.get_text().strip()
            if self.ext_sub_row.get_visible() else "",
            advanced=adv,
            hidden=copy.deepcopy(self.settings.hidden),
        )
        if self.rb_quality.get_active():
            job.size_mode, job.quality = "quality", int(self.quality_spin.get_value())
        elif self.rb_custom.get_active():
            job.size_mode, job.size_mb = "custom", int(self.custom_spin.get_value())
        else:
            job.size_mode, job.size_mb = "preset", PRESETS[self.preset_dd.get_selected()][1]
        return job

    def _add_job(self) -> None:
        job = self._job_from_ui()
        if not job:
            return
        if any(j.output == job.output and j.status != "done" for j in self.jobs):
            self._alert("Duplicate", f"There is already a job writing to\n{job.output}")
            return
        self.jobs.append(job)
        save_queue(self.jobs)
        self.settings.save()
        self._refresh_queue()
        self.log(f"Added job: {Path(job.output).name}")

    def _job_desc(self, job: Job) -> str:
        if job.size_mode == "quality":
            size = f"quality {job.quality} %"
        else:
            size = f"{job.size_mb} MB" + (f" ({job.split_parts} parts)" if job.split_parts > 1 else "")
        codec = "XviD" if job.advanced.codec == "xvid" else "x264"
        title = f"title {job.dvd_title} · " if job.dvd_title is not None else ""
        return f"{title}{codec} · {size} · {job.advanced.container.upper()}"

    def _refresh_queue(self) -> None:
        selected = self.queue.get_selected_row()
        sel_index = selected.get_index() if selected else -1
        while (row := self.queue.get_row_at_index(0)) is not None:
            self.queue.remove(row)
        for job in self.jobs:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=10)
            for m in ("top", "bottom", "start", "end"):
                getattr(box, f"set_margin_{m}")(6)
            cb = Gtk.CheckButton(active=job.enabled, valign=Gtk.Align.CENTER)
            cb.connect("toggled", lambda b, j=job: self._toggle_job(j, b.get_active()))
            box.append(cb)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            name = Gtk.Label(label=Path(job.output).name, xalign=0, ellipsize=3)
            desc = Gtk.Label(label=self._job_desc(job), xalign=0, ellipsize=3)
            desc.add_css_class("dim-label")
            desc.add_css_class("caption")
            texts.append(name)
            texts.append(desc)
            box.append(texts)
            pill = Gtk.Label(label=job.status, valign=Gtk.Align.CENTER)
            pill.add_css_class("status-pill")
            if job.status in STATUS_CLASS:
                pill.add_css_class(STATUS_CLASS[job.status])
            box.append(pill)
            row.set_child(box)
            row.set_tooltip_text(f"{job.input}\n→ {job.output}")
            self.queue.append(row)
        if 0 <= sel_index < len(self.jobs):
            self.queue.select_row(self.queue.get_row_at_index(sel_index))
        self._update_buttons()

    def _toggle_job(self, job: Job, on: bool) -> None:
        job.enabled = on
        save_queue(self.jobs)

    def _selected_job_index(self) -> int:
        row = self.queue.get_selected_row()
        return row.get_index() if row else -1

    def _move_job(self, delta: int) -> None:
        i = self._selected_job_index()
        j = i + delta
        if i < 0 or not (0 <= j < len(self.jobs)):
            return
        if self.running and "running" in (self.jobs[i].status, self.jobs[j].status):
            return
        self.jobs[i], self.jobs[j] = self.jobs[j], self.jobs[i]
        save_queue(self.jobs)
        self._refresh_queue()
        self.queue.select_row(self.queue.get_row_at_index(j))

    def _delete_job(self) -> None:
        i = self._selected_job_index()
        if i < 0 or (self.running and self.jobs[i].status == "running"):
            return
        del self.jobs[i]
        save_queue(self.jobs)
        self._refresh_queue()

    def _update_buttons(self) -> None:
        sel = self._selected_job_index()
        pending = any(j.enabled and j.status != "done" for j in self.jobs)
        enable = {
            "start": not self.running and pending,
            "abort": self.running,
            "add-job": self.info is not None,
            "preview": self.info is not None,
            "crop-preview": self.info is not None,
            "job-up": sel > 0,
            "job-down": 0 <= sel < len(self.jobs) - 1,
            "job-delete": sel >= 0 and not (self.running and self.jobs[sel].status == "running"),
        }
        for name, on in enable.items():
            act = self.lookup_action(name)
            if act:
                act.set_enabled(on)

    def _update_summary(self) -> None:
        self.summary.set_label(summary(self.settings.advanced))

    # ============================================================ dialogs

    def _show_advanced(self) -> None:
        def applied():
            self.settings.save()
            self._update_summary()
            self._fill_subs()
            out = self.output_entry.get_text().strip()
            if out:
                self.output_entry.set_text(
                    str(Path(out).with_suffix("." + self.settings.advanced.container)))
        AdvancedDialog(self, self.settings.advanced, applied).present()

    def _show_hidden(self) -> None:
        HiddenDialog(self, self.settings.hidden, self.settings.save).present()

    def _crop_preview(self) -> None:
        job = self._job_from_ui()
        if not job:
            return
        CropPreviewDialog(self, job, self.settings.hidden, self.log,
                          self.settings.save).present()

    def _show_about(self) -> None:
        about = Gtk.AboutDialog(
            transient_for=self, modal=True, program_name=APP_NAME, version=VERSION,
            logo_icon_name=APP_ID,
            comments="A small tribute to Auto Gordian Knot, rebuilt for modern Linux.\n"
                     "Converts DVDs and video files to XviD or x264 with automatic "
                     "cropping, deinterlacing/IVTC, a compressibility test and "
                     "target size in one or two passes.",
            license_type=Gtk.License.GPL_2_0,
            website="https://github.com/soulspin/Ripped",
            website_label="Powered by FFmpeg, libxvid, x264 and LAME",
        )
        about.present()

    def _show_shortcuts(self) -> None:
        self._alert("Keyboard Shortcuts", "\n".join([
            "Ctrl+O      Choose source",
            "Ctrl+S      Choose output file",
            "Ctrl+U      Choose external subtitle file",
            "Ctrl+F8     Show/hide external input subtitles",
            "Ctrl+E      Advanced settings",
            "Ctrl+F9     Hidden options",
            "Ctrl+B      Crop preview",
            "Ctrl+P      Preview",
            "Ctrl+Enter  Add job",
            "Ctrl+R      Start encoding",
            "Ctrl+.      Abort encoding",
            "Ctrl+↑/↓    Move job",
            "Ctrl+D      Remove job",
        ]))

    def _open_logs(self) -> None:
        d = DATA_DIR / "logs"
        d.mkdir(parents=True, exist_ok=True)
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(d))).launch(self, None, None)

    # ============================================================ preview

    def _preview(self) -> None:
        job = self._job_from_ui()
        if not job:
            return
        self.preview_btn.set_sensitive(False)
        self.log("Preparing preview …")

        def work():
            try:
                runner = Runner(job, lambda m: _ui(self.log, m),
                                lambda st, f, i: _ui(self._on_progress, st, f, i))
                cmd = runner.preview_command()
                _ui(self.log, "$ " + " ".join(cmd))
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:  # noqa: BLE001
                _ui(self.log, f"The preview failed: {e}")
            finally:
                _ui(self.preview_btn.set_sensitive, True)
                _ui(self._reset_progress)

        threading.Thread(target=work, daemon=True).start()

    # ============================================================ encode

    def _start(self) -> None:
        if self.running:
            return
        todo = [j for j in self.jobs if j.enabled and j.status != "done"]
        if not todo:
            return
        self.running = True
        self._update_buttons()
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        aborted = False
        done = failed = 0
        for job in list(self.jobs):
            if aborted or not self.running:
                break
            if not job.enabled or job.status == "done" or job not in self.jobs:
                continue
            _ui(self._set_status, job, "running")
            _ui(self._open_log_file, job)
            runner = Runner(job, lambda m: _ui(self.log, m),
                            lambda st, f, i: _ui(self._on_progress, st, f, i))
            self.runner = runner
            try:
                runner.run()
                status = "done"
                done += 1
            except Aborted:
                status = "aborted"
                aborted = True
            except Exception as e:  # noqa: BLE001
                status = "failed"
                failed += 1
                _ui(self.log, f"ERROR: {e}")
            self.runner = None
            _ui(self._set_status, job, status)
            _ui(self._close_log_file)
        _ui(self._finished, done, failed, aborted)

    def _set_status(self, job: Job, status: str) -> None:
        job.status = status
        save_queue(self.jobs)
        self._refresh_queue()

    def _open_log_file(self, job: Job) -> None:
        d = DATA_DIR / "logs"
        d.mkdir(parents=True, exist_ok=True)
        name = f"{datetime.now():%Y%m%d-%H%M%S}-{Path(job.output).stem}.log"
        self._log_file = open(d / name, "w", encoding="utf-8")  # noqa: SIM115

    def _close_log_file(self) -> None:
        if self._log_file:
            self._log_file.close()
            self._log_file = None

    def _on_progress(self, stage: str, frac: float, info: str) -> None:
        self.progress.set_fraction(frac)
        text = f"{stage} – {frac * 100:.0f} %"
        if info:
            text += f"  ·  {info}"
        self.progress.set_text(text)

    def _reset_progress(self) -> None:
        if not self.running:
            self.progress.set_fraction(0)
            self.progress.set_text("Ready")

    def _finished(self, done: int, failed: int, aborted: bool) -> None:
        self.running = False
        self._update_buttons()
        self._reset_progress()
        msg = f"{done} job(s) done" + (f", {failed} failed" if failed else "")
        if aborted:
            msg += ", aborted"
        self.log(f"Queue finished: {msg}.")
        n = Gio.Notification.new(f"{APP_NAME}: encoding finished")
        n.set_body(msg)
        self.app.send_notification("done", n)
        if self.shutdown_cb.get_active() and not aborted:
            self.log("Shutting down the computer in 60 seconds …")
            GLib.timeout_add_seconds(60, self._shutdown)

    def _shutdown(self) -> bool:
        if self.shutdown_cb.get_active():
            subprocess.Popen(["systemctl", "poweroff"])
        return False

    def _confirm_abort(self) -> None:
        if not self.running:
            return
        d = Gtk.AlertDialog()
        d.set_message("Abort encoding?")
        d.set_detail("The current job is stopped and has to be redone from the start.")
        d.set_buttons(["Keep encoding", "Abort job"])
        d.set_cancel_button(0)
        d.set_default_button(0)
        d.choose(self, None, self._on_abort_choice)

    def _on_abort_choice(self, dialog, result) -> None:
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            return
        if choice == 1 and self.runner:
            self.log("Aborting …")
            self.running = False
            self.runner.abort()


class RippedApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.window: MainWindow | None = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        provider = Gtk.CssProvider()
        if hasattr(provider, "load_from_string"):
            provider.load_from_string(CSS)
        else:
            provider.load_from_data(CSS.encode())
        display = Gdk.Display.get_default()
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # Running from the source tree: find the app icon next to the package.
        icons = Path(__file__).resolve().parent.parent / "data" / "icons"
        if icons.is_dir():
            Gtk.IconTheme.get_for_display(display).add_search_path(str(icons))
        Gtk.Window.set_default_icon_name(APP_ID)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Ctrl>q"])

    def do_activate(self):
        if not self.window:
            self.window = MainWindow(self)
            self.window.connect("close-request", self._on_close)
        self.window.present()

    def do_open(self, files, n_files, hint):
        self.do_activate()
        if files and files[0].get_path():
            self.window._set_input(files[0].get_path())

    def _on_close(self, win: MainWindow) -> bool:
        win.settings.save()
        save_queue(win.jobs)
        if win.running and win.runner:
            win.runner.abort()
        return False


def main(argv: list[str]) -> int:
    return RippedApp().run(argv)
