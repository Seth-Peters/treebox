/* treebox docs enhancements — self-contained, no external requests.
 *
 * 1. Console code blocks: the Material copy button copies only the
 *    `$ `-prefixed commands, never the output printed under them.
 * 2. ✓ / ✗ marks in console output get their CLI colors.
 * 3. A "Copy page" button at the top of every page copies that page's raw
 *    Markdown source (embedded by hooks/copy_page.py) — an agent-ready copy,
 *    entirely offline.
 */
(function () {
  // Copy icon (default) and check icon (shown green after a successful copy).
  var COPY_SVG =
    '<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">' +
    '<path fill="currentColor" d="M19 21H8V7h11m0-2H8a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2m-3-4H4a2 2 0 0 0-2 2v14h2V3h12z"/></svg>';
  var CHECK_SVG =
    '<svg class="tx-copy-page__check" viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">' +
    '<path fill="currentColor" d="M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>';

  // base64 (ASCII) → original UTF-8 string.
  function decodeUtf8Base64(b64) {
    return decodeURIComponent(
      Array.prototype.map
        .call(atob(b64), function (c) {
          return "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2);
        })
        .join("")
    );
  }

  function copyPageButton() {
    var carrier = document.querySelector("script.tx-page-md");
    var article = document.querySelector(".md-content__inner");
    if (!carrier || !article || article.querySelector(".tx-copy-page")) return;

    var markdown;
    try {
      markdown = decodeUtf8Base64(carrier.textContent.trim());
    } catch (e) {
      return; // malformed payload — leave the page untouched
    }
    var url = carrier.getAttribute("data-url") || "";
    var payload = url ? "> Source: " + url + "\n\n" + markdown : markdown;

    var bar = document.createElement("div");
    bar.className = "tx-copy-page-bar";
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tx-copy-page";
    btn.setAttribute("aria-label", "Copy this page as Markdown");
    // Both labels are always in the DOM, overlapping in one grid cell, so the
    // pill is permanently sized to the wider ("Copy page") — the copied state
    // only flips visibility, never reflows. The icon swaps the same way.
    btn.innerHTML =
      COPY_SVG +
      CHECK_SVG +
      '<span class="tx-copy-page__label">' +
      '<span class="tx-copy-page__word tx-copy-page__word--copy">Copy page</span>' +
      '<span class="tx-copy-page__word tx-copy-page__word--done">Copied</span>' +
      "</span>";

    btn.addEventListener("click", function () {
      navigator.clipboard.writeText(payload).then(function () {
        btn.classList.add("is-copied");
        clearTimeout(btn._reset);
        btn._reset = setTimeout(function () {
          btn.classList.remove("is-copied");
        }, 2000);
      });
    });

    bar.appendChild(btn);
    article.insertBefore(bar, article.firstChild);
  }

  function enhance() {
    copyPageButton();

    document.querySelectorAll("button[data-clipboard-target]").forEach(function (btn) {
      var sel = btn.getAttribute("data-clipboard-target");
      if (!sel) return;
      var code = document.querySelector(sel);
      if (!code || !code.querySelector(".gp")) return;
      var cmds = code.innerText
        .split("\n")
        .filter(function (l) {
          return l.startsWith("$ ");
        })
        .map(function (l) {
          return l.slice(2);
        })
        .join("\n");
      if (cmds) btn.setAttribute("data-clipboard-text", cmds);
    });

    document.querySelectorAll(".highlight .go").forEach(function (sp) {
      if (sp.dataset.tx) return;
      sp.dataset.tx = "1";
      sp.innerHTML = sp.innerHTML
        .replace(/✓/g, '<span class="c-ok">✓</span>')
        .replace(/✗/g, '<span class="c-err">✗</span>');
    });
  }

  function run() {
    enhance();
    // second pass: Material injects copy buttons on the same tick we run in
    setTimeout(enhance, 300);
  }

  if (typeof document$ !== "undefined") {
    document$.subscribe(run);
  } else {
    document.addEventListener("DOMContentLoaded", run);
  }
})();
