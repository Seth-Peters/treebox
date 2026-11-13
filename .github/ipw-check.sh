#!/bin/bash
# Temporary proof script (removed before merge). Runs inside the bundled
# sandbox image as the agent user and drives Chromium in the three supported
# ways: playwright-cli, the playwright command, and the playwright library.
# Screenshots go to /out. Exit status is the number of failed uses.
set -uo pipefail

out=${OUT_DIR:-/out}
mkdir -p "$out" /tmp/site /tmp/ws
cat > /tmp/site/index.html <<'H'
<!doctype html><html><head><meta charset="utf-8"><title>treebox ipw check</title><style>body{font-family:sans-serif;background:#f4f1ea;padding:40px} h1{color:#2a6f4e} .box{width:200px;height:80px;background:linear-gradient(90deg,#e4572e,#29335c)}</style></head>
<body><h1>Chromium in a treebox sandbox</h1><p id="msg">Emoji and CJK: 🌳 树 箱</p><div class="box"></div></body></html>
H
(cd /tmp/site && exec python -m http.server 8765 --bind 127.0.0.1 >/dev/null 2>&1) &
sleep 1

echo "PLAYWRIGHT_MCP_SANDBOX=${PLAYWRIGHT_MCP_SANDBOX-<unset>}"
failed=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; failed=$((failed + 1)); }

echo "== 1. playwright-cli (open, eval, screenshot)"
cd /tmp/ws && git init -q . && playwright-cli install --skills >/dev/null 2>&1 || echo "playwright-cli install --skills failed"
timeout 90 playwright-cli open http://127.0.0.1:8765/ 2>&1 | head -20
msg=$(timeout 30 playwright-cli eval "document.querySelector('#msg').textContent" 2>&1)
echo "$msg" | head -5
browser=$(pgrep -a -f chrome-headless-shell | grep -v -e '--type=' | head -1)
if [[ $browser == *--no-sandbox* ]]; then echo "browser process has --no-sandbox"; else echo "browser process has no --no-sandbox: ${browser:0:160}"; fi
timeout 30 playwright-cli screenshot --filename="$out/playwright-cli.png" 2>&1 | tail -2
timeout 30 playwright-cli close-all >/dev/null 2>&1
if [[ $msg == *"Emoji and CJK"* && -s $out/playwright-cli.png ]]; then pass playwright-cli; else fail playwright-cli; fi

echo "== 2. playwright screenshot"
if timeout 90 playwright screenshot http://127.0.0.1:8765/ "$out/playwright-command.png" 2>&1 | tail -20 && [[ -s $out/playwright-command.png ]]; then
  pass "playwright screenshot"
else
  fail "playwright screenshot"
fi

echo "== 3. playwright library script"
cat > /tmp/ws/script.js <<'JS'
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('http://127.0.0.1:8765/');
  console.log('text:', await page.textContent('#msg'));
  await page.screenshot({ path: process.argv[2] });
  await browser.close();
})().catch((e) => { console.error(e.message.split('\n').slice(0, 20).join('\n')); process.exit(1); });
JS
if NODE_PATH="$(npm root -g)" timeout 90 node /tmp/ws/script.js "$out/playwright-library.png" 2>&1 && [[ -s $out/playwright-library.png ]]; then
  pass "playwright library"
else
  fail "playwright library"
fi

ls -la "$out"
echo "failed uses: $failed"
exit "$failed"
