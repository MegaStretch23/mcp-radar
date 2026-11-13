#!/usr/bin/env python3
"""mcp-radar: a directory and growth-trend site for public MCP servers. Python standard library only.

  python mcp_radar.py run                 # snapshot the public sources, then render the static site into ./docs
  python mcp_radar.py snapshot            # pull the sources, write snapshots/<date>.json + servers.json
  python mcp_radar.py build               # render docs/ (index, category pages, trends, data.json) from the saved files, offline
  python mcp_radar.py top --by delta7     # print the top servers by stars, 7-day or 30-day star delta

Public sources only, no logins, no scraping:
  GitHub search API (topic mcp-server; set GITHUB_TOKEN for the higher rate limit), the official MCP registry
  (registry.modelcontextprotocol.io), the npm search API (keywords:mcp-server) and the PyPI JSON API
  (PyPI has no search API: it is queried only for packages the registry lists).
One row = one repository (or one package when no repository is known). The GitHub topic is self-applied by repo owners,
so a row is "listed as an MCP server", not "verified to work".
"""
from __future__ import annotations

import argparse, datetime as dt, html, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

VERSION = "0.1.0"
UA = f"mcp-radar/{VERSION} (+https://github.com/MegaStretch23/mcp-radar)"
REGISTRY = "https://registry.modelcontextprotocol.io/v0/servers"

CATEGORIES = [  # first match wins; plain keyword rules on name + description + topics, so approximate by design
    ("browser-automation", "Browser and scraping", r"browser|playwright|puppeteer|selenium|scrap(e|er|ing)|crawl"),
    ("databases", "Databases", r"database|postgres|mysql|sqlite|mongodb|redis|supabase|clickhouse|snowflake|bigquery|\bsql\b|vector (db|store|database)"),
    ("cloud-devops", "Cloud and DevOps", r"kubernetes|docker|terraform|\baws\b|azure|\bgcp\b|cloudflare|devops|ci/cd|observability|grafana|prometheus"),
    ("security", "Security", r"security|vulnerab|pentest|malware|\bcve\b|threat"),
    ("finance-crypto", "Finance and crypto", r"financ|stock|trading|crypto|blockchain|ethereum|solana|bitcoin|payment|invoice|\bdefi\b"),
    ("communication", "Communication", r"slack|discord|telegram|e-?mail|gmail|whatsapp|\bsms\b|messaging"),
    ("productivity", "Productivity", r"notion|obsidian|calendar|todo|jira|linear|asana|trello|confluence|airtable|google (docs|drive|sheets)"),
    ("search-research", "Search and research", r"search|research|arxiv|wikipedia|\bnews\b|\brss\b"),
    ("ai-agents-memory", "AI agents and memory", r"memory|knowledge graph|\brag\b|\bagents?\b|\bllm\b|prompt|embedding"),
    ("files-documents", "Files and documents", r"filesystem|file system|\bpdf\b|markdown|document|\bocr\b|spreadsheet|excel"),
    ("media-design", "Media and design", r"image|video|audio|music|figma|design|\b3d\b|blender|speech"),
    ("iot-hardware", "IoT and hardware", r"\biot\b|arduino|raspberry|home assistant|hardware|robot|serial|esp32"),
    ("developer-tools", "Developer tools", r"github|gitlab|\bgit\b|\bcode\b|\bide\b|lint|debug|\bsdk\b|\bapi\b|\bcli\b|testing"),
]
CAT_LABEL = {s: l for s, l, _ in CATEGORIES} | {"other": "Other"}


# ---------------------------------------------------------------- http
class Client:
    """fetch(url, headers) -> parsed JSON. Tests replace `fetch` with recorded fixtures; the default retries on rate limits."""

    def __init__(self, fetch=None, sleep=time.sleep):
        self.fetch, self.sleep = fetch or self._fetch, sleep

    def _fetch(self, url: str, headers: dict | None = None):
        last = None
        for attempt in range(4):
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (403, 429) and attempt < 3:
                    wait = e.headers.get("Retry-After")
                    reset = e.headers.get("X-RateLimit-Reset")
                    secs = int(wait) if wait and wait.isdigit() else (max(int(reset) - int(time.time()), 1) if reset and reset.isdigit() else 30)
                    self.sleep(min(secs + 2, 90)); continue
                if e.code >= 500 and attempt < 3:
                    self.sleep(3 * (attempt + 1)); continue
                raise
            except (urllib.error.URLError, TimeoutError) as e:
                last = e
                self.sleep(3 * (attempt + 1))
        raise RuntimeError(f"GET {url.split('?')[0]} failed: {last}")


def clean_text(s, n=220) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def safe_url(u) -> str | None:
    u = str(u or "").strip()
    return u if re.match(r"^https?://[^\s<>\"']+$", u) else None


def github_repo(url) -> str | None:
    """'git+https://github.com/o/r.git', 'https://github.com/o/r/tree/main/x', 'git@github.com:o/r.git' -> 'o/r'."""
    m = re.search(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", str(url or ""))
    if not m:
        return None
    repo = re.sub(r"\.git$", "", m.group(2))
    return f"{m.group(1)}/{repo}" if repo else None


# ---------------------------------------------------------------- sources
def fetch_github(c: Client, token: str | None, today: dt.date, pages: int) -> tuple[list[dict], list[str]]:
    """Three searches on topic:mcp-server (all-time by stars, created in the last 90 d, created in the last 30 d), up to `pages` x 100 each (GitHub caps a search at 1000)."""
    h = {"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {token}"} if token else {})}
    qs = ["topic:mcp-server", f"topic:mcp-server created:>={(today - dt.timedelta(days=90)).isoformat()}", f"topic:mcp-server created:>={(today - dt.timedelta(days=30)).isoformat()}"]
    out, errs = {}, []
    for q in qs:
        for page in range(1, pages + 1):
            url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode({"q": q, "sort": "stars", "order": "desc", "per_page": 100, "page": page})
            try:
                d = c.fetch(url, h)
            except urllib.error.HTTPError as e:
                if e.code == 422 and page > 1:   # past the 1000-result window
                    break
                errs.append(f"github search '{q}' page {page}: HTTP {e.code}"); break
            except Exception as e:
                errs.append(f"github search '{q}' page {page}: {e}"); break
            items = d.get("items") or []
            for i in items:
                out[i["full_name"].lower()] = repo_record(i)
            if len(items) < 100:
                break
    return list(out.values()), errs


def repo_record(i: dict) -> dict:
    return {"repo": i["full_name"], "stars": i.get("stargazers_count"), "forks": i.get("forks_count"), "pushed_at": i.get("pushed_at"), "created_at": i.get("created_at"),
            "topics": list(i.get("topics") or [])[:12], "language": i.get("language"), "archived": bool(i.get("archived")), "license": (i.get("license") or {}).get("spdx_id"),
            "description": clean_text(i.get("description")), "url": safe_url(i.get("html_url"))}


def month_ranges(today: dt.date, first=(2024, 11)) -> list[tuple[str, str, str]]:
    """(label, start, end) for each calendar month from the MCP launch month (Nov 2024) to the month of `today`."""
    out, y, m = [], first[0], first[1]
    while (y, m) <= (today.year, today.month):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append((f"{y}-{m:02d}", f"{y}-{m:02d}-01", (dt.date(ny, nm, 1) - dt.timedelta(days=1)).isoformat())); y, m = ny, nm
    return out


def fetch_totals(c: Client, token: str | None, today: dt.date) -> tuple[dict, list[str]]:
    """GitHub's own total_count for topic:mcp-server (all time, created in the last 30 days, and per creation month). One call each, per_page=1."""
    h = {"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {token}"} if token else {})}
    errs, tot = [], {"monthly": {}}

    def count(q: str):
        try:
            return int(c.fetch("https://api.github.com/search/repositories?" + urllib.parse.urlencode({"q": q, "per_page": 1}), h)["total_count"])
        except Exception as e:
            errs.append(f"github total '{q}': {e}"); return None
    tot["topic_total"] = count("topic:mcp-server")
    tot["created_30d"] = count(f"topic:mcp-server created:>={(today - dt.timedelta(days=30)).isoformat()}")
    for label, a, b in month_ranges(today):
        n = count(f"topic:mcp-server created:{a}..{b}")
        if n is not None:
            tot["monthly"][label] = n
    return tot, errs


def fetch_registry(c: Client, pages: int, page_size: int = 100) -> tuple[list[dict], list[str]]:
    out, errs, cursor = {}, [], None
    for _ in range(pages):
        url = f"{REGISTRY}?limit={page_size}" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        try:
            d = c.fetch(url, None)
        except Exception as e:
            errs.append(f"registry page: {e}"); break
        for s in d.get("servers") or []:
            sv, meta = s.get("server") or {}, (s.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {}
            if meta.get("isLatest") is False or not sv.get("name"):
                continue
            pk = [{"type": p.get("registryType"), "id": p.get("identifier")} for p in sv.get("packages") or [] if p.get("identifier")]
            home = (sv.get("repository") or {}).get("url") or sv.get("websiteUrl") or next((r.get("url") for r in sv.get("remotes") or []), None)
            out[sv["name"]] = {"registry_name": sv["name"], "title": sv.get("title") or sv["name"], "description": clean_text(sv.get("description")), "version": sv.get("version"),
                               "repo": github_repo((sv.get("repository") or {}).get("url")), "url": safe_url(home), "packages": pk, "published_at": meta.get("publishedAt")}
        cursor = (d.get("metadata") or {}).get("nextCursor")
        if not cursor:
            break
    return list(out.values()), errs


def fetch_npm(c: Client, pages: int) -> tuple[list[dict], list[str]]:
    out, errs = {}, []
    for p in range(pages):
        try:
            d = c.fetch(f"https://registry.npmjs.org/-/v1/search?text=keywords:mcp-server&size=250&from={p * 250}", None)
        except Exception as e:
            errs.append(f"npm search from={p * 250}: {e}"); break
        objs = d.get("objects") or []
        for o in objs:
            k, dl = o.get("package") or {}, o.get("downloads") or {}
            if not k.get("name"):
                continue
            links = k.get("links") or {}
            out[k["name"]] = {"npm": k["name"], "description": clean_text(k.get("description")), "version": k.get("version"), "weekly": dl.get("weekly"), "monthly": dl.get("monthly"),
                              "repo": github_repo(links.get("repository")), "url": safe_url(links.get("homepage") or links.get("npm")), "date": k.get("date"), "keywords": list(k.get("keywords") or [])[:8]}
        if len(objs) < 250:
            break
    return list(out.values()), errs


def fetch_pypi(c: Client, names: list[str]) -> tuple[dict, list[str]]:
    out, errs = {}, []
    for n in names:
        try:
            d = c.fetch(f"https://pypi.org/pypi/{urllib.parse.quote(n)}/json", None)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                errs.append(f"pypi {n}: HTTP {e.code}")
            continue
        except Exception as e:
            errs.append(f"pypi {n}: {e}"); continue
        info = d.get("info") or {}
        urls = d.get("urls") or []
        out[n] = {"version": info.get("version"), "uploaded": (urls[0].get("upload_time_iso_8601") if urls else None),
                  "repo": next((github_repo(v) for v in (info.get("project_urls") or {}).values() if github_repo(v)), None)}
    return out, errs


# ---------------------------------------------------------------- merge
def categorize(text: str) -> str:
    t = text.lower()
    for slug, _, pat in CATEGORIES:
        if re.search(pat, t):
            return slug
    return "other"


def merge(gh: list[dict], reg: list[dict], npm: list[dict], pypi: dict, repo_stats: dict) -> list[dict]:
    """One row per repository ('gh:owner/repo', case-insensitive); packages with no known repository keep 'npm:<name>' / 'reg:<name>'."""
    rows: dict[str, dict] = {}

    def row(key: str, **base) -> dict:
        if key not in rows:
            rows[key] = {"id": key, "name": base.get("name"), "description": "", "url": None, "repo": base.get("repo"), "stars": None, "forks": None, "pushed_at": None, "created_at": None,
                         "topics": [], "language": None, "archived": False, "license": None, "sources": [], "registry_names": [], "npm": None, "pypi": None}
        return rows[key]

    def add_repo(r: dict, g: dict):
        for k in ("stars", "forks", "pushed_at", "created_at", "language", "archived", "license"):
            r[k] = g.get(k)
        r["topics"] = g.get("topics") or r["topics"]
        r["repo"] = g["repo"]; r["name"] = g["repo"].split("/")[1]
        r["description"] = g.get("description") or r["description"]
        r["url"] = g.get("url") or f"https://github.com/{g['repo']}"

    for g in gh:
        r = row("gh:" + g["repo"].lower(), repo=g["repo"]); add_repo(r, g); r["sources"].append("github")
    stats = {k.lower(): v for k, v in repo_stats.items()}
    for g in reg:
        key = "gh:" + g["repo"].lower() if g["repo"] else "reg:" + g["registry_name"]
        r = row(key, name=g["title"], repo=g["repo"])
        if g["repo"] and r["stars"] is None and g["repo"].lower() in stats:
            add_repo(r, stats[g["repo"].lower()])
        if g["repo"] and not r["url"]:
            r["url"] = f"https://github.com/{g['repo']}"; r["name"] = g["repo"].split("/")[1]
        r["name"] = r["name"] or g["title"]
        r["url"] = r["url"] or g["url"]
        r["description"] = r["description"] or g["description"]
        r["sources"].append("registry"); r["registry_names"].append(g["registry_name"])
        for p in g["packages"]:
            if p["type"] == "pypi" and p["id"] in pypi:
                r["pypi"] = {"name": p["id"], "version": pypi[p["id"]]["version"], "uploaded": pypi[p["id"]]["uploaded"]}
            elif p["type"] == "pypi" and not r["pypi"]:
                r["pypi"] = {"name": p["id"], "version": None, "uploaded": None}
    for n in npm:
        key = "gh:" + n["repo"].lower() if n["repo"] else "npm:" + n["npm"]
        r = row(key, name=n["npm"], repo=n["repo"])
        if n["repo"] and r["stars"] is None and n["repo"].lower() in stats:
            add_repo(r, stats[n["repo"].lower()])
        if n["repo"] and not r["url"]:
            r["url"] = f"https://github.com/{n['repo']}"; r["name"] = n["repo"].split("/")[1]
        r["url"] = r["url"] or n["url"]
        r["description"] = r["description"] or n["description"]
        if not r["npm"] or (n["weekly"] or 0) > (r["npm"].get("weekly") or 0):   # a monorepo ships several packages: keep its most downloaded one
            r["npm"] = {"name": n["npm"], "version": n["version"], "weekly": n["weekly"], "monthly": n["monthly"]}
        r["sources"].append("npm")
    for r in rows.values():
        r["sources"] = sorted(set(r["sources"]))
        r["category"] = categorize(" ".join([r["name"] or "", r["description"] or "", " ".join(r["topics"])]))
        r["name"] = r["name"] or r["id"]
    return sorted(rows.values(), key=lambda r: (-(r["stars"] if r["stars"] is not None else -1), r["id"]))


def progress(msg: str):
    print(msg, file=sys.stderr, flush=True)


def collect(c: Client, today: dt.date, token: str | None, gh_pages=10, reg_pages=100, npm_pages=8, lookups=300, pypi_lookups=150) -> tuple[list[dict], dict, list[str]]:
    gh, e1 = fetch_github(c, token, today, gh_pages); progress(f"github search: {len(gh)} repositories")
    totals, e0 = fetch_totals(c, token, today); progress(f"github totals: {totals.get('topic_total')} repositories under topic mcp-server")
    reg, e2 = fetch_registry(c, reg_pages); progress(f"registry: {len(reg)} latest server entries")
    npm, e3 = fetch_npm(c, npm_pages); progress(f"npm: {len(npm)} packages")
    have = {g["repo"].lower() for g in gh}
    need = []
    for repo in [x["repo"] for x in reg + npm if x["repo"]]:
        if repo.lower() not in have and repo.lower() not in {n.lower() for n in need}:
            need.append(repo)
    stats, e4 = {}, []
    h = {"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {token}"} if token else {})}
    for repo in need[:lookups]:
        try:
            stats[repo] = repo_record(c.fetch(f"https://api.github.com/repos/{repo}", h))
        except urllib.error.HTTPError as e:
            if e.code not in (404, 451):
                e4.append(f"github repo {repo}: HTTP {e.code}")
        except Exception as e:
            e4.append(f"github repo {repo}: {e}")
    progress(f"github repo lookups: {len(stats)} of {len(need)} repositories not in the search results")
    pynames = [p["id"] for g in reg for p in g["packages"] if p["type"] == "pypi"][:pypi_lookups]
    pypi, e5 = fetch_pypi(c, pynames)
    servers = merge(gh, reg, npm, pypi, stats)
    info = {"totals": totals, "github_repos": len(gh), "registry_servers": len(reg), "npm_packages": len(npm), "pypi_lookups": len(pynames), "pypi_found": len(pypi),
            "repo_lookups": len(stats), "repo_lookups_skipped": max(len(need) - lookups, 0)}
    return servers, info, e0 + e1 + e2 + e3 + e4 + e5


# ---------------------------------------------------------------- snapshots + trends
def snap_of(servers: list[dict], date: str, info: dict, errors: list[str]) -> dict:
    return {"date": date, "version": VERSION, "sources": info, "errors": errors[:50],
            "servers": {s["id"]: {"s": s["stars"], "f": s["forks"], "p": s["pushed_at"], "n": (s["npm"] or {}).get("weekly")} for s in servers}}


def write_snapshot(root: Path, servers: list[dict], date: str, info: dict, errors: list[str]) -> Path:
    (root / "snapshots").mkdir(parents=True, exist_ok=True)
    p = root / "snapshots" / f"{date}.json"
    p.write_text(json.dumps(snap_of(servers, date, info, errors), separators=(",", ":")), encoding="utf-8")
    (root / "servers.json").write_text(json.dumps({"date": date, "servers": servers}, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    return p


def load_snapshots(root: Path) -> list[dict]:
    out = []
    for p in sorted((root / "snapshots").glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d.get("date"))) and isinstance(d.get("servers"), dict):
                out.append(d)
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda d: d["date"])


def baseline(snaps: list[dict], today: str, days: int) -> dict | None:
    """The latest snapshot at least `days` old, but not more than twice that old (so a 7-day delta is never quietly a 60-day one)."""
    t = dt.date.fromisoformat(today)
    ok = [s for s in snaps if days <= (t - dt.date.fromisoformat(s["date"])).days <= days * 2]
    return ok[-1] if ok else None


def trends(snaps: list[dict], servers: list[dict], today: str) -> dict:
    cur = next((s for s in snaps if s["date"] == today), None)
    t = dt.date.fromisoformat(today)
    out = {"date": today, "snapshots": len(snaps), "first_snapshot": snaps[0]["date"] if snaps else None}
    deltas = {}
    for days in (7, 30):
        b = baseline(snaps, today, days)
        out[f"baseline{days}"] = {"date": b["date"], "span_days": (t - dt.date.fromisoformat(b["date"])).days} if b else None
        d = {}
        if b and cur:
            for sid, now in cur["servers"].items():
                old = b["servers"].get(sid)
                if old and now.get("s") is not None and old.get("s") is not None:
                    d[sid] = now["s"] - old["s"]
        deltas[days] = d
    by_id = {s["id"]: s for s in servers}

    def top(days, n=25):
        rows = [(sid, v) for sid, v in deltas[days].items() if v > 0 and sid in by_id]
        rows.sort(key=lambda x: (-x[1], x[0]))
        return [{"id": sid, "name": by_id[sid]["name"], "repo": by_id[sid]["repo"], "delta": v, "stars": by_id[sid]["stars"]} for sid, v in rows[:n]]
    out["risers7"], out["risers30"] = top(7), top(30)
    out["delta7"], out["delta30"] = deltas[7], deltas[30]
    b7 = baseline(snaps, today, 7)
    out["new_in_snapshot"] = ([{"id": s["id"], "name": s["name"], "stars": s["stars"]} for s in servers if s["id"] not in b7["servers"]][:50]) if b7 else None
    cutoff = (t - dt.timedelta(days=30)).isoformat()
    recent = [s for s in servers if (s["created_at"] or "") >= cutoff and s["created_at"]]
    out["created_30d"] = sorted([{"id": s["id"], "name": s["name"], "repo": s["repo"], "stars": s["stars"], "created_at": s["created_at"][:10]} for s in recent], key=lambda r: (-(r["stars"] or 0), r["id"]))[:50]
    out["created_30d_in_directory"] = len(recent)
    tot = ((cur or {}).get("sources") or {}).get("totals") or {}
    out["github_topic_total"], out["created_30d_total"], out["created_by_month"] = tot.get("topic_total"), tot.get("created_30d"), tot.get("monthly") or {}
    return out


def star_history(snaps: list[dict], sid: str) -> list[tuple[str, int]]:
    return [(s["date"], s["servers"][sid]["s"]) for s in snaps if sid in s["servers"] and s["servers"][sid].get("s") is not None]


# ---------------------------------------------------------------- render
CSS = """body{font:15px/1.5 system-ui,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}a{color:#7cc4ff}main{max-width:1180px;margin:0 auto;padding:20px}
h1{margin:.2em 0}.sub{color:#9aa}table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{padding:5px 8px;border-bottom:1px solid #262a33;text-align:left;vertical-align:top}
th{position:sticky;top:0;background:#171a21}td.n,th.n{text-align:right;white-space:nowrap}.cats a{display:inline-block;margin:2px 8px 2px 0}.card{background:#171a21;border:1px solid #262a33;border-radius:8px;padding:12px 16px;margin:14px 0}
input{background:#171a21;color:#e6e6e6;border:1px solid #333;border-radius:6px;padding:7px 10px;width:320px;max-width:100%}.up{color:#6fd48a}.muted{color:#8a929c}svg text{fill:#9aa;font:11px system-ui}svg rect.b{fill:#4c9be8}svg polyline{fill:none;stroke:#6fd48a;stroke-width:1.5}footer{color:#8a929c;margin:30px 0}"""


def esc(x) -> str:
    return html.escape(str(x if x is not None else ""), quote=True)


def num(x) -> str:
    return "-" if x is None else f"{x:,}"


def dnum(x) -> str:
    return "-" if x is None else (f'<span class="up">+{x:,}</span>' if x > 0 else f"{x:,}")


def svg_bars(by_month: dict[str, int], w=760, h=220) -> str:
    if not by_month:
        return ""
    ks, mx = list(by_month), max(by_month.values())
    bw = (w - 50) / len(ks)
    bars = []
    for i, k in enumerate(ks):
        bh = int((h - 50) * by_month[k] / mx)
        x = 40 + i * bw
        bars.append(f'<rect class="b" x="{x:.1f}" y="{h - 30 - bh}" width="{max(bw - 2, 1):.1f}" height="{bh}"><title>{esc(k)}: {by_month[k]}</title></rect>')
        if i % max(len(ks) // 8, 1) == 0:
            bars.append(f'<text x="{x:.1f}" y="{h - 12}">{esc(k)}</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Servers by repository creation month">'
            f'<text x="2" y="14">{mx}</text><text x="2" y="{h - 32}">0</text>{"".join(bars)}</svg>')


def svg_spark(hist: list[tuple[str, int]], w=120, h=28) -> str:
    if len(hist) < 2:
        return ""
    vals = [v for _, v in hist]
    lo, hi = min(vals), max(vals)
    pts = " ".join(f"{2 + i * (w - 4) / (len(vals) - 1):.1f},{h - 3 - (h - 6) * ((v - lo) / (hi - lo) if hi > lo else 0.5):.1f}" for i, v in enumerate(vals))
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}"><title>{esc(hist[0][0])} to {esc(hist[-1][0])}: {lo} to {hi} stars</title><polyline points="{pts}"/></svg>'


def table(servers: list[dict], d7: dict, d30: dict, prefix: str, hist: dict | None = None) -> str:
    rows = []
    for i, s in enumerate(servers, 1):
        link = f'<a href="{esc(s["url"])}" rel="noopener">{esc(s["name"])}</a>' if s["url"] else esc(s["name"])
        spark = svg_spark(hist.get(s["id"], [])) if hist else ""
        rows.append(f'<tr><td class="n">{i}</td><td>{link}{" <span class=muted>(archived)</span>" if s["archived"] else ""}<br><span class="muted">{esc(s["repo"] or s["id"])}</span></td>'
                    f'<td><a href="{prefix}category/{esc(s["category"])}.html">{esc(CAT_LABEL[s["category"]])}</a></td><td class="n">{num(s["stars"])}</td><td class="n">{num(s["forks"])}</td>'
                    f'<td class="n">{dnum(d7.get(s["id"]))}</td><td class="n">{dnum(d30.get(s["id"]))}</td><td class="n">{num((s["npm"] or {}).get("weekly"))}</td>'
                    f'<td class="n">{esc((s["pushed_at"] or "")[:10]) or "-"}</td><td>{esc(s["description"])}{spark}</td></tr>')
    return ('<table class="dir"><thead><tr><th class="n">#</th><th>Server</th><th>Category</th><th class="n">Stars</th><th class="n">Forks</th><th class="n">7d</th><th class="n">30d</th>'
            '<th class="n">npm/week</th><th class="n">Last push</th><th>Description</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table>")


def page(title: str, body: str, prefix: str = "") -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><style>{CSS}</style></head>'
            f'<body><main><nav><a href="{prefix}index.html">Directory</a> · <a href="{prefix}trends.html">Trends</a> · <a href="{prefix}data.json">data.json</a> · '
            f'<a href="https://github.com/MegaStretch23/mcp-radar">GitHub</a></nav>{body}'
            f'<footer>mcp-radar {VERSION}, MIT. Data: GitHub search API, the official MCP registry, npm search, PyPI JSON API. A row is listed as an MCP server by its owner; it is not tested or endorsed here.</footer></main></body></html>')


INDEX_ROWS, CAT_ROWS = 1000, 1000


def build_site(root: Path, site: Path, today: str | None = None) -> dict:
    data = json.loads((root / "servers.json").read_text(encoding="utf-8"))
    servers, date = data["servers"], today or data["date"]
    snaps = load_snapshots(root)
    tr = trends(snaps, servers, date)
    d7, d30 = tr.pop("delta7"), tr.pop("delta30")
    hist = {s["id"]: star_history(snaps, s["id"]) for s in servers} if len(snaps) > 1 else {}
    (site / "category").mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for s in servers:
        counts[s["category"]] = counts.get(s["category"], 0) + 1
    cats = "".join(f'<a href="category/{esc(k)}.html">{esc(CAT_LABEL[k])} ({counts[k]})</a>' for k in [c[0] for c in CATEGORIES] + ["other"] if k in counts)
    withstars = sum(1 for s in servers if s["stars"] is not None)
    note = ("Star deltas need an earlier snapshot: the first one is " + esc(tr["first_snapshot"]) + ", so 7-day columns fill from " +
            (dt.date.fromisoformat(tr["first_snapshot"]) + dt.timedelta(days=7)).isoformat() + ".") if not tr["baseline7"] and tr["first_snapshot"] else ""
    topic = (f"GitHub lists {tr['github_topic_total']:,} repositories under the topic mcp-server (GitHub's own count); this directory holds the {sum(1 for x in servers if 'github' in x['sources']):,} most starred or newest of them plus registry and npm entries. " if tr.get("github_topic_total") else "")
    idx = (f'<h1>MCP ecosystem radar</h1><p class="sub">{len(servers):,} public MCP servers, snapshot {esc(date)}. {withstars:,} have a GitHub star count. {note}</p>'
           f'<p class="muted">Showing the top {min(INDEX_ROWS, len(servers)):,} by stars; every row is in <a href="data.json">data.json</a>. {topic}</p>'
           f'<div class="card cats">{cats}</div><p><input id="q" type="search" placeholder="Filter by name, repo or description" aria-label="Filter"></p>' + table(servers[:INDEX_ROWS], d7, d30, "", hist) +
           '<script>var q=document.getElementById("q");q.addEventListener("input",function(){var v=q.value.toLowerCase();document.querySelectorAll("table.dir tbody tr").forEach(function(r){r.style.display=r.textContent.toLowerCase().indexOf(v)<0?"none":""})})</script>')
    (site / "index.html").write_text(page("MCP ecosystem radar", idx), encoding="utf-8")
    for k, n in counts.items():
        sub = [s for s in servers if s["category"] == k]
        (site / "category" / f"{k}.html").write_text(page(f"{CAT_LABEL[k]} - MCP radar", f'<h1>{esc(CAT_LABEL[k])}</h1><p class="sub">{n:,} servers, by stars{f"; the top {CAT_ROWS:,} are listed here, all rows are in data.json" if n > CAT_ROWS else ""}.</p>' + table(sub[:CAT_ROWS], d7, d30, "../", hist), "../"), encoding="utf-8")
    t = [f'<h1>Trends</h1><p class="sub">{len(snaps)} snapshot(s), first {esc(tr["first_snapshot"])}, latest {esc(date)}.</p>']
    if tr["created_by_month"]:
        t.append('<div class="card"><h2>GitHub repositories tagged mcp-server, by creation month</h2><p class="muted">GitHub\'s own search count for topic:mcp-server and each creation month, read ' + esc(date) +
                 f'. It counts repositories whose owner added the topic, not verified servers; deleted or untagged repositories are not in it. Sum of the months shown: {sum(tr["created_by_month"].values()):,}.</p>' + svg_bars(tr["created_by_month"]) + "</div>")
    for days, key in ((7, "risers7"), (30, "risers30")):
        b = tr[f"baseline{days}"]
        if b:
            rows = "".join(f'<tr><td>{esc(r["name"])}<br><span class="muted">{esc(r["repo"] or r["id"])}</span></td><td class="n">{dnum(r["delta"])}</td><td class="n">{num(r["stars"])}</td></tr>' for r in tr[key])
            t.append(f'<div class="card"><h2>Top risers, {days} days</h2><p class="muted">Star gain since snapshot {esc(b["date"])} ({b["span_days"]} days ago).</p><table><thead><tr><th>Server</th><th class="n">Stars gained</th><th class="n">Stars</th></tr></thead><tbody>{rows}</tbody></table></div>')
        else:
            t.append(f'<div class="card"><h2>Top risers, {days} days</h2><p class="muted">No snapshot that is {days} to {days * 2} days old yet. Snapshots so far: {len(snaps)}, first {esc(tr["first_snapshot"])}.</p></div>')
    rows = "".join(f'<tr><td>{esc(r["name"])}<br><span class="muted">{esc(r["repo"] or r["id"])}</span></td><td>{esc(r["created_at"])}</td><td class="n">{num(r["stars"])}</td></tr>' for r in tr["created_30d"])
    t.append(f'<div class="card"><h2>New repositories, last 30 days</h2><p class="muted">{f"GitHub counts {tr['created_30d_total']:,} repositories with topic mcp-server created in the 30 days before {esc(date)}; " if tr.get("created_30d_total") is not None else ""}{tr["created_30d_in_directory"]:,} of them are in this directory (the newest 1,000 by stars at most); top 50 by stars below.</p><table><thead><tr><th>Server</th><th>Created</th><th class="n">Stars</th></tr></thead><tbody>{rows}</tbody></table></div>')
    (site / "trends.html").write_text(page("Trends - MCP radar", "".join(t)), encoding="utf-8")
    out = {"generated_for": date, "version": VERSION, "count": len(servers), "with_stars": withstars, "categories": counts, "trends": tr,
           "servers": [{**s, "delta7": d7.get(s["id"]), "delta30": d30.get(s["id"])} for s in servers]}
    (site / "data.json").write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    (site / ".nojekyll").write_text("", encoding="utf-8")
    return {"servers": len(servers), "with_stars": withstars, "categories": len(counts), "snapshots": len(snaps)}


# ---------------------------------------------------------------- cli
def cmd_snapshot(a, client: Client | None = None) -> int:
    root = Path(a.root)
    today = a.date or dt.datetime.now(dt.timezone.utc).date().isoformat()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    servers, info, errs = collect(client or Client(), dt.date.fromisoformat(today), token, a.github_pages, a.registry_pages, a.npm_pages, a.lookups, a.pypi_lookups)
    if info["github_repos"] == 0:
        print("FAILED: GitHub search returned nothing; no snapshot written (a snapshot without GitHub would make every star delta wrong)."); [print("  ", e) for e in errs[:5]]
        return 2
    p = write_snapshot(root, servers, today, info, errs)
    print(f"snapshot {p}: {len(servers)} servers  ({json.dumps(info)})")
    for e in errs[:10]:
        print("FAILED source call:", e)
    return 1 if errs else 0


def cmd_build(a) -> int:
    root = Path(a.root)
    if not (root / "servers.json").exists():
        print("FAILED: no servers.json; run `snapshot` first"); return 1
    r = build_site(root, Path(a.site) if a.site else root / "docs")
    print("built", json.dumps(r)); return 0


def cmd_top(a) -> int:
    root = Path(a.root)
    data = json.loads((root / "servers.json").read_text(encoding="utf-8"))
    tr = trends(load_snapshots(root), data["servers"], data["date"])
    if a.by == "stars":
        rows = [(s["stars"], s["repo"] or s["id"]) for s in data["servers"] if s["stars"] is not None]
    else:
        d = tr["delta7" if a.by == "delta7" else "delta30"]
        if not d:
            print(f"no {a.by} yet: needs a snapshot {7 if a.by == 'delta7' else 30} to {14 if a.by == 'delta7' else 60} days old"); return 0
        names = {s["id"]: s["repo"] or s["id"] for s in data["servers"]}
        rows = [(v, names[k]) for k, v in d.items() if k in names]
    for v, n in sorted(rows, key=lambda r: (-r[0], r[1]))[: a.n]:
        print(f"{v:>9,}  {n}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mcp_radar", description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=".", help="folder holding snapshots/ and servers.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("snapshot", "run"):
        p = sub.add_parser(name)
        p.add_argument("--date"); p.add_argument("--github-pages", type=int, default=10); p.add_argument("--registry-pages", type=int, default=100)
        p.add_argument("--npm-pages", type=int, default=8); p.add_argument("--lookups", type=int, default=300); p.add_argument("--pypi-lookups", type=int, default=150)
        p.add_argument("--site")
    sub.add_parser("build").add_argument("--site")
    t = sub.add_parser("top"); t.add_argument("--by", choices=["stars", "delta7", "delta30"], default="stars"); t.add_argument("-n", type=int, default=20)
    a = ap.parse_args(argv)
    if a.cmd == "snapshot":
        return min(cmd_snapshot(a), 1)
    if a.cmd == "run":
        rc = cmd_snapshot(a)
        return 1 if rc == 2 else (cmd_build(a) or rc)
    return {"build": cmd_build, "top": cmd_top}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
