#!/usr/bin/env python3
"""Cattura di una pagina a schermo intero via CDP."""
import base64
import json
import time

from cdp import Chromium, WSError

# Selettori di banner cookie/consenso piu' diffusi. Non risolve il problema in
# generale — e' dichiarato nei limiti noti — ma toglie i casi piu' comuni.
HIDE_CSS = """
[id*="onetrust"], [class*="onetrust"], #onetrust-consent-sdk,
[id*="cookie" i][class*="banner" i], [class*="cookie-consent" i],
[class*="cookie-notice" i], [id*="cookie-law" i], [class*="gdpr" i],
[id*="didomi"], [class*="didomi"], #usercentrics-root,
[class*="consent" i][class*="modal" i], [aria-label*="cookie" i],
.qc-cmp2-container, #CybotCookiebotDialog, .cmp-container,
[class*="paywall" i][class*="overlay" i] { display: none !important; }
html, body { overflow: visible !important; }
"""


def capture(url: str, png_path: str, user_agent: str = "",
            width: int = 1280, settle: float = 2.5,
            max_height: int = 30000, timeout: float = 60.0) -> dict:
    """
    Scatta la pagina intera. Ritorna metadati (dimensioni, url finale, stato).
    Solleva WSError/RuntimeError in caso di fallimento.
    """
    br = Chromium(user_agent=user_agent, width=width, timeout=timeout)
    try:
        br.call("Page.enable")
        br.call("Network.enable")
        br.call("Runtime.enable")

        # Lo stato HTTP arriva come evento; lo raccogliamo dopo il navigate.
        br.call("Page.navigate", url=url)

        # Attesa pragmatica: il CDP offre eventi di load ma su pagine con
        # risorse lente non arrivano mai. Meglio un'attesa limitata e certa.
        time.sleep(settle)

        # Nasconde i banner prima di misurare l'altezza: se resta un overlay
        # a tutto schermo, l'altezza misurata sarebbe quella del banner.
        br.call("Runtime.evaluate", expression=f"""
            (function() {{
              const s = document.createElement('style');
              s.textContent = {json.dumps(HIDE_CSS)};
              document.documentElement.appendChild(s);
              // porta a fondo pagina per innescare il caricamento pigro
              window.scrollTo(0, document.body.scrollHeight);
              return true;
            }})()
        """)
        time.sleep(1.2)
        br.call("Runtime.evaluate", expression="window.scrollTo(0,0)")

        metrics = br.call("Page.getLayoutMetrics")
        css = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
        w = int(css.get("width") or width)
        h = int(css.get("height") or 0)
        if h <= 0:
            raise RuntimeError("altezza della pagina non determinabile")
        clipped = h > max_height
        h = min(h, max_height)

        final_url = br.call(
            "Runtime.evaluate", expression="document.location.href",
            returnByValue=True).get("result", {}).get("value", url)
        title = br.call(
            "Runtime.evaluate", expression="document.title",
            returnByValue=True).get("result", {}).get("value", "")

        shot = br.call(
            "Page.captureScreenshot",
            format="png",
            captureBeyondViewport=True,   # <- la ragione per cui serve il CDP
            clip={"x": 0, "y": 0, "width": w, "height": h, "scale": 1},
        )
        data = base64.b64decode(shot["data"])
        with open(png_path, "wb") as f:
            f.write(data)
        import os
        os.chmod(png_path, 0o644)   # mkstemp docet: mai lasciare 0600

        return {
            "width": w, "height": h, "bytes": len(data),
            "final_url": final_url, "title": title, "clipped": clipped,
        }
    finally:
        br.close()


if __name__ == "__main__":
    import sys
    r = capture(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "out.png")
    print(json.dumps(r, ensure_ascii=False, indent=2))
