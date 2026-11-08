#!/usr/bin/env python3
# rog-tune: Spotlight. One search box for apps, open windows, files, maths/units, clipboard history and system actions.
#   Enter run/open   Ctrl+Enter reveal folder / run in a terminal   Ctrl+C copy (a file copies as the file)   Esc close
#   Ctrl+J/K or Ctrl+N/P or Up/Down move   Alt+1..9 pick a row   Tab next row
#   prefixes:  =2+2 (maths)   c word (clipboard history)   > cmd (run a command)   w word (windows only)
# Searches while I type (plocate for files, qalc for maths). Closes itself when it loses focus. Nothing stays resident.
import json, math, os, re, shlex, subprocess, sys, threading, time
from pathlib import Path
import gi
gi.require_version("Gtk", "3.0"); gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, Gio, GLib, Pango

HOME = Path.home()
STATE = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "spotlight"
STATE.mkdir(parents=True, exist_ok=True)
HIST = STATE / "history.json"
SCRIPTS = HOME / ".config/scripts"
MAXROWS = 10

# ---------------------------------------------------------------- helpers
def sh(cmd, timeout=1.0):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""

def spawn(cmd):
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    threading.Thread(target=p.wait, daemon=True).start()     # reap it when it ends (no <defunct> children of this long-lived process)

def copy(text):
    p = subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    p.stdin.write(text.encode()); p.stdin.close()

def copy_file(path):
    """Alt+Enter on a file: put the file itself on the clipboard. Text -> its text; image -> the picture; anything else -> the file (paste it into a mail, chat or file manager)."""
    import mimetypes
    mt = mimetypes.guess_type(path)[0] or ""
    def pipe(args, data):
        pr = subprocess.Popen(["xclip", "-selection", "clipboard"] + args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        pr.stdin.write(data); pr.stdin.close()
    try:
        if mt.startswith("image/") and os.path.getsize(path) < 40e6:
            pipe(["-t", mt], open(path, "rb").read()); return
        if (mt.startswith("text/") or mt in ("application/json", "application/x-tex", "application/xml") or path.endswith((".md", ".tex", ".bib", ".org", ".py", ".sh", ".R", ".qmd", ".csv", ".txt"))) and os.path.getsize(path) < 2e6:
            pipe([], open(path, "rb").read()); return
    except Exception:
        pass
    uri = Path(path).resolve().as_uri().encode()
    pipe(["-t", "x-special/gnome-copied-files"], b"copy\n" + uri)

def load_hist():
    try:
        return json.loads(HIST.read_text())
    except Exception:
        return {}

HISTORY = load_hist()

def bump(key):
    n, _ = HISTORY.get(key, [0, 0])
    HISTORY[key] = [n + 1, int(time.time())]
    try:
        HIST.write_text(json.dumps(HISTORY))
    except Exception:
        pass

def frecency(key):
    n, last = HISTORY.get(key, [0, 0])
    if not n:
        return 0.0
    age_days = (time.time() - last) / 86400
    return min(40.0, 8.0 * n) / (1 + age_days / 14)

def match(q, text):
    """0 = no match. Every word of the query must match; prefix > word start > substring > in-order letters."""
    text = text.lower()
    total = 0
    for tok in q.lower().split():
        if text.startswith(tok): s = 100
        elif re.search(r"[\s\-_./]" + re.escape(tok), text): s = 80
        elif tok in text: s = 60
        else:
            s = 0
        if not s:
            return 0
        total += s
    return total / max(1, len(q.split()))

# ---------------------------------------------------------------- sources
class Item:
    __slots__ = ("kind", "title", "sub", "icon", "score", "run", "alt", "reveal", "key")
    def __init__(self, kind, title, sub="", icon="", score=0, run=None, alt=None, reveal=None, key=None):
        self.kind, self.title, self.sub, self.icon, self.score = kind, title, sub, icon, score
        self.run, self.alt, self.reveal, self.key = run, alt, reveal, key

APPS = []
def load_apps():
    del APPS[:]
    for a in Gio.AppInfo.get_all():
        if not a.should_show():
            continue
        kw = " ".join(a.get_keywords() or []) if hasattr(a, "get_keywords") else ""
        APPS.append((a, a.get_display_name() or "", (a.get_description() or ""), kw, a.get_id() or ""))

def launch_app(a):
    d = Gio.DesktopAppInfo.new(a.get_id()) if a.get_id() else None
    term = d.get_string("Terminal") == "true" if d else False
    if term:
        cmd = a.get_commandline() or ""
        cmd = re.sub(r"%[a-zA-Z]", "", cmd).strip()
        spawn(["kitty", "sh", "-lc", cmd]); return
    ctx = Gdk.Display.get_default().get_app_launch_context()
    try:
        a.launch([], ctx)
    except Exception:
        spawn(["sh", "-lc", re.sub(r"%[a-zA-Z]", "", a.get_commandline() or "")])

def apps_for(q):
    out = []
    for a, name, desc, kw, aid in APPS:
        if q:
            s = max(match(q, name) * 1.0, match(q, aid.replace(".desktop", "")) * 0.6)
            for txt, w in ((kw, 0.7), (desc, 0.35)):
                m2 = match(q, txt)
                if m2 >= 60:
                    s = max(s, m2 * w)
            if not s:
                continue
        else:
            s = 0
            if not frecency("app:" + aid):
                continue
        out.append(Item("App", name, desc, "", 20 + s + frecency("app:" + aid), run=lambda a=a: launch_app(a),
                        alt=lambda n=name: n, key="app:" + aid))
        out[-1].icon = a.get_icon()
    return out

_THUMBS = {}
def window_thumb(wid, title):
    """A small picture of a window (only works for windows on the visible workspace); cached for a few seconds."""
    from gi.repository import GdkPixbuf
    k = (wid, title); hit = _THUMBS.get(k)
    if hit and time.time() - hit[0] < 8: return hit[1]
    pix = None
    try:
        r = subprocess.run(["import", "-window", str(wid), "-resize", "96x64", "png:-"], capture_output=True, timeout=1.5)
        if r.returncode == 0 and len(r.stdout) > 400:
            ld = GdkPixbuf.PixbufLoader(); ld.write(r.stdout); ld.close(); pix = ld.get_pixbuf()
    except Exception:
        pix = None
    _THUMBS[k] = (time.time(), pix)
    return pix

def windows_for(q, only=False):
    if not q and not only:
        return []
    try:
        tree = json.loads(sh(["i3-msg", "-t", "get_tree"], 1.5))
    except Exception:
        return []
    out = []
    def walk(n, ws=""):
        if n.get("type") == "workspace":
            ws = n.get("name", "")
        if n.get("window") and n.get("name") and ws and not ws.startswith("__"):
            cls = (n.get("window_properties") or {}).get("class", "")
            s = max(match(q, n["name"]), match(q, cls) * 0.9) if q else 50
            if s:
                cid = n["id"]
                thumb = window_thumb(n["window"], n["name"]) if (only and len(out) < 8) else None     # pictures only in "w " mode
                out.append(Item("Window", n["name"], f"{cls} · workspace {ws}", thumb or cls.lower(), (60 if only else 35) + s,
                                run=lambda cid=cid: sh(["i3-msg", f"[con_id={cid}] focus"])))
        for c in n.get("nodes", []) + n.get("floating_nodes", []):
            walk(c, ws)
    walk(tree)
    return out

def action_items():
    def A(title, keywords, cmd, icon):
        return (title, keywords, cmd, icon)
    s = str(SCRIPTS)
    acts = [
        A("Lock screen", "lock", [str(HOME / ".config/i3lock/lock.sh")], "system-lock-screen"),
        A("Sleep", "suspend sleep", ["systemctl", "suspend"], "system-suspend"),
        A("Restart", "reboot restart", ["systemctl", "reboot"], "system-reboot"),
        A("Shut down", "poweroff shutdown power off", ["systemctl", "poweroff"], "system-shutdown"),
        A("Log out", "logout exit i3", ["i3-msg", "exit"], "system-log-out"),
        A("Performance mode: Quiet (least battery)", "performance mode quiet battery saver silent", [s + "/power-mode.sh", "quiet"], "battery-caution"),
        A("Performance mode: Balanced", "performance mode balanced normal", [s + "/power-mode.sh", "balanced"], "battery-good"),
        A("Performance mode: Performance (CPU and fans unrestricted)", "performance mode fast turbo max", [s + "/power-mode.sh", "performance"], "battery-full-charged"),
        A("Night tint on/off", "night light tint warm", [s + "/nightlight.sh", "toggle"], "weather-clear-night"),
        A("Caffeine on/off (keep screen awake)", "caffeine awake idle presentation", [s + "/caffeine.sh", "toggle"], "coffee"),
        A("Tablet mode on/off", "tablet keyboard touch", [s + "/tablet.sh", "toggle"], "input-tablet"),
        A("Freeze/unfreeze auto-rotate", "rotate rotation lock orientation", [s + "/autorotate.sh", "lock"], "object-rotate-right"),
        A("Next wallpaper", "wallpaper background", [str(HOME / ".config/wallpaper-config/wallpaper-cycle.sh")], "preferences-desktop-wallpaper"),
        A("Browse all apps", "apps all browse show all applications grid", [s + "/rog-ui.sh", "apps"], "view-app-grid"),
        A("Back up to hp-server…", "backup rsync sync server hp external", [s + "/rog-ui.sh", "backup"], "drive-harddisk"),
        A("Settings", "settings preferences options configure toggles", [s + "/rog-ui.sh", "settings"], "preferences-system"),
        A("System update (reads Arch news first)", "update upgrade pacman news arch system packages", ["kitty", "--class", "rog-sync", s + "/rog-update.sh"], "system-software-update"),
        A("Roll back the last update", "rollback roll back undo update downgrade restore packages", ["kitty", "--class", "rog-sync", s + "/rog-update.sh", "--rollback"], "document-revert"),
        A("Task monitor", "task monitor processes cpu memory", [s + "/sysmon.sh"], "utilities-system-monitor"),
        A("Keybinds", "keys shortcuts keybinds help", [s + "/keys.sh"], "input-keyboard"),
        A("Restart i3", "reload restart i3", ["i3-msg", "restart"], "view-refresh"),
    ]
    try:
        if "resume_offset" in Path("/proc/cmdline").read_text():
            acts.append(A("Hibernate", "hibernate", ["systemctl", "hibernate"], "system-suspend-hibernate"))
    except Exception:
        pass
    return acts

def actions_for(q):
    if len(q) < 2:
        return []
    out = []
    for title, kw, cmd, icon in action_items():
        s = max(match(q, title), match(q, kw) * 0.8)
        if s:
            out.append(Item("Action", title, "Action", icon, 12 + s,
                            run=lambda c=cmd: spawn(c), key="act:" + title))
    return out

ROOTS = (str(HOME) + "/", "/mnt/", "/media/", "/run/media/", "/srv/")
JUNK = ("site-packages", "/env/", "/venv/", "/.venv/", "/.local/share/", "/yay/", "/go/pkg/", "/target/debug/", "/target/release/", "/dist-packages/", "/.local/state/", "/.docker/", "/snap/", "/.gradle/", "/.m2/", "/.ollama/", "/libreoffice/", "/.config/Code/", "/.config/BraveSoftware/", "/.cache/", "/.git/", "/node_modules/", "/.local/share/Trash/", "/.zen/", "/.mozilla/", "/.config/google-chrome/",
        "/.config/chromium/", "/.npm/", "/.cargo/registry/", "/.rustup/", "/__pycache__/", "/.local/share/pnpm/", "/.nvm/",
        "/.var/", "/.steam/", "/.wine/", "/proc/", "/sys/", "/usr/lib/", "/usr/share/", "/var/", "/opt/", "/.local/lib/")

def FILEMETA(p):
    return file_meta(p)

def files_for(q):
    if len(q) < 2:
        return []
    out = sh(["plocate", "-i", "-b", "-l", "300", "--", q.replace(" ", "*")], 0.8).splitlines()
    items = []
    home = str(HOME)
    for p in out:
        if not p.startswith(ROOTS) or any(j in p for j in JUNK) or not os.path.exists(p):
            continue
        name = os.path.basename(p)
        s = match(q, name)
        if not s:
            s = 20
        rel = p[len(home):] if p.startswith(home) else p
        hidden = sum(1 for c in rel.split("/")[:-1] if c.startswith("."))
        s = s * 0.5 - 15 * hidden - 1.5 * rel.count("/") - 8 * (name.startswith(".")) + (10 if p.startswith(home) else 0)
        d = os.path.dirname(p).replace(home, "~")
        isdir = os.path.isdir(p)
        items.append(Item("Folder" if isdir else "File", name, FILEMETA(p) + "  ·  " + d, "folder" if isdir else Gio.content_type_get_icon(Gio.content_type_guess(p, None)[0]),
                          s, run=lambda p=p: spawn(["xdg-open", p]), alt=lambda p=p: p,
                          reveal=lambda p=p: spawn(["xdg-open", os.path.dirname(p)]), key="file:" + p))
    items.sort(key=lambda i: -i.score)
    return items[:6]

def recent_files(limit=3):
    out = []
    xbel = HOME / ".local/share/recently-used.xbel"
    try:
        txt = xbel.read_text()
    except Exception:
        return out
    from urllib.parse import unquote
    for m in reversed(re.findall(r'<bookmark href="file://([^"]+)"', txt)):
        p = unquote(m)
        if os.path.exists(p) and not any(j in p for j in JUNK):
            out.append(Item("Recent", os.path.basename(p), os.path.dirname(p).replace(str(HOME), "~"),
                            Gio.content_type_get_icon(Gio.content_type_guess(p, None)[0]), 5,
                            run=lambda p=p: spawn(["xdg-open", p]), alt=lambda p=p: p,
                            reveal=lambda p=p: spawn(["xdg-open", os.path.dirname(p)]), key="file:" + p))
        if len(out) >= limit:
            break
    return out

MATH = re.compile(r"^=|[0-9].*[-+*/^%()×÷]|\d\s*[a-zA-Z°]+\s+(to|in|as)\s+\S+|^(sqrt|sin|cos|tan|log|ln|exp|abs)\b", re.I)
def calc_norm(expr):
    """Say what people mean: `in` is a conversion (not inches), c/f/k after a number or `to` are temperatures, `15% of 80` is a product."""
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s+", r"(\1/100) * ", expr)
    expr = re.sub(r"\s+(?:in|as)\s+(?=(?!to\b)[A-Za-z°/])", " to ", expr)
    temp = {"c": "celsius", "f": "fahrenheit", "k": "kelvin"}
    expr = re.sub(r"(?<=\d)\s*°?\s*([cfCF])\b(?!\w)", lambda m: " " + temp[m.group(1).lower()], expr)
    expr = re.sub(r"(?<=\bto)\s+°?([cfkCFK])\s*$", lambda m: " " + temp[m.group(1).lower()], expr)
    return expr

def calc_for(q):
    if not MATH.search(q):
        return []
    expr = q.lstrip("=").strip()
    if not expr:
        return []
    shown = expr
    expr = calc_norm(expr)
    r = sh(["qalc", "-t", expr], 1.0).strip().splitlines()
    r = r[-1].strip() if r else ""
    m = re.match(r"^(.+?)\s+to\s+([A-Za-z°]+)$", expr)
    if m and re.search(r"\S\s\+\s\d", r):         # qalc splits miles/feet into "3 mi + 188 yd + ..."; give one number instead
        n = sh(["qalc", "-t", f"({m.group(1)}) / (1 {m.group(2)})"], 1.0).strip().splitlines()
        if n and re.match(r"^-?[0-9.]+(E[-+]?\d+)?$", n[-1].strip()):
            r = f"{n[-1].strip()} {m.group(2)}"
    if not r or r.lower().startswith(("error", "warning")) or r == expr:
        return []
    r = r.replace("≈", "").strip()
    return [Item("Calc", r, f"{shown}  ·  Enter copies", "accessories-calculator", 1000, run=lambda r=r: copy(r), alt=lambda r=r: r)]

CLIPFILE = STATE / "clipboard.json"
CLIPIMG = STATE / "clipimg"
CLIP_MAX = 3000            # entries kept (text and images together); greenclip kept 200
CLIP_MAX_IMAGES = 300
CLIP_MAX_CHARS = 500000
_clip_cache = []           # the resident process keeps the history in memory: searching never re-reads the file

def load_clip():
    global _clip_cache
    if not _clip_cache:
        try:
            _clip_cache = json.loads(CLIPFILE.read_text())
        except Exception:
            _clip_cache = []
    return _clip_cache

def save_clip():
    try:
        tmp = CLIPFILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_clip_cache)); os.chmod(tmp, 0o600); tmp.replace(CLIPFILE)
    except Exception:
        pass

def prune_clip():
    global _clip_cache
    imgs = 0; keep = []
    for e in _clip_cache:
        if "img" in e:
            imgs += 1
            if imgs > CLIP_MAX_IMAGES:
                try: (CLIPIMG / e["img"]).unlink()
                except Exception: pass
                continue
        keep.append(e)
    _clip_cache = keep[:CLIP_MAX]

def store_clip(text, ts=None):
    """Called by the resident process whenever the clipboard changes (event driven, no polling)."""
    if not text or not text.strip() or len(text) > CLIP_MAX_CHARS:
        return
    load_clip()
    _clip_cache[:] = [e for e in _clip_cache if e.get("s") != text]
    _clip_cache.insert(0, {"t": int(ts or time.time()), "s": text})
    prune_clip(); save_clip()

def store_clip_image(png, w=0, h=0, ts=None):
    import hashlib
    name = hashlib.sha1(png).hexdigest()[:20] + ".png"
    CLIPIMG.mkdir(parents=True, exist_ok=True)
    p = CLIPIMG / name
    if not p.exists():
        p.write_bytes(png); os.chmod(p, 0o600)
    load_clip()
    _clip_cache[:] = [e for e in _clip_cache if e.get("img") != name]
    _clip_cache.insert(0, {"t": int(ts or time.time()), "img": name, "w": w, "h": h})
    prune_clip(); save_clip()

def clip_changed(cb):
    """Clipboard owner changed: text is stored as text; if there is no text but an image (a screenshot), as an image."""
    def got_image(c, pix, d):
        if pix is None: return
        ok, buf = pix.save_to_bufferv("png", [], [])
        if ok and len(buf) < 40_000_000: store_clip_image(bytes(buf), pix.get_width(), pix.get_height())
    def got_text(c, text, d):
        if text is not None and text.strip(): store_clip(text)
        elif text is None: c.request_image(got_image, None)
    cb.request_text(got_text, None)

def clip_for(q):
    from gi.repository import GdkPixbuf
    items = []
    ql = q.lower()
    for i, e in enumerate(load_clip()):
        if "img" in e:
            if ql and not any(w in ("image", "img", "screenshot", "picture", "png") or w in "image screenshot" for w in ql.split()):
                continue
            p = CLIPIMG / e["img"]
            if not p.exists(): continue
            try: pix = GdkPixbuf.Pixbuf.new_from_file_at_size(str(p), 56, 56)
            except Exception: pix = None
            if not e.get("w"):                                  # imported entries have no size on record: read it from the file once
                try:
                    info = GdkPixbuf.Pixbuf.get_file_info(str(p)); e["w"], e["h"] = info[1], info[2]
                except Exception: pass
            items.append(Item("Clip", f"Image  {e.get('w', '?')} × {e.get('h', '?')}", "", pix or "image-x-generic",
                              500 - i, run=lambda p=p: spawn(["sh", "-c", f"xclip -selection clipboard -t image/png -i '{p}'"]),
                              reveal=lambda p=p: spawn(["xdg-open", str(p)])))
        else:
            text = e.get("s", "")
            if ql and ql not in text.lower():
                continue
            lines = text.strip().splitlines() or [text]
            title = lines[0].strip()[:140] + (f"   (+{len(lines) - 1} lines)" if len(lines) > 1 else "")
            items.append(Item("Clip", title, "", "edit-paste", 500 - i,
                              run=lambda t=text: copy(t), alt=lambda t=text: t))
        if len(items) >= MAXROWS:
            break
    return items

def import_greenclip():
    """One-off: bring the old greenclip history in (exact text and images), oldest first so the order is kept."""
    import subprocess as sp
    lines = sp.run(["greenclip", "print"], capture_output=True, text=True).stdout.splitlines()
    now = int(time.time()); n = len(lines); done = 0
    load_clip()
    for k, line in enumerate(reversed(lines)):
        ts = now - (n - k) * 600                          # greenclip kept no times: spread them out, keeping the order
        if not line.strip(): continue
        pr = sp.Popen(["greenclip", "print", line], stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        time.sleep(0.25)
        try:
            if line.startswith("image/"):
                png = sp.run(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"], capture_output=True, timeout=3).stdout
                if png[:4] == b"\x89PNG": store_clip_image(png, ts=ts); done += 1
            else:
                txt = sp.run(["xclip", "-selection", "clipboard", "-o"], capture_output=True, timeout=3).stdout.decode("utf-8", "replace")
                if txt.strip(): store_clip(txt, ts=ts); done += 1
        finally:
            pr.kill()
    print(f"imported {done} of {n} entries from greenclip")

def web_for(q):
    if len(q) < 2:
        return []
    from urllib.parse import quote_plus
    if re.match(r"^(https?://|www\.)\S+$", q):
        url = q if q.startswith("http") else "https://" + q
        return [Item("Web", f"Open {url}", "", "web-browser", 900, run=lambda: spawn(["xdg-open", url]))]
    url = "https://duckduckgo.com/?q=" + quote_plus(q)
    return [Item("Web", f"Search the web for “{q}”", "", "web-browser", -100, run=lambda: spawn(["xdg-open", url]))]

# ---------------------------------------------------------------- timers and reminders
ALARMS = STATE / "alarms.json"
_DUR = re.compile(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|m|min|mins|minutes?|s|sec|secs|seconds?)(?![a-z])", re.I)

def parse_duration(text):
    """'25m', '1h30m', '90s', '1 hour 15 min', or a bare number = minutes. Returns (seconds, rest-of-text) or None."""
    t = text.strip(); total = 0; pos = 0; found = False
    for m in _DUR.finditer(t):
        if t[pos:m.start()].strip(): break
        n = float(m.group(1)); u = m.group(2).lower()[0]
        total += n * (3600 if u == "h" else 60 if u == "m" else 1); pos = m.end(); found = True
    if not found:
        m = re.match(r"^(\d+(?:\.\d+)?)\b", t)
        if not m: return None
        total = float(m.group(1)) * 60; pos = m.end()
    return (int(total), t[pos:].strip()) if 0 < total <= 7 * 86400 else None

def parse_clock(text):
    """'15:30', '3pm', '3:30 pm', optionally prefixed by 'tomorrow'. Returns (seconds from now, rest) or None."""
    t = text.strip(); day = 0
    m0 = re.match(r"^(tomorrow|tmrw)\s+", t, re.I)
    if m0: day = 1; t = t[m0.end():]
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", t, re.I)
    if not m: return None
    h = int(m.group(1)); mi = int(m.group(2) or 0); ap = (m.group(3) or "").lower()
    if not m.group(2) and not ap: return None              # a lone number is not a time of day
    if ap == "pm" and h < 12: h += 12
    if ap == "am" and h == 12: h = 0
    if h > 23 or mi > 59: return None
    now = time.time(); lt = time.localtime(now)
    due = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + day, h, mi, 0, 0, 0, -1))
    if due <= now and not day: due += 86400
    return (int(math.ceil(due - now)), t[m.end():].strip())

def fmt_secs(n):
    h, r = divmod(int(n), 3600); m, sec = divmod(r, 60)
    return " ".join(x for x in (f"{h} h" if h else "", f"{m} min" if m else "", f"{sec} s" if sec and not h else "") if x) or "0 s"

def load_alarms():
    try: return json.loads(ALARMS.read_text())
    except Exception: return {}

def schedule_alarm(secs, msg):
    unit = f"rog-alarm-{int(time.time())}"
    env = [f"--setenv={k}={os.environ[k]}" for k in ("DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS") if k in os.environ]
    subprocess.run(["systemd-run", "--user", "--collect", f"--unit={unit}", f"--on-active={max(100, int(secs * 1000) - 400)}ms", "--timer-property=AccuracySec=10ms"] + env +   # fire early by the ~0.4 s the alarm window needs to start
                   ["python3", str(SCRIPTS / "alarm.py"), msg or "Time is up"], capture_output=True)
    a = load_alarms(); a[unit] = {"msg": msg or "Timer", "due": int(time.time() + secs)}
    a = {k: v for k, v in a.items() if v["due"] > time.time() - 60}
    ALARMS.write_text(json.dumps(a))

def cancel_alarm(unit):
    subprocess.run(["systemctl", "--user", "stop", unit + ".timer"], capture_output=True)
    a = load_alarms(); a.pop(unit, None); ALARMS.write_text(json.dumps(a))

def alarm_items(q):
    ql = q.strip().lower()
    items = []
    m = re.match(r"^(timer|countdown)\s+(.+)$", ql)
    if m:
        d = parse_duration(m.group(2))
        if d:
            secs, label = d; due = time.strftime("%H:%M", time.localtime(time.time() + secs))
            items.append(Item("Timer", f"Timer for {fmt_secs(secs)}" + (f" — {label}" if label else ""), f"Rings at {due}  ·  press Enter to start",
                              "appointment-soon", 1000, run=lambda s=secs, l=label: schedule_alarm(s, l or f"Timer ({fmt_secs(s)}) is up")))
        return items
    m = re.match(r"^remind(?:er)?(?:\s+me)?\s+(?:to\s+)?(.+)$", ql)
    if m:
        body = m.group(1); res = None
        mi = re.match(r"^in\s+(.+)$", body)
        if mi:
            d = parse_duration(mi.group(1))
            if d: res = d
        if not res:
            mc = re.match(r"^(?:at\s+)?(.+)$", body)
            if mc: res = parse_clock(mc.group(1))
        if res:
            secs, label = res; due = time.strftime("%a %H:%M", time.localtime(time.time() + secs))
            items.append(Item("Timer", (label or "Reminder"), f"Reminder {due}  ·  press Enter to set it",
                              "appointment-soon", 1000, run=lambda s=secs, l=label: schedule_alarm(s, l or "Reminder")))
        return items
    if ql in ("timers", "alarms", "reminders", "timer", "remind"):
        a = load_alarms(); now = time.time()
        for unit, v in sorted(a.items(), key=lambda kv: kv[1]["due"]):
            if v["due"] > now:
                items.append(Item("Timer", v["msg"], f"Rings in {fmt_secs(v['due'] - now)} ({time.strftime('%H:%M', time.localtime(v['due']))})  ·  Enter cancels it",
                                  "appointment-soon", 900, run=lambda u=unit: cancel_alarm(u)))
        if not items:
            items.append(Item("Timer", "No timers or reminders pending", "Try:  timer 25m   ·   remind in 10m call back   ·   remind 15:30 stand up", "appointment-soon", 900))
    return items

# ---------------------------------------------------------------- more sources (each is wrapped so one failure cannot break Spotlight)
KINDS = {"pdf": (".pdf",), "doc": (".doc", ".docx", ".odt", ".rtf", ".pages"), "slides": (".ppt", ".pptx", ".odp", ".key"),
         "sheet": (".xls", ".xlsx", ".ods", ".csv", ".tsv"), "image": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp", ".tif", ".tiff"),
         "text": (".txt", ".md", ".org", ".rst", ".tex", ".bib"), "code": (".py", ".rs", ".c", ".h", ".cpp", ".js", ".ts", ".sh", ".go", ".java", ".ipynb", ".r"),
         "audio": (".mp3", ".flac", ".ogg", ".opus", ".wav", ".m4a"), "video": (".mp4", ".mkv", ".webm", ".avi", ".mov"), "archive": (".zip", ".tar", ".gz", ".xz", ".7z", ".rar", ".tgz")}

def split_filters(q):
    """kind:pdf  in:notes  modified:today|week|month   (any order); returns ({filters}, remaining text)."""
    flt = {}; rest = []
    for tok in q.split():
        m = re.match(r"^(kind|type|in|modified|mod):(\S*)$", tok, re.I)
        if m and m.group(2):
            k = m.group(1).lower(); k = {"type": "kind", "mod": "modified"}.get(k, k); flt[k] = m.group(2).lower()
        else: rest.append(tok)
    return flt, " ".join(rest)

def filtered_files(flt, text):
    kind = flt.get("kind"); exts = KINDS.get(kind) if kind else None
    if kind and kind != "folder" and not exts: return []
    home = str(HOME); hre = re.escape(home); inn = re.escape(flt["in"]) if flt.get("in") else ""
    if text:
        pat = ["plocate", "-i", "-b", "-l", "4000", "--", text.replace(" ", "*")]
    elif exts:
        pat = ["plocate", "--regex", "-i", "-l", "8000", "--", f"^{hre}/.*{inn}.*({'|'.join(re.escape(e) for e in exts)})$"]
    else:
        pat = ["plocate", "--regex", "-i", "-l", "4000", "--", f"^{hre}/.*{inn or '[^/]'}[^/]*$"]
    out = sh(pat, 3.0).splitlines(); res = []
    cutoff = {"today": 86400, "day": 86400, "week": 7 * 86400, "month": 30 * 86400}.get(flt.get("modified", ""), 0)
    for p in out:
        if not p.startswith(ROOTS) or any(j in p for j in JUNK): continue
        rel = p[len(home):] if p.startswith(home) else p
        if any(c.startswith(".") for c in rel.split("/")[:-1]): continue            # hidden folders are noise for a filtered listing
        if exts and not p.lower().endswith(exts): continue
        if kind == "folder" and not os.path.isdir(p): continue
        if flt.get("in") and flt["in"] not in p.lower().replace(home.lower(), ""): continue
        try: mt = os.stat(p).st_mtime
        except OSError: continue
        if cutoff and time.time() - mt > cutoff: continue
        res.append((mt, p))
    res.sort(reverse=True)
    items = []
    for mt, p in res[:MAXROWS]:
        isdir = os.path.isdir(p)
        items.append(Item("Folder" if isdir else "File", os.path.basename(p), file_meta(p) + "  ·  " + os.path.dirname(p).replace(home, "~"),
                          "folder" if isdir else Gio.content_type_get_icon(Gio.content_type_guess(p, None)[0]), 100 - len(items),
                          run=lambda p=p: spawn(["xdg-open", p]), alt=lambda p=p: p, reveal=lambda p=p: spawn(["xdg-open", os.path.dirname(p)]), key="file:" + p))
    return items

ALIASES_FILE = HOME / ".config/spotlight/aliases"
def alias_items(q):
    try:
        if not ALIASES_FILE.exists():
            ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
            ALIASES_FILE.write_text("# your own shortcuts for Spotlight:  name = what to open  (a folder, a file, a web address, or a command)\\n"
                                    "# examples (remove the # to use):\\n# thesis = ~/Documents/thesis\\n# overleaf = https://www.overleaf.com/project\\n# gpu = kitty -e nvidia-smi\\n")
        out = []; ql = q.strip().lower()
        for line in ALIASES_FILE.read_text().splitlines():
            if "=" not in line or line.lstrip().startswith("#"): continue
            name, target = (x.strip() for x in line.split("=", 1))
            if not name or not target or not (ql == name.lower() or name.lower().startswith(ql)): continue
            path = os.path.expanduser(target)
            if re.match(r"^https?://", target) or os.path.exists(path): fn = lambda t=path if os.path.exists(path) else target: spawn(["xdg-open", t])
            else: fn = lambda t=target: spawn(["sh", "-lc", t])
            out.append(Item("Alias", name, target, "emblem-symbolic-link" if False else "go-jump", 1500 if ql == name.lower() else 900, run=fn, key="alias:" + name))
        return out
    except Exception:
        return []

ACTIONS_OF = {}
def app_action_items(q):
    if len(q) < 3: return []
    out = []
    for a, name, desc, kw, aid in APPS:
        acts = ACTIONS_OF.get(aid)
        if acts is None:
            try:
                d = Gio.DesktopAppInfo.new(aid); acts = [(x, d.get_action_name(x)) for x in (d.list_actions() if d else [])]
            except Exception: acts = []
            ACTIONS_OF[aid] = acts
        for act, label in acts:
            sc = match(q, f"{name} {label}")
            if sc and sc >= 60 and match(q.split()[0], name):
                out.append(Item("Action", f"{name}: {label}", "App action", a.get_icon(), 18 + sc * 0.9,
                                run=lambda aid=aid, act=act: Gio.DesktopAppInfo.new(aid).launch_action(act, Gdk.Display.get_default().get_app_launch_context()), key=f"act:{aid}:{act}"))
    return out

_EMOJI = []
def emoji_items(q):
    import unicodedata
    if not _EMOJI:
        for lo, hi in ((0x1F300, 0x1FAFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF), (0x1F000, 0x1F2FF)):
            for cp in range(lo, hi + 1):
                try: _EMOJI.append((chr(cp), unicodedata.name(chr(cp)).lower()))
                except ValueError: pass
        for cp in (0x2764, 0x2705, 0x274C, 0x2728, 0x2B50, 0x203C, 0x2049, 0x2753, 0x2757, 0x00A9, 0x00AE, 0x2122): _EMOJI.append((chr(cp), unicodedata.name(chr(cp), "").lower()))
    words = q.lower().split()
    out = []
    for ch, nm in _EMOJI:
        if all(w in nm for w in words):
            out.append(Item("Emoji", f"{ch}   {nm}", "Enter copies it", "face-smile", 500 - len(out), run=lambda c=ch: copy(c), alt=lambda c=ch: c))
            if len(out) >= MAXROWS: break
    return out

def history_items(q):
    try: lines = (HOME / ".bash_history").read_text(errors="replace").splitlines()
    except Exception: return []
    seen = set(); out = []; ql = q.lower()
    for cmd in reversed(lines):
        cmd = cmd.strip()
        if not cmd or cmd in seen or cmd.startswith("#"): continue
        seen.add(cmd)
        if ql and ql not in cmd.lower(): continue
        out.append(Item("Cmd", cmd[:140], "Shell history  ·  Enter copies  ·  Ctrl+Enter runs it in a terminal", "utilities-terminal", 500 - len(out),
                        run=lambda c=cmd: copy(c), alt=lambda c=cmd: c, reveal=lambda c=cmd: spawn(["kitty", "sh", "-lc", c + "; echo; read -rp 'press Enter to close '"])))
        if len(out) >= MAXROWS: break
    return out

# ---- bookmarks (Chrome, Firefox/Zen, qutebrowser): indexed once, re-read only when a file changed
_BM = {"sig": None, "items": []}
def bookmark_files():
    fs = []
    for pat in (".config/google-chrome/*/Bookmarks", ".config/chromium/*/Bookmarks", ".zen/*/places.sqlite", ".config/mozilla/firefox/*/places.sqlite",
                ".mozilla/firefox/*/places.sqlite", ".config/qutebrowser/quickmarks", ".config/qutebrowser/bookmarks/urls"):
        fs += [Path(p) for p in __import__("glob").glob(str(HOME / pat))]
    return fs

def load_bookmarks():
    files = bookmark_files()
    sig = tuple((str(f), int(f.stat().st_mtime)) for f in files if f.exists())
    if sig == _BM["sig"]: return _BM["items"]
    out = []
    import tempfile, shutil, sqlite3
    for f in files:
        try:
            if f.name == "Bookmarks":
                def walk(n):
                    if n.get("type") == "url": out.append((n.get("name", ""), n.get("url", "")))
                    for c in n.get("children", []): walk(c)
                for r in json.loads(f.read_text()).get("roots", {}).values():
                    if isinstance(r, dict): walk(r)
            elif f.name == "places.sqlite":
                tmp = Path(tempfile.mkdtemp(prefix="bm-")); shutil.copy(f, tmp / "p.sqlite")
                for ext in ("-wal", "-shm"):
                    w = f.with_name(f.name + ext)
                    if w.exists(): shutil.copy(w, tmp / ("p.sqlite" + ext))
                con = sqlite3.connect(str(tmp / "p.sqlite"))
                for t, u in con.execute("SELECT b.title, p.url FROM moz_bookmarks b JOIN moz_places p ON b.fk = p.id WHERE b.type = 1 AND p.url NOT LIKE 'place:%'"):
                    out.append((t or u, u))
                con.close(); shutil.rmtree(tmp, ignore_errors=True)
            elif f.name == "quickmarks":
                for l in f.read_text().splitlines():
                    parts = l.rsplit(" ", 1)
                    if len(parts) == 2: out.append((parts[0], parts[1]))
            elif f.name == "urls":
                for l in f.read_text().splitlines():
                    parts = l.split(" ", 1)
                    if parts and parts[0].startswith("http"): out.append((parts[1] if len(parts) > 1 else parts[0], parts[0]))
        except Exception:
            continue
    seen = set(); items = []
    for t, u in out:
        if u and u not in seen: seen.add(u); items.append((t, u))
    _BM["sig"] = sig; _BM["items"] = items
    return items

def bookmark_items(q):
    if len(q) < 3: return []
    out = []
    for t, u in load_bookmarks():
        sc = max(match(q, t), match(q, u) * 0.6)
        if sc >= 60:
            out.append(Item("Bookmark", t[:100], u[:120], "user-bookmarks", 28 + sc * 0.5, run=lambda u=u: spawn(["xdg-open", u]), alt=lambda u=u: u, key="bm:" + u))
    out.sort(key=lambda i: -i.score)
    return out[:4]

# ---- Zotero library (title, first author, year); Enter opens the item in Zotero, Ctrl+Y previews its PDF
_ZOT = {"sig": None, "path": None}
def zotero_items(q):
    import sqlite3, shutil
    db = None
    for c in (HOME / "Zotero/zotero.sqlite", HOME / ".zotero/zotero.sqlite"):
        if c.exists(): db = c; break
    if db is None or len(q) < 3: return []
    sig = int(db.stat().st_mtime); cache = STATE / "zotero.sqlite"
    if _ZOT["sig"] != sig:
        shutil.copy(db, cache); _ZOT["sig"] = sig
    con = sqlite3.connect(str(cache)); out = []
    try:
        rows = con.execute("""
            SELECT i.itemID, i.key, v.value,
                   (SELECT c.lastName FROM itemCreators ic JOIN creators c ON ic.creatorID = c.creatorID WHERE ic.itemID = i.itemID ORDER BY ic.orderIndex LIMIT 1),
                   (SELECT substr(v2.value, 1, 4) FROM itemData d2 JOIN itemDataValues v2 ON d2.valueID = v2.valueID JOIN fields f2 ON d2.fieldID = f2.fieldID
                     WHERE d2.itemID = i.itemID AND f2.fieldName = 'date')
            FROM items i JOIN itemData d ON d.itemID = i.itemID JOIN fields f ON d.fieldID = f.fieldID AND f.fieldName = 'title'
                 JOIN itemDataValues v ON d.valueID = v.valueID
            WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems) AND v.value LIKE ? ESCAPE '\\' LIMIT 40""",
            ("%" + q.replace("%", "").replace("_", " ") + "%",)).fetchall()
        for iid, key, title, author, year in rows:
            att = con.execute("SELECT a.path, k.key FROM itemAttachments a JOIN items k ON a.itemID = k.itemID WHERE a.parentItemID = ? AND a.path LIKE 'storage:%' LIMIT 1", (iid,)).fetchone()
            pdf = (db.parent / "storage" / att[1] / att[0][len("storage:"):]) if att else None
            sub = " · ".join(x for x in (author, year, "PDF attached" if pdf and pdf.exists() else "") if x) or "Zotero"
            out.append(Item("Zotero", title[:110], sub, "accessories-dictionary", 40 + match(q, title) * 0.4,
                            run=lambda k=key: spawn(["xdg-open", f"zotero://select/library/items/{k}"]),
                            alt=(lambda p=pdf: str(p)) if pdf and pdf.exists() else (lambda t=title: t), key="zot:" + key))
    except Exception:
        out = []
    con.close()
    return sorted(out, key=lambda i: -i.score)[:4]

def last_backup_hint():
    try: ts = float((STATE / "last-backup").read_text())
    except Exception: return None
    days = (time.time() - ts) / 86400
    return int(days) if days >= 7 else None

def search(q):
    q = q.strip()
    if q.startswith(">"):
        cmd = q[1:].strip()
        return [Item("Run", cmd or "type a command", "Enter runs it · Ctrl+Enter in a terminal", "utilities-terminal", 1000,
                     run=(lambda: spawn(["sh", "-lc", cmd])) if cmd else None,
                     reveal=(lambda: spawn(["kitty", "sh", "-lc", cmd + "; read -rp 'done> '"])) if cmd else None)]
    ai = alarm_items(q)
    if ai: return ai
    flt, rest = split_filters(q)
    if flt:
        try: return filtered_files(flt, rest)
        except Exception: return []
    if q.startswith(":") and len(q) > 1:
        try: return emoji_items(q[1:].strip())
        except Exception: return []
    mh = re.match(r"^(h|hist|history)\s+(.*)$", q) if " " in q else None
    if mh or q in ("h", "hist", "history"):
        return history_items(mh.group(2) if mh else "")
    if re.match(r"^c(lip)?\s", q) or q in ("c", "clip"):
        return clip_for(q.split(None, 1)[1] if " " in q else "")
    if re.match(r"^w\s", q) or q == "w":
        return sorted(windows_for(q[2:].strip(), only=True), key=lambda i: -i.score)[:MAXROWS]
    items = calc_for(q) + apps_for(q) + windows_for(q) + actions_for(q) + files_for(q) + web_for(q)
    for src in (alias_items, app_action_items, bookmark_items, zotero_items):
        try: items += src(q)
        except Exception: pass
    if len(q) >= 3 and not any(i.kind in ("File", "Folder", "Recent") for i in items):
        items.append(Item("Deep", f"Search inside documents for “{q}”", "Looks inside your notes, PDFs and text files  ·  press Enter  ·  the first search builds a PDF text cache and is slower",
                          "system-search", -50, key="deep:" + q))
    items.sort(key=lambda i: -i.score)
    return items[:MAXROWS]

# ---------------------------------------------------------------- deep search (inside documents)
def deep_roots():
    f = HOME / ".config/spotlight/roots"
    try:
        r = [Path(os.path.expanduser(l.strip())) for l in f.read_text().splitlines() if l.strip() and not l.startswith("#")]
        r = [p for p in r if p.exists()]
        if r: return r
    except Exception:
        pass
    return [p for p in (HOME / "notes", HOME / "Documents") if p.exists()]

def deep_items(q):
    cmd = ["rg", "--no-heading", "--with-filename", "--line-number", "--max-count", "1", "--max-columns", "240", "--max-columns-preview",
           "-i", "-F", "--pre", str(SCRIPTS / "rg-pre.sh"), "--pre-glob", "*.pdf",
           "-g", "*.{md,txt,tex,pdf,org,rst,bib,csv,html,ipynb,qmd,R,py}",
           "-g", "!{node_modules,.git,site-packages,__pycache__,.venv,venv,.obsidian,.trash}", "--", q] + [str(r) for r in deep_roots()]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=40).stdout
    except Exception:
        out = ""
    items = []
    for line in out.splitlines():
        m = re.match(r"^(.*?):(\d+):(.*)$", line)
        if not m: continue
        p, ln, text = m.group(1), m.group(2), m.group(3).strip()
        if not os.path.exists(p): continue
        sub = (text[:110] if not p.lower().endswith(".pdf") else text[:110]) or os.path.dirname(p)
        items.append(Item("Match", os.path.basename(p), f"{sub}   ·   {os.path.dirname(p).replace(str(HOME), '~')}",
                          Gio.content_type_get_icon(Gio.content_type_guess(p, None)[0]), 0,
                          run=lambda p=p: spawn(["xdg-open", p]), alt=lambda p=p: p,
                          reveal=lambda p=p: spawn(["xdg-open", os.path.dirname(p)])))
        if len(items) >= 12: break
    return items

# ---------------------------------------------------------------- UI
# ---------------------------------------------------------------- appearance: light / dark / follow the night schedule, and the accent colour
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
    if m == "custom":
        v = setting("ACCENT_HEX", "")
        if re.match(r"^#[0-9a-fA-F]{6}$", v): return v
    if m == "wallpaper":
        try:
            v = (HOME / ".cache/accent-wallpaper").read_text().strip()
            if re.match(r"^#[0-9a-fA-F]{6}$", v): return v
        except Exception: pass
    return "#c47f1c"

def _rgb(h): return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)

def themed(css, alpha, dark, acc):
    """css is written in the light palette with the amber accent; swap colours for dark mode and for another accent."""
    r, g, b = _rgb(acc)
    if dark:
        for a, c in (("rgba(238,233,224,__ALPHA__)", "rgba(31,28,25,__ALPHA__)"), ("rgba(60,45,20,0.16)", "rgba(255,255,255,0.10)"), ("rgba(60,45,20,0.14)", "rgba(255,255,255,0.10)"),
                     ("rgba(60,45,20,0.10)", "rgba(255,236,210,0.13)"), ("#2a251e", "#f6efe4"), ("#7d7466", "#a39685"), ("#7b7164", "#a39685"), ("#8d8274", "#8d8274"),
                     ("#8a6a2e", "#d9a95a")):
            css = css.replace(a.encode(), c.encode())
        caret = "#%02x%02x%02x" % tuple(min(255, int(x * 1.18 + 20)) for x in (r, g, b))
    else:
        caret = "#%02x%02x%02x" % tuple(int(x * 0.92) for x in (r, g, b))
    css = css.replace(b"#b86f12", caret.encode()).replace(b"rgba(196,127,28,0.35)", f"rgba({r},{g},{b},0.35)".encode())
    return css.replace(b"__ALPHA__", f"{alpha:.2f}".encode())

def scope(css, cls):
    """Prefix every rule with .cls so the styles of this window cannot leak into the Settings window (they share one process)."""
    out = []
    for m in re.finditer(rb"([^{}]+)\{([^{}]*)\}", css):
        new = []
        for sel in (x.strip() for x in m.group(1).split(b",")):
            new.append(b"window." + cls + sel[6:] if sel.startswith(b"window") else b"." + cls + b" " + sel)
        out.append(b", ".join(new) + b" {" + m.group(2) + b"}")
    return b"\n".join(out)

CSS = b"""
window { background-color: rgba(238,233,224,__ALPHA__); border: 1px solid rgba(60,45,20,0.16); border-radius: 26px; }
.bar { padding: 8px 26px 4px 26px; }
entry { background: transparent; color: #2a251e; font-size: 25pt; font-weight: 300; border: none; box-shadow: none; padding: 12px 6px; caret-color: #b86f12; min-height: 54px; }
entry selection { background: rgba(196,127,28,0.35); color: #2a251e; }
.mag { color: #7b7164; }
separator { background: rgba(60,45,20,0.14); min-height: 1px; margin: 0 22px; }
list { background: transparent; padding: 10px 14px 14px 14px; }
row { padding: 9px 14px; border-radius: 14px; }
row:selected { background: rgba(60,45,20,0.10); }
.t { color: #2a251e; font-size: 14pt; }
.big { font-size: 24pt; font-weight: 300; }
.s { color: #7d7466; font-size: 10.5pt; }
.n { color: transparent; font-size: 9pt; }
row:selected .n { color: #8d8274; }
.empty { color: #7d7466; font-size: 12pt; padding: 18px 26px; }
.head { color: #7d7466; font-size: 10pt; font-weight: bold; padding: 12px 26px 4px 26px; }
.tiles { padding: 4px 16px 8px 16px; }
.allbtn { color: #7d7466; font-size: 10pt; padding: 12px 26px 4px 26px; }
flowbox { background: transparent; padding: 4px 14px 10px 14px; }
.tile { padding: 12px 6px 8px 6px; border-radius: 16px; }
.alltile { padding: 16px 6px 12px 6px; }
.alltile .tl { font-size: 12.5pt; }
.hint { color: #8a6a2e; font-size: 10pt; padding: 8px 26px 6px 26px; }
.foot { color: #8d8274; font-size: 9.5pt; padding: 2px 26px 12px 26px; }
.pvname { color: #2a251e; font-size: 14pt; padding: 6px 26px 2px 26px; }
.pvmeta { color: #7d7466; font-size: 10pt; padding: 0 26px 8px 26px; }
textview, textview text { background: transparent; color: #2a251e; font-family: monospace; font-size: 10.5pt; }
.tile.sel { background: rgba(60,45,20,0.10); }
.tl { color: #2a251e; font-size: 11pt; }
.frow { padding: 8px 14px; border-radius: 14px; margin: 0 14px; }
.frow.sel { background: rgba(60,45,20,0.10); }
"""

def when(ts):
    d = time.localtime(ts); now = time.localtime()
    days = (time.mktime(now[:3] + (0,) * 6) - time.mktime(d[:3] + (0,) * 6)) / 86400
    hm = time.strftime("%H:%M", d)
    if days < 1: return f"Today, {hm}"
    if days < 2: return f"Yesterday, {hm}"
    return time.strftime("%d %b %Y", d)

def file_meta(p):
    try:
        st = os.stat(p)
    except Exception:
        return ""
    ct = Gio.content_type_guess(p, None)[0]
    if os.path.isdir(p): kind = "Folder"
    elif ct == "application/octet-stream" or ct.startswith("inode/"):
        ext = os.path.splitext(p)[1].lstrip(".")
        kind = f"{ext.upper()} file" if ext else "Document"
    else: kind = Gio.content_type_get_description(ct)
    size = "" if os.path.isdir(p) else " · " + GLib.format_size(st.st_size)
    return f"{kind}{size} · {when(st.st_mtime)}"

def setting(key, default):
    try:
        for l in (HOME / ".config/rog/settings.env").read_text().splitlines():
            if l.startswith(key + "="):
                return l.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return default

def recent_apps(n=4):
    out = []
    byid = {aid: (a, name) for a, name, desc, kw, aid in APPS}
    for key, (cnt, last) in sorted(HISTORY.items(), key=lambda kv: -kv[1][1]):
        if key.startswith("app:") and key[4:] in byid:
            a, name = byid[key[4:]]
            out.append(Item("App", name, "", a.get_icon(), 0, run=lambda a=a: launch_app(a), key=key))
        if len(out) >= n: break
    return out

def recent_file_items(n=4):
    seen, out = set(), []
    hist = [k[5:] for k, v in sorted(HISTORY.items(), key=lambda kv: -kv[1][1]) if k.startswith("file:")]
    for p in hist + [i.alt() for i in recent_files_all()]:
        if p in seen or not os.path.exists(p) or any(j in p for j in JUNK): continue
        seen.add(p)
        out.append(Item("File", os.path.basename(p) or p, file_meta(p) + "  ·  " + os.path.dirname(p).replace(str(HOME), "~"),
                        Gio.content_type_get_icon(Gio.content_type_guess(p, None)[0]), 0, run=lambda p=p: spawn(["xdg-open", p]),
                        alt=lambda p=p: p, reveal=lambda p=p: spawn(["xdg-open", os.path.dirname(p)]), key="file:" + p))
        if len(out) >= n: break
    return out

def recent_files_all():
    global MAXRECENT
    return recent_files(limit=12)

class Spot(Gtk.Window):
    def __init__(self, daemon=False):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.daemon = daemon
        self.get_style_context().add_class("spotlight")
        self.set_wmclass("spotlight", "Spotlight")
        self.set_title("Spotlight")
        self.set_decorated(False)
        self.cur_w = 860; self.set_size_request(860, -1)
        self.set_resizable(False)
        self.set_opacity(0.0)                               # stay invisible until placed: no first-frame garbage, no jump
        scr = self.get_screen(); vis = scr.get_rgba_visual()
        if vis: self.set_visual(vis)
        self.prov = Gtk.CssProvider(); self.alpha = None; self.apply_alpha()
        Gtk.StyleContext.add_provider_for_screen(scr, self.prov, Gtk.STYLE_PROVIDER_PRIORITY_USER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL); self.add(box)
        bar = Gtk.Box(spacing=12); bar.get_style_context().add_class("bar")
        mag = Gtk.Image.new_from_icon_name("system-search-symbolic", Gtk.IconSize.DND); mag.set_pixel_size(30)
        mag.get_style_context().add_class("mag"); bar.pack_start(mag, False, False, 0)
        self.entry = Gtk.Entry(); self.entry.set_placeholder_text("Spotlight Search"); self.entry.set_has_frame(False)
        bar.pack_start(self.entry, True, True, 0)
        box.pack_start(bar, False, False, 0)
        self.sep = Gtk.Separator(); box.pack_start(self.sep, False, False, 0)
        self.home = Gtk.Box(orientation=Gtk.Orientation.VERTICAL); box.pack_start(self.home, False, False, 0)
        self.list = Gtk.ListBox(); self.list.set_selection_mode(Gtk.SelectionMode.SINGLE); self.list.set_activate_on_single_click(True)
        box.pack_start(self.list, False, False, 0)
        self.footer = Gtk.Label(label="Ctrl+Y preview   ·   Ctrl+Enter reveal or run in terminal   ·   Ctrl+C copy   ·   Alt+A all apps", xalign=0)
        self.footer.get_style_context().add_class("foot"); box.pack_start(self.footer, False, False, 0)
        self.items = []; self.timer = 0; self.focused = False; self.deep_q = None
        self.home_items = []; self.home_widgets = []; self.home_sel = 0; self.mode = "list"; self.revealed = False
        self.chg = self.entry.connect("changed", self.on_changed)
        self.connect("key-press-event", self.on_key)
        self.connect("key-release-event", self.on_key_release)
        self.space_down = False; self.space_timer = 0; self.space_peek = False
        self.connect("focus-in-event", lambda *a: setattr(self, "focused", True))
        self.connect("focus-out-event", self.on_focus_out)
        self.connect("map-event", self.on_map)
        self.connect_after("draw", self.on_draw)
        self.placed = self.drawn = False
        self.list.connect("row-activated", lambda lb, row: self.activate(row.get_index(), 0))
        if not daemon: self.connect("destroy", Gtk.main_quit)
        self.show_home()

    # ---- placement: fixed distance from the top, centred, revealed only after i3 has put it there
    def set_width(self, w):
        changed = (w != self.cur_w); self.cur_w = w
        self.set_size_request(w, -1); self.resize(w, 1)
        if changed and self.get_mapped(): GLib.timeout_add(60, self.recenter)

    def recenter(self):                                    # the box is anchored at its top-left: keep it centred after a width change
        try:
            d = Gdk.Display.get_default(); mon = d.get_primary_monitor() or d.get_monitor(0); g = mon.get_geometry()
            w, h = self.get_size()
            subprocess.run(["i3-msg", f'[class="^Spotlight$"] move absolute position {g.x + (g.width - w) // 2} px {g.y + int(g.height * 0.15)} px'], capture_output=True, timeout=2)
        except Exception:
            pass
        return False

    def _showall(self):
        # a resident (hidden) window must not be mapped by building its content: only show the children until present_fresh() maps it
        if self.daemon and not self.get_visible(): self.get_child().show_all()
        else: self.show_all()

    def on_map(self, *a):
        self.placed = False; self.drawn = False
        GLib.idle_add(self.place)

    def on_draw(self, *a):                                 # first real paint after a map: the window is ARGB, so showing it earlier = a blurred blank box
        if not self.drawn:
            self.drawn = True; self.maybe_reveal()
        return False

    def maybe_reveal(self):
        if self.placed and self.drawn and not self.revealed:
            GLib.timeout_add(14, self.reveal)

    def place(self):
        try:
            d = Gdk.Display.get_default(); mon = d.get_primary_monitor() or d.get_monitor(0); g = mon.get_geometry()
            w, h = self.get_size()
            x = g.x + (g.width - w) // 2; y = g.y + int(g.height * 0.15)
            subprocess.run(["i3-msg", f'[class="^Spotlight$"] move absolute position {x} px {y} px'], capture_output=True, timeout=2)
        except Exception:
            pass
        self.placed = True; self.maybe_reveal()
        return False

    def reveal(self):
        self.set_opacity(1.0); self.revealed = True
        return False

    def on_focus_out(self, *a):
        if self.focused:
            GLib.timeout_add(150, self.close_if_unfocused)

    def close_if_unfocused(self):
        if not self.is_active():
            self.dismiss()
        return False

    def dismiss(self):
        if not self.daemon:
            self.destroy(); return
        try:                                               # a resident process that has grown past 150 MB restarts itself (the clipboard history is on disk)
            rss = int(open("/proc/self/statm").read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
            if rss > 150: GLib.idle_add(lambda: os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), "--daemon"]))
        except Exception: pass
        if self.timer:
            GLib.source_remove(self.timer); self.timer = 0
        self.deep_q = None; self.focused = False; self.revealed = False
        self.set_opacity(0.0); self.hide()

    def apply_alpha(self):                                 # (name kept) alpha from the Settings slider, light/dark, accent
        try: a = max(0.30, min(1.0, float(setting("SPOT_OPACITY", "62")) / 100))
        except ValueError: a = 0.62
        key = (a, is_dark(), accent_hex())
        if key != self.alpha:                              # only re-style when something changed
            self.alpha = key
            self.prov.load_from_data(scope(themed(CSS, key[0], key[1], key[2]), b"spotlight"))

    def present_fresh(self):                               # resident mode: show again from a clean state
        self.apply_alpha()
        if self.timer:
            GLib.source_remove(self.timer); self.timer = 0
        self.deep_q = None
        self.entry.handler_block(self.chg); self.entry.set_text(""); self.entry.handler_unblock(self.chg)
        self.show_home()
        self.focused = False; self.set_opacity(0.0)
        self.show(); self.present(); self.entry.grab_focus()

    def on_changed(self, *a):
        self.deep_q = None
        if self.timer:
            GLib.source_remove(self.timer)
        self.timer = GLib.timeout_add(90, self.refresh)   # debounce: search once typing pauses

    def refresh(self):
        self.timer = 0
        q = self.entry.get_text().strip()
        if not q:
            self.show_home(); return False
        if re.match(r"^/\s*\S", q):                        # "/ phrase" goes straight to the deep search
            self.start_deep(q[1:].strip()); return False
        self.render(search(q), q)
        return False

    # ---- empty state: recent apps as a grid, recent files as a list (4 each)
    def clear_home(self):
        for c in self.home.get_children(): self.home.remove(c)
        self.home_items, self.home_widgets, self.home_sel = [], [], 0

    def show_home(self):
        self.mode = "home"
        for c in self.list.get_children(): self.list.remove(c)
        self.items = []; self.clear_home()
        if setting("SPOT_RECENTS", "1") == "1":
            apps, files = recent_apps(4), recent_file_items(4)
            if apps:
                hb = Gtk.Box(); h = Gtk.Label(label="Recent apps", xalign=0); h.get_style_context().add_class("head"); hb.pack_start(h, True, True, 0)
                ev0 = Gtk.EventBox(); sa = Gtk.Label(label="Show all  (Alt+A)", xalign=1); sa.get_style_context().add_class("allbtn"); ev0.add(sa)
                ev0.connect("button-press-event", lambda w, e: self.show_all_apps()); hb.pack_end(ev0, False, False, 0)
                self.home.pack_start(hb, False, False, 0)
                row = Gtk.Box(homogeneous=True, spacing=6); row.get_style_context().add_class("tiles")
                for it in apps:
                    ev = Gtk.EventBox(); v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6); v.get_style_context().add_class("tile")
                    img = Gtk.Image.new_from_gicon(it.icon, Gtk.IconSize.DIALOG) if it.icon else Gtk.Image.new_from_icon_name("application-x-executable", Gtk.IconSize.DIALOG)
                    img.set_pixel_size(52); v.pack_start(img, False, False, 0)
                    l = Gtk.Label(label=it.title); l.set_ellipsize(Pango.EllipsizeMode.END); l.set_max_width_chars(16); l.get_style_context().add_class("tl")
                    v.pack_start(l, False, False, 0); ev.add(v)
                    idx = len(self.home_items)
                    ev.connect("button-press-event", lambda w, e, i=idx: self.activate_home(i))
                    row.pack_start(ev, True, True, 0); self.home_items.append(it); self.home_widgets.append(v)
                self.home.pack_start(row, False, False, 0)
            if files:
                h = Gtk.Label(label="Recent files", xalign=0); h.get_style_context().add_class("head"); self.home.pack_start(h, False, False, 0)
                for it in files:
                    ev = Gtk.EventBox(); r = self.make_row(it, 99); r.get_style_context().add_class("frow"); ev.add(r)
                    idx = len(self.home_items)
                    ev.connect("button-press-event", lambda w, e, i=idx: self.activate_home(i))
                    self.home.pack_start(ev, False, False, 0); self.home_items.append(it); self.home_widgets.append(r)
                sp = Gtk.Box(); sp.set_size_request(-1, 10); self.home.pack_start(sp, False, False, 0)
        has = bool(self.home_items)
        dd = last_backup_hint() if setting("SPOT_RECENTS", "1") == "1" else None
        if dd is not None:
            ev = Gtk.EventBox(); lb = Gtk.Label(label=f"Your last backup was {dd} days ago  —  click to back up", xalign=0); lb.get_style_context().add_class("hint"); ev.add(lb)
            ev.connect("button-press-event", lambda w, e: (spawn([str(SCRIPTS / "rog-ui.sh"), "backup"]), self.dismiss())); self.home.pack_start(ev, False, False, 0); has = True
        self._showall()
        self.sep.set_visible(has); self.home.set_visible(has); self.list.set_visible(False); self.footer.set_visible(False)
        self.paint_home()
        self.set_width(860)

    # ---- browse every installed app (alphabetical grid); typing still searches
    def show_all_apps(self):
        self.mode = "all"
        for c in self.list.get_children(): self.list.remove(c)
        self.items = []; self.clear_home()
        hb = Gtk.Box(); h = Gtk.Label(label="All apps", xalign=0); h.get_style_context().add_class("head"); hb.pack_start(h, True, True, 0)
        ev0 = Gtk.EventBox(); bk = Gtk.Label(label="Back  (Esc)", xalign=1); bk.get_style_context().add_class("allbtn"); ev0.add(bk)
        ev0.connect("button-press-event", lambda w, e: self.show_home()); hb.pack_end(ev0, False, False, 0)
        self.home.pack_start(hb, False, False, 0)
        sc = Gtk.ScrolledWindow(); sc.set_min_content_height(560); sc.set_max_content_height(560); sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        fb = Gtk.FlowBox(); fb.set_selection_mode(Gtk.SelectionMode.NONE); fb.set_homogeneous(True)
        fb.set_min_children_per_line(4); fb.set_max_children_per_line(4); fb.set_column_spacing(6); fb.set_row_spacing(4)
        for a, name, desc, kw, aid in sorted(APPS, key=lambda t: t[1].lower()):
            it = Item("App", name, "", a.get_icon(), 0, run=lambda a=a: launch_app(a), key="app:" + aid)
            ev = Gtk.EventBox(); v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6); v.get_style_context().add_class("tile"); v.get_style_context().add_class("alltile")
            img = Gtk.Image.new_from_gicon(it.icon, Gtk.IconSize.DIALOG) if it.icon else Gtk.Image.new_from_icon_name("application-x-executable", Gtk.IconSize.DIALOG)
            img.set_pixel_size(64); v.pack_start(img, False, False, 0)
            l = Gtk.Label(label=name); l.set_ellipsize(Pango.EllipsizeMode.END); l.set_max_width_chars(18); l.get_style_context().add_class("tl")
            v.pack_start(l, False, False, 0); ev.add(v)
            idx = len(self.home_items)
            ev.connect("button-press-event", lambda w, e, i=idx: self.activate_home(i))
            fb.add(ev); self.home_items.append(it); self.home_widgets.append(v)
        sc.add(fb); self.home.pack_start(sc, False, False, 0)
        self.all_scroll = sc; self.all_flow = fb
        self.show_all() if not (self.daemon and not self.get_visible()) else self.get_child().show_all()
        self.sep.set_visible(True); self.home.set_visible(True); self.list.set_visible(False); self.footer.set_visible(False)
        self.set_width(1000); self.paint_home()

    def scroll_to_selected(self):
        if self.mode != "all" or not self.home_widgets: return
        w = self.home_widgets[self.home_sel]
        r = w.translate_coordinates(self.all_flow, 0, 0)           # (x, y), or None before the first layout
        if r:
            adj = self.all_scroll.get_vadjustment(); adj.clamp_page(r[1] - 6, r[1] + w.get_allocated_height() + 6)

    # ---- Quick-Look style preview of the selected file (Ctrl+Y)
    def item_path(self, it):
        try:
            p = it.alt() if it.alt else None
            return p if p and os.path.exists(p) else None
        except Exception:
            return None

    def show_preview(self):
        row = self.list.get_selected_row()
        if not row or not (0 <= row.get_index() < len(self.items)): return
        idx = row.get_index(); it = self.items[idx]; p = self.item_path(it)
        if not p:
            return
        self.prev = (list(self.items), self.entry.get_text().strip(), idx)
        self.mode = "preview"
        for c in self.list.get_children(): self.list.remove(c)
        self.clear_home()
        nm = Gtk.Label(label=os.path.basename(p) or p, xalign=0); nm.set_ellipsize(Pango.EllipsizeMode.MIDDLE); nm.get_style_context().add_class("pvname"); self.home.pack_start(nm, False, False, 0)
        mt = Gtk.Label(label=file_meta(p) + "   ·   Esc or Ctrl+Y goes back   ·   Enter opens", xalign=0); mt.get_style_context().add_class("pvmeta"); self.home.pack_start(mt, False, False, 0)
        sc = Gtk.ScrolledWindow(); sc.set_min_content_height(520); sc.set_max_content_height(520); sc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sc.add(self.preview_widget(p)); self.pv_scroll = sc; self.pv_path = p
        self.home.pack_start(sc, False, False, 0)
        self._showall()
        self.sep.set_visible(True); self.home.set_visible(True); self.list.set_visible(False); self.footer.set_visible(False)
        self.set_width(1000)

    def leave_preview(self):
        items, q, idx = self.prev
        self.render(items, q)
        r = self.list.get_row_at_index(idx)
        if r: self.list.select_row(r)

    def preview_widget(self, p):
        from gi.repository import GdkPixbuf
        mime = Gio.content_type_guess(p, None)[0] or ""
        low = p.lower()
        try:
            if os.path.isdir(p):
                names = sorted(os.listdir(p), key=str.lower)[:200]
                txt = "\n".join((n + ("/" if os.path.isdir(os.path.join(p, n)) else "")) for n in names) or "(empty folder)"
                return self.text_view(txt)
            if mime.startswith("image/"):
                pix = GdkPixbuf.Pixbuf.new_from_file_at_size(p, 960, 560)
                return Gtk.Image.new_from_pixbuf(pix)
            if mime == "application/pdf" or low.endswith(".pdf"):
                out = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "spotlight-preview"
                subprocess.run(["pdftoppm", "-png", "-r", "80", "-f", "1", "-l", "1", "-singlefile", p, str(out)], capture_output=True, timeout=8)
                img = out.with_suffix(".png")
                if img.exists():
                    pix = GdkPixbuf.Pixbuf.new_from_file_at_size(str(img), 960, 3000); img.unlink()
                    return Gtk.Image.new_from_pixbuf(pix)
            textish = mime.startswith("text/") or low.endswith((".md", ".tex", ".bib", ".py", ".json", ".csv", ".yaml", ".yml", ".toml", ".conf", ".ini",
                                                                 ".sh", ".rs", ".c", ".h", ".js", ".ts", ".html", ".css", ".log", ".org", ".rst", ".ipynb", ".qmd", ".r"))
            if textish:
                with open(p, "rb") as f: data = f.read(60000)
                return self.text_view(data.decode("utf-8", "replace"))
        except Exception as e:
            return self.text_view(f"Cannot preview this file ({e}).")
        return self.text_view(f"{file_meta(p)}\n\nNo preview for this kind of file. Press Enter to open it.")

    def text_view(self, txt):
        tv = Gtk.TextView(); tv.set_editable(False); tv.set_cursor_visible(False); tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        tv.set_left_margin(26); tv.set_right_margin(26); tv.set_top_margin(8); tv.get_buffer().set_text(txt); return tv

    def paint_home(self):
        for i, w in enumerate(self.home_widgets):
            ctx = w.get_style_context()
            (ctx.add_class if i == self.home_sel else ctx.remove_class)("sel")
        self.scroll_to_selected()

    def activate_home(self, i, mode=0):
        if 0 <= i < len(self.home_items):
            self.run_item(self.home_items[i], mode)

    # ---- results
    def render(self, found, q, note=None):
        self.mode = "list"
        self.clear_home(); self.home.set_visible(False)
        for c in self.list.get_children():
            self.list.remove(c)
        self.items = list(found)
        for n, it in enumerate(self.items):
            row = Gtk.ListBoxRow(); row.add(self.make_row(it, n)); self.list.add(row)
        if note or (q and not self.items):
            lab = Gtk.Label(label=note or "No results", xalign=0); lab.get_style_context().add_class("empty")
            r = Gtk.ListBoxRow(); r.set_selectable(False); r.set_activatable(False); r.add(lab); self.list.add(r)
        self._showall()
        vis = bool(self.items or q or note)
        self.sep.set_visible(vis); self.list.set_visible(vis); self.home.set_visible(False)
        self.footer.set_visible(bool(self.items) and self.mode == "list")
        if self.items:
            self.list.select_row(self.list.get_row_at_index(0))
        self.set_width(860)

    def start_deep(self, q):
        import threading
        self.deep_q = q
        self.render([], q, f"Searching inside documents for “{q}”…")
        def work():
            res = deep_items(q)
            GLib.idle_add(self.show_deep, q, res)
        threading.Thread(target=work, daemon=True).start()

    def show_deep(self, q, res):
        if self.deep_q == q:                               # I may have typed on since
            self.render(res, q, None if res else f"Nothing inside your documents matches “{q}”")
        return False

    def make_row(self, it, n):
        h = Gtk.Box(spacing=16)
        if isinstance(it.icon, str) and it.icon:
            img = Gtk.Image.new_from_icon_name(it.icon, Gtk.IconSize.DND)
        elif it.icon is not None and it.icon.__class__.__name__ == "Pixbuf":
            img = Gtk.Image.new_from_pixbuf(it.icon)
        elif it.icon:
            img = Gtk.Image.new_from_gicon(it.icon, Gtk.IconSize.DND)
        else:
            img = Gtk.Image.new_from_icon_name("application-x-executable", Gtk.IconSize.DND)
        if it.icon is None or it.icon.__class__.__name__ != "Pixbuf": img.set_pixel_size(40)
        h.pack_start(img, False, False, 0)
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL); v.set_valign(Gtk.Align.CENTER)
        t = Gtk.Label(label=it.title, xalign=0); t.set_ellipsize(Pango.EllipsizeMode.END); t.set_max_width_chars(52)
        t.get_style_context().add_class("t")
        if it.kind == "Calc": t.get_style_context().add_class("big")
        v.pack_start(t, False, False, 0)
        if it.sub:
            s = Gtk.Label(label=it.sub, xalign=0); s.set_ellipsize(Pango.EllipsizeMode.MIDDLE); s.set_max_width_chars(80); s.get_style_context().add_class("s")
            v.pack_start(s, False, False, 0)
        h.pack_start(v, True, True, 0)
        if n < 9:
            nn = Gtk.Label(label=f"Alt+{n+1}", xalign=1); nn.get_style_context().add_class("n"); h.pack_end(nn, False, False, 6)
        return h

    def move(self, d):
        if self.mode in ("home", "all"):
            if self.home_items:
                self.home_sel = (self.home_sel + d) % len(self.home_items); self.paint_home()
            return
        if not self.items:
            return
        cur = self.list.get_selected_row(); i = cur.get_index() if cur else -1
        self.list.select_row(self.list.get_row_at_index((i + d) % len(self.items)))

    def vmove(self, d):                                    # up/down in the home view: grid row <-> file list
        if not self.home_items: return
        napps = sum(1 for it in self.home_items if it.kind == "App")
        if d > 0 and self.home_sel < napps and len(self.home_items) > napps: self.home_sel = napps
        elif d > 0: self.home_sel = min(len(self.home_items) - 1, self.home_sel + 1) if self.home_sel >= napps else self.home_sel
        elif d < 0 and self.home_sel == napps and napps: self.home_sel = 0
        elif d < 0 and self.home_sel > napps: self.home_sel -= 1
        self.paint_home()

    def run_item(self, it, mode):
        if it.kind == "Deep":                              # ask-first deep search: stay open and show the matches
            self.start_deep(it.key[5:]); return
        fn = it.run
        if mode == 1 and it.reveal: fn = it.reveal
        if mode == 2 and it.alt:                           # one copy key for everything: a file copies as the file itself, anything else as its text
            fp = self.item_path(it) if it.kind in ("File", "Folder", "Recent", "Match") else None
            if fp: fn = lambda: copy_file(fp)
            else: txt = it.alt(); fn = lambda: copy(txt)
        if not fn:
            return
        if it.key and not it.key.startswith("deep:"): bump(it.key)
        fn()
        self.dismiss()

    def activate(self, i, mode):
        if 0 <= i < len(self.items):
            self.run_item(self.items[i], mode)

    # ---- hold Space = peek at the selected file (like macOS Quick Look); a quick tap still types a space
    def previewable_selection(self):
        if self.mode != "list": return False
        row = self.list.get_selected_row()
        if not row or not (0 <= row.get_index() < len(self.items)): return False
        return self.item_path(self.items[row.get_index()]) is not None

    def insert_space(self):
        t = self.entry.get_text(); p = self.entry.get_position()
        self.entry.set_text(t[:p] + " " + t[p:]); self.entry.set_position(p + 1)

    def space_fire(self):
        self.space_timer = 0
        if self.space_down and self.previewable_selection():
            self.space_peek = True; self.show_preview()
        elif self.space_down:                              # nothing to preview: it was just a slow space
            self.space_down = False; self.insert_space()
        return False

    def flush_space(self):                                 # another key arrived while Space was pending: type the space first, keep order
        if self.space_timer: GLib.source_remove(self.space_timer); self.space_timer = 0
        if self.space_down and not self.space_peek: self.insert_space()
        self.space_down = False

    def on_key_release(self, w, e):
        if e.keyval == Gdk.KEY_space and self.space_down:
            if self.space_timer: GLib.source_remove(self.space_timer); self.space_timer = 0
            if self.space_peek and self.mode == "preview": self.leave_preview()
            else: self.insert_space()                      # released before the threshold: an ordinary space
            self.space_down = False; self.space_peek = False
            return True
        return False

    def on_key(self, w, e):
        k = e.keyval; st = e.state & Gtk.accelerator_get_default_mod_mask()
        ctrl = bool(st & Gdk.ModifierType.CONTROL_MASK); alt = bool(st & Gdk.ModifierType.MOD1_MASK); shift = bool(st & Gdk.ModifierType.SHIFT_MASK)
        if k == Gdk.KEY_space and not (ctrl or alt):
            if self.space_down: return True                # autorepeat while held
            if self.mode == "list" and self.previewable_selection():
                self.space_down = True; self.space_peek = False
                self.space_timer = GLib.timeout_add(380, self.space_fire); return True
        elif self.space_down and not self.space_peek:
            self.flush_space()
        home = self.mode in ("home", "all")
        if self.mode == "preview":
            if k == Gdk.KEY_Escape or (ctrl and k == Gdk.KEY_y):
                self.leave_preview(); return True
            if k in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                spawn(["xdg-open", self.pv_path]); self.dismiss(); return True
            adj = self.pv_scroll.get_vadjustment(); stp = {Gdk.KEY_Down: 60, Gdk.KEY_Up: -60, Gdk.KEY_Page_Down: 420, Gdk.KEY_Page_Up: -420}.get(k)
            if stp: adj.set_value(max(0, min(adj.get_upper() - adj.get_page_size(), adj.get_value() + stp))); return True
        if ctrl and k == Gdk.KEY_y and self.mode == "list":
            self.show_preview(); return True
        if k == Gdk.KEY_Escape or (ctrl and k in (Gdk.KEY_bracketleft, Gdk.KEY_c)):
            if self.mode == "all": self.show_home(); return True      # first Esc goes back to the home view
            self.dismiss(); return True
        if alt and k == Gdk.KEY_a and self.mode != "all":
            self.show_all_apps(); return True
        if self.mode == "all" and k in (Gdk.KEY_Down, Gdk.KEY_Up) or (self.mode == "all" and ctrl and k in (Gdk.KEY_j, Gdk.KEY_k, Gdk.KEY_n, Gdk.KEY_p)):
            step = 4 if k in (Gdk.KEY_Down, Gdk.KEY_j, Gdk.KEY_n) else -4
            self.home_sel = max(0, min(len(self.home_items) - 1, self.home_sel + step)); self.paint_home(); return True
        if home and (k in (Gdk.KEY_Right,) or (ctrl and k == Gdk.KEY_l)):
            self.move(1); return True
        if home and (k in (Gdk.KEY_Left,) or (ctrl and k == Gdk.KEY_h)):
            self.move(-1); return True
        if k in (Gdk.KEY_Down,) or (ctrl and k in (Gdk.KEY_j, Gdk.KEY_n)):
            (self.vmove(1) if self.mode == "home" else self.move(1)); return True
        if k in (Gdk.KEY_Up,) or (ctrl and k in (Gdk.KEY_k, Gdk.KEY_p)):
            (self.vmove(-1) if self.mode == "home" else self.move(-1)); return True
        if k == Gdk.KEY_Tab:
            self.move(1); return True
        if k == Gdk.KEY_ISO_Left_Tab:
            self.move(-1); return True
        if k in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            m = 2 if alt else 1 if (ctrl or shift) else 0
            if home: self.activate_home(self.home_sel, m)
            else:
                row = self.list.get_selected_row(); self.activate(row.get_index() if row else 0, m)
            return True
        if alt and Gdk.KEY_1 <= k <= Gdk.KEY_9 and not home:
            self.activate(k - Gdk.KEY_1, 0); return True
        if ctrl and not shift and k == Gdk.KEY_c and not self.entry.get_selection_bounds():   # Ctrl+C copies the highlighted result (text in the box still copies as usual)
            if home: self.activate_home(self.home_sel, 2)
            else:
                row = self.list.get_selected_row()
                if row: self.activate(row.get_index(), 2)
            return True
        if ctrl and k == Gdk.KEY_u:
            self.entry.set_text(""); return True
        return False

def daemon(first=None):
    """Resident mode: Spotlight is built once and shown on request (~instant); commands arrive on a FIFO. (Settings is NOT kept in memory.)"""
    import signal
    RT = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")); fifo = RT / "rog-ui.fifo"; pidf = RT / "rog-ui.pid"
    try:                                                   # one resident instance only: hand the command to the running one instead
        old = int(pidf.read_text())
        if old != os.getpid() and b"spotlight.py" in Path(f"/proc/{old}/cmdline").read_bytes():
            if first:
                fd0 = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK); os.write(fd0, (first + "\n").encode()); os.close(fd0)
            return
    except Exception:
        pass
    load_apps()
    Gio.AppInfoMonitor.get().connect("changed", lambda m: load_apps())     # new/removed apps show up without a restart
    spot = Spot(daemon=True)
    cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    cb.connect("owner-change", lambda c, ev: clip_changed(c))
    clip_changed(cb)
    def handle(cmd):
        if cmd == "clip":
            spot.present_fresh(); spot.entry.set_text("c "); spot.entry.set_position(-1)
        elif cmd == "apps":
            spot.present_fresh(); spot.show_all_apps()
        elif cmd == "spotlight":
            if spot.get_visible(): spot.dismiss()
            else: spot.present_fresh()
        elif cmd == "quit": Gtk.main_quit()
    try: os.mkfifo(fifo, 0o600)
    except FileExistsError: pass
    fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    def on_data(fd_, cond):
        try: data = os.read(fd_, 4096).decode()
        except BlockingIOError: return True
        for c in data.split():
            handle(c)
        return True
    GLib.io_add_watch(fd, GLib.IO_IN, on_data)
    pidf.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *a: Gtk.main_quit())
    if first: GLib.idle_add(lambda: (handle(first), False)[1])
    Gtk.main()
    try:
        if pidf.read_text().strip() == str(os.getpid()): pidf.unlink()   # never delete a newer instance's files
    except Exception: pass

def main():
    if "--search" in sys.argv:                       # headless: print what the box would show (used for testing)
        load_apps()
        q = " ".join(a for a in sys.argv[1:] if a != "--search")
        for it in search(q):
            print(f"{it.score:7.1f}  {it.kind:7} {it.title}  |  {it.sub}")
        return
    if "--import-greenclip" in sys.argv:
        import_greenclip(); return
    if "--daemon" in sys.argv:
        rest = [a for a in sys.argv[1:] if a != "--daemon"]
        daemon(rest[0] if rest else None); return
    load_apps()
    w = Spot(); w.show(); w.present(); w.entry.grab_focus()
    Gtk.main()

if __name__ == "__main__":
    main()
