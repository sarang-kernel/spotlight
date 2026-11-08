#!/usr/bin/env bash
# rog-tune: client for the resident Spotlight/Settings process (it starts at login, so opening is near-instant).
#   rog-ui.sh spotlight          show Spotlight (toggles); lives in a small resident process
#   rog-ui.sh apps               browse every installed app (also: Alt+A inside Spotlight)
#   rog-ui.sh clip               Spotlight pre-filled with "c ": my clipboard history (the same process records it)
#   rog-ui.sh settings | backup  open Settings / the backup chooser as ordinary windows (nothing stays in memory afterwards)
#   rog-ui.sh start | restart | quit   manage the resident Spotlight process
RT=${XDG_RUNTIME_DIR:-/tmp}; FIFO=$RT/rog-ui.fifo; PIDF=$RT/rog-ui.pid
SCRIPT=$HOME/.config/scripts/spotlight.py
alive() { [[ -r $PIDF ]] && grep -q spotlight.py "/proc/$(<"$PIDF")/cmdline" 2>/dev/null && [[ -p $FIFO ]]; }
launch() { setsid python3 "$SCRIPT" --daemon "$@" >/dev/null 2>&1 </dev/null & }
case ${1:-spotlight} in
start) alive || launch ;;
restart) if [[ -r $PIDF ]]; then old=$(<"$PIDF"); kill "$old" 2>/dev/null; for _ in {1..20}; do [[ -d /proc/$old ]] || break; sleep 0.1; done; fi; launch ;;
quit) [[ -r $PIDF ]] && kill "$(<"$PIDF")" 2>/dev/null ;;
settings) if pgrep -xf "python3 $HOME/.config/scripts/settings.py" >/dev/null; then i3-msg '[class="^RogSettings$"] focus' >/dev/null; else setsid python3 "$HOME/.config/scripts/settings.py" >/dev/null 2>&1 </dev/null & fi ;;
backup) setsid python3 "$HOME/.config/scripts/settings.py" --backup >/dev/null 2>&1 </dev/null & ;;
*) if alive; then printf '%s\n' "$1" >"$FIFO"; else launch "$1"; fi ;;
esac
