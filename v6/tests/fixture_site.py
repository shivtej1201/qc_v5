"""Deterministic fixture site for V6 tests: three templates, known problems, and an Akamai-style challenge host.

Templates
  /product/<n>   no <h1>; cookie banner that pushes content down (layout shift); a button with no accessible name;
                 links to /gone (404)
  /article/<n>   clean page with an <h1>, lazy images
  /city/<n>      JavaScript shell: <h1> and links are added by script; throws a console error
Other
  /challenge/... 403 with Akamai markers
  /robots.txt, /gone (404)
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title><meta name="description" content="Fixture page used by the QC V6 test suite for real visits.">
<style>body{{font:16px sans-serif;margin:0}} header,footer{{padding:12px;background:#eee}} main{{padding:16px}}
.tile{{display:inline-block;width:140px;height:100px;margin:6px;background:#ddd}} .tall{{height:2600px}}</style></head><body>
<header class="top"><nav class="menu"><a href="/">Home</a> <a href="/article/1">Articles</a> <a href="/gone">Old page</a></nav></header>"""
FOOT = """<footer class="site-foot"><p>Footer</p></footer></body></html>"""


def product(n: int) -> str:
    banner = """<script>setTimeout(function(){var b=document.createElement('div');b.id='cookie';
      b.style.cssText='height:180px;background:#333;color:#fff;padding:16px';
      b.innerHTML='We use cookies. <button id=acc>Accept all</button>';
      document.body.insertBefore(b, document.body.firstChild);
      document.getElementById('acc').onclick=function(){b.remove();};}, 600);</script>"""
    tiles = "".join(f'<div class="tile"><a href="/product/{(n + k) % 30}">Similar {k}</a></div>' for k in range(6))
    return (HEAD.format(title=f"Cast iron pan {n} — Fixture Store") +
            f'<main class="pdp"><div class="gallery"><img src="/img/{n}.png" width="300" height="200" alt="Pan {n}"></div>'
            f'<div class="buy"><span class="price">Rs {1000 + n}</span><button class="add"><svg width="16" height="16"></svg></button></div>'
            f'<section class="specs"><p>Seasoned iron.</p>{tiles}</section><div class="tall"></div></main>{banner}' + FOOT)


def article(n: int) -> str:
    paras = "".join(f"<p>Paragraph {k} of article {n}.</p>" for k in range(8))
    imgs = "".join(f'<img loading="lazy" src="/img/a{n}{k}.png" width="200" height="120" alt="Figure {k}">' for k in range(3))
    return (HEAD.format(title=f"How EMIs work, part {n}") +
            f'<main class="post"><article class="story"><h1>How EMIs work, part {n}</h1>{paras}<div class="tall"></div>{imgs}</article></main>' + FOOT)


def city(n: int) -> str:
    js = f"""<script>document.addEventListener('DOMContentLoaded', function(){{
      var m=document.getElementById('app'); m.innerHTML='<h1>Personal loan in City {n}</h1>' +
      Array.from({{length: 8}}, function(_, k){{ return '<a href="/city/'+k+'">City '+k+'</a>'; }}).join(' ');
      window.offers.city;  /* throws: offers is undefined */ }});</script>"""
    return HEAD.format(title=f"Personal loan in City {n}") + f'<main class="app-shell"><div id="app"></div></main>{js}' + FOOT


def apply_page(broken: bool) -> str:
    handler = ("function(){ var d = window.form.city; }" if broken else
               "function(){ document.getElementById('result').textContent = 'You are eligible for up to Rs 8,00,000'; "
               "document.getElementById('result').hidden = false; }")
    return (HEAD.format(title="Check loan eligibility") +
            f"""<main><h1>Check your loan eligibility</h1>
            <label for="inc">Monthly income</label> <input id="inc" name="inc" inputmode="numeric">
            <label for="city">City</label> <select id="city"><option>Mumbai</option><option>Pune</option></select>
            <button id="go" type="button">Continue</button>
            <p id="result" role="status" hidden></p>
            <button type="button">Submit application</button></main>
            <script>document.getElementById('go').addEventListener('click', {handler});</script>""" + FOOT)


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits: dict = {}

    def do_GET(self):
        p = self.path.split("?")[0]
        Handler.hits[p] = Handler.hits.get(p, 0) + 1
        Handler.hits["__ua__"] = self.headers.get("User-Agent", "")
        Handler.hits["__token__"] = self.headers.get("X-QC-Audit", "")
        host = self.headers.get("Host", "127.0.0.1")
        if p == "/robots.txt":
            return self._send(200, f"User-agent: *\nAllow: /\nSitemap: http://{host}/sitemap.xml\n", "text/plain")
        if p == "/sitemap.xml":
            locs = "".join(f"<url><loc>http://{host}/{t}/{i}</loc></url>" for t in ("product", "article", "city") for i in range(30))
            return self._send(200, f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>',
                              "application/xml")
        if p.startswith("/img/"):
            return self._send(200, PNG, "image/png")
        if p.startswith("/challenge"):
            return self._send(403, "<html><title>Access Denied</title><body>Access Denied. Reference #18.abc</body></html>",
                              "text/html", {"Server": "AkamaiGHost", "Set-Cookie": "_abck=xyz; Path=/"})
        parts = [x for x in p.split("/") if x]
        if len(parts) == 2 and parts[1].isdigit():
            fn = {"product": product, "article": article, "city": city}.get(parts[0])
            if fn:
                return self._send(200, fn(int(parts[1])), "text/html; charset=utf-8")
        if p == "/":
            return self._send(200, article(0), "text/html; charset=utf-8")
        if p in ("/apply", "/apply-broken"):
            return self._send(200, apply_page(p == "/apply-broken"), "text/html; charset=utf-8")
        return self._send(404, "<html><title>Not found</title><body>Not found</body></html>", "text/html")

    def _send(self, code, body, ctype, extra=None):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


class FixtureSite:
    def __init__(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.srv.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def urls(self, per_template: int = 6):
        return [f"{self.base}/{t}/{i}" for t in ("product", "article", "city") for i in range(per_template)]

    def close(self):
        self.srv.shutdown()
