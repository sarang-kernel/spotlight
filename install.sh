#!/usr/bin/env bash
# Installs Spotlight into ~/.config/scripts (existing copies are saved as *.old) and prints the i3 lines to add.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd); d=$HOME/.config/scripts; mkdir -p "$d"
for f in spotlight.py rog-ui.sh alarm.py rg-pre.sh power-menu.py; do
	[[ -e $d/$f ]] && ! cmp -s "$here/$f" "$d/$f" && cp -a "$d/$f" "$d/$f.old"
	install -m 755 "$here/$f" "$d/$f"
done
cat <<EOT
Installed to $d. Add to ~/.config/i3/config and reload i3:
  exec_always --no-startup-id ~/.config/scripts/rog-ui.sh start
  bindsym \$mod+d exec --no-startup-id ~/.config/scripts/rog-ui.sh spotlight
  bindsym \$mod+Shift+v exec --no-startup-id ~/.config/scripts/rog-ui.sh clip
  bindsym \$mod+Escape exec --no-startup-id python3 ~/.config/scripts/power-menu.py
  for_window [class="^Spotlight\$"] floating enable, border none, move position center, move up 260px
  for_window [class="^RogPower\$"] floating enable, border none, move position center
  for_window [class="^RogAlarm\$"] floating enable, border pixel 3, move position center
EOT
