# FactNews (SourceNews)

Live: https://justinguillaume-beep.github.io/SourceNews/ · JSON: https://justinguillaume-beep.github.io/SourceNews/stories.json · Text: https://justinguillaume-beep.github.io/SourceNews/stories.txt

Same story from many outlets (left to right), reduced to checkable facts. Rebuilt every 30 min by GitHub Actions and served on GitHub Pages:
`index.html` (UI), `stories.json` (machine-readable), `stories.txt` (plain text). Local: `pip install -r requirements.txt && python3 app.py 8080`. Export: `python3 app.py --export --out site`.
Facts are extracted heuristically from RSS headlines and descriptions, and from the article body when the full page can be fetched. Paywalled or blocked pages fall back to the RSS summary. Each fact is marked `body` or `summary`. Source counts are word-overlap based, not verified.
