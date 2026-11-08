# Spotlight for i3

A macOS-style launcher for X11 window managers (written for i3 on Arch). One search box for apps, open windows, files, maths and unit conversion, clipboard history and system actions. It is a small resident GTK3 process, so it opens in about 20 ms.

## What it does
| Type | Result |
|---|---|
| a name | apps (recents first), open windows, files and folders (`plocate`), bookmarks (Chrome, Firefox/Zen, qutebrowser) |
| `15% of 80`, `5 km in mi`, `20 c in f`, `=2+2` | calculator and unit converter (`qalc`) |
| `c word` | clipboard history: text and images, searchable (recorded by the same process, no polling daemon) |
| `kind:pdf in:thesis modified:week` | file filters |
| `:smile` | emoji |
| `timer 25m`, `remind in 10m call back`, `remind 15:30 stand up` | timers and reminders that ring in a window (`alarm.py`) |
| `/ phrase` | deep full-text search with `ripgrep` (PDFs through `pdftotext`, cached) |
| `> cmd`, `w name` | run a command, windows only |
| `lock`, `restart`, `shut down`, `log out`, `hibernate` ... | system actions |

Keys: **Enter** run, **Ctrl+Enter** reveal in the file manager, **Ctrl+C** (or **Alt+Enter**) copy (a file copies as the file, an image as the picture), **Ctrl+J/K** or arrows move, **Alt+1..9** pick a row, hold **Space** (or **Ctrl+Y**) to preview, **Alt+A** browse all apps, **Esc** close.

Light, dark or night-schedule look, with a configurable accent colour: set `APPEARANCE`, `ACCENT_MODE`, `ACCENT_HEX`, `SPOT_OPACITY` in `~/.config/rog/settings.env` (`KEY="value"` lines). Your own shortcuts go in `~/.config/spotlight/aliases` (`name = folder | file | URL | command`).

## Power menu
`power-menu.py` (Super+Escape): Lock, Log out, Sleep, Hibernate, Restart, Shut down as one row of tiles. `h`/`l` or arrows move, `1`-`6` pick, Enter runs; destructive actions need a second Enter.

## Install
```
./install.sh        # copies the scripts to ~/.config/scripts and prints the i3 lines to add
```
Needs: `python-gobject gtk3 libqalculate plocate xclip`; optional `ripgrep poppler` (deep search), `imagemagick` (previews), a Papirus or Adwaita icon theme.

## Honest notes
- Some actions call scripts of the author's own setup (performance modes, night tint, backups, update/rollback). Without those scripts the rows are simply missing or do nothing; everything above works on its own.
- Run it with `rog-ui.sh start`; `rog-ui.sh restart` after editing the script. Start it from your desktop session, not over ssh: it passes its environment to everything it launches.
- No licence has been chosen yet.
