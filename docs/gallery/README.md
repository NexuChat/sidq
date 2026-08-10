# Devpost gallery boards

Seven boards — six at 1920×1080 for the gallery, plus a 1800×1200 thumbnail — for the submission gallery, composed in the landing
page's exact palette and type system. The rendered PNGs sit beside this file;
the HTML sources live in `src/`.

- `06-swarm.png` — four auditors, one catalog, no coordinator

To regenerate a board after editing its source:

```bash
cd docs/gallery
google-chrome --headless=new --disable-gpu --window-size=1920,1080 \
  --hide-scrollbars --screenshot=01-cover.png "file://$PWD/src/01-cover.html"
```

`src/03-architecture.html` embeds a copy of `docs/architecture.svg` taken at
generation time. After editing the canonical SVG, synchronize both the landing
page copy and the gallery source first:

```bash
python3 scripts/sync_architecture.py
```
