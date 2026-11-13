import datetime as dt, json, sys, urllib.error, xml.dom.minidom
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mcp_radar as mr  # noqa: E402

FX = Path(__file__).parent / "fixtures"


def fx(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


def http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, None)


class FakeNet:
    """Routes the exact URLs the tool calls to recorded fixtures; any other URL is a test failure."""

    def __init__(self, gh_pages=1, fail=()):
        self.calls, self.fail = [], set(fail)
        self.gh = fx("github_search.json")

    def __call__(self, url, headers=None):
        self.calls.append(url)
        for key in self.fail:
            if key in url:
                raise http_error(500)
        if "api.github.com/search/repositories" in url:
            if url.endswith("per_page=1"):
                return {"total_count": 5000 if "created" not in url else 7}
            return self.gh
        if "api.github.com/repos/" in url:
            repo = fx("github_repo.json")
            if "missing" in url:
                raise http_error(404)
            return repo
        if "registry.modelcontextprotocol.io" in url:
            return fx("registry_page.json")
        if "registry.npmjs.org" in url:
            return fx("npm_search.json")
        if "pypi.org/pypi/" in url:
            return fx("pypi_mcp.json")
        raise AssertionError("unexpected URL " + url)


def collect(net=None, **kw):
    net = net or FakeNet()
    args = dict(gh_pages=1, reg_pages=2, npm_pages=1, lookups=50, pypi_lookups=10)
    args.update(kw)
    return mr.collect(mr.Client(fetch=net), dt.date(2026, 11, 13), "tok", **args), net


# ---- helpers
@pytest.mark.parametrize("url,want", [
    ("git+https://github.com/callstack/agent-device.git", "callstack/agent-device"),
    ("https://github.com/o/r/tree/main/sub", "o/r"), ("git@github.com:o/r.git", "o/r"),
    ("https://gitlab.com/o/r", None), (None, None), ("", None)])
def test_github_repo(url, want):
    assert mr.github_repo(url) == want


def test_safe_url_and_clean_text():
    assert mr.safe_url("https://a.example/x") and not mr.safe_url("javascript:alert(1)") and not mr.safe_url('https://a.example/"><script>')
    assert mr.clean_text("a\n\n  b") == "a b" and len(mr.clean_text("x" * 500)) == 220 and mr.clean_text(None) == ""


def test_categorize():
    assert mr.categorize("Playwright browser control") == "browser-automation"
    assert mr.categorize("Query your postgres database") == "databases"
    assert mr.categorize("Send a Slack message") == "communication"
    assert mr.categorize("nothing relevant here") == "other"
    assert mr.categorize("A GitHub issue helper") == "developer-tools"


# ---- sources
def test_github_fetch_records():
    recs, errs = mr.fetch_github(mr.Client(fetch=FakeNet()), "tok", dt.date(2026, 11, 13), 1)
    assert not errs and len(recs) == 6   # the three queries return the same fixture; keyed by repo name
    r = recs[0]
    assert r["repo"] == "n8n-io/n8n" and r["stars"] > 1000 and r["url"].startswith("https://github.com/")


def test_github_three_queries_issued():
    net = FakeNet(); mr.fetch_github(mr.Client(fetch=net), None, dt.date(2026, 11, 13), 1)
    qs = [u for u in net.calls if "search/repositories" in u]
    qs = [u for u in qs if not u.endswith("per_page=1")]
    assert len(qs) == 3 and any("created%3A%3E%3D2026-08-15" in u for u in qs) and any("created%3A%3E%3D2026-10-14" in u for u in qs)


def test_github_pagination_stops_at_1000_window():
    full = {"items": [dict(fx("github_search.json")["items"][0], full_name=f"o/r{i}") for i in range(100)]}

    def fetch(url, h):
        if "&page=3" in url:
            raise http_error(422)
        return full if "&page=1" in url else {"items": [dict(full["items"][0], full_name=f"p/r{i}") for i in range(100)]}
    recs, errs = mr.fetch_github(mr.Client(fetch=fetch), None, dt.date(2026, 11, 13), 5)
    assert len(recs) == 200 and not errs


def test_github_error_is_reported_not_swallowed():
    recs, errs = mr.fetch_github(mr.Client(fetch=FakeNet(fail=["search/repositories"])), None, dt.date(2026, 11, 13), 1)
    assert recs == [] and len(errs) == 3 and "HTTP 500" in errs[0]


def test_registry_keeps_latest_only_and_parses_packages():
    recs, errs = mr.fetch_registry(mr.Client(fetch=FakeNet()), 3)
    names = [r["registry_name"] for r in recs]
    assert not errs and "ac.inference.sh/mcp" not in names   # the only row for it in the fixture is a non-latest version
    npm_row = next(r for r in recs if r["registry_name"] == "agency.extremely/agency")
    assert npm_row["repo"] == "maxkxd/extremely-autonomous-agents" and npm_row["packages"] == [{"type": "npm", "id": "@extremely-agency/mcp"}]


def test_registry_follows_cursor():
    pages = [{"servers": fx("registry_page.json")["servers"][1:2], "metadata": {"nextCursor": "c1"}}, {"servers": fx("registry_page.json")["servers"][2:3], "metadata": {}}]
    seen = []

    def fetch(url, h):
        seen.append(url); return pages[len(seen) - 1]
    recs, _ = mr.fetch_registry(mr.Client(fetch=fetch), 10)
    assert len(recs) == 2 and "cursor=c1" in seen[1]


def test_npm_parse():
    recs, errs = mr.fetch_npm(mr.Client(fetch=FakeNet()), 3)
    assert not errs and len(recs) == 5
    r = next(x for x in recs if x["npm"] == "@zereight/mcp-gitlab")
    assert r["repo"] == "zereight/gitlab-mcp" and r["weekly"] > 0


def test_pypi_parse_and_404():
    c = mr.Client(fetch=lambda u, h: fx("pypi_mcp.json") if u.endswith("/mcp/json") else (_ for _ in ()).throw(http_error(404)))
    out, errs = mr.fetch_pypi(c, ["mcp", "gone"])
    assert out["mcp"]["version"] and out["mcp"]["repo"] == "modelcontextprotocol/python-sdk" and "gone" not in out and not errs


def test_client_retries_rate_limit():
    waits, n = [], {"i": 0}

    class H(dict):
        pass

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, *a): return b'{"ok": 1}'

    def fake_open(req, timeout=0):
        n["i"] += 1
        if n["i"] < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "x", {"Retry-After": "7"}, None)
        return FakeResp()
    orig = mr.urllib.request.urlopen
    mr.urllib.request.urlopen = fake_open
    try:
        assert mr.Client(sleep=waits.append).fetch("http://x") == {"ok": 1}
    finally:
        mr.urllib.request.urlopen = orig
    assert waits == [9, 9]


# ---- merge
def test_collect_merges_by_repo():
    (servers, info, errs), net = collect()
    ids = [s["id"] for s in servers]
    assert not errs and len(ids) == len(set(ids))
    assert info["github_repos"] == 6 and info["registry_servers"] == 4 and info["npm_packages"] == 5
    by = {s["id"]: s for s in servers}
    agent = by["gh:callstack/agent-device"]   # npm package whose repository link is on GitHub: one row, npm numbers attached
    assert agent["npm"]["weekly"] > 0 and "npm" in agent["sources"]
    reg_only = [s for s in servers if s["registry_names"] and s["id"].startswith("gh:maxkxd")]
    assert reg_only and reg_only[0]["stars"] is not None   # stars via the single-repo lookup
    assert all(s["category"] in mr.CAT_LABEL for s in servers)
    assert servers == sorted(servers, key=lambda s: (-(s["stars"] if s["stars"] is not None else -1), s["id"]))


def test_pypi_attached_to_registry_row():
    (servers, _, _), _ = collect()
    row = next(s for s in servers if "ai.anomalyarmor/armor-mcp" in s["registry_names"])
    assert row["pypi"]["name"] == "armor-mcp"


def test_repo_lookup_404_is_skipped():
    net = FakeNet()
    (servers, info, errs), _ = collect(net, lookups=0)
    assert info["repo_lookups"] == 0 and info["repo_lookups_skipped"] > 0 and not errs


def test_source_failure_is_listed_in_errors():
    (servers, info, errs), _ = collect(FakeNet(fail=["registry.npmjs.org"]))
    assert info["npm_packages"] == 0 and any(e.startswith("npm search") for e in errs) and servers


# ---- snapshots + trends
def mk(sid, stars, **kw):
    return {"id": sid, "name": sid, "repo": sid.split(":")[1], "stars": stars, "forks": 1, "pushed_at": "2026-11-01T00:00:00Z", "created_at": kw.get("created", "2026-01-05T00:00:00Z"),
            "description": "d", "url": "https://github.com/" + sid.split(":")[1], "topics": [], "language": None, "archived": False, "license": None, "sources": ["github"],
            "registry_names": [], "npm": None, "pypi": None, "category": "other"}


def snap(date, stars):
    return {"date": date, "servers": {k: {"s": v, "f": 0, "p": None, "n": None} for k, v in stars.items()}}


def test_baseline_window():
    snaps = [snap("2026-10-01", {}), snap("2026-11-05", {}), snap("2026-11-10", {})]
    assert mr.baseline(snaps, "2026-11-13", 7)["date"] == "2026-11-05"   # 8 days old, the latest one that is >= 7 days old
    assert mr.baseline(snaps, "2026-11-13", 30)["date"] == "2026-10-01"   # 43 days old: within 2x30
    assert mr.baseline([snap("2026-11-12", {})], "2026-11-13", 7) is None   # too fresh
    assert mr.baseline([snap("2026-08-01", {})], "2026-11-13", 30) is None   # older than 2x: refuse to call it a 30-day delta


def test_trends_deltas_risers_and_new():
    servers = [mk("gh:a/a", 150), mk("gh:b/b", 30), mk("gh:c/c", 90, created="2026-11-01T00:00:00Z"), mk("gh:d/d", None)]
    snaps = [snap("2026-11-01", {"gh:a/a": 100, "gh:b/b": 40, "gh:d/d": None}), snap("2026-11-13", {"gh:a/a": 150, "gh:b/b": 30, "gh:c/c": 90, "gh:d/d": None})]
    t = mr.trends(snaps, servers, "2026-11-13")
    assert t["baseline7"]["span_days"] == 12 and t["baseline30"] is None and t["risers30"] == []
    assert t["delta7"] == {"gh:a/a": 50, "gh:b/b": -10}   # c is new, d has no stars: no delta
    assert [r["id"] for r in t["risers7"]] == ["gh:a/a"]   # only positive gains
    assert [r["id"] for r in t["new_in_snapshot"]] == ["gh:c/c"]
    assert t["created_30d_in_directory"] == 1 and t["created_by_month"] == {} and t["github_topic_total"] is None


def test_trends_reads_totals_from_current_snapshot():
    cur = snap("2026-11-13", {"gh:a/a": 5}); cur["sources"] = {"totals": {"topic_total": 99, "created_30d": 12, "monthly": {"2026-10": 8}}}
    t = mr.trends([cur], [mk("gh:a/a", 5)], "2026-11-13")
    assert (t["github_topic_total"], t["created_30d_total"], t["created_by_month"]) == (99, 12, {"2026-10": 8})


def test_month_ranges():
    r = mr.month_ranges(dt.date(2025, 2, 10))
    assert r[0] == ("2024-11", "2024-11-01", "2024-11-30") and r[1][2] == "2024-12-31" and r[-1] == ("2025-02", "2025-02-01", "2025-02-28") and len(r) == 4
    assert mr.month_ranges(dt.date(2024, 10, 1)) == []


def test_fetch_totals_and_partial_failure():
    tot, errs = mr.fetch_totals(mr.Client(fetch=FakeNet()), None, dt.date(2026, 11, 13))
    assert not errs and tot["topic_total"] == 5000 and tot["created_30d"] == 7 and list(tot["monthly"])[0] == "2024-11" and len(tot["monthly"]) == 25
    calls = {"n": 0}

    def flaky(url, h):
        calls["n"] += 1
        if calls["n"] == 3:
            raise http_error(500)
        return {"total_count": 4}
    tot, errs = mr.fetch_totals(mr.Client(fetch=flaky), None, dt.date(2026, 11, 13))
    assert len(errs) == 1 and len(tot["monthly"]) == 24   # the failed month is absent, never zero


def test_trends_first_snapshot_has_no_deltas():
    t = mr.trends([snap("2026-11-13", {"gh:a/a": 5})], [mk("gh:a/a", 5)], "2026-11-13")
    assert t["baseline7"] is None and t["delta7"] == {} and t["new_in_snapshot"] is None


def test_snapshot_roundtrip(tmp_path):
    servers = [mk("gh:a/a", 5)]
    p = mr.write_snapshot(tmp_path, servers, "2026-11-13", {"x": 1}, [])
    assert json.loads(p.read_text())["servers"]["gh:a/a"]["s"] == 5
    assert mr.load_snapshots(tmp_path)[0]["date"] == "2026-11-13"
    (tmp_path / "snapshots" / "bad.json").write_text("{nope")
    (tmp_path / "snapshots" / "2026-01-01.json").write_text(json.dumps({"date": "../x", "servers": {}}))
    assert len(mr.load_snapshots(tmp_path)) == 1   # corrupt or mis-dated files are ignored


# ---- render
def test_svg_valid_xml():
    xml.dom.minidom.parseString(mr.svg_bars({"2026-01": 3, "2026-02": 9, "2026-03": 1}))
    xml.dom.minidom.parseString(mr.svg_spark([("2026-11-01", 5), ("2026-11-02", 9), ("2026-11-03", 9)]))
    assert mr.svg_spark([("2026-11-01", 5)]) == "" and mr.svg_bars({}) == ""
    xml.dom.minidom.parseString(mr.svg_spark([("a", 3), ("b", 3)]))   # flat series: no divide by zero


def test_build_site_escapes_untrusted_text(tmp_path):
    evil = mk("gh:x/x", 5); evil["description"] = '<script>alert(1)</script>"><img src=x onerror=1>'; evil["name"] = "<b>n</b>"
    (tmp_path / "servers.json").write_text(json.dumps({"date": "2026-11-13", "servers": [evil, mk("gh:y/y", 2)]}))
    mr.write_snapshot(tmp_path, [evil, mk("gh:y/y", 2)], "2026-11-13", {}, [])
    mr.build_site(tmp_path, tmp_path / "site")
    page = (tmp_path / "site" / "index.html").read_text()
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)" in page and "<b>n</b>" not in page and "onerror=1>" not in page


def test_build_site_files_and_data(tmp_path):
    (servers, info, errs), _ = collect()
    mr.write_snapshot(tmp_path, servers, "2026-11-13", info, errs)
    r = mr.build_site(tmp_path, tmp_path / "docs")
    assert r["servers"] == len(servers)
    for f in ("index.html", "trends.html", "data.json", ".nojekyll"):
        assert (tmp_path / "docs" / f).exists()
    cats = {s["category"] for s in servers}
    assert {p.stem for p in (tmp_path / "docs" / "category").glob("*.html")} == cats
    d = json.loads((tmp_path / "docs" / "data.json").read_text())
    assert d["count"] == len(servers) and len(d["servers"]) == len(servers) and d["trends"]["baseline7"] is None
    idx = (tmp_path / "docs" / "index.html").read_text()
    assert f"{len(servers):,} public MCP servers" in idx and "GitHub lists 5,000 repositories under the topic mcp-server" in idx and "fill from 2026-11-20" in idx
    assert idx.count("<tr>") == len(servers) + 1
    assert "GitHub repositories tagged mcp-server, by creation month" in (tmp_path / "docs" / "trends.html").read_text()
    assert 'href="category/' in idx and 'href="../index.html"' in (tmp_path / "docs" / "category" / f"{next(iter(cats))}.html").read_text()


def test_index_row_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "INDEX_ROWS", 3); monkeypatch.setattr(mr, "CAT_ROWS", 2)
    servers = [mk(f"gh:o/r{i}", 100 - i) for i in range(6)]
    mr.write_snapshot(tmp_path, servers, "2026-11-13", {}, [])
    mr.build_site(tmp_path, tmp_path / "docs")
    assert (tmp_path / "docs" / "index.html").read_text().count("<tr>") == 4 and "Showing the top 3 by stars" in (tmp_path / "docs" / "index.html").read_text()
    other = (tmp_path / "docs" / "category" / "other.html").read_text()
    assert other.count("<tr>") == 3 and "the top 2 are listed here" in other
    assert json.loads((tmp_path / "docs" / "data.json").read_text())["count"] == 6


def test_build_with_history_shows_deltas_and_sparklines(tmp_path):
    servers = [mk("gh:a/a", 150), mk("gh:b/b", 30)]
    (tmp_path / "servers.json").write_text(json.dumps({"date": "2026-11-13", "servers": servers}))
    (tmp_path / "snapshots").mkdir()
    for s in (snap("2026-11-05", {"gh:a/a": 100, "gh:b/b": 31}), snap("2026-11-13", {"gh:a/a": 150, "gh:b/b": 30})):
        (tmp_path / "snapshots" / f"{s['date']}.json").write_text(json.dumps(s))
    mr.build_site(tmp_path, tmp_path / "docs")
    idx = (tmp_path / "docs" / "index.html").read_text()
    assert '<span class="up">+50</span>' in idx and "<polyline" in idx
    tr = (tmp_path / "docs" / "trends.html").read_text()
    assert "Star gain since snapshot 2026-11-05 (8 days ago)" in tr and "No snapshot that is 30 to 60 days old yet" in tr
    d = json.loads((tmp_path / "docs" / "data.json").read_text())
    assert d["servers"][0]["delta7"] == 50


# ---- cli
def test_cli_snapshot_build_top(tmp_path, capsys):
    net = FakeNet()
    a = type("A", (), dict(root=str(tmp_path), date="2026-11-13", github_pages=1, registry_pages=2, npm_pages=1, lookups=50, pypi_lookups=5))()
    assert mr.cmd_snapshot(a, mr.Client(fetch=net)) == 0
    assert mr.main(["--root", str(tmp_path), "build"]) == 0
    assert mr.main(["--root", str(tmp_path), "top", "-n", "2"]) == 0
    out = capsys.readouterr().out
    assert "snapshot" in out and "built" in out and "n8n-io/n8n" in out
    assert mr.main(["--root", str(tmp_path), "top", "--by", "delta7"]) == 0
    assert "no delta7 yet" in capsys.readouterr().out


def test_cli_github_down_writes_nothing(tmp_path, capsys):
    a = type("A", (), dict(root=str(tmp_path), date="2026-11-13", github_pages=1, registry_pages=2, npm_pages=1, lookups=5, pypi_lookups=5))()
    assert mr.cmd_snapshot(a, mr.Client(fetch=FakeNet(fail=["search/repositories"]))) == 2
    assert "FAILED" in capsys.readouterr().out and not (tmp_path / "snapshots").exists()


def test_cli_partial_failure_exit_nonzero_but_writes(tmp_path, capsys):
    a = type("A", (), dict(root=str(tmp_path), date="2026-11-13", github_pages=1, registry_pages=2, npm_pages=1, lookups=5, pypi_lookups=5))()
    assert mr.cmd_snapshot(a, mr.Client(fetch=FakeNet(fail=["registry.modelcontextprotocol.io"]))) == 1
    assert "FAILED source call" in capsys.readouterr().out and (tmp_path / "snapshots" / "2026-11-13.json").exists()
