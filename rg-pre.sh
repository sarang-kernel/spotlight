#!/usr/bin/env bash
# rog-tune: ripgrep --pre helper for Spotlight's deep search: PDF text is extracted once and cached (by path + mtime)
f=$1; C=${XDG_CACHE_HOME:-$HOME/.cache}/spotlight/pdf; mkdir -p "$C"
k=$(printf '%s' "$f$(stat -c %Y "$f")" | md5sum | cut -d' ' -f1)
[[ -s $C/$k ]] || pdftotext -q -layout "$f" "$C/$k.tmp" 2>/dev/null && mv -f "$C/$k.tmp" "$C/$k" 2>/dev/null
cat "$C/$k" 2>/dev/null
exit 0
