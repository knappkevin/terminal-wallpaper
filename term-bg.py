#!/usr/bin/env python3
"""Terminal Wallpaper helper (thin).

Renders a transparent VTE terminal on the Wayland BOTTOM layer (above the
QML wallpaper the plugin now renders, below every normal window). Empty and
erased cells are see-through, so the wallpaper behind shows through the
text. A flat tint rectangle under the terminal gives the configured
background opacity.

The wallpaper image, theme palette and the 420ms reveal transition are all
handled in QML by Service.qml (ported from omarchy.background), using the
same IPC-driven detection stock uses. This helper owns NONE of that: no
image loading, no animation, no mtime poll of the background symlink, no
daemonization, no singleton lock. It is a plain child of omarchy-shell,
respawned by the plugin on exit.

Theme palette + settings are re-applied in place on SIGHUP, which the plugin
sends whenever the shell's Color singleton changes (the same event stock's
UI reacts to) or when the settings file changes.
"""

import argparse
import json
import os
import signal
import subprocess
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("Vte", "2.91")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, GLib, GLibUnix, Gtk, GtkLayerShell, Pango, Vte

THEME_COLORS_PATH = "~/.local/state/omarchy/current/theme/colors.toml"

# (Terminal widget is sized to whole VTE cells and aligned to the bar by
# _cell_aligned_rect -- no fixed pixel inset needed here.)


def hex_to_rgba(color, alpha):
    s = str(color or "").lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) < 6:
        s = "cacccc"
    try:
        r = int(s[0:2], 16) / 255.0
        g = int(s[2:4], 16) / 255.0
        b = int(s[4:6], 16) / 255.0
    except ValueError:
        r = g = b = 0.8
    return Gdk.RGBA(r, g, b, max(0.0, min(1.0, alpha)))


def load_config(path):
    # Canonical defaults live in Service.qml (`defaults`); the plugin always
    # writes a complete merged config, so these are only safety fallbacks for
    # a hand-edited file that drops a key.
    cfg = {}
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                cfg.update(data)
        except (OSError, ValueError) as exc:
            print("term-bg: failed to read config: %s" % exc, file=sys.stderr)
    cfg["command"] = str(cfg.get("command") or "")
    cfg["fontFamily"] = str(cfg.get("fontFamily") or "monospace")
    cfg["fontSize"] = float(cfg.get("fontSize", 14)) or 14
    cfg["fgColor"] = str(cfg.get("fgColor") or "#dddddd")
    cfg["bgColor"] = str(cfg.get("bgColor") or "#111111")
    cfg["bgOpacity"] = float(cfg.get("bgOpacity", 0.0))
    cfg["autoRestart"] = bool(cfg.get("autoRestart", False))
    cfg["cursorVisible"] = bool(cfg.get("cursorVisible", False))
    cfg["useTheme"] = bool(cfg.get("useTheme", True))
    cfg["interactive"] = bool(cfg.get("interactive", False))
    for key in ("posX", "posY", "sizeX", "sizeY"):
        cfg[key] = float(cfg.get(key, 0 if key in ("posX", "posY") else 100))
    return cfg


def _rgba_mix(a, b, t):
    return Gdk.RGBA(
        a.red + (b.red - a.red) * t,
        a.green + (b.green - a.green) * t,
        a.blue + (b.blue - a.blue) * t,
        1.0,
    )


def load_theme_colors():
    """Map colors.toml onto VTE slots exactly like omarchy-theme-set's
    generated ghostty.conf/alacritty.toml."""
    import tomllib

    data = {}
    path = os.path.expanduser(THEME_COLORS_PATH)
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print("term-bg: cannot read %s: %s" % (path, exc), file=sys.stderr)

    def rgb(r, g, b):
        return Gdk.RGBA(r, g, b, 1.0)

    def key(name, fallback):
        val = data.get(name)
        if isinstance(val, str) and val.startswith("#"):
            return hex_to_rgba(val, 1.0)
        return fallback

    fg = key("foreground", rgb(1.0, 1.0, 1.0))
    bg = key("background", rgb(0.0, 0.0, 0.0))
    bright_fg = key("bright_foreground", fg)
    muted = key("muted", _rgba_mix(bg, fg, 0.4))
    selection = key("selection", _rgba_mix(bg, fg, 0.25))
    hues = ("red", "green", "yellow", "blue", "magenta", "cyan")
    hue_defaults = {
        "red": (1.0, 0.33, 0.33), "green": (0.33, 1.0, 0.33),
        "yellow": (1.0, 1.0, 0.33), "blue": (0.33, 0.55, 1.0),
        "magenta": (1.0, 0.33, 1.0), "cyan": (0.33, 1.0, 1.0),
    }

    # 0=black(background) 7=white(foreground) 8=bright black(muted),
    # mirroring the generated ghostty palette entries.
    palette = [bg]
    for name in hues:
        d = hue_defaults[name]
        palette.append(key(name, rgb(*d)))
    palette.append(fg)
    palette.append(muted)
    for i, name in enumerate(hues):  # 9-14 fall back to the normal hue
        palette.append(key("bright_" + name, palette[i + 1]))
    palette.append(bright_fg)

    return {
        "fg": fg,
        "bg": bg,
        "palette": palette,
        "cursor_bg": bright_fg,
        "cursor_fg": bg,
        "sel_bg": selection,
        "sel_fg": bright_fg,
    }


def manual_theme(fg, base):
    """Manual hex mode: only the two configured colors, VTE default palette."""
    return {
        "fg": fg,
        "bg": base,
        "palette": None,
        "cursor_bg": base,
        "cursor_fg": fg,
        "sel_bg": base,
        "sel_fg": fg,
    }


def resolve_theme(cfg):
    if cfg["useTheme"]:
        return load_theme_colors()
    return manual_theme(
        hex_to_rgba(cfg["fgColor"], 1.0),
        hex_to_rgba(cfg["bgColor"], 1.0),
    )


def apply_terminal_colors(terminal, theme, opacity):
    bg = Gdk.RGBA(theme["bg"].red, theme["bg"].green, theme["bg"].blue, opacity)
    terminal.set_colors(theme["fg"], bg, theme["palette"])
    terminal.set_color_highlight(theme["sel_bg"])
    terminal.set_color_highlight_foreground(theme["sel_fg"])
    terminal.set_color_cursor(theme["cursor_bg"])
    terminal.set_color_cursor_foreground(theme["cursor_fg"])


class TerminalWallpaper:
    def __init__(self, cfg, cfg_path, debug=False):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.debug = debug
        self.windows = []
        self.terminals = []
        self.tints = []
        self.monitors = []
        self._terminal_rects = []
        self._bar_cache = []
        self.child_pid = 0
        self.restart_delay = 1.0
        self.restart_source = None
        self._reposition_pending = False
        self._rebuild_pending = False
        self._rebuilding = False
        self.gates = []
        self._win_handler_ids = []
        self._monitor_signaled = set()
        self._surface_sizes = {}

    def _dbg(self, msg):
        # Best-effort breadcrumb trail (capped); only written when the helper
        # is started with --debug. Default installs leave no log files behind.
        if not self.debug:
            return
        try:
            import time as _t
            p = os.path.expanduser("~/.local/state/terminal-wallpaper-debug.log")
            if os.path.exists(p) and os.path.getsize(p) > 2_000_000:
                os.truncate(p, 0)
            with open(p, "a") as fh:
                fh.write("%.3f %s\n" % (_t.monotonic() % 100000, msg))
        except Exception:
            pass

    # ------------------------------------------------------------ per monitor
    def build_monitor(self, monitor):
        self._dbg("build_monitor entry")
        mon_w, mon_h, tx, ty, tw, th = self._compute_geometry(monitor)
        top_in, bot_in, left_in, right_in = self._bar_insets_for(monitor.get_geometry())

        win = Gtk.Window()
        win.set_title("terminal-wallpaper")
        win.set_app_paintable(True)
        destroy_id = win.connect("destroy", lambda *a: Gtk.main_quit())

        screen = win.get_screen()
        visual = screen.get_rgba_visual()
        if visual and screen.is_composited():
            win.set_visual(visual)

        GtkLayerShell.init_for_window(win)
        GtkLayerShell.set_namespace(win, "omarchy-terminal-wallpaper")
        # BOTTOM layer: above the QML wallpaper (BACKGROUND layer) by protocol
        # guarantee, below every normal window. Never on BACKGROUND, where the
        # stacking order against the plugin's QML surface would be undefined.
        GtkLayerShell.set_layer(win, GtkLayerShell.Layer.BOTTOM)
        GtkLayerShell.set_monitor(win, monitor)
        # Anchor to every edge except the top. Anchoring the top would let the
        # desktop bar's exclusive zone push us down, leaving a gap behind the
        # (translucent) bar; bottom-anchoring keeps us at the very top so the
        # surface fills the whole monitor including behind the bar.
        for edge in (
            GtkLayerShell.Edge.LEFT,
            GtkLayerShell.Edge.RIGHT,
            GtkLayerShell.Edge.BOTTOM,
        ):
            GtkLayerShell.set_anchor(win, edge, True)
            GtkLayerShell.set_margin(win, edge, 0)
        GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.NONE)

        fixed = Gtk.Fixed()
        win.add(fixed)

        # Full-screen transparent surface that forces the layer-shell window to
        # the whole monitor. A term-sized child would let bottom-anchoring
        # shrink the surface to the terminal's height and glue it to the bottom
        # of the screen, ignoring posY. Only the terminal's rect is painted
        # with the tint; the rest stays clear so the QML wallpaper shows
        # through. Single flat fill -- no image, no scaling, no animation -- so
        # there is nothing here that can crash the helper during a theme switch.
        tint = Gtk.DrawingArea()
        tint.set_size_request(mon_w, mon_h)
        tint._rect = (tx, ty, tw, th)
        tint.connect("draw", self._draw_tint)
        # The tint DrawingArea spans the whole monitor (transparent outside the
        # terminal rect), so it is what receives pointer events on the "wallpaper
        # space" the terminal doesn't occupy -- the QML wallpaper behind never
        # sees a click. Route double-clicks there to the menu, same as the VTE.
        tint.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        tint.connect("button-press-event", self._on_tint_press)
        fixed.put(tint, 0, 0)
        self.tints.append(tint)

        terminal = Vte.Terminal()
        terminal.set_scrollback_lines(0)
        terminal.set_mouse_autohide(True)
        terminal.set_cursor_blink_mode(Vte.CursorBlinkMode.OFF)

        if visual:
            terminal.set_visual(visual)
        # This VTE build never paints cells whose background is the default
        # (regardless of clear-background or alpha), which is ideal here: empty
        # AND erased cells stay see-through so the wallpaper behind shows
        # through, and app erases can never leave an opaque slab. Only
        # explicitly colored cells get painted by VTE.
        terminal.set_clear_background(False)

        font = Pango.FontDescription("%s %d" % (self.cfg["fontFamily"], int(self.cfg["fontSize"])))
        terminal.set_font(font)

        # Round to whole VTE cells and align the grid flush to the bar so the
        # sub-cell remainder lands on the non-bar side (no "gap of nothing"
        # between bar and text). get_char_width/height are valid right after
        # set_font, before realize.
        cell_w = terminal.get_char_width() or 0
        cell_h = terminal.get_char_height() or 0
        self._cell_w, self._cell_h = cell_w, cell_h
        term_x, term_y, term_w, term_h = self._cell_aligned_rect(
            tx, ty, tw, th, top_in, bot_in, left_in, right_in, cell_w, cell_h)
        terminal.set_size_request(term_w, term_h)

        theme = resolve_theme(self.cfg)
        opacity = min(1.0, max(0.0, float(self.cfg["bgOpacity"])))
        self._theme_bg = Gdk.RGBA(theme["bg"].red, theme["bg"].green, theme["bg"].blue, 1.0)
        self._opacity = opacity
        apply_terminal_colors(terminal, theme, opacity)

        child_id = terminal.connect("child-exited", self._on_child_exited)
        terminal.connect("button-press-event", self._on_button_press)
        fixed.put(terminal, term_x, term_y)

        # Transparent full-screen gate above the terminal. It is hidden by
        # default (clickable terminal = on) and only shown when the user turns
        # interaction off, at which point it swallows every pointer event.
        gate = Gtk.EventBox()
        gate.set_no_show_all(True)
        gate.set_size_request(mon_w, mon_h)
        gate.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.SCROLL_MASK)
        gate.connect("button-press-event", self._on_gate_press)
        gate.connect("scroll-event", self._on_gate_scroll)
        fixed.put(gate, 0, 0)
        self.gates = getattr(self, "gates", [])
        self.gates.append(gate)

        win.show_all()
        self._apply_interactive()
        idx = len(self.windows)
        self.windows.append(win)
        self.terminals.append(terminal)
        self.monitors.append(monitor)
        self._win_handler_ids.append((destroy_id, child_id))
        self._terminal_rects = getattr(self, "_terminal_rects", [])
        self._terminal_rects.append((term_x, term_y, term_w, term_h))
        # Record the surface size a fresh build negotiated so a later
        # reposition doesn't re-run the collapse trick on an already-correct
        # window (that would cause an avoidable flicker).
        self._surface_sizes[idx] = (mon_w, mon_h)

        # React to Hyprland scale/resolution changes the way the stock QML
        # background does via Quickshell's reactive screen model. GdkMonitor
        # emits notify::geometry and notify::scale-factor when the wl_output's
        # logical size or scale changes; GdkScreen::monitors-changed covers the
        # rest of a monitor reconfiguration. Connect per-monitor once: the
        # callback finds the monitor's current window index dynamically, so it
        # stays valid across window rebuilds (scale changes).
        if monitor not in self._monitor_signaled:
            monitor.connect("notify::geometry", self._on_monitor_changed, monitor)
            monitor.connect("notify::scale-factor", self._on_monitor_changed, monitor)
            self._monitor_signaled.add(monitor)
        if not getattr(self, "_screen_monitors_connected", False):
            win.get_screen().connect("monitors-changed", self._on_monitors_changed)
            self._screen_monitors_connected = True
        self.spawn(terminal)

    def _draw_tint(self, widget, cr):
        # Theme background at bgOpacity over the terminal's rect only. At
        # opacity 1 the terminal is solid; at 0 it is fully clear. The rest of
        # the full-screen surface stays transparent so the QML wallpaper
        # behind shows through.
        opacity = self._opacity
        if opacity <= 0.0:
            return True
        rect = getattr(widget, "_rect", None)
        if not rect:
            return True
        bg = self._theme_bg
        cr.set_source_rgba(bg.red, bg.green, bg.blue, opacity)
        cr.rectangle(rect[0], rect[1], rect[2], rect[3])
        cr.fill()
        return True

    # --------------------------------------------- desktop-bar avoidance
    # GDK's GdkMonitor.get_workarea() does NOT reflect Hyprland layer-shell
    # exclusive zones, so the bar's height never reaches us that way. The bar
    # is a layer surface (namespace "omarchy-bar"); query `hyprctl layers` for
    # its geometry and subtract it from the terminal's vertical axis so output
    # is never hidden under the bar (fullscreen/top). The wallpaper surface
    # stays full-monitor (behind the bar); only the terminal + tint rect avoid.
    def _query_bar_layers(self):
        """Return [(x, y, w, h), ...] for every omarchy-bar layer surface."""
        try:
            out = subprocess.check_output(
                ["hyprctl", "layers", "-j"], timeout=2)
            data = json.loads(out)
        except Exception as exc:
            self._dbg("query_bar_layers failed: %r" % exc)
            return []
        bars = []
        try:
            for mon in data.values():
                for _level, entries in mon.get("levels", {}).items():
                    for l in entries:
                        if l.get("namespace") == "omarchy-bar":
                            bars.append((l.get("x", 0), l.get("y", 0),
                                         l.get("w", 0), l.get("h", 0)))
        except Exception as exc:
            self._dbg("query_bar_layers parse failed: %r" % exc)
        return bars

    def _bar_insets_for(self, geom):
        """(top, bottom, left, right) insets in pixels, relative to this
        monitor's edges, for omarchy-bar surfaces that overlap it. A bar is
        classified by orientation: a short bar spanning the width is a top or
        bottom bar (vertical inset); a narrow bar spanning the height is a
        left or right bar (horizontal inset). 0 on every axis if no bar."""
        top = bot = left = right = 0
        mon_left = geom.x
        mon_right = geom.x + geom.width
        mon_top = geom.y
        mon_bottom = geom.y + geom.height
        mon_cx = geom.x + geom.width / 2.0
        mon_cy = geom.y + geom.height / 2.0
        for (bx, by, bw, bh) in self._bar_cache or []:
            if not (bx < mon_right and bx + bw > mon_left and by < mon_bottom and by + bh > mon_top):
                continue  # no overlap with this monitor
            # Horizontal bar (top/bottom): spans most of the width, short.
            if bw >= geom.width * 0.5 and bh < geom.height * 0.5:
                if by + bh / 2.0 < mon_cy:
                    top = max(top, (by + bh) - mon_top)
                else:
                    bot = max(bot, mon_bottom - by)
            # Vertical bar (left/right): spans most of the height, narrow.
            elif bh >= geom.height * 0.5 and bw < geom.width * 0.5:
                if bx + bw / 2.0 < mon_cx:
                    left = max(left, (bx + bw) - mon_left)
                else:
                    right = max(right, mon_right - bx)
        return max(0, top), max(0, bot), max(0, left), max(0, right)

    def _logical_geometry(self, monitor):
        """Return (Gdk.Rectangle, ok) for a monitor's logical geometry. GDK's
        GdkMonitor geometry can lag the compositor after a Hyprland scale
        change (the wl_output logical size updates but GDK keeps the old
        value, so a reposition would size the surface wrong), so read the size
        from hyprctl -- which reports physical width/height plus scale -- and
        fall back to GDK on any failure, with ok=False so the caller can retry
        once the compositor settles. The monitor is matched to hyprctl by the
        GDK model id appearing in the connector name or description."""
        model = (monitor.get_model() or "").strip()
        try:
            out = subprocess.check_output(["hyprctl", "monitors", "-j"], timeout=2)
            entries = json.loads(out)
            pick = None
            for m in entries:
                name = m.get("name") or ""
                desc = m.get("description") or ""
                if not model or model in name or model in desc:
                    pick = m
                    break
            if pick is None and len(entries) == 1:
                pick = entries[0]
            if pick is not None:
                s = max(0.001, float(pick.get("scale") or 1))
                rect = Gdk.Rectangle()
                rect.x = int(pick.get("x", 0))
                rect.y = int(pick.get("y", 0))
                rect.width = int(pick.get("width", 0) / s)
                rect.height = int(pick.get("height", 0) / s)
                return rect, True
        except Exception as exc:
            self._dbg("logical_geometry failed: %r" % exc)
        g = monitor.get_geometry()
        return Gdk.Rectangle(g.x, g.y, g.width, g.height), False

    def _compute_geometry(self, monitor, geom=None):
        """(full_w, full_h, term_x, term_y, term_w, term_h). full_* is the whole
        monitor (the wallpaper surface renders behind the bar); the terminal
        rect avoids the bar on whichever axes it occupies -- a top/bottom bar
        insets the vertical axis, a left/right bar insets the horizontal axis."""
        if not self._bar_cache:
            self._bar_cache = self._query_bar_layers()
        geom = geom if geom is not None else self._logical_geometry(monitor)[0]
        full_w, full_h = geom.width, geom.height
        top_in, bot_in, left_in, right_in = self._bar_insets_for(geom)
        eff_h = max(1, full_h - top_in - bot_in)
        eff_w = max(1, full_w - left_in - right_in)
        tw = max(1, int(round(eff_w * self.cfg["sizeX"] / 100.0)))
        th = max(1, int(round(eff_h * self.cfg["sizeY"] / 100.0)))
        tx = int(round(left_in + eff_w * self.cfg["posX"] / 100.0))
        ty = int(round(top_in + eff_h * self.cfg["posY"] / 100.0))
        return full_w, full_h, tx, ty, tw, th

    def _cell_aligned_rect(self, tx, ty, tw, th, top_in, bot_in, left_in, right_in,
                           cell_w, cell_h):
        """Round the terminal widget to exact VTE cell multiples and align the
        grid flush to the bar-adjacent edge. VTE aligns its cell grid to the
        widget's top-left, so any sub-cell remainder normally lands on the
        bottom/right -- which, for a bottom or right bar, is right next to the
        bar and reads as a "gap of nothing" (tint without text). By rounding to
        whole cells and shifting the widget so the grid's bar-adjacent edge
        sits exactly on the bar, the remainder is pushed to the opposite
        (non-bar) side, where it is invisible. The tint still covers the full
        rect (tx,ty,tw,th); only the terminal widget is shrunk to whole cells
        and shifted."""
        if cell_w and cell_w > 0:
            cols = max(1, int(tw // cell_w))
            term_w = cols * cell_w
        else:
            term_w = tw
        if cell_h and cell_h > 0:
            rows = max(1, int(th // cell_h))
            term_h = rows * cell_h
        else:
            term_h = th
        # Right bar -> right-align (leftover on the left). Else left-align.
        term_x = tx + (tw - term_w) if right_in > 0 else tx
        # Bottom bar -> bottom-align (leftover on top). Else top-align.
        term_y = ty + (th - term_h) if bot_in > 0 else ty
        return term_x, term_y, term_w, term_h

    # --------------------------------------- reactive geometry (scale etc.)
    def _on_monitor_changed(self, _monitor, pspec, mon):
        # A Hyprland scale change stalls IPC for a moment and GDK's monitor
        # geometry can lag the new logical size, so defer until the compositor
        # settles, then REBUILD the surfaces: gtk-layer-shell cannot reliably
        # resize an anchored surface across a scale jump (the window allocation
        # is pinned to the last configured size), but a freshly-created surface
        # is always negotiated at the monitor's current logical size. Geometry/
        # resolution changes are safe to reflow in place.
        if pspec and pspec.name == "scale-factor":
            self._schedule_rebuild(defer_ms=2000)
        else:
            self._schedule_reposition()

    def _on_monitors_changed(self, _screen):
        # A monitor reconfiguration (scale, resolution, layout) -- recompute
        # every surface in place. Coalesced so a multi-monitor reconfig that
        # fires several signals lands as one pass.
        self._schedule_reposition()

    def _schedule_reposition(self, defer_ms=0):
        if self._reposition_pending:
            return
        self._reposition_pending = True
        if defer_ms and defer_ms > 0:
            GLib.timeout_add(defer_ms, self._reposition_all)
        else:
            GLib.idle_add(self._reposition_all)

    def _schedule_rebuild(self, defer_ms=0):
        if getattr(self, "_rebuild_pending", False):
            return
        self._rebuild_pending = True
        if defer_ms and defer_ms > 0:
            GLib.timeout_add(defer_ms, self._rebuild_all)
        else:
            GLib.idle_add(self._rebuild_all)

    def _rebuild_all(self):
        """Scale changed: rebuild every monitor's surface from scratch. Fresh
        surfaces always negotiate at the monitor's current logical size, which
        the in-place resize never managed reliably across scale jumps."""
        self._rebuild_pending = False
        self._bar_cache = self._query_bar_layers()
        monitors = list(self.monitors)
        for mon in monitors:
            self._rebuild_monitor(self.monitors.index(mon))
        self._reposition_all()

    def _rebuild_monitor(self, idx):
        mon = self.monitors[idx]
        win = self.windows[idx]
        destroy_id, child_id = self._win_handler_ids[idx]
        win.disconnect(destroy_id)
        self.terminals[idx].disconnect(child_id)
        self._rebuilding = True
        try:
            win.destroy()
        finally:
            self._rebuilding = False
        for lst in (self.windows, self.terminals, self.tints, self.monitors,
                    self._terminal_rects, self._win_handler_ids, self.gates):
            del lst[idx]
        self.build_monitor(mon)

    def _reposition_all(self):
        self._reposition_pending = False
        # Bar height can change with the theme/font; re-query before
        # repositioning so the terminal tracks the bar.
        self._bar_cache = self._query_bar_layers()
        for i in range(len(self.windows)):
            self._reposition(i)
        # If hyprctl was unreachable (scale-change IPC freeze) the geometry may
        # have fallen back to stale GDK values; retry once the compositor
        # settles so the surface renegotiates to the correct size.
        if not getattr(self, "_last_geom_ok", True):
            GLib.timeout_add(1500, self._reposition_all)

    def _reposition(self, i):
        """Recompute terminal geometry for monitor i from its CURRENT geometry
        (logical pixels, which already account for scale) and apply it in
        place: full-screen tint, terminal size + position with insets, tint
        painted rect. The terminal's vertical axis avoids the desktop bar; the
        full-screen surface stays behind it. No VTE respawn, no layer-surface
        teardown."""
        if i >= len(self.windows) or i >= len(self.monitors):
            return
        win = self.windows[i]
        terminal = self.terminals[i]
        tint = self.tints[i]
        fixed = win.get_child()
        mon = self.monitors[i]
        geom, geom_ok = self._logical_geometry(mon)
        self._last_geom_ok = geom_ok
        mw, mh, tx, ty, tw, th = self._compute_geometry(mon, geom)
        t, b, l, r = self._bar_insets_for(geom)
        cell_w = getattr(self, "_cell_w", 0) or terminal.get_char_width() or 0
        cell_h = getattr(self, "_cell_h", 0) or terminal.get_char_height() or 0
        term_x, term_y, term_w, term_h = self._cell_aligned_rect(
            tx, ty, tw, th, t, b, l, r, cell_w, cell_h)
        self._dbg("reposition i=%d geom=%dx%d insets=T%d B%d L%d R%d cell=%dx%d tx=%d ty=%d tw=%d th=%d -> term=%dx%d at(%d,%d)"
                  % (i, mw, mh, t, b, l, r, cell_w, cell_h, tx, ty, tw, th, term_w, term_h, term_x, term_y))
        # Update the content size requests FIRST: the window's minimum size
        # comes from its children, so if the tint still requests the old
        # (larger) size the window can never shrink. Then renegotiate the
        # layer surface: gtk-layer-shell locks the window allocation to the
        # compositor's configured surface size, so a plain set_size_request or
        # resize is ignored. Per the library docs, set the request and then
        # resize to 1x1 to force re-allocation to the new request -- collapse
        # to a tiny size first so the allocation actually changes, making both
        # grow and shrink renegotiate the surface. Anchors on opposite edges
        # override the request on those axes -- we only anchor the bottom, so
        # the height comes from the request while left+right force the width
        # to the monitor.
        tint.set_size_request(mw, mh)
        tint._rect = (tx, ty, tw, th)
        terminal.set_size_request(term_w, term_h)
        sizes = getattr(self, "_surface_sizes", {})
        if sizes.get(i) != (mw, mh):
            win.set_size_request(1, 1)
            win.resize(1, 1)
            win.set_size_request(mw, mh)
            win.resize(1, 1)
            sizes[i] = (mw, mh)
            self._surface_sizes = sizes
        if fixed is not None:
            fixed.move(terminal, term_x, term_y)
        tint.queue_draw()
        self._terminal_rects[i] = (tx, ty, tw, th)

    # -------------------------------------------------- live re-apply (SIGHUP)
    def reload(self, *args):
        """Re-read settings + theme and apply in place. SIGHUP is sent by the
        plugin whenever the shell's Color singleton changes (the same event
        stock's UI reacts to) or when the settings file changes. The layer
        surface is never torn down -- that is the black flash -- so a changed
        command/font is the only thing that respawns the VTE child."""
        self._dbg("reload entry")
        try:
            fresh = load_config(self.cfg_path)
        except Exception as exc:
            print("term-bg: config re-read failed: %s" % exc, file=sys.stderr)
            return
        old = self.cfg
        self.cfg = fresh

        theme = resolve_theme(fresh)
        opacity = min(1.0, max(0.0, float(fresh["bgOpacity"])))
        self._theme_bg = Gdk.RGBA(theme["bg"].red, theme["bg"].green, theme["bg"].blue, 1.0)
        self._opacity = opacity

        # The bar height tracks the theme/font; re-query before repositioning so
        # the terminal stays clear of it after a theme switch.
        self._bar_cache = self._query_bar_layers()

        # Geometry (size/position/insets, full-screen surface) is shared with
        # the scale-change path: reposition each monitor in place, then apply
        # theme + font over the top.
        for i in range(len(self.windows)):
            self._reposition(i)
            terminal = self.terminals[i]
            apply_terminal_colors(terminal, theme, opacity)
            terminal.set_font(Pango.FontDescription(
                "%s %d" % (fresh["fontFamily"], int(fresh["fontSize"]))))
            self.tints[i].queue_draw()
        self._apply_interactive()

        # A changed command, font family, font size, OR terminal size needs a
        # fresh VTE child. A live set_font() (no respawn) reflows the existing
        # buffer but leaves a stale scroll offset from the old cell size, so the
        # top of the output stays scrolled off ("stuck at the top, cut off") --
        # most visible after a max->min size swing. Respawning re-runs the
        # command against a reset terminal, regenerating the output at the new
        # size with a clean viewport. The same applies to a size change: a
        # one-shot command (fastfetch) prints once at spawn size and will not
        # re-fit a larger surface, so a preset/size change re-runs it. Position
        # (posX/posY) is intentionally excluded -- moving the terminal never
        # needs a re-render. Everything else (colors, opacity, geometry, cursor)
        # is already applied above and never respawns.
        if str(fresh.get("debug")) != str(old.get("debug")):
            # --debug is fixed at process spawn, so a toggle needs a fresh
            # process: exit and let omarchy-shell relaunch us with the flag.
            os._exit(0)
        if any(str(fresh.get(k)) != str(old.get(k))
               for k in ("command", "fontFamily", "fontSize", "sizeX", "sizeY")):
            for terminal in self.terminals:
                if self.child_pid:
                    try:
                        os.kill(self.child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                terminal.reset(True, True)
                self.spawn(terminal)

    # -------------------------------------------------------------- cursor
    def _start_cursor_enforcer(self, terminal):
        """Drive cursor visibility with DECTCEM (\\e[?25l / \\e[?25h).

        This is the standard hide mechanism every shell uses; VTE's color
        API cannot hide the cursor because an unfocused terminal draws it
        as an outline whose stroke ignores the cursor colors' alpha.
        """
        visible = bool(self.cfg["cursorVisible"])
        seq = b"\x1b[?25h" if visible else b"\x1b[?25l"

        def feed_once():
            terminal.feed(seq)
            return False

        GLib.timeout_add(400, feed_once)
        # Re-assert on a slow beat, consulting the LIVE config so the toggle
        # applies without any restart.
        def keep_hidden():
            if not terminal.get_has_window():
                return False
            vis = bool(self.cfg["cursorVisible"])
            cur = b"\x1b[?25h" if vis else b"\x1b[?25l"
            if getattr(terminal, "_last_cursor_seq", None) != cur:
                terminal.feed(cur)
                terminal._last_cursor_seq = cur
            return True

        GLib.timeout_add_seconds(2, keep_hidden)

    # -------------------------------------------------------------- spawn
    def spawn(self, terminal):
        self.restart_source = None
        argv = ["bash", "-lc", self.cfg["command"]]
        try:
            ok, pid = terminal.spawn_sync(
                Vte.PtyFlags.DEFAULT,
                os.path.expanduser("~"),
                argv,
                None,
                GLib.SpawnFlags.DEFAULT,
                None,
                None,
            )
            if ok and pid:
                self.child_pid = pid
                self._start_cursor_enforcer(terminal)
            elif ok:
                print("term-bg: spawn returned no pid", file=sys.stderr)
        except GLib.Error as exc:
            print("term-bg: spawn failed: %s" % exc, file=sys.stderr)

    def _on_child_exited(self, terminal, _status):
        if getattr(self, "_rebuilding", False):
            return
        if terminal not in self.terminals:
            return
        if not self.cfg["autoRestart"]:
            return
        if self.restart_source is not None:
            return
        delay_ms = int(min(30.0, self.restart_delay) * 1000)
        self.restart_delay = min(30.0, self.restart_delay * 1.5)
        self.restart_source = GLib.timeout_add(delay_ms, self._respawn)

    def _respawn(self):
        self.restart_source = None
        for terminal in self.terminals:
            self.spawn(terminal)
        return GLib.SOURCE_REMOVE

    def _on_button_press(self, _terminal, event):
        # Double-click on the wallpaper opens the config menu. Single clicks
        # fall through to the terminal (e.g. clicking around a TUI).
        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS and event.button == 1:
            self.open_menu()
            return True
        return False

    def _on_tint_press(self, _tint, event):
        # Double-click on the terminal-free wallpaper area opens the config
        # menu too. The QML wallpaper's MouseArea never fires because this
        # full-screen transparent surface sits above it and eats every click.
        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS and event.button == 1:
            self.open_menu()
            return True
        return False

    def _on_gate_press(self, _gate, event):
        # When 'clickable terminal' is off this full-screen gate sits above the
        # terminal and swallows every pointer event so a TUI like btop can't be
        # poked. Single clicks do nothing; double-clicks still open the menu.
        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS and event.button == 1:
            self.open_menu()
        return True

    def _on_gate_scroll(self, _gate, _event):
        return True

    def _apply_interactive(self):
        """Honor the 'clickable terminal' setting. When off, a transparent
        full-screen EventBox above the terminal swallows clicks/scroll (no TUI
        interaction), while the double-click menu still opens. When on, the
        gate is hidden and the terminal receives clicks as usual. The
        compositor already denies keyboard focus (KeyboardMode.NONE), so this
        fully deactivates the terminal."""
        on = self.cfg.get("interactive", False)
        for gate in getattr(self, "gates", []):
            gate.set_visible(not on)
        self._dbg("interactive=%s gates=%d" % (on, len(getattr(self, "gates", []))))

    def open_menu(self):
        import subprocess
        try:
            subprocess.Popen(
                ["omarchy-shell", "-q", "terminal-wallpaper", "open"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            print("term-bg: open-menu failed: %s" % exc, file=sys.stderr)

    def _kill_stale_instances(self):
        """Only one helper may own the desktop's terminal layer. The shell
        spawns this script as a child Process, but when quickshell itself is
        killed/restarted the child is orphaned instead of terminated -- and
        the fresh shell then spawns a second helper, leaving two full-screen
        VTE surfaces stacked (double text). Scan /proc for any other
        term-bg.py process and SIGTERM it before we create our own surfaces."""
        me = os.getpid()
        killed = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == me:
                continue
            try:
                with open("/proc/%d/cmdline" % pid, "rb") as fh:
                    parts = fh.read().decode("utf-8", "replace").split("\0")
            except Exception:
                continue  # vanished or not ours to read
            if any(p.endswith("term-bg.py") for p in parts):
                try:
                    os.kill(pid, signal.SIGTERM)
                    killed.append(pid)
                except Exception:
                    pass
        if killed:
            self._dbg("killed stale helper instances: %s" % killed)
            import time
            time.sleep(0.3)  # let them exit before our layer surfaces map

    def run(self):
        self._dbg("run() entry, registering SIGHUP")
        self._kill_stale_instances()
        signal.signal(signal.SIGINT, lambda *a: Gtk.main_quit())
        signal.signal(signal.SIGTERM, lambda *a: Gtk.main_quit())
        # SIGHUP = "re-apply in place" from the plugin (settings change, or a
        # theme switch via the shell's Color singleton). The gi binding's
        # GLib.unix_signal_add is deprecated and, in this pygobject/Python
        # combo, never actually fires the callback -- so every SIGHUP was
        # silently dropped and the helper never repositioned/reloaded after
        # startup. GLibUnix.signal_add is the supported replacement and fires
        # reliably. Returning SOURCE_CONTINUE keeps it armed for every nudge.
        GLibUnix.signal_add(
            GLib.PRIORITY_DEFAULT, signal.SIGHUP, self._on_sighup
        )
        # Bar position/size changes (e.g. moving the bar from top to left)
        # don't fire any GdkMonitor or shell signal we can hook, so poll
        # `hyprctl layers` at a low frequency and reposition only when the bar
        # geometry actually changed. A single subprocess every 2s; the
        # comparison short-circuits the reposition when nothing moved.
        GLib.timeout_add_seconds(2, self._bar_watchdog_tick)
        Gtk.main()

    def _on_sighup(self):
        self._dbg("SIGHUP received")
        GLib.idle_add(self.reload)
        return GLib.SOURCE_CONTINUE

    def _bar_watchdog_tick(self):
        fresh = self._query_bar_layers()
        if fresh != self._bar_cache:
            self._dbg("bar moved: %s -> %s" % (self._bar_cache, fresh))
            self._bar_cache = fresh
            for i in range(len(self.windows)):
                self._reposition(i)
        return True

    def _startup_recheck(self):
        """The helper can start before omarchy-bar has committed its layer
        surface, so the first `hyprctl layers` query sees no bar and the
        terminal lands at y=0 (under the bar). Re-query and reposition a few
        times until the bar shows up, then stop."""
        self._bar_cache = self._query_bar_layers()
        self._reposition_all()
        self._startup_retries = getattr(self, "_startup_retries", 0) + 1
        if not self._bar_cache and self._startup_retries < 6:
            return True  # bar not mapped yet; keep retrying
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="", help="path to JSON config file")
    parser.add_argument("--debug", action="store_true",
                        help="write debug breadcrumbs to ~/.local/state/terminal-wallpaper-debug.log")
    args = parser.parse_args()

    cfg = load_config(args.config)
    app = TerminalWallpaper(cfg, args.config, debug=args.debug)

    display = Gdk.Display.get_default()
    n = display.get_n_monitors()
    for i in range(n):
        monitor = display.get_monitor(i)
        if monitor:
            app.build_monitor(monitor)
    # The bar may not be mapped yet when the helper starts; re-check shortly.
    GLib.timeout_add_seconds(2, app._startup_recheck)
    app.run()


if __name__ == "__main__":
    main()
