#!/usr/bin/env python3
# rog-tune: the window a Spotlight timer or reminder shows when it rings. Stays until you dismiss it (Enter / Esc); S snoozes 5 minutes.
#   alarm.py "message"        (started by systemd-run --user when the time comes)
import json, os, subprocess, sys, threading, time
from pathlib import Path
import gi
gi.require_version("Gtk", "3.0"); gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib

HOME = Path.home()
MSG = " ".join(sys.argv[1:]) or "Time is up"
STATE = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "spotlight"
CSS = b"""
window.alarm { background-color: rgba(238,233,224,0.97); border: 3px solid #c47f1c; border-radius: 22px; }
.alarm .big { color: #2a251e; font-size: 30pt; font-weight: 300; padding: 34px 44px 10px 44px; }
.alarm .sub { color: #7d7466; font-size: 12pt; padding: 0 44px 30px 44px; }
"""

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

def themed_css():
    css = CSS.decode()
    if is_dark():
        for a, c in (("rgba(238,233,224,0.97)", "rgba(31,28,25,0.97)"), ("#2a251e", "#f6efe4"), ("#7d7466", "#a39685")): css = css.replace(a, c)
    return css.replace("#c47f1c", accent_hex()).encode()

def setting(key, default):
    try:
        for l in (HOME / ".config/rog/settings.env").read_text().splitlines():
            if l.startswith(key + "="): return l.split("=", 1)[1].strip().strip('"')
    except Exception: pass
    return default

stop = threading.Event()
def ring():
    snd = "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"
    if setting("ALARM_SOUND", "1") != "1" or not os.path.exists(snd): return
    for _ in range(4):
        if stop.is_set(): return
        subprocess.run(["paplay", snd], capture_output=True)
        stop.wait(1.5)

def snooze():
    unit = f"rog-alarm-{int(time.time())}"
    env = [f"--setenv={k}={os.environ[k]}" for k in ("DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS") if k in os.environ]
    subprocess.run(["systemd-run", "--user", "--collect", f"--unit={unit}", "--on-active=300s", "--timer-property=AccuracySec=50ms"] + env + ["python3", str(Path(__file__).resolve()), MSG], capture_output=True)
    try:
        f = STATE / "alarms.json"; a = json.loads(f.read_text()) if f.exists() else {}
        a[unit] = {"msg": MSG, "due": int(time.time() + 300)}; f.write_text(json.dumps(a))
    except Exception: pass

class Win(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.set_wmclass("rogalarm", "RogAlarm"); self.set_title("Alarm"); self.set_decorated(False)
        self.set_default_size(620, -1); self.set_keep_above(True)
        scr = self.get_screen(); vis = scr.get_rgba_visual()
        if vis: self.set_visual(vis)
        self.get_style_context().add_class("alarm")
        prov = Gtk.CssProvider(); prov.load_from_data(themed_css()); Gtk.StyleContext.add_provider_for_screen(scr, prov, Gtk.STYLE_PROVIDER_PRIORITY_USER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL); box.get_style_context().add_class("alarm"); self.add(box)
        t = Gtk.Label(label="⏰  " + MSG, xalign=0); t.set_line_wrap(True); t.set_max_width_chars(34); t.get_style_context().add_class("big"); box.pack_start(t, False, False, 0)
        s = Gtk.Label(label="Enter or Esc to dismiss   ·   S to snooze 5 minutes", xalign=0); s.get_style_context().add_class("sub"); box.pack_start(s, False, False, 0)
        self.connect("key-press-event", self.on_key); self.connect("destroy", lambda *a: (stop.set(), Gtk.main_quit()))
        self.show_all(); self.present()
        threading.Thread(target=ring, daemon=True).start()

    def on_key(self, w, e):
        if e.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Escape): self.destroy(); return True
        if e.keyval in (Gdk.KEY_s, Gdk.KEY_S): snooze(); self.destroy(); return True
        return False

if __name__ == "__main__":
    Win(); Gtk.main()
