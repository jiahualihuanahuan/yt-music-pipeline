#!/usr/bin/env bash
# Install Deno permanently and make sure it is on PATH for future shells.
# Safe to re-run. Run once after a fresh machine or if Deno disappears.

set -euo pipefail

echo "=== Deno permanent setup ==="

# 1. Install if missing
if ! command -v deno >/dev/null 2>&1 && [[ ! -x "$HOME/.deno/bin/deno" ]]; then
  echo "Installing Deno..."
  curl -fsSL https://deno.land/install.sh | sh
else
  echo "Deno already present."
fi

# 2. Ensure ~/.deno/bin is on PATH in bashrc and profile
DENO_BIN="$HOME/.deno/bin"
LINE="export PATH=\"$DENO_BIN:\$PATH\""

for rc in "$HOME/.bashrc" "$HOME/.profile" "$HOME/.bash_profile"; do
  if [[ -f "$rc" ]] || [[ "$rc" == "$HOME/.bashrc" ]]; then
    touch "$rc"
    if ! grep -q ".deno/bin" "$rc"; then
      echo "$LINE" >> "$rc"
      echo "Added Deno to $rc"
    fi
  fi
done

# 3. Also drop a symlink into /usr/local/bin if we can (survives user PATH resets)
if [[ -x "$HOME/.deno/bin/deno" ]]; then
  if [[ -w /usr/local/bin ]]; then
    ln -sf "$HOME/.deno/bin/deno" /usr/local/bin/deno
    echo "Symlinked to /usr/local/bin/deno"
  else
    echo "(skip symlink: no write permission to /usr/local/bin)"
  fi
fi

echo ""
echo "Done. Current Deno:"
"$HOME/.deno/bin/deno" --version || true
echo ""
echo "Re-open the terminal or run: source ~/.bashrc"
