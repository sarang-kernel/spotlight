# Spotlight for i3

I missed macOS's Spotlight, so I built one for i3. One search box for apps, open windows, files, maths and unit conversion, clipboard history and system actions. It's a small resident GTK3 process, so it pops up in about 20 ms, which is faster than I can change my mind about what I wanted.

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

It comes in light, dark or follow-the-clock flavours, with an accent colour of my choosing: `APPEARANCE`, `ACCENT_MODE`, `ACCENT_HEX` and `SPOT_OPACITY` live in `~/.config/rog/settings.env` (plain `KEY="value"` lines). My own shortcuts go in `~/.config/spotlight/aliases` (`name = folder | file | URL | command`).

## The power menu
I wanted pretty shutdown buttons, so `power-menu.py` (Super+Escape) shows Lock, Log out, Sleep, Hibernate, Restart and Shut down as one row of tiles. `h`/`l` or arrows move, `1`-`6` pick, Enter runs. The destructive ones need a second Enter, so I can't nuke my session by accident.

## Install
```
./install.sh        # copies the scripts to ~/.config/scripts and prints the i3 lines to add
```
Needs: `python-gobject gtk3 libqalculate plocate xclip`. Optional: `ripgrep poppler` (deep search), `imagemagick` (previews), a Papirus or Adwaita icon theme.

## Things I should confess
- Some actions call scripts from my own setup (performance modes, night tint, backups, update/rollback). Without those scripts the rows are simply missing or do nothing; everything above works on its own.
- Start it with `rog-ui.sh start`, and run `rog-ui.sh restart` after editing the script. Start it from the desktop session, not over ssh: it hands its environment to everything it launches, and I learned that the hard way.
- I haven't picked a licence yet.
