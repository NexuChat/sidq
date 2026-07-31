# Devpost gallery boards

Five 1920×1080 boards for the submission gallery, composed in the landing
page's exact palette and type system. The rendered PNGs sit beside this file;
the HTML sources live in `src/`.

To regenerate a board after editing its source:

```bash
cd docs/gallery
google-chrome --headless=new --disable-gpu --window-size=1920,1080 \
  --hide-scrollbars --screenshot=01-cover.png "file://$PWD/src/01-cover.html"
```

`src/03-architecture.html` embeds a copy of `docs/architecture.svg` taken at
generation time. After editing the SVG, rebuild that source first:

```bash
python3 - <<'EOF'
svg = open('docs/architecture.svg').read()
html = open('docs/gallery/src/03-architecture.html').read()
start = html.index('<svg'); end = html.index('</svg>') + len('</svg>')
open('docs/gallery/src/03-architecture.html', 'w').write(
    html[:start] + svg[svg.index('<svg'):svg.index('</svg>') + len('</svg>')] + html[end:]
)
EOF
```
