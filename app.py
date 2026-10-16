#!/usr/bin/env python3
"""FactNews: multi-outlet RSS clustering with extractive fact bullets.

Full pages are fetched when a source allows it, and facts are taken from the
article body as well as the RSS headline and summary. Paywalled or blocked
pages fall back to the summary. Each fact is marked body or summary.
"""
import gzip, html, json, math, os, re, sys, threading, time, urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

try:
    import trafilatura
except ImportError:
    trafilatura = None

FEEDS = {
 "BBC": ("https://feeds.bbci.co.uk/news/world/rss.xml","Center"),
 "BBC US": ("https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml","Center"),
 "NPR": ("https://feeds.npr.org/1001/rss.xml","Lean Left"),
 "The Guardian": ("https://www.theguardian.com/world/rss","Left"),
 "Guardian US": ("https://www.theguardian.com/us-news/rss","Left"),
 "NYT": ("https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml","Lean Left"),
 "CBS News": ("https://www.cbsnews.com/latest/rss/main","Lean Left"),
 "ABC News": ("https://abcnews.go.com/abcnews/topstories","Lean Left"),
 "Al Jazeera": ("https://www.aljazeera.com/xml/rss/all.xml","Center"),
 "Fox News": ("https://moxie.foxnews.com/google-publisher/latest.xml","Right"),
 "NY Post": ("https://nypost.com/news/feed/","Right"),
 "Washington Examiner": ("https://www.washingtonexaminer.com/feed/","Right"),
 "WSJ": ("https://feeds.a.dj.com/rss/RSSWorldNews.xml","Lean Right"),
 "The Hill": ("https://thehill.com/news/feed/","Center"),
 "Daily Caller": ("https://dailycaller.com/feed/","Right"),
 "HuffPost": ("https://www.huffpost.com/section/politics/feed?x=1","Left"),
 "MSNBC": ("https://www.msnbc.com/rss","Left"),
 "Vox": ("https://www.vox.com/rss/index.xml","Left"),
 "Washington Times": ("https://www.washingtontimes.com/rss/headlines/news/","Lean Right"),
 "Newsmax": ("https://www.newsmax.com/rss/Newsfront/1/","Right"),
 "National Review": ("https://www.nationalreview.com/news/feed/","Right"),
 "Breitbart": ("https://www.breitbart.com/feed/","Right"),
 "The Federalist": ("https://thefederalist.com/feed/","Right"),
 "Daily Wire": ("https://www.dailywire.com/feeds/rss.xml","Right"),
}
SIDE = {"Left": "left", "Lean Left": "left", "Center": "center", "Lean Right": "right", "Right": "right"}
# RSS feeds already answer this UA. Article fetches use an identifiable bot UA.
FEED_UA = {"User-Agent": "Mozilla/5.0 FactNews/0.2 (+https://justinguillaume-beep.github.io/sourcenews)"}
ARTICLE_UA = {
 "User-Agent": "Mozilla/5.0 (compatible; FactNews/1.0; +https://justinguillaume-beep.github.io/sourcenews)",
 "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
 "Accept-Language": "en-US,en;q=0.8",
}
STOP = set("""a an the and or but of to in on at for from by with as is are was were be been being it its this that these those he she they his her their them we you i not no
has have had will would can could should may might do does did said says say after over into about than more new up out who what when where how why just also amid via""".split())
OPINION = set("""shocking outrageous disgraceful stunning slams blasts destroys rips brutal horrific terrible amazing incredible disaster disastrous radical extremist woke
chaos chaotic insane absurd ridiculous furious fury outrage bombshell explosive scathing humiliating humiliation embarrassing pathetic heroic evil
dangerous unhinged crazy apparently allegedly arguably clearly obviously surely unfortunately sadly hopefully best worst greatest massive huge
devastating jaw-dropping epic must should opinion analysis commentary think believe feel""".split())
OPINION_TITLE = re.compile(r"^(opinion|analysis|commentary|editorial|review|podcast|watch|listen|live|quiz)\b|:\s*opinion", re.I)
BOILER = re.compile(r"\b(cookies?|newsletter|subscribe|sign up|privacy policy|all rights reserved|advertisement|related articles|recommended stories)\b", re.I)
_ABBREV = re.compile(r"(?:Mr|Mrs|Ms|Dr|Sen|Rep|Gov|Gen|Col|Lt|Sgt|Prof|Jr|Sr|St|vs|Inc|Ltd|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|a\.m|p\.m|U\.S|U\.K|U\.N|D\.C)\.$", re.I)
_DATELINE = re.compile(r"^[A-Z][\w .,'’-]{0,40}\s[–—-]\s+")
PAYWALL = ("subscribe to continue", "subscribe to read", "sign in to continue", "register to continue",
           "to continue reading", "enable javascript", "please enable cookies", "access denied", "are you a robot", "captcha")
# Stay well inside the 30-minute Pages cron: cap unique URLs and wall time.
BODY_WORKERS = 12
BODY_TIMEOUT = 8
BODY_BUDGET = int(os.environ.get("FACTNEWS_BODY_BUDGET", "180"))
BODY_MAX_URLS = int(os.environ.get("FACTNEWS_BODY_MAX_URLS", "220"))
BODY_MAX_CHARS = 5000
BODY_MAX_SENTS = 20
BODY_MAX_BYTES = 1_000_000

def clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()

def fetch(name):
    url, lean = FEEDS[name]
    try:
        data = urllib.request.urlopen(urllib.request.Request(url, headers=FEED_UA), timeout=15).read()
        root = ET.fromstring(data)
    except Exception as e:
        print(f"[feed] {name}: {e}", file=sys.stderr); return []
    out = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"): continue
        g = lambda t: next((c for c in it if c.tag.split("}")[-1] == t), None)
        title = clean(getattr(g("title"), "text", ""))
        link_el = g("link"); link = (link_el.text or link_el.get("href", "")) if link_el is not None else ""
        desc = clean(getattr(g("description"), "text", None) or getattr(g("summary"), "text", "") or "")
        date = getattr(g("pubDate"), "text", None) or getattr(g("updated"), "text", None) or ""
        try: ts = parsedate_to_datetime(date).timestamp()
        except Exception: ts = time.time()
        if title and not OPINION_TITLE.search(title):
            out.append(dict(source=name, lean=lean, title=title, link=(link or "").strip(), desc=desc[:600], ts=ts))
    return out[:40]

def fetch_body(url):
    """Return main-article text, or None when the page is missing, tiny, or blocked."""
    if trafilatura is None or not url.startswith("http"):
        return None
    req = urllib.request.Request(url, headers=ARTICLE_UA)
    try:
        with urllib.request.urlopen(req, timeout=BODY_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if any(x in ctype for x in ("image/", "video/", "audio/", "application/pdf", "application/zip", "application/json")):
                return None
            raw = resp.read(BODY_MAX_BYTES)
    except Exception:
        return None
    if not raw or len(raw) < 800:
        return None
    if raw[:2] == b"\x1f\x8b":
        try: raw = gzip.decompress(raw)
        except Exception: return None
    try:
        text = trafilatura.extract(raw, url=url, include_comments=False, include_tables=False,
                                   include_links=False, favor_precision=True)
    except Exception:
        return None
    if not text:
        return None
    text = clean(text)
    if len(text) < 350:
        return None
    if any(p in text[:500].lower() for p in PAYWALL):
        return None
    text = text[:BODY_MAX_CHARS]
    cut = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
    if cut >= 400:
        text = text[:cut + 1]
    return text

def priority_urls(groups):
    """One newest URL per outlet in the largest multi-source stories, then the rest."""
    ranked = sorted(groups, key=lambda g: (-len({a["source"] for a in g}), -max(a["ts"] for a in g)))
    first, rest, seen = [], [], set()
    def add(bucket, a):
        u = (a.get("link") or "").strip()
        if not u.startswith("http") or u in seen: return
        seen.add(u); bucket.append(u)
    for g in ranked:
        multi = len({a["source"] for a in g}) >= 2
        newest = {}
        for a in sorted(g, key=lambda a: -a["ts"]):
            newest.setdefault(a["source"], a)
        if multi:
            for a in newest.values(): add(first, a)
        for a in g: add(rest, a)
    return (first + rest)[:BODY_MAX_URLS]

def attach_bodies(arts, groups):
    if trafilatura is None:
        print("[bodies] trafilatura not installed; using RSS summaries only", file=sys.stderr)
        return 0, 0
    urls = priority_urls(groups)
    found = {}
    host_sems, sem_lock = {}, threading.Lock()
    def host_sem(url):
        host = urlparse(url).netloc.lower()
        with sem_lock:
            sem = host_sems.get(host)
            if sem is None:
                sem = threading.Semaphore(3)
                host_sems[host] = sem
            return sem
    def job(url):
        sem = host_sem(url)
        if not sem.acquire(timeout=BODY_TIMEOUT):
            return url, None
        try:
            time.sleep(0.15)
            return url, fetch_body(url)
        finally:
            sem.release()
    if not urls:
        return 0, 0
    with ThreadPoolExecutor(BODY_WORKERS) as ex:
        futs = [ex.submit(job, u) for u in urls]
        try:
            for fut in as_completed(futs, timeout=BODY_BUDGET):
                try: url, body = fut.result()
                except Exception: continue
                if body: found[url] = body
        except TimeoutError:
            for fut in futs: fut.cancel()
            print(f"[bodies] budget {BODY_BUDGET}s reached with {len(found)} pages", file=sys.stderr)
    n = 0
    for a in arts:
        body = found.get((a.get("link") or "").strip())
        if body:
            a["body"] = body
            n += 1
    print(f"[bodies] {len(found)}/{len(urls)} pages fetched, {n} articles enriched", file=sys.stderr)
    return len(found), len(urls)

def toks(s):
    return [w for w in re.findall(r"[a-z0-9']+", s.lower()) if w not in STOP and len(w) > 2]

def stem(w): return re.sub(r"(ing|ed|es|s)$", "", w) if len(w) > 4 else w

def vec(a, idf):
    c = Counter(stem(w) for w in toks(a["title"] + " " + a["title"] + " " + a["desc"]))
    v = {w: n * idf.get(w, 1) for w, n in c.items()}
    norm = math.sqrt(sum(x * x for x in v.values())) or 1
    return {w: x / norm for w, x in v.items()}

def cos(a, b): return sum(x * b.get(w, 0) for w, x in a.items())

def cluster(arts, thr=0.28):
    df = Counter()
    for a in arts: df.update(set(stem(w) for w in toks(a["title"] + " " + a["desc"])))
    N = len(arts) or 1
    idf = {w: math.log(N / (1 + n)) + 1 for w, n in df.items()}
    vs = [vec(a, idf) for a in arts]
    clusters = []  # each: list of idx, centroid
    for i, v in enumerate(vs):
        best, bs = None, thr
        for c in clusters:
            s = max(cos(v, vs[j]) for j in c["m"])
            if s > bs: best, bs = c, s
        if best: best["m"].append(i)
        else: clusters.append({"m": [i]})
    return [[arts[j] for j in c["m"]] for c in clusters], idf

def sentences(t):
    t = re.sub(r"\s+", " ", t or "").strip()
    # "ended. - Next sentence" is two sentences, not one bullet glued on.
    t = re.sub(r"(?<=[.!?])\s+[-–•·]\s+(?=[A-Z\"'“])", " ", t)
    parts = re.split(r"(?<=[.!?])[\"'”’)]*\s+(?=[A-Z\"'“])", t)
    merged, buf = [], ""
    for p in parts:
        p = p.strip()
        if not p: continue
        buf = (buf + " " + p).strip() if buf else p
        if _ABBREV.search(buf) and len(buf) < 280:
            continue
        merged.append(buf)
        buf = ""
    return [s for s in merged if len(s) > 25]

_BROKEN = re.compile(r"\s,\s|\b(?:the|a|an)\s+of\b")

def prepare_body_sentence(s):
    s = re.sub(r"^(?:[-–•·]\s*)?Published\s+", "", s).strip()
    s = _DATELINE.sub("", s).strip()
    if not (40 <= len(s) <= 400) or len(s.split()) < 8: return None
    if BOILER.search(s) or _BROKEN.search(s) or _ABBREV.search(s.rstrip("\"'”’ ")): return None
    if not re.search(r"[.!?][\"'”’)]*$", s): return None
    if re.match(r"^(Watch|Read more|Click|Photo:|Video:)\b", s, re.I): return None
    return s

def factual_score(s):
    w = re.findall(r"[A-Za-z'-]+", s); lw = [x.lower() for x in w]
    if any(x in OPINION for x in lw) or "?" in s or "!" in s: return -1
    sc = 0
    sc += 2 * len(re.findall(r"\b\d[\d,.]*%?\b", s))
    sc += len(re.findall(r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|January|February|March|April|May|June|July|August|September|October|November|December|yesterday|today)\b", s))
    sc += 1.5 * len(re.findall(r"\b(said|says|told|according to|announced|confirmed|reported)\b", s))
    sc += 0.5 * sum(1 for x in w[1:] if x[:1].isupper())
    return sc

def neutralize(s):
    s = re.sub(r"\b(" + "|".join(sorted(OPINION, key=len, reverse=True)) + r")\b\s*", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()

def keyset(s, idf):
    # Words never seen in any RSS title/summary still count, so body-only
    # details can form a fact. Summary wording is unchanged: those stems are in idf.
    return set(stem(w) for w in toks(s) if idf.get(stem(w), 3.5) > 2.0)

def pick_facts(facts, limit=6, body_slots=2):
    """Keep the highest-scoring facts, and keep article-body facts visible."""
    ranked = sorted(facts, key=lambda f: -f["score"])
    chosen, seen = [], set()
    for f in ranked:
        if f["origin"] != "body": continue
        chosen.append(f); seen.add(id(f))
        if len(chosen) >= body_slots: break
    for f in ranked:
        if id(f) in seen: continue
        chosen.append(f)
        if len(chosen) >= limit: break
    chosen.sort(key=lambda f: -f["score"])
    return chosen[:limit]

def summarize(group, idf):
    group = sorted(group, key=lambda a: -a["ts"])
    srcs = sorted({a["source"] for a in group})
    art_keys = {}
    for a in group:
        blob = a["title"] + " " + (a.get("desc") or "") + " " + (a.get("body") or "")
        art_keys[id(a)] = keyset(blob, idf)
    cands = []
    for a in group:
        bits = [a["title"].rstrip(".") + "."]
        if a.get("desc"): bits += sentences(a["desc"])
        for s in bits:
            sc = factual_score(s)
            if sc < 0: continue
            cands.append((s, a["source"], sc, "summary"))
    for a in group:
        if not a.get("body"): continue
        kept = 0
        for raw in sentences(a["body"]):
            s = prepare_body_sentence(raw)
            if not s: continue
            sc = factual_score(s)
            if sc < 0: continue
            cands.append((s, a["source"], sc, "body"))
            kept += 1
            if kept >= BODY_MAX_SENTS: break
    facts, seen = [], []
    for s, src, sc, origin in cands:
        k = keyset(s, idf)
        if len(k) < 2: continue
        support = {src}
        for a in group:
            if a["source"] == src: continue
            ak = art_keys[id(a)]
            if ak and len(k & ak) / len(k) >= 0.4: support.add(a["source"])
        if any(len(k & k2) / max(1, min(len(k), len(k2))) > 0.6 for k2 in seen): continue
        text = neutralize(s.rstrip("."))
        if len(text) < 25: continue
        seen.append(k)
        facts.append(dict(text=text + ".", sources=sorted(support), score=sc + 3 * (len(support) - 1), origin=origin))
    facts = pick_facts(facts)
    def hscore(a):
        others = " ".join(b["title"] for b in group if b is not a)
        return len(keyset(a["title"], idf) & keyset(others, idf)) - 3 * sum(w.lower() in OPINION for w in a["title"].split()) - 0.02 * len(a["title"])
    head = neutralize(max(group, key=hscore)["title"]).rstrip("!")
    head = re.sub(r"^(BREAKING|WATCH|LIVE|EXCLUSIVE)[:\s-]+", "", head, flags=re.I)
    bias, blind = bias_of(group)
    return dict(headline=head, facts=facts, sources=srcs, leans=Counter(a["lean"] for a in group),
                bias=bias, blindspot=blind,
                articles=[dict(source=a["source"], lean=a["lean"], title=a["title"], link=a["link"]) for a in group],
                ts=max(a["ts"] for a in group))

def bias_of(group):
    """Count each outlet once. Left includes Lean Left; Right includes Lean Right."""
    outlets = {}
    for a in group:
        outlets.setdefault(a["source"], a["lean"])
    counts = {"left": 0, "center": 0, "right": 0}
    for lean in outlets.values():
        counts[SIDE.get(lean, "center")] += 1
    total = sum(counts.values())
    if not total:
        pct = {"left": 0, "center": 0, "right": 0}
    else:
        raw = {k: counts[k] * 100 / total for k in counts}
        pct = {k: int(raw[k]) for k in counts}
        remainder = 100 - sum(pct.values())
        order = sorted(counts, key=lambda k: (raw[k] - pct[k], counts[k]), reverse=True)
        for i in range(remainder):
            pct[order[i]] += 1
    weak = []
    for side, other in (("left", "right"), ("right", "left")):
        share = (counts[side] / total) if total else 0
        if counts[other] >= 3 and (counts[side] == 0 or share <= 0.20):
            weak.append((share, counts[side], side))
    blind = None
    if weak:
        weak.sort()
        if not (len(weak) == 2 and weak[0][0] == weak[1][0] and weak[0][1] == weak[1][1]):
            blind = weak[0][2]
    bias = {
        "left": counts["left"], "center": counts["center"], "right": counts["right"],
        "left_pct": pct["left"], "center_pct": pct["center"], "right_pct": pct["right"],
    }
    return bias, blind

def blindspot_label(side):
    if side == "left": return "Blindspot: little Left coverage"
    if side == "right": return "Blindspot: little Right coverage"
    return None

STATE = {"stories": [], "updated": 0, "feeds": {}}
LOCK = threading.Lock()

def refresh():
    t0 = time.time()
    with ThreadPoolExecutor(16) as ex: res = dict(zip(FEEDS, ex.map(fetch, FEEDS)))
    arts = [a for v in res.values() for a in v]
    groups, idf = cluster(arts)
    attach_bodies(arts, groups)
    stories = [summarize(g, idf) for g in groups]
    stories.sort(key=lambda s: (-len(s["sources"]), -s["ts"]))
    n_body = sum(1 for s in stories for f in s["facts"] if f.get("origin") == "body")
    catalog = [{"name": name, "lean": lean} for name, (_, lean) in FEEDS.items()]
    with LOCK:
        STATE.update(stories=stories, updated=time.time(), feeds={k: len(v) for k, v in res.items()}, catalog=catalog)
    print(f"[refresh] {len(arts)} articles, {len(stories)} stories, multi-source: {sum(len(s['sources'])>1 for s in stories)}, body facts: {n_body}, {time.time()-t0:.0f}s", file=sys.stderr)

PAGE = open(__file__.replace("app.py", "page.html")).read()

def render_static(min_sources=2, limit=40):
    with LOCK: data = dict(STATE, stories=[s for s in STATE["stories"] if len(s["sources"]) >= min_sources][:limit])
    return PAGE.replace("/*__DATA__*/null", json.dumps(data))

def export(out, min_sources=2, limit=40):
    import datetime
    with LOCK:
        st = [s for s in STATE["stories"] if len(s["sources"]) >= min_sources][:limit]
        upd = datetime.datetime.fromtimestamp(STATE["updated"], datetime.timezone.utc).isoformat()
    iso = lambda t: datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat()
    def fact_row(f):
        return {"text": f["text"], "source_count": len(f["sources"]), "sources": f["sources"],
                "single_source": len(f["sources"]) == 1, "origin": f.get("origin", "summary")}
    def story_row(i, s):
        b = s["bias"]
        return {"id": i + 1, "headline": s["headline"], "latest_article_at": iso(s["ts"]), "outlets": s["sources"],
                "outlet_count": len(s["sources"]),
                "left": b["left"], "center": b["center"], "right": b["right"],
                "left_pct": b["left_pct"], "center_pct": b["center_pct"], "right_pct": b["right_pct"],
                "blindspot": s.get("blindspot"),
                "facts": [fact_row(f) for f in s["facts"]],
                "articles": [{"outlet": a["source"], "lean": a["lean"], "title": a["title"], "url": a["link"]} for a in s["articles"]]}
    js = {"generated_at": upd, "story_count": len(st), "stories": [story_row(i, s) for i, s in enumerate(st)]}
    open(os.path.join(out, "stories.json"), "w").write(json.dumps(js, indent=1, ensure_ascii=False))
    L = [f"FACTNEWS - just the facts. Generated {upd}. {len(st)} stories covered by 2+ outlets.",
         "Coverage counts each outlet once: Left includes Lean Left, Right includes Lean Right.",
         "A blindspot is a side with 0 outlets or 20% or less, while the opposite side has at least 3.",
         "Each fact is marked (article body) when it was read from the full page, or (summary) when it came from the RSS headline or description.", ""]
    for x in js["stories"]:
        spot = blindspot_label(x.get("blindspot")) or "Blindspot: none"
        L += [f"## {x['id']}. {x['headline']}",
              f"Updated: {x['latest_article_at']} | Outlets ({x['outlet_count']}): {', '.join(x['outlets'])}",
              f"Coverage: Left {x['left']} ({x['left_pct']}%) · Center {x['center']} ({x['center_pct']}%) · Right {x['right']} ({x['right_pct']}%)",
              spot, "Facts:"]
        for f in x["facts"]:
            where = "article body" if f["origin"] == "body" else "summary"
            if f["single_source"]:
                L.append(f"- {f['text']} [SINGLE SOURCE: {f['sources'][0]}] ({where})")
            else:
                L.append(f"- {f['text']} [{f['source_count']} sources: {', '.join(f['sources'])}] ({where})")
        L += ["Links:"] + [f"- {a['outlet']}: {a['url']}" for a in x["articles"]] + [""]
    open(os.path.join(out, "stories.txt"), "w").write("\n".join(L))
    with LOCK: page = PAGE.replace("/*__DATA__*/null", json.dumps(dict(STATE, stories=st)))
    open(os.path.join(out, "index.html"), "w").write(page)
    import shutil
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("manifest.webmanifest", "sw.js", "icon-192.png", "icon-512.png"):
        src, dst = os.path.join(here, name), os.path.join(out, name)
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copyfile(src, dst)
    n_body = sum(1 for s in js["stories"] for f in s["facts"] if f["origin"] == "body")
    n_sum = sum(1 for s in js["stories"] for f in s["facts"] if f["origin"] != "body")
    print(f"exported {len(st)} stories to {out} ({n_body} article-body facts, {n_sum} summary facts)")

class H(BaseHTTPRequestHandler):
    def send(self, body, ct="text/html"):
        b = body.encode(); self.send_response(200); self.send_header("Content-Type", ct + "; charset=utf-8")
        self.send_header("Content-Length", len(b)); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith("/api/stories"):
            with LOCK: self.send(json.dumps(STATE), "application/json")
        elif self.path.startswith("/api/refresh"):
            refresh()
            with LOCK: self.send(json.dumps(STATE), "application/json")
        else: self.send(PAGE)
    def log_message(self, *a): pass

if __name__ == "__main__":
    refresh()
    if "--export" in sys.argv:
        out = sys.argv[sys.argv.index("--out")+1] if "--out" in sys.argv else os.path.dirname(os.path.abspath(__file__))
        os.makedirs(out, exist_ok=True); export(out); sys.exit()
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8080
    def loop():
        while True: time.sleep(900); refresh()
    threading.Thread(target=loop, daemon=True).start()
    print(f"FactNews on http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
