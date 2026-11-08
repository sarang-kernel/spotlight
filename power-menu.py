#!/usr/bin/env python3
# rog-tune: power menu (Super+Escape). One row of big tiles: Lock, Log out, Sleep, Hibernate, Restart, Shut down.
#   h / l / arrows / Tab move · 1-6 pick · Enter runs · Esc or q closes
#   Lock and Sleep run at once; Log out, Hibernate, Restart and Shut down ask for a second Enter so a slip never ends the session.
import os, subprocess, sys, time
from pathlib import Path
import gi
gi.require_version("Gtk", "3.0"); gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib

HOME = Path.home()
def setting(key, default):
    try:
        for l in (HOME / ".config/rog/settings.env").read_text().splitlines():
            if l.startswith(key + "="): return l.split("=", 1)[1].strip().strip('"')
    except Exception: pass
    return default

def is_dark():
    mode = setting("APPEARANCE", "light")
    if mode == "dark": return True
    if mode != "schedule": return False
    try: a = int(setting("NIGHT_START", "19")); b = int(setting("NIGHT_END", "7"))
    except ValueError: a, b = 19, 7
    h = time.localtime().tm_hour
    return (h >= a or h < b) if a > b else (a <= h < b)

def accent_hex():
    m = setting("ACCENT_MODE", "amber")
    try:
        if m == "custom" and len(setting("ACCENT_HEX", "")) == 7: return setting("ACCENT_HEX", "")
        if m == "wallpaper":
            v = (HOME / ".cache/accent-wallpaper").read_text().strip()
            if len(v) == 7: return v
    except Exception: pass
    return "#c47f1c"

CSS = """
window.power { background-color: transparent; }
.pcard { background-color: rgba(238,233,224,0.97); border-radius: 26px; border: 1px solid rgba(42,37,30,0.10); padding: 22px 26px 18px 26px; }
.phead { color: #7d7466; font-size: 11pt; padding: 0 6px 14px 6px; }
.ptile { background: transparent; border: 2px solid transparent; border-radius: 20px; padding: 16px 8px 12px 8px; min-width: 112px; box-shadow: none; transition: all 120ms ease; }
.ptile image { color: #2a251e; }
.ptile.armed image { color: #ffffff; }
.ptile label { color: #2a251e; font-size: 12pt; }
.ptile .num { color: #7d7466; font-size: 9pt; }
.ptile.sel { background-color: rgba(196,127,28,0.16); border-color: #c47f1c; }
.ptile.armed { background-color: #c47f1c; border-color: #c47f1c; }
.ptile.armed label { color: #ffffff; }
.pmsg { color: #7d7466; font-size: 11pt; padding: 12px 6px 0 6px; }
.pmsg.warn { color: #c47f1c; font-weight: 600; }
"""
def themed_css():
    css = CSS
    if is_dark():
        for a, c in (("rgba(238,233,224,0.97)", "rgba(31,28,25,0.97)"), ("rgba(42,37,30,0.10)", "rgba(238,233,224,0.12)"),
                     ("#2a251e", "#f6efe4"), ("#7d7466", "#a39685")): css = css.replace(a, c)
    acc = accent_hex(); r, g, b = (int(acc[i:i + 2], 16) for i in (1, 3, 5))
    return css.replace("rgba(196,127,28,0.16)", f"rgba({r},{g},{b},0.18)").replace("#c47f1c", acc).encode()

def detach(cmd):
    subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# label, icon, command, needs a second Enter, confirmation text
ACTIONS = [
    ("Lock", "system-lock-screen", [str(HOME / ".config/i3lock/lock.sh")], False, ""),
    ("Log out", "system-log-out", ["i3-msg", "exit"], True, "Press Enter again to log out"),
    ("Sleep", "system-suspend", ["systemctl", "suspend"], False, ""),
    ("Hibernate", "system-suspend-hibernate", ["systemctl", "hibernate"], True, "Press Enter again to hibernate (saves to disk, powers off)"),
    ("Restart", "system-reboot", ["systemctl", "reboot"], True, "Press Enter again to restart"),
    ("Shut down", "system-shutdown", ["systemctl", "poweroff"], True, "Press Enter again to shut down"),
]
try:
    if "resume=" not in Path("/proc/cmdline").read_text(): ACTIONS = [a for a in ACTIONS if a[0] != "Hibernate"]
except Exception: pass

def status_line():
    parts = [os.uname().nodename]
    try:
        s = int(float(Path("/proc/uptime").read_text().split()[0])); d, s = divmod(s, 86400); h, s = divmod(s, 3600)
        parts.append("up " + (f"{d}d " if d else "") + f"{h}h {s // 60:02d}m")
    except Exception: pass
    try:
        for b in sorted(Path("/sys/class/power_supply").glob("BAT*")):
            parts.append(f"battery {(b / 'capacity').read_text().strip()}% ({(b / 'status').read_text().strip().lower()})"); break
    except Exception: pass
    return "   ·   ".join(parts)

class Win(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.set_wmclass("rogpower", "RogPower"); self.set_title("Power"); self.set_decorated(False); self.set_keep_above(True)
        scr = self.get_screen(); vis = scr.get_rgba_visual()
        if vis: self.set_visual(vis)
        self.set_app_paintable(True)
        self.get_style_context().add_class("power")
        prov = Gtk.CssProvider(); prov.load_from_data(themed_css()); Gtk.StyleContext.add_provider_for_screen(scr, prov, Gtk.STYLE_PROVIDER_PRIORITY_USER)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL); card.get_style_context().add_class("pcard"); self.add(card)
        head = Gtk.Label(label=status_line(), xalign=0.5); head.get_style_context().add_class("phead"); card.pack_start(head, False, False, 0)
        row = Gtk.Box(spacing=6); card.pack_start(row, False, False, 0)
        self.tiles = []; self.sel = 0; self.armed = None
        for i, (label, icon, cmd, ask, msg) in enumerate(ACTIONS):
            b = Gtk.Button(); b.set_relief(Gtk.ReliefStyle.NONE); b.set_can_focus(False); b.get_style_context().add_class("ptile")
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            im = Gtk.Image.new_from_icon_name(icon + "-symbolic", Gtk.IconSize.DIALOG); im.set_pixel_size(40); v.pack_start(im, False, False, 0)
            t = Gtk.Label(label=label); v.pack_start(t, False, False, 0)
            n = Gtk.Label(label=str(i + 1)); n.get_style_context().add_class("num"); v.pack_start(n, False, False, 0)
            b.add(v); b.connect("clicked", lambda w, i=i: self.choose(i)); row.pack_start(b, True, True, 0); self.tiles.append(b)
        self.msg = Gtk.Label(label="h / l move   ·   Enter runs   ·   Esc closes", xalign=0.5); self.msg.get_style_context().add_class("pmsg"); card.pack_start(self.msg, False, False, 0)
        self.connect("key-press-event", self.on_key); self.connect("destroy", Gtk.main_quit)
        self.refresh(); self.show_all(); self.present()

    def refresh(self):
        for i, b in enumerate(self.tiles):
            c = b.get_style_context()
            for k in ("sel", "armed"): c.remove_class(k)
            if i == self.sel: c.add_class("armed" if self.armed == i else "sel")
        ctx = self.msg.get_style_context()
        if self.armed is not None: self.msg.set_text(ACTIONS[self.armed][4]); ctx.add_class("warn")
        else: self.msg.set_text("h / l move   ·   Enter runs   ·   Esc closes"); ctx.remove_class("warn")

    def move(self, d): self.sel = (self.sel + d) % len(ACTIONS); self.armed = None; self.refresh()

    def choose(self, i):
        if i != self.sel: self.sel = i; self.armed = None
        self.run()

    def run(self):
        label, icon, cmd, ask, msg = ACTIONS[self.sel]
        if ask and self.armed != self.sel: self.armed = self.sel; self.refresh(); return
        self.hide(); Gdk.flush()
        detach(cmd); GLib.timeout_add(200, Gtk.main_quit)

    def on_key(self, w, e):
        k = e.keyval
        if k in (Gdk.KEY_Escape, Gdk.KEY_q): self.destroy(); return True
        if k in (Gdk.KEY_Right, Gdk.KEY_l, Gdk.KEY_Tab): self.move(1); return True
        if k in (Gdk.KEY_Left, Gdk.KEY_h, Gdk.KEY_ISO_Left_Tab): self.move(-1); return True
        if k in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space): self.run(); return True
        if Gdk.KEY_1 <= k <= Gdk.KEY_9 and k - Gdk.KEY_1 < len(ACTIONS): self.sel = k - Gdk.KEY_1; self.armed = None; self.run(); return True
        return False

if __name__ == "__main__":
    # a second press of the key while the menu is open closes it
    try:
        me = os.getpid(); r = subprocess.run(["pgrep", "-xf", f"python3 {Path(__file__).resolve()}"], capture_output=True, text=True).stdout.split()
        others = [p for p in r if int(p) != me]
        if others: os.kill(int(others[0]), 15); sys.exit(0)
    except Exception: pass
    Win(); Gtk.main()
