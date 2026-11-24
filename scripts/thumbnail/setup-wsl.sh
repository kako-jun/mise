#!/usr/bin/env bash
# Run inside the collector machine's Arch WSL as the same user that runs the Windows task.
set -euo pipefail
for program in bwrap systemd-run node npm uv; do
  command -v "$program" >/dev/null || { printf 'missing: %s\n' "$program" >&2; exit 1; }
done
install_dir="$HOME/.local/share/mise-media"
mkdir -p "$install_dir"
npm install --prefix "$install_dir" --no-save --no-audit playwright@1.56.1
PLAYWRIGHT_BROWSERS_PATH="$install_dir/browsers" node "$install_dir/node_modules/playwright/cli.js" install --only-shell chromium
revision=$(node -e 'const p=require(process.argv[1]);process.stdout.write(p.browsers.find(x => x.name === "chromium-headless-shell").revision)' "$install_dir/node_modules/playwright-core/browsers.json")
browser=$(find "$install_dir/browsers/chromium_headless_shell-$revision" -type f \( -name chrome-headless-shell -o -name headless_shell \) | head -n 1)
if [[ -z "$browser" || ! -x "$browser" ]]; then
  printf 'Chromium headless shell missing\n' >&2
  exit 1
fi
ln -sfn "$browser" "$install_dir/chromium"
printf 'PLAYWRIGHT_DIR=%s\nCHROMIUM_PATH=%s\n' "$install_dir/node_modules/playwright" "$browser"
