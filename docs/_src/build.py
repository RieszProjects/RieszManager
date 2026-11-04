#!/usr/bin/env python3
"""Build the documentation site from the page bodies in this folder.

    python3 docs/_src/build.py

Each page body (zh/<page>.html, en/<page>.html) holds only the article: the
build wraps it in the shared header, the site and page contents (generated
from the <h2>/<h3> ids), the previous/next links and the footer, and writes
docs/<page>.html (Chinese) and docs/en/<page>.html (English). The home page
(index) has its own layout and no sidebar. GitHub Pages serves docs/ as is;
this folder starts with an underscore, so Jekyll leaves it out of the site.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent
OUT = SRC.parent
REPO = "https://github.com/RieszProjects/RieszManager"
FONTS = ("https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600;700"
         "&family=Inter:wght@400;500;600&family=Noto+Sans+SC:wght@400;500;700"
         "&family=Noto+Serif+SC:wght@500;600&family=JetBrains+Mono:wght@400&display=swap")
PAGES = ["index", "quickstart", "guide", "deploy"]

TEXT = {
    "zh": {
        "lang": "zh-CN", "site": "RieszManager 文档", "sub": "文档", "other": "English",
        "names": {"index": "前言", "quickstart": "快速上手", "guide": "使用指南", "deploy": "部署教程"},
        "docs": "文档", "this_page": "本页", "prev": "上一篇", "next": "下一篇", "menu": "目录",
        "foot": f'RieszManager 以 <a href="{REPO}/blob/main/LICENSE">Apache License 2.0</a> 开源。界面截图来自演示数据。',
    },
    "en": {
        "lang": "en", "site": "RieszManager Docs", "sub": "Docs", "other": "中文",
        "names": {"index": "Introduction", "quickstart": "Quickstart", "guide": "User Guide", "deploy": "Deployment"},
        "docs": "Docs", "this_page": "On this page", "prev": "Previous", "next": "Next", "menu": "Menu",
        "foot": f'RieszManager is open source under the <a href="{REPO}/blob/main/LICENSE">Apache License 2.0</a>. Screenshots show demo data.',
    },
}

GH_ICON = ('<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38'
           ' 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72'
           ' 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21'
           ' 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87'
           ' 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.19 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>')


def headings(body: str) -> list[tuple[int, str, str]]:
    found = []
    for m in re.finditer(r'<h([23])\s+id="([^"]+)"[^>]*>(.*?)</h\1>', body, re.S):
        title = re.sub(r"<[^>]+>", "", m.group(3)).strip()
        found.append((int(m.group(1)), m.group(2), title))
    return found


def page_title(body: str, fallback: str) -> str:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else fallback


def build(lang: str, page: str) -> None:
    t = TEXT[lang]
    body = (SRC / lang / f"{page}.html").read_text(encoding="utf-8").strip()
    prefix = "" if lang == "zh" else "../"           # path from the page to docs/
    other = f"en/{page}.html" if lang == "zh" else f"../{page}.html"
    name = t["names"][page]
    title = t["site"] if page == "index" else f"{page_title(body, name)} · {t['site']}"

    active = ' class="active"'
    current = ' class="current"'
    nav = "".join(f'<a href="{p}.html"{active if p == page else ""}>{t["names"][p]}</a>' for p in PAGES)
    header = f"""<header class="top">
  <div class="top-inner">
    <a class="brand" href="index.html"><img src="{prefix}assets/logo.png" alt=""><span class="word">Riesz</span><span class="sub">{t['sub']}</span></a>
    <button class="menu-toggle" aria-expanded="false">{t['menu']}</button>
    <nav>
      {nav}
      <a class="lang" href="{other}">{t['other']}</a>
      <a class="gh" href="{REPO}">{GH_ICON}GitHub</a>
    </nav>
  </div>
</header>"""

    if page == "index":
        main = f'<main class="page single">\n{body}\n</main>'
    else:
        site = "".join(f'<li><a href="{p}.html"{current if p == page else ""}>{t["names"][p]}</a></li>' for p in PAGES)
        # FAQ questions are long; the page contents stop at the FAQ heading itself.
        local = "".join(
            f'<li class="l{lvl}"><a href="#{hid}">{html.escape(text)}</a></li>'
            for lvl, hid, text in headings(body) if not hid.startswith("faq-")
        )
        i = PAGES.index(page)
        prev_link = f'<a class="prev" href="{PAGES[i - 1]}.html"><span class="dir">{t["prev"]}</span><span class="name">{t["names"][PAGES[i - 1]]}</span></a>'
        next_link = (f'<a class="next" href="{PAGES[i + 1]}.html"><span class="dir">{t["next"]}</span>'
                     f'<span class="name">{t["names"][PAGES[i + 1]]}</span></a>') if i + 1 < len(PAGES) else ""
        main = f"""<main class="page">
  <aside class="toc">
    <div class="toc-title">{t['docs']}</div>
    <ul class="toc-site">{site}</ul>
    <div class="toc-title">{t['this_page']}</div>
    <ul class="toc-page">{local}</ul>
  </aside>
  <article>
{body}
    <nav class="pager">{prev_link}{next_link}</nav>
  </article>
</main>"""

    doc = f"""<!doctype html>
<html lang="{t['lang']}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(t['site'])}">
<link rel="icon" href="{prefix}assets/logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}">
<link rel="stylesheet" href="{prefix}assets/style.css">
<script src="{prefix}assets/menu.js" defer></script>
</head>
<body>
{header}
{main}
<footer class="foot">{t['foot']} <a href="{REPO}">GitHub</a></footer>
</body>
</html>
"""
    # Pages in en/ reach shared assets one level up; their own links stay relative.
    if lang == "en":
        doc = doc.replace('src="assets/', 'src="../assets/').replace('href="assets/', 'href="../assets/')
    target = OUT / (f"{page}.html" if lang == "zh" else f"en/{page}.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(doc, encoding="utf-8")
    print("wrote", target.relative_to(OUT.parent))


if __name__ == "__main__":
    for lang in TEXT:
        for page in PAGES:
            if (SRC / lang / f"{page}.html").exists():
                build(lang, page)
