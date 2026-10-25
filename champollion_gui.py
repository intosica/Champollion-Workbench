#!/usr/bin/env python3
"""
Champollion Workbench
======================
A comfortable native X11/Xorg (GTK3) front-end for the Papyrus modding
toolchain:

  * Champollion  -- decompiles .pex  -> .psc  (PEX to Papyrus source)
  * Caprica      -- compiles   .psc  -> .pex  (Papyrus source to PEX)

Neither tool is bundled here -- this app is a launcher/front-end that shells
out to the real command-line executables. Build them first (see the "Build
& Setup" tab, or scripts/build_champollion.sh) and point this app at the
resulting binaries.

Requires: Python 3 + PyGObject (GTK 3). On most distros:
    Debian/Ubuntu:  sudo apt install python3-gi gir1.2-gtk-3.0
    Fedora:         sudo dnf install python3-gobject gtk3
    Arch:           sudo pacman -S python-gobject gtk3

Run:
    python3 champollion_gui.py
"""

import json
import os
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gdk, Gio, Gtk, Pango  # noqa: E402

APP_ID = "org.champollion.workbench"
CONFIG_DIR = Path(GLib.get_user_config_dir()) / "champollion-gui"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Session data (input lists, folders, options -- can be sizable and change on
# every Run) lives next to this script instead of the XDG config dir, so it's
# portable with the app install. It's written immediately whenever it
# changes rather than only at clean shutdown, because a PC restart/logout
# typically kills this process outright (no GTK "destroy" signal ever
# fires) -- see save_session()/persist_session_now() below.
SCRIPT_DIR = Path(os.path.abspath(__file__)).resolve().parent
SESSION_DIR = SCRIPT_DIR / "tmp"
SESSION_FILE = SESSION_DIR / "champollion_session.json"

GAMES = ["skyrim", "skyrimvr", "fallout4", "fallout4vr", "starfield"]

DEFAULT_CONFIG = {
    "champollion_bin": "",
    "caprica_bin": "",
    "wine_bin": "wine",
    "wine_prefix": "",
    "default_game": "skyrim",
    "dark_theme": True,
    "save_session": False,
}


def info_icon(markup):
    """A small (i) icon that shows `markup` as a tooltip on hover -- used
    instead of a permanently-visible block of explanatory text, since this
    window doesn't support resizing and long notes eat the layout."""
    icon = Gtk.Image.new_from_icon_name("dialog-information-symbolic", Gtk.IconSize.BUTTON)
    icon.set_tooltip_markup(markup)
    icon.set_halign(Gtk.Align.START)
    return icon


def to_windows_path(path, cfg, env=None):
    """Translate an absolute Linux path to its Wine-side Windows-style
    equivalent (e.g. /media/foo/bar -> Z:\\media\\foo\\bar, or the real
    mapped drive letter if one exists) using `winepath -w`.

    Wine *does* auto-translate some Unix path arguments passed to a .exe,
    but that heuristic is unreliable -- it can silently fail for paths
    containing spaces, or ones on a mount with no drive-letter mapping.
    When that happens, the Windows program receives the raw Unix path,
    later concatenates it with a Windows-style "\\" + filename itself, and
    ends up trying to open a mixed-separator path that doesn't resolve to
    anything, which is exactly the "ios_base::failbit" crash this avoids.

    Returns the translated path, or the original path unchanged if
    `winepath` isn't available or the call fails for any reason (best
    effort -- never raises).
    """
    try:
        result = subprocess.run(
            ["winepath", "-w", path], env=env, capture_output=True, text=True, timeout=10
        )
        out = result.stdout.strip()
        if result.returncode == 0 and out:
            return out
    except (OSError, subprocess.TimeoutExpired):
        pass
    return path


def _translate_wine_arg(arg, cfg, env):
    """Translate the absolute-Unix-path portions of a single argv token.

    Most args here are plain flags/values ("-g", "skyrim") and pass through
    untouched. Import lists are a single ";"-joined token (e.g.
    "./."/abs/path1;/abs/path2"), so each ";"-separated piece is checked
    and translated independently rather than the token as a whole.
    """
    if not isinstance(arg, str):
        return arg
    parts = arg.split(";")
    changed = False
    new_parts = []
    for part in parts:
        if part.startswith("/"):
            translated = to_windows_path(part, cfg, env=env)
            if translated != part:
                changed = True
            new_parts.append(translated)
        else:
            new_parts.append(part)
    return ";".join(new_parts) if changed else arg


def wrap_command(cfg, binpath, args):
    """Build the actual argv + env to run `binpath` with `args`.

    Any path ending in .exe is a Windows binary: on Linux it can never be
    exec'd directly (there's no ELF interpreter for a PE image), so it is
    unconditionally run through Wine. There is deliberately no setting to
    turn this off -- a raw execve() of a .exe can only ever fail with
    "unable to find an interpreter", so gating Wine behind a togglable
    preference just invites that failure mode back in.
    Returns (cmd_list, env_dict_or_None).
    """
    if binpath.lower().endswith(".exe"):
        wine = cfg.get("wine_bin", "wine") or "wine"
        tokens = shlex.split(wine)
        env = None
        # Allow shell-style leading VAR=value assignments, e.g. "WINEARCH=win64 wine64"
        while tokens and "=" in tokens[0] and not tokens[0].startswith(("/", ".", "-")):
            key, _, val = tokens.pop(0).partition("=")
            env = env or dict(os.environ)
            env[key] = val
        if not tokens:
            tokens = ["wine"]
        prefix = (cfg.get("wine_prefix") or "").strip()
        if prefix:
            env = env or dict(os.environ)
            env["WINEPREFIX"] = prefix
        # Explicitly translate absolute Unix paths in the arguments (input/
        # output folders, flags files, etc.) to real Windows paths, rather
        # than trusting Wine's own argv auto-translation -- see
        # to_windows_path() for why that's needed.
        translated_args = [_translate_wine_arg(a, cfg, env) for a in args]
        cmd = tokens + [binpath] + translated_args
        return cmd, env
    return [binpath] + args, None


def read_pe_arch(path):
    """Peek at a Windows binary's own header and report what it actually is.

    Returns a short human string like "PE32+ (64-bit, x86-64)" or
    "PE32 (32-bit, i386)", or None if the file isn't a valid MZ/PE image
    at all (corrupted download, HTML error page saved with a .exe name,
    git-lfs pointer stub, etc). Raises OSError if the file can't be read.
    """
    with open(path, "rb") as f:
        dos = f.read(64)
        if len(dos) < 64 or dos[:2] != b"MZ":
            return None
        pe_offset = struct.unpack("<I", dos[60:64])[0]
        f.seek(pe_offset)
        sig = f.read(4)
        if sig != b"PE\x00\x00":
            return None
        machine_bytes = f.read(2)
        if len(machine_bytes) < 2:
            return None
        machine = struct.unpack("<H", machine_bytes)[0]
    return {
        0x014C: "PE32 (32-bit, i386)",
        0x8664: "PE32+ (64-bit, x86-64)",
        0xAA64: "PE32+ (64-bit, ARM64)",
        0x0200: "PE32+ (64-bit, IA64)",
    }.get(machine, f"PE (unrecognized machine type 0x{machine:04x})")


def wine_prefix_arch(prefix):
    """Read #arch=winNN out of a WINEPREFIX's system.reg, if it exists yet."""
    wineprefix = Path(prefix) if prefix else Path.home() / ".wine"
    reg = wineprefix / "system.reg"
    if not reg.exists():
        return None
    try:
        with open(reg, "r", errors="ignore") as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                if line.startswith("#arch="):
                    return line.strip().split("=", 1)[1]
    except OSError:
        return None
    return None


def diagnose_exe(path, cfg):
    """Best-effort explanation for why a .exe might fail as "Bad EXE format".

    Returns a list of (text, tag) lines suitable for panel.append_line.
    """
    lines = []
    try:
        arch = read_pe_arch(path)
    except OSError as e:
        lines.append((f"\u2717 Could not read {path}: {e}", "err"))
        return lines

    if arch is None:
        lines.append((
            f"\u2717 {path} is not a valid Windows PE executable (no MZ/PE "
            "header found). It's likely a corrupted or incomplete download, "
            "an HTML page saved with a .exe name, or a git-lfs pointer "
            "stub instead of the real file. Re-download it and check the "
            "file size looks right before retrying.",
            "err",
        ))
        return lines

    lines.append((f"\u2139 {os.path.basename(path)} is {arch}.", "dim"))

    is64 = "64-bit" in arch
    wine_bin = (cfg.get("wine_bin") or "wine").strip()
    # Strip any leading VAR=value tokens to find the real wine binary name.
    tokens = shlex.split(wine_bin) if wine_bin else ["wine"]
    while tokens and "=" in tokens[0] and not tokens[0].startswith(("/", ".", "-")):
        tokens.pop(0)
    wine_exe_name = tokens[0] if tokens else "wine"
    wine_path = shutil.which(wine_exe_name)
    have_wine64 = shutil.which("wine64") is not None

    prefix_arch = wine_prefix_arch(cfg.get("wine_prefix") or "")
    if prefix_arch:
        lines.append((f"\u2139 WINEPREFIX architecture: {prefix_arch}.", "dim"))
        if is64 and prefix_arch == "win32":
            lines.append((
                "\u2717 This is a 64-bit executable but the Wine prefix is "
                "win32 (32-bit-only) -- that alone produces \"Bad EXE "
                "format\". Point \"Wine prefix\" at a new, empty folder and "
                "set \"Wine command\" to WINEARCH=win64 wine64 so a fresh "
                "64-bit prefix gets created, or delete/rename the existing "
                "32-bit prefix.",
                "err",
            ))
    elif is64 and not have_wine64 and wine_exe_name == "wine":
        lines.append((
            "\u26a0 This is a 64-bit executable, no wine64 binary was found "
            "on PATH, and no existing prefix was found to check. If the "
            "system \"wine\" is a 32-bit-only build (common when only the "
            "i386 Wine package is installed without multiarch/wine64), 64-bit "
            "EXEs fail with exactly \"Bad EXE format\". Try installing the "
            "64-bit Wine package, or set \"Wine command\" to "
            "WINEARCH=win64 wine64.",
            "warn",
        ))

    if wine_path is None:
        lines.append((
            f"\u2717 Wine binary \"{wine_exe_name}\" was not found on PATH.",
            "err",
        ))

    return lines


def diagnose_launch_error(exc, binpath, cfg):
    """Turn a raw OSError from trying to launch binpath into a helpful hint."""
    is_exe = binpath.lower().endswith(".exe")
    if isinstance(exc, PermissionError):
        return (
            f"\u2717 Permission denied: {binpath}\n"
            "  The executable bit probably isn't set. Try: "
            f"chmod +x {shlex.quote(binpath)}  (or use the \"Fix permissions\" "
            "button in Build & Setup)."
        )
    if isinstance(exc, FileNotFoundError):
        if is_exe:
            return (
                f"\u2717 Could not run {binpath}. If this is meant to run "
                "through Wine, make sure `wine` (or wine64) is installed and "
                "on your PATH."
            )
        return f"\u2717 No such file: {binpath}"
    return f"\u2717 {exc}"


# --------------------------------------------------------------------------
# Config persistence
# --------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.exists():
            cfg.update(json.loads(CONFIG_FILE.read_text()))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


def save_session(data):
    """Write session data next to the script, atomically (write to a temp
    file in the same dir, then os.replace -- so a kill mid-write can never
    leave a half-written/corrupt session file behind)."""
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SESSION_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, SESSION_FILE)
    except Exception:
        pass


def load_session():
    try:
        if SESSION_FILE.exists():
            return json.loads(SESSION_FILE.read_text())
    except Exception:
        pass
    return {}


def clear_session():
    try:
        SESSION_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def find_on_path(name):
    from shutil import which

    return which(name) or ""


# --------------------------------------------------------------------------
# A reusable "pick files/folders" list widget
# --------------------------------------------------------------------------
class PathListPanel(Gtk.Box):
    """A toolbar + scrollable list for building up a list of input paths."""

    def __init__(self, title, pick_folders_too=True, filters=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.filters = filters or []
        self.pick_folders_too = pick_folders_too

        label = Gtk.Label(label=title, xalign=0)
        label.get_style_context().add_class("dim-label")
        self.pack_start(label, False, False, 0)

        self.store = Gtk.ListStore(str)
        self.view = Gtk.TreeView(model=self.store)
        self.view.set_headers_visible(False)
        self.view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.MIDDLE)
        col = Gtk.TreeViewColumn("Path", renderer, text=0)
        self.view.append_column(col)
        self.view.connect("button-press-event", self.on_button_press)
        self.view.connect("key-press-event", self.on_key_press)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(110)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.view)
        self.pack_start(scroller, True, True, 0)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_file_btn = Gtk.Button.new_with_label("Add files\u2026")
        add_file_btn.connect("clicked", self.on_add_files)
        toolbar.pack_start(add_file_btn, False, False, 0)

        if pick_folders_too:
            add_folder_btn = Gtk.Button.new_with_label("Add folder\u2026")
            add_folder_btn.connect("clicked", self.on_add_folder)
            toolbar.pack_start(add_folder_btn, False, False, 0)

        remove_btn = Gtk.Button.new_with_label("Remove selected")
        remove_btn.connect("clicked", self.on_remove_selected)
        toolbar.pack_start(remove_btn, False, False, 0)

        clear_btn = Gtk.Button.new_with_label("Clear")
        clear_btn.connect("clicked", self.on_clear)
        toolbar.pack_start(clear_btn, False, False, 0)

        self.pack_start(toolbar, False, False, 0)

    def on_add_files(self, _btn):
        dlg = Gtk.FileChooserDialog(
            title="Select files",
            transient_for=self.get_toplevel(),
            action=Gtk.FileChooserAction.OPEN,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )
        dlg.set_select_multiple(True)
        for name, patterns in self.filters:
            f = Gtk.FileFilter()
            f.set_name(name)
            for p in patterns:
                f.add_pattern(p)
            dlg.add_filter(f)
        if dlg.run() == Gtk.ResponseType.OK:
            for p in dlg.get_filenames():
                self.store.append([p])
        dlg.destroy()

    def on_add_folder(self, _btn):
        dlg = Gtk.FileChooserDialog(
            title="Select folder",
            transient_for=self.get_toplevel(),
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            "_Select", Gtk.ResponseType.OK,
        )
        if dlg.run() == Gtk.ResponseType.OK:
            self.store.append([dlg.get_filename()])
        dlg.destroy()

    def on_remove_selected(self, _btn):
        sel = self.view.get_selection()
        model, sel_paths = sel.get_selected_rows()
        # Delete via TreeRowReferences so removing one row doesn't invalidate
        # the TreePaths of the others still queued for deletion.
        refs = [Gtk.TreeRowReference.new(model, p) for p in sel_paths]
        for ref in refs:
            treeiter = model.get_iter(ref.get_path())
            model.remove(treeiter)

    def on_clear(self, _btn):
        self.store.clear()

    def copy_selected_to_clipboard(self):
        model, sel_paths = self.view.get_selection().get_selected_rows()
        texts = [model[p][0] for p in sel_paths]
        if not texts:
            return
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text("\n".join(texts), -1)
        clipboard.store()  # keep it in the clipboard after the app loses focus

    def on_button_press(self, _widget, event):
        if event.button != 3:
            return False
        sel = self.view.get_selection()
        hit = self.view.get_path_at_pos(int(event.x), int(event.y))
        if hit is not None:
            path = hit[0]
            if not sel.path_is_selected(path):
                sel.unselect_all()
                sel.select_path(path)
        menu = Gtk.Menu()
        item = Gtk.MenuItem(label="Copy")
        item.set_sensitive(sel.count_selected_rows() > 0)
        item.connect("activate", lambda _i: self.copy_selected_to_clipboard())
        menu.append(item)
        menu.show_all()
        menu.popup(None, None, None, None, event.button, event.time)
        return True

    def on_key_press(self, _widget, event):
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and event.keyval in (Gdk.KEY_c, Gdk.KEY_C):
            self.copy_selected_to_clipboard()
            return True
        return False

    def paths(self):
        return [row[0] for row in self.store]

    def set_paths(self, paths):
        self.store.clear()
        for p in paths or []:
            self.store.append([p])


# --------------------------------------------------------------------------
# Log / console panel with colored stdout/stderr and running-process control
# --------------------------------------------------------------------------
class RunPanel(Gtk.Box):
    """Run button + spinner + status line + scrolling monospace log."""

    def __init__(self, on_run, cfg=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.on_run_cb = on_run
        self.cfg = cfg
        self.proc = None

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.run_btn = Gtk.Button.new_with_label("Run")
        self.run_btn.get_style_context().add_class("suggested-action")
        self.run_btn.connect("clicked", self._on_run_clicked)
        controls.pack_start(self.run_btn, False, False, 0)

        self.cancel_btn = Gtk.Button.new_with_label("Cancel")
        self.cancel_btn.set_sensitive(False)
        self.cancel_btn.connect("clicked", self._on_cancel_clicked)
        controls.pack_start(self.cancel_btn, False, False, 0)

        self.spinner = Gtk.Spinner()
        controls.pack_start(self.spinner, False, False, 0)

        self.status = Gtk.Label(label="Idle", xalign=0)
        controls.pack_start(self.status, True, True, 0)

        clear_log_btn = Gtk.Button.new_with_label("Clear log")
        clear_log_btn.connect("clicked", lambda _b: self.buffer.set_text(""))
        controls.pack_end(clear_log_btn, False, False, 0)

        self.pack_start(controls, False, False, 0)

        self.textview = Gtk.TextView()
        self.textview.set_editable(False)
        self.textview.set_monospace(True)
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.buffer = self.textview.get_buffer()
        self.tag_err = self.buffer.create_tag("err", foreground="#e05c5c")
        self.tag_ok = self.buffer.create_tag("ok", foreground="#5ce07b")
        self.tag_dim = self.buffer.create_tag("dim", foreground="#8a8a8a")
        self.tag_warn = self.buffer.create_tag("warn", foreground="#e0b95c")

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.set_min_content_height(220)
        scroller.add(self.textview)
        self.pack_start(scroller, True, True, 0)

    def _on_run_clicked(self, _btn):
        try:
            result = self.on_run_cb()
        except ValueError as e:
            self.append_line(str(e), "err")
            return
        if not result or not result[0]:
            return
        cmd, cwd = result[0], result[1]
        env = result[2] if len(result) > 2 else None
        self.append_line("$ " + " ".join(shlex.quote(c) for c in cmd), "dim")
        if env is not None and env.get("WINEPREFIX"):
            self.append_line(f"  (WINEPREFIX={env['WINEPREFIX']})", "dim")
        self.run_btn.set_sensitive(False)
        self.cancel_btn.set_sensitive(True)
        self.spinner.start()
        self.status.set_text("Running\u2026")
        threading.Thread(target=self._run_thread, args=(cmd, cwd, env), daemon=True).start()

    def _on_cancel_clicked(self, _btn):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.append_line("\u25a0 Cancelled by user", "err")

    def _run_thread(self, cmd, cwd, env=None):
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in self.proc.stdout:
                GLib.idle_add(self.append_line, line.rstrip("\n"), None)
            code = self.proc.wait()
        except (PermissionError, FileNotFoundError) as e:
            binpath = cmd[0] if not cmd[0].endswith("wine") and not cmd[0].endswith("wine64") else (cmd[1] if len(cmd) > 1 else cmd[0])
            msg = diagnose_launch_error(e, binpath, self.cfg or {})
            GLib.idle_add(self.append_line, msg, "err")
            code = -1
        except Exception as e:  # noqa: BLE001
            GLib.idle_add(self.append_line, f"\u2717 {e}", "err")
            code = -1
        GLib.idle_add(self._on_finished, code)

    def _on_finished(self, code):
        self.spinner.stop()
        self.run_btn.set_sensitive(True)
        self.cancel_btn.set_sensitive(False)
        if code == 0:
            self.status.set_text("Finished successfully")
            self.append_line("\u2713 Done (exit code 0)", "ok")
        else:
            self.status.set_text(f"Exited with code {code}")
            self.append_line(f"\u2717 Exit code {code}", "err")
        return False

    def append_line(self, text, tag_name):
        end = self.buffer.get_end_iter()
        if tag_name:
            self.buffer.insert_with_tags(end, text + "\n", self.buffer.get_tag_table().lookup(tag_name))
        else:
            self.buffer.insert(end, text + "\n")
        mark = self.buffer.create_mark(None, self.buffer.get_end_iter(), False)
        self.textview.scroll_to_mark(mark, 0.0, False, 0, 1)
        return False


# --------------------------------------------------------------------------
# Decompile tab (Champollion)
# --------------------------------------------------------------------------
class DecompileTab(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_border_width(16)
        self.app = app

        self.inputs = PathListPanel(
            "Input .pex files or folders",
            filters=[("PEX files", ["*.pex"])],
        )
        self.pack_start(self.inputs, True, True, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        row = 0

        grid.attach(Gtk.Label(label="Output (.psc) folder:", xalign=0), 0, row, 1, 1)
        self.out_chooser = Gtk.FileChooserButton(
            title="Output folder", action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        self.out_chooser.set_hexpand(True)
        grid.attach(self.out_chooser, 1, row, 2, 1)
        row += 1

        self.asm_check = Gtk.CheckButton(label="Also write disassembly (.pas) to:")
        grid.attach(self.asm_check, 0, row, 1, 1)
        self.asm_chooser = Gtk.FileChooserButton(
            title="Assembly output folder", action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        self.asm_chooser.set_sensitive(False)
        self.asm_check.connect(
            "toggled", lambda b: self.asm_chooser.set_sensitive(b.get_active())
        )
        grid.attach(self.asm_chooser, 1, row, 2, 1)
        row += 1

        self.pack_start(grid, False, False, 0)

        flags_frame = Gtk.Frame(label="Options")
        flags_box = Gtk.FlowBox()
        flags_box.set_selection_mode(Gtk.SelectionMode.NONE)
        flags_box.set_max_children_per_line(3)
        flags_box.set_border_width(8)

        self.chk_comment = Gtk.CheckButton(label="Annotate with assembly (-c)")
        self.chk_threaded = Gtk.CheckButton(label="Parallelize (-t)")
        self.chk_recursive = Gtk.CheckButton(label="Recurse into folders (-r)")
        self.chk_subdirs = Gtk.CheckButton(label="Recreate subdirs, FO4 (-s)")
        self.chk_header = Gtk.CheckButton(label="Write file header (-e)")
        self.chk_verbose = Gtk.CheckButton(label="Verbose (-v)")
        self.chk_debugfuncs = Gtk.CheckButton(label="Decompile debug funcs (--debug-funcs)")
        self.chk_nodebugline = Gtk.CheckButton(label="Omit debug line comments (--no-debug-line)")
        self.chk_trace = Gtk.CheckButton(label="Trace / rebuild log (-g)")

        for w in (
            self.chk_comment, self.chk_threaded, self.chk_recursive,
            self.chk_subdirs, self.chk_header, self.chk_verbose,
            self.chk_debugfuncs, self.chk_nodebugline, self.chk_trace,
        ):
            flags_box.add(w)
        flags_frame.add(flags_box)
        self.pack_start(flags_frame, False, False, 0)

        extra_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        extra_box.pack_start(Gtk.Label(label="Extra arguments:"), False, False, 0)
        self.extra_entry = Gtk.Entry()
        self.extra_entry.set_placeholder_text("e.g. --print-info  (advanced / future flags)")
        extra_box.pack_start(self.extra_entry, True, True, 0)
        self.pack_start(extra_box, False, False, 0)

        self.run_panel = RunPanel(self.build_command, cfg=app.cfg)
        self.pack_start(self.run_panel, True, True, 0)

    def build_command(self):
        binpath = self.app.cfg["champollion_bin"]
        if not binpath:
            raise ValueError("Set the Champollion executable path in the Build & Setup tab first.")
        paths = self.inputs.paths()
        if not paths:
            raise ValueError("Add at least one .pex file or folder to decompile.")
        out = self.out_chooser.get_filename()
        if not out:
            raise ValueError("Choose an output folder for the decompiled .psc files.")

        self.app.persist_session_now()

        cmd = paths + ["-p", out]
        if self.asm_check.get_active():
            asm_dir = self.asm_chooser.get_filename()
            cmd += ["-a", asm_dir] if asm_dir else ["-a"]
        if self.chk_comment.get_active():
            cmd.append("-c")
        if self.chk_threaded.get_active():
            cmd.append("-t")
        if self.chk_recursive.get_active():
            cmd.append("-r")
        if self.chk_subdirs.get_active():
            cmd.append("-s")
        if self.chk_header.get_active():
            cmd.append("-e")
        if self.chk_verbose.get_active():
            cmd.append("-v")
        if self.chk_debugfuncs.get_active():
            cmd.append("--debug-funcs")
        if self.chk_nodebugline.get_active():
            cmd.append("--no-debug-line")
        if self.chk_trace.get_active():
            cmd.append("-g")
        extra = self.extra_entry.get_text().strip()
        if extra:
            cmd += shlex.split(extra)
        final_cmd, env = wrap_command(self.app.cfg, binpath, cmd)
        return final_cmd, None, env

    def snapshot(self):
        return {
            "inputs": self.inputs.paths(),
            "out": self.out_chooser.get_filename() or "",
            "asm_on": self.asm_check.get_active(),
            "asm_dir": self.asm_chooser.get_filename() or "",
            "comment": self.chk_comment.get_active(),
            "threaded": self.chk_threaded.get_active(),
            "recursive": self.chk_recursive.get_active(),
            "subdirs": self.chk_subdirs.get_active(),
            "header": self.chk_header.get_active(),
            "verbose": self.chk_verbose.get_active(),
            "debugfuncs": self.chk_debugfuncs.get_active(),
            "nodebugline": self.chk_nodebugline.get_active(),
            "trace": self.chk_trace.get_active(),
            "extra": self.extra_entry.get_text(),
        }

    def restore(self, data):
        if not data:
            return
        self.inputs.set_paths(data.get("inputs"))
        if data.get("out"):
            self.out_chooser.set_filename(data["out"])
        self.asm_check.set_active(data.get("asm_on", False))
        if data.get("asm_dir"):
            self.asm_chooser.set_filename(data["asm_dir"])
        self.chk_comment.set_active(data.get("comment", False))
        self.chk_threaded.set_active(data.get("threaded", False))
        self.chk_recursive.set_active(data.get("recursive", False))
        self.chk_subdirs.set_active(data.get("subdirs", False))
        self.chk_header.set_active(data.get("header", False))
        self.chk_verbose.set_active(data.get("verbose", False))
        self.chk_debugfuncs.set_active(data.get("debugfuncs", False))
        self.chk_nodebugline.set_active(data.get("nodebugline", False))
        self.chk_trace.set_active(data.get("trace", False))
        self.extra_entry.set_text(data.get("extra", ""))


# --------------------------------------------------------------------------
# Extract archives tab (BA2 + BSA)
# --------------------------------------------------------------------------
class ExtractArchiveTab(Gtk.Box):
    """Unpacks Bethesda .ba2 (GNRL) and .bsa archives -- handy for pulling
    loose .pex files out of an archive before feeding them to the Decompile
    tab. Shells out to scripts/extract_archive.py via the current Python
    interpreter, reusing the same RunPanel used for Champollion/Caprica so
    output streams live into the log.

    .ba2 extraction needs nothing beyond the standard library. .bsa
    extraction is delegated to the third-party 'bethesda-structs' package
    (plus 'lz4' for LZ4-compressed archives) -- that's only imported by the
    script when a .bsa is actually encountered, so .ba2-only use never
    needs it installed."""

    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_border_width(16)
        self.app = app

        self.inputs = PathListPanel(
            "Input .ba2 / .bsa archives",
            pick_folders_too=False,
            filters=[("Bethesda archives", ["*.ba2", "*.BA2", "*.bsa", "*.BSA"])],
        )
        self.pack_start(self.inputs, True, True, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        row = 0
        grid.attach(Gtk.Label(label="Output folder:", xalign=0), 0, row, 1, 1)
        self.out_chooser = Gtk.FileChooserButton(
            title="Output folder", action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        self.out_chooser.set_hexpand(True)
        grid.attach(self.out_chooser, 1, row, 1, 1)
        grid.attach(info_icon(
            "Each archive's files are unpacked here, preserving the "
            "relative paths stored inside the archive. Multiple archives "
            "can be extracted in one run, mixing .ba2 and .bsa freely -- "
            "they land side by side in this same folder.\n\n"
            "<b>.ba2</b>: only 'GNRL' (general-purpose) archives are "
            "supported; 'DX10' texture archives aren't handled.\n\n"
            "<b>.bsa</b>: extracted via the third-party 'bethesda-structs' "
            "package. If it's missing, install it first:\n"
            "<tt>pip install bethesda-structs lz4</tt>"
        ), 2, row, 1, 1)
        row += 1
        self.pack_start(grid, False, False, 0)

        self.run_panel = RunPanel(self.build_command, cfg=app.cfg)
        self.pack_start(self.run_panel, True, True, 0)

    def build_command(self):
        paths = self.inputs.paths()
        if not paths:
            raise ValueError("Add at least one .ba2 or .bsa archive to extract.")
        out = self.out_chooser.get_filename()
        if not out:
            raise ValueError("Choose an output folder to extract into.")

        script = Path(__file__).resolve().parent / "scripts" / "extract_archive.py"
        if not script.is_file():
            raise ValueError(
                f"extract_archive.py not found at {script}. Keep champollion_gui.py "
                "and its scripts/ folder together (don't move the .py file on its own)."
            )

        self.app.persist_session_now()

        cmd = [sys.executable, str(script), out] + paths
        return cmd, None, None

    def snapshot(self):
        return {
            "inputs": self.inputs.paths(),
            "out": self.out_chooser.get_filename() or "",
        }

    def restore(self, data):
        if not data:
            return
        self.inputs.set_paths(data.get("inputs"))
        if data.get("out"):
            self.out_chooser.set_filename(data["out"])


# --------------------------------------------------------------------------
# Recompile tab (Caprica)
# --------------------------------------------------------------------------
class RecompileTab(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_border_width(16)
        self.app = app

        self.inputs = PathListPanel(
            "Input .psc files or folders",
            filters=[("Papyrus source", ["*.psc"])],
        )
        self.pack_start(self.inputs, True, True, 0)

        self.imports = PathListPanel(
            "Import folders (script source dependencies)",
            pick_folders_too=True,
            filters=[],
        )
        self.pack_start(self.imports, True, True, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        row = 0

        grid.attach(Gtk.Label(label="Working directory:", xalign=0), 0, row, 1, 1)
        self.cwd_chooser = Gtk.FileChooserButton(
            title="Working directory (script root -- becomes \".\" for imports)",
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        self.cwd_chooser.set_hexpand(True)
        grid.attach(self.cwd_chooser, 1, row, 1, 1)
        grid.attach(info_icon(
            "Leave unset to auto-use the first input's folder. Caprica "
            "derives each script's namespace from its path relative to this "
            "directory (or an import folder) -- e.g. a script whose internal "
            "name is 'StartMeUp:Foo' must live in a StartMeUp\\ subfolder "
            "under here, and quest/dialogue fragments under "
            "Fragments\\Quests, Fragments\\TopicInfos, Fragments\\Terminals, "
            "matching how the vanilla source tree is laid out. Compiling a "
            "flat folder of fragment scripts will fail with a namespace "
            "mismatch until they're sorted into those subfolders."
        ), 2, row, 1, 1)
        row += 1

        grid.attach(Gtk.Label(label="Output (.pex) folder:", xalign=0), 0, row, 1, 1)
        self.out_chooser = Gtk.FileChooserButton(
            title="Output folder", action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        self.out_chooser.set_hexpand(True)
        grid.attach(self.out_chooser, 1, row, 2, 1)
        row += 1

        grid.attach(Gtk.Label(label="Target game:", xalign=0), 0, row, 1, 1)
        self.game_combo = Gtk.ComboBoxText()
        for g in GAMES:
            self.game_combo.append_text(g)
        self.game_combo.set_active(GAMES.index(app.cfg.get("default_game", "skyrim")))
        grid.attach(self.game_combo, 1, row, 1, 1)
        row += 1

        self.flags_check = Gtk.CheckButton(label="Custom flags file:")
        grid.attach(self.flags_check, 0, row, 1, 1)
        self.flags_chooser = Gtk.FileChooserButton(
            title="Flags file (.flg)", action=Gtk.FileChooserAction.OPEN
        )
        self.flags_chooser.set_sensitive(False)
        self.flags_check.connect(
            "toggled", lambda b: self.flags_chooser.set_sensitive(b.get_active())
        )
        grid.attach(self.flags_chooser, 1, row, 1, 1)
        grid.attach(info_icon(
            "<b>Required for every target except Starfield</b> -- "
            "Caprica only has a built-in default flags file for Starfield; "
            "fallout4, fallout4vr, skyrim and skyrimvr all hard-fail with "
            "\"No user flags have been defined\" unless you point this at a "
            "real *_Papyrus_Flags.flg (e.g. Institute_Papyrus_Flags.flg for "
            "Fallout 4, TESV_Papyrus_Flags.flg for Skyrim -- normally sitting "
            "alongside the game's vanilla source scripts)."
        ), 2, row, 1, 1)
        row += 1

        self.pack_start(grid, False, False, 0)

        flags_frame = Gtk.Frame(label="Options")
        flags_box = Gtk.FlowBox()
        flags_box.set_selection_mode(Gtk.SelectionMode.NONE)
        flags_box.set_max_children_per_line(3)
        flags_box.set_border_width(8)
        self.chk_recursive = Gtk.CheckButton(label="Recurse into folders")
        self.chk_verbose = Gtk.CheckButton(label="Verbose")
        self.chk_all_warn_err = Gtk.CheckButton(label="Warnings as errors")
        for w in (self.chk_recursive, self.chk_verbose, self.chk_all_warn_err):
            flags_box.add(w)
        flags_frame.add(flags_box)
        options_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        options_row.pack_start(flags_frame, True, True, 0)
        options_icon = info_icon(
            "Caprica's exact flag names vary a bit by fork/version. If a "
            "checkbox here doesn't match your build, clear it and pass the "
            "real flag via <b>Extra arguments</b> instead."
        )
        options_icon.set_valign(Gtk.Align.CENTER)
        options_row.pack_start(options_icon, False, False, 4)
        self.pack_start(options_row, False, False, 0)

        extra_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        extra_box.pack_start(Gtk.Label(label="Extra arguments:"), False, False, 0)
        self.extra_entry = Gtk.Entry()
        self.extra_entry.set_placeholder_text("any additional Caprica CLI flags, verbatim")
        extra_box.pack_start(self.extra_entry, True, True, 0)
        self.pack_start(extra_box, False, False, 0)

        self.run_panel = RunPanel(self.build_command, cfg=app.cfg)
        self.pack_start(self.run_panel, True, True, 0)

    def build_command(self):
        binpath = self.app.cfg["caprica_bin"]
        if not binpath:
            raise ValueError("Set the Caprica executable path in the Build & Setup tab first.")
        paths = self.inputs.paths()
        if not paths:
            raise ValueError("Add at least one .psc file or folder to compile.")
        out = self.out_chooser.get_filename()
        if not out:
            raise ValueError("Choose an output folder for the compiled .pex files.")

        self.app.persist_session_now()

        game = self.game_combo.get_active_text()
        flags_on = self.flags_check.get_active()
        flags_file = self.flags_chooser.get_filename() if flags_on else None
        if game != "starfield" and not (flags_on and flags_file):
            raise ValueError(
                f"Caprica has no built-in default flags for '{game}' -- tick "
                "\"Custom flags file\" above and point it at that game's "
                "*_Papyrus_Flags.flg, or compilation will fail with "
                "\"No user flags have been defined\"."
            )

        # Working directory: explicit choice, or auto-derived from the first
        # input so relative paths + "-i ." resolve namespaces the same way a
        # manual `cd` into the script root does. NOTE: this fallback only
        # works correctly for a flat script folder. If scripts live under
        # namespace subfolders (e.g. StartMeUp/Foo.psc, matching an internal
        # "StartMeUp:Foo" scriptname), the true root is the folder that
        # *contains* StartMeUp/, not StartMeUp/ itself -- set the Working
        # directory field explicitly in that case, or namespaces will resolve
        # wrong (or empty) just like they did before this was a project-root.
        cwd = self.cwd_chooser.get_filename()
        if not cwd:
            first = paths[0]
            cwd = first if os.path.isdir(first) else os.path.dirname(first)
        cwd = os.path.abspath(cwd)

        def relativize(p):
            ap = os.path.abspath(p)
            try:
                rel = os.path.relpath(ap, cwd)
            except ValueError:
                return ap  # different drive/mount; fall back to absolute
            return rel if not rel.startswith("..") else ap

        rel_paths = [relativize(p) for p in paths]

        cmd = rel_paths + ["-o", out]
        cmd += ["-g", game]
        imports = ["."] + [relativize(p) for p in self.imports.paths()]
        cmd += ["-i", ";".join(imports)]
        if flags_file:
            cmd += ["--flags", flags_file]
        if self.chk_recursive.get_active():
            cmd.append("-r")
        if self.chk_verbose.get_active():
            cmd.append("--verbose")
        if self.chk_all_warn_err.get_active():
            cmd.append("-WE")
        extra = self.extra_entry.get_text().strip()
        if extra:
            cmd += shlex.split(extra)
        final_cmd, env = wrap_command(self.app.cfg, binpath, cmd)
        return final_cmd, cwd, env

    def snapshot(self):
        return {
            "inputs": self.inputs.paths(),
            "imports": self.imports.paths(),
            "cwd": self.cwd_chooser.get_filename() or "",
            "out": self.out_chooser.get_filename() or "",
            "game": self.game_combo.get_active_text() or "",
            "flags_on": self.flags_check.get_active(),
            "flags_file": self.flags_chooser.get_filename() or "",
            "recursive": self.chk_recursive.get_active(),
            "verbose": self.chk_verbose.get_active(),
            "all_warn_err": self.chk_all_warn_err.get_active(),
            "extra": self.extra_entry.get_text(),
        }

    def restore(self, data):
        if not data:
            return
        self.inputs.set_paths(data.get("inputs"))
        self.imports.set_paths(data.get("imports"))
        if data.get("cwd"):
            self.cwd_chooser.set_filename(data["cwd"])
        if data.get("out"):
            self.out_chooser.set_filename(data["out"])
        if data.get("game") in GAMES:
            self.game_combo.set_active(GAMES.index(data["game"]))
        self.flags_check.set_active(data.get("flags_on", False))
        if data.get("flags_file"):
            self.flags_chooser.set_filename(data["flags_file"])
        self.chk_recursive.set_active(data.get("recursive", False))
        self.chk_verbose.set_active(data.get("verbose", False))
        self.chk_all_warn_err.set_active(data.get("all_warn_err", False))
        self.extra_entry.set_text(data.get("extra", ""))


# --------------------------------------------------------------------------
# Build & Setup tab
# --------------------------------------------------------------------------
class SetupTab(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.set_border_width(16)
        self.app = app

        intro = Gtk.Label(xalign=0)
        intro.set_line_wrap(True)
        intro.set_markup(
            "This app only drives the two command-line tools -- it does not "
            "contain them. Point the fields below at real executables, or use "
            "the build helper for Champollion.\n\n"
            "<b>Note:</b> the Caprica source bundled alongside Champollion in "
            "many releases links directly against the Windows API and is "
            "Windows-only as shipped. On Linux either run a Windows build "
            "through Wine (tick the box below), cross-compile it yourself, or "
            "use a Linux-ported fork if one is available to you."
        )
        self.pack_start(intro, False, False, 0)

        grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        row = 0

        grid.attach(Gtk.Label(label="Champollion executable:", xalign=0), 0, row, 1, 1)
        self.champollion_entry = Gtk.Entry()
        self.champollion_entry.set_hexpand(True)
        grid.attach(self.champollion_entry, 1, row, 1, 1)
        b1 = Gtk.Button.new_with_label("Browse\u2026")
        b1.connect("clicked", self.browse_champollion)
        grid.attach(b1, 2, row, 1, 1)
        t1 = Gtk.Button.new_with_label("Test")
        t1.connect("clicked", lambda _b: self.test_binary(self.champollion_entry.get_text()))
        grid.attach(t1, 3, row, 1, 1)
        p1 = Gtk.Button.new_with_label("Fix permissions")
        p1.set_tooltip_text("chmod +x this file")
        p1.connect("clicked", lambda _b: self.fix_permissions(self.champollion_entry.get_text()))
        grid.attach(p1, 4, row, 1, 1)
        d1 = Gtk.Button.new_with_label("Diagnose")
        d1.set_tooltip_text("Inspect the .exe's own header and compare it against your Wine setup")
        d1.connect("clicked", lambda _b: self.diagnose_binary(self.champollion_entry.get_text()))
        grid.attach(d1, 5, row, 1, 1)
        row += 1

        grid.attach(Gtk.Label(label="Caprica executable:", xalign=0), 0, row, 1, 1)
        self.caprica_entry = Gtk.Entry()
        self.caprica_entry.set_hexpand(True)
        grid.attach(self.caprica_entry, 1, row, 1, 1)
        b2 = Gtk.Button.new_with_label("Browse\u2026")
        b2.connect("clicked", self.browse_caprica)
        grid.attach(b2, 2, row, 1, 1)
        t2 = Gtk.Button.new_with_label("Test")
        t2.connect("clicked", lambda _b: self.test_binary(self.caprica_entry.get_text()))
        grid.attach(t2, 3, row, 1, 1)
        p2 = Gtk.Button.new_with_label("Fix permissions")
        p2.set_tooltip_text("chmod +x this file")
        p2.connect("clicked", lambda _b: self.fix_permissions(self.caprica_entry.get_text()))
        grid.attach(p2, 4, row, 1, 1)
        d2 = Gtk.Button.new_with_label("Diagnose")
        d2.set_tooltip_text("Inspect the .exe's own header and compare it against your Wine setup")
        d2.connect("clicked", lambda _b: self.diagnose_binary(self.caprica_entry.get_text()))
        grid.attach(d2, 5, row, 1, 1)
        row += 1

        grid.attach(Gtk.Label(label="Wine command:", xalign=0), 0, row, 1, 1)
        self.wine_entry = Gtk.Entry()
        self.wine_entry.set_text("wine")
        self.wine_entry.set_placeholder_text("wine, wine64, or e.g. WINEARCH=win64 wine64")
        grid.attach(self.wine_entry, 1, row, 1, 1)
        grid.attach(info_icon(
            ".exe files are always run through Wine -- there's no native way "
            "to run a Windows binary on Linux."
        ), 2, row, 1, 1)
        row += 1

        grid.attach(Gtk.Label(label="Wine prefix (optional):", xalign=0), 0, row, 1, 1)
        self.wine_prefix_entry = Gtk.Entry()
        self.wine_prefix_entry.set_placeholder_text("e.g. /home/you/.wine64 -- leave blank to use the default prefix")
        grid.attach(self.wine_prefix_entry, 1, row, 1, 1)
        grid.attach(info_icon(
            "A native Windows .exe can never run directly on Linux -- if "
            "\"Bad EXE format\" shows up even through Wine, the file itself "
            "is likely corrupted or architecture-mismatched. Verify with "
            "<tt>file /path/to/thing.exe</tt> (should say \"PE32+ "
            "executable\"), and try <tt>WINEARCH=win64 wine64</tt> as the "
            "Wine command if it's a 64-bit build."
        ), 2, row, 1, 1)
        row += 1

        self.pack_start(grid, False, False, 0)

        self.save_session_check = Gtk.CheckButton(
            label="Remember Decompile/Recompile tab inputs between launches"
        )
        session_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        session_row.pack_start(self.save_session_check, False, False, 0)
        session_icon = info_icon(
            "When on, the file lists, folders, working directory, target "
            "game, flags file, and option checkboxes on the Decompile and "
            "Recompile tabs are saved to a session file in the app's own "
            "tmp/ folder (next to this script) every time you click Run, "
            "and restored next time you open the app -- including after a "
            "PC restart, unlike relying on a clean window close. Turn it "
            "off to stop saving and delete that file."
        )
        session_row.pack_start(session_icon, False, False, 0)
        self.pack_start(session_row, False, False, 0)

        save_btn = Gtk.Button.new_with_label("Save settings")
        save_btn.get_style_context().add_class("suggested-action")
        save_btn.connect("clicked", self.save_settings)
        self.pack_start(save_btn, False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        self.pack_start(sep, False, False, 6)

        build_label = Gtk.Label(xalign=0)
        build_label.set_markup("<b>Build Champollion from source (CMake)</b>")
        self.pack_start(build_label, False, False, 0)

        bgrid = Gtk.Grid(column_spacing=10, row_spacing=8)
        bgrid.attach(Gtk.Label(label="Champollion source folder:", xalign=0), 0, 0, 1, 1)
        self.src_chooser = Gtk.FileChooserButton(
            title="Champollion source folder (contains CMakeLists.txt)",
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        self.src_chooser.set_hexpand(True)
        bgrid.attach(self.src_chooser, 1, 0, 2, 1)

        bgrid.attach(Gtk.Label(label="Build folder:", xalign=0), 0, 1, 1, 1)
        self.build_chooser = Gtk.FileChooserButton(
            title="Build output folder", action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        bgrid.attach(self.build_chooser, 1, 1, 2, 1)
        self.pack_start(bgrid, False, False, 0)

        help_label = Gtk.Label(xalign=0)
        help_label.set_line_wrap(True)
        help_label.set_markup(
            "<small>Requires cmake, a C++17/20 compiler, Boost (program_options) "
            "and {fmt} to already be installed on this machine. The bundled "
            "scripts/build_champollion.sh runs the equivalent "
            "<tt>cmake -B build &amp;&amp; cmake --build build</tt> commands for you.</small>"
        )
        self.pack_start(help_label, False, False, 0)

        self.build_run_panel = RunPanel(self.build_champollion_command, cfg=app.cfg)
        self.build_run_panel.run_btn.set_label("Build Champollion")
        self.pack_start(self.build_run_panel, True, True, 0)

        self.load_from_config()

    def load_from_config(self):
        cfg = self.app.cfg
        self.champollion_entry.set_text(cfg.get("champollion_bin", ""))
        self.caprica_entry.set_text(cfg.get("caprica_bin", ""))
        self.wine_entry.set_text(cfg.get("wine_bin", "wine"))
        self.wine_prefix_entry.set_text(cfg.get("wine_prefix", ""))
        self.save_session_check.set_active(cfg.get("save_session", False))

    def browse_champollion(self, _btn):
        p = self._pick_file("Select Champollion executable")
        if p:
            self.champollion_entry.set_text(p)

    def browse_caprica(self, _btn):
        p = self._pick_file("Select Caprica executable")
        if p:
            self.caprica_entry.set_text(p)

    def _pick_file(self, title):
        dlg = Gtk.FileChooserDialog(
            title=title, transient_for=self.get_toplevel(), action=Gtk.FileChooserAction.OPEN
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )
        result = None
        if dlg.run() == Gtk.ResponseType.OK:
            result = dlg.get_filename()
        dlg.destroy()
        return result

    def _live_cfg(self):
        """Settings as currently shown in the fields, even if not saved yet."""
        return {
            "wine_bin": self.wine_entry.get_text().strip() or "wine",
            "wine_prefix": self.wine_prefix_entry.get_text().strip(),
        }

    def test_binary(self, path):
        panel = self.build_run_panel
        if not path:
            panel.append_line("\u2717 No path set.", "err")
            return
        cfg = self._live_cfg()
        cmd, env = wrap_command(cfg, path, ["--version"])
        panel.append_line("$ " + " ".join(shlex.quote(c) for c in cmd), "dim")
        if env and env.get("WINEPREFIX"):
            panel.append_line(f"  (WINEPREFIX={env['WINEPREFIX']})", "dim")
        try:
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=20)
            out = (result.stdout + result.stderr).strip() or "(no output)"
            panel.append_line(out, "ok" if result.returncode == 0 else "err")
        except (PermissionError, FileNotFoundError) as e:
            panel.append_line(diagnose_launch_error(e, path, cfg), "err")
        except Exception as e:  # noqa: BLE001
            panel.append_line(f"\u2717 {e}", "err")

    def diagnose_binary(self, path):
        panel = self.build_run_panel
        if not path:
            panel.append_line("\u2717 No path set.", "err")
            return
        if not os.path.exists(path):
            panel.append_line(f"\u2717 No such file: {path}", "err")
            return
        cfg = self._live_cfg()
        panel.append_line(f"$ diagnose {path}", "dim")
        for text, tag in diagnose_exe(path, cfg):
            panel.append_line(text, tag)

    def fix_permissions(self, path):
        panel = self.build_run_panel
        if not path:
            panel.append_line("\u2717 No path set.", "err")
            return
        try:
            st = os.stat(path)
            os.chmod(path, st.st_mode | 0o111)
            panel.append_line(f"\u2713 chmod +x {path}", "ok")
        except Exception as e:  # noqa: BLE001
            panel.append_line(f"\u2717 Could not chmod {path}: {e}", "err")

    def save_settings(self, _btn):
        cfg = self.app.cfg
        cfg["champollion_bin"] = self.champollion_entry.get_text().strip()
        cfg["caprica_bin"] = self.caprica_entry.get_text().strip()
        cfg["wine_bin"] = self.wine_entry.get_text().strip() or "wine"
        cfg["wine_prefix"] = self.wine_prefix_entry.get_text().strip()
        cfg["save_session"] = self.save_session_check.get_active()
        save_config(cfg)
        if cfg["save_session"]:
            self.app.persist_session_now()
        else:
            clear_session()
        self.build_run_panel.append_line("\u2713 Settings saved.", "ok")

    def build_champollion_command(self):
        src = self.src_chooser.get_filename()
        build = self.build_chooser.get_filename()
        if not src:
            raise ValueError("Choose the Champollion source folder (the one with CMakeLists.txt).")
        if not build:
            raise ValueError("Choose a build output folder.")
        script = Path(__file__).resolve().parent / "scripts" / "build_champollion.sh"
        if not script.is_file():
            raise ValueError(
                f"build_champollion.sh not found at {script}. Keep champollion_gui.py "
                "and its scripts/ folder together (don't move the .py file on its own)."
            )
        return ["bash", str(script), src, build], None


# --------------------------------------------------------------------------
# Main application window
# --------------------------------------------------------------------------
class Workbench(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.cfg = load_config()
        if not self.cfg.get("champollion_bin"):
            self.cfg["champollion_bin"] = find_on_path("Champollion")
        if not self.cfg.get("caprica_bin"):
            self.cfg["caprica_bin"] = find_on_path("Caprica")
        self.decompile_tab = None
        self.recompile_tab = None
        self.extract_tab = None

    def persist_session_now(self):
        """Write the current session to disk immediately. Called after every
        Run (not just at clean shutdown), because a PC restart/logout kills
        this process without ever sending GTK a "destroy" event to save on."""
        if not self.cfg.get("save_session"):
            return
        if self.decompile_tab is None or self.recompile_tab is None or self.extract_tab is None:
            return
        save_session({
            "decompile": self.decompile_tab.snapshot(),
            "recompile": self.recompile_tab.snapshot(),
            "extract": self.extract_tab.snapshot(),
        })

    def do_activate(self):
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-application-prefer-dark-theme", self.cfg.get("dark_theme", True))

        win = Gtk.ApplicationWindow(application=self, title="Champollion Workbench")
        win.set_default_size(880, 720)

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("Champollion Workbench")
        header.set_subtitle("Papyrus decompile / recompile")
        win.set_titlebar(header)

        theme_btn = Gtk.ToggleButton()
        theme_btn.set_image(Gtk.Image.new_from_icon_name("weather-clear-night-symbolic", Gtk.IconSize.BUTTON))
        theme_btn.set_tooltip_text("Toggle dark theme")
        theme_btn.set_active(self.cfg.get("dark_theme", True))

        def on_theme_toggle(btn):
            self.cfg["dark_theme"] = btn.get_active()
            settings.set_property("gtk-application-prefer-dark-theme", btn.get_active())
            save_config(self.cfg)

        theme_btn.connect("toggled", on_theme_toggle)
        header.pack_end(theme_btn)

        notebook = Gtk.Notebook()
        decompile_tab = DecompileTab(self)
        recompile_tab = RecompileTab(self)
        extract_tab = ExtractArchiveTab(self)
        self.decompile_tab = decompile_tab
        self.recompile_tab = recompile_tab
        self.extract_tab = extract_tab
        notebook.append_page(decompile_tab, Gtk.Label(label="Decompile  (PEX \u2192 PSC)"))
        notebook.append_page(recompile_tab, Gtk.Label(label="Recompile  (PSC \u2192 PEX)"))
        notebook.append_page(extract_tab, Gtk.Label(label="Extract Archives  (BA2/BSA)"))
        notebook.append_page(SetupTab(self), Gtk.Label(label="Build && Setup"))

        if self.cfg.get("save_session"):
            session = load_session()
            decompile_tab.restore(session.get("decompile"))
            recompile_tab.restore(session.get("recompile"))
            extract_tab.restore(session.get("extract"))

        def on_destroy(_w):
            self.persist_session_now()
            save_config(self.cfg)

        # Backstop for restart/logout: a session manager typically sends
        # SIGTERM (sometimes with only a brief grace period before SIGKILL)
        # rather than a clean window-close, so GTK's "destroy" signal above
        # never fires in that case. Save synchronously right here instead.
        def on_terminate_signal(signum, _frame):
            self.persist_session_now()
            save_config(self.cfg)
            GLib.idle_add(self.quit)

        signal.signal(signal.SIGTERM, on_terminate_signal)
        signal.signal(signal.SIGINT, on_terminate_signal)

        win.add(notebook)
        win.connect("destroy", on_destroy)
        win.show_all()


def main():
    app = Workbench()
    app.run()


if __name__ == "__main__":
    main()
