// Favicon + tab title injector for the CMS Open Payments Data Analyst.
//
// Chainlit 2.x doesn't expose a `favicon` field in config.toml, so we
// inject a <link rel="icon"> pointing at our square avatar PNG. Also
// pins document.title in case Chainlit's default overrides UI.name.
//
// Wired up via `custom_js = "/public/favicon-inject.js"` in .chainlit/config.toml.
(function () {
  function apply() {
    // Remove any existing icon/shortcut-icon links, then add ours. This
    // handles Chainlit's own favicon (if present) and any stale cached
    // link tags the SPA may have mounted on a previous route.
    document
      .querySelectorAll("link[rel~='icon'], link[rel='shortcut icon']")
      .forEach(function (el) {
        el.parentNode && el.parentNode.removeChild(el);
      });

    var link = document.createElement("link");
    link.rel = "icon";
    link.type = "image/png";
    link.href = "/public/openpayments-avatar.png";
    document.head.appendChild(link);

    document.title = "CMS Open Payments Data Analyst";
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", apply);
  } else {
    apply();
  }
})();
