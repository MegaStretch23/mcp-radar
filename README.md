# mcp-radar

A directory and growth-trend site for public MCP servers, built from public APIs only. Python standard library, no dependencies.

**Live site: https://megastretch23.github.io/mcp-radar/** (directory table, 14 category pages, trends page with inline SVG charts, `data.json`).

The first saved run (2026-11-13) lists **7,589** servers. The site is rebuilt from dated snapshots in `snapshots/`; the 7-day and 30-day star deltas, top risers and sparklines appear once snapshots are that far apart (one snapshot exists today, so those columns are empty until 2026-11-20 and 2026-12-13).

## Quickstart

```bash
git clone https://github.com/MegaStretch23/mcp-radar && cd mcp-radar
python3 mcp_radar.py build              # offline: render docs/ from the committed snapshot (servers.json)
python3 mcp_radar.py top -n 10          # most starred servers in that snapshot
python3 -m http.server -d docs 8000     # open http://127.0.0.1:8000/

GITHUB_TOKEN=<a token with no scopes> python3 mcp_radar.py run   # take a fresh snapshot from the live sources, then rebuild docs/
python3 mcp_radar.py top --by delta7    # after a snapshot 7+ days old exists
pip install pytest && python3 -m pytest -q   # 36 offline tests on recorded fixtures
```

Without `GITHUB_TOKEN` the GitHub search API allows 10 requests a minute and the run waits on the limit; with one it is 30. A full run makes roughly 450 requests one after the other and took several minutes on the machine that made the sample below.

## Sample output (saved run, `samples/run.txt`)

```
$ python mcp_radar.py run
github search: 2635 repositories
github totals: 43137 repositories under topic mcp-server
registry: 3400 latest server entries
npm: 2000 packages
github repo lookups: 242 of 2767 repositories not in the search results
snapshot snapshots/2026-11-13.json: 7589 servers  ({"totals": {...}, "github_repos": 2635, "registry_servers": 3400, "npm_packages": 2000, "pypi_lookups": 49, "pypi_found": 49, "repo_lookups": 242, "repo_lookups_skipped": 2467})
built {"servers": 7589, "with_stars": 2877, "categories": 14, "snapshots": 1}

$ python mcp_radar.py top -n 3
  210,363  n8n-io/n8n
  108,811  google-gemini/gemini-cli
  102,351  D4Vinci/Scrapling

$ python mcp_radar.py top --by delta7
no delta7 yet: needs a snapshot 7 to 14 days old
```

What those numbers are, from that run:

- 7,589 rows: 5,402 are GitHub repositories, 2,187 are registry or npm entries with no GitHub repository. 3,230 rows have an official-registry entry, 2,635 came from the GitHub search, 1,922 have an npm package, 196 are in more than one source.
- 2,877 rows have a star count. The other 4,712 are registry or npm entries whose repository was not looked up (the run caps repository lookups at 300; 242 resolved, the rest 404 or skipped) or that have no GitHub repository. They sort after the starred rows and have no star trend.
- GitHub reports 43,137 repositories under the topic `mcp-server`. The directory holds the 2,635 the three searches returned (the top 1,000 by stars, plus the top 1,000 created in the last 90 days and in the last 30 days), not all 43,137.
- The trends page chart is GitHub's own count per creation month (one `total_count` query per month), not the size of this directory.

## Sources (public APIs, no logins, no scraping)

| source | what it gives | note |
|---|---|---|
| GitHub search API, `topic:mcp-server` | stars, forks, last push, creation date, topics, license | three queries (all time, created in the last 90 days, created in the last 30 days), up to 1,000 results each |
| Official MCP registry, `registry.modelcontextprotocol.io/v0/servers` | name, description, repository, packages, remotes | latest version of each server only |
| npm search API, `keywords:mcp-server` | package, weekly and monthly downloads, repository link | first 2,000 results |
| PyPI JSON API | version and upload time | PyPI has no search API, so it is queried only for the PyPI packages the registry lists (49 in the saved run) |

One row is one repository (case-insensitive). A monorepo that ships several servers or packages is one row. A package with no known repository keeps its own row.

## What it does

- `snapshot` writes `snapshots/<date>.json` (stars, forks, last push, npm weekly downloads per row id) and `servers.json` (the row details).
- `build` renders `docs/`: `index.html` (top 1,000 by stars, with a filter box), `category/<name>.html`, `trends.html`, `data.json` (every row, with 7-day and 30-day star deltas once they exist). Deploy `docs/` to GitHub Pages.
- A delta uses the latest snapshot that is at least 7 (or 30) days old and no more than twice that. The page shows the date and span it used. A row missing from the older snapshot gets no delta rather than its full star count.
- Categories are keyword rules on name, description and topics (first match wins), so they are approximate. See `CATEGORIES` in `mcp_radar.py`.
- All text from the sources is HTML-escaped and only `http(s)` links are written.

## Limits

- "Listed as an MCP server" means the owner applied the topic or published the registry entry. Nothing here runs or tests a server; some starred repositories under the topic (a workflow tool, a CLI) are not MCP servers.
- Stars are GitHub stars. The star deltas are differences between snapshots, not activity or usage.
- The chart and the directory count different things (see above).

## Related, unrelated to this tool

There is no paid version of mcp-radar. Other things I publish: a pay-per-event [SEC Form 4 insider cluster scanner](https://apify.com/johnveds15/sec-form4-insider-clusters) on Apify ($0.0004 per Form 4 filing parsed, $0.02 per insider cluster detected, price read from the Apify API on 2026-11-13), and two source-code packs on Gumroad: [Swarm Simulation Toy, browser source code](https://vedovino.gumroad.com/l/tslqtn) ($9) and [Browser Frame-Interpolation and Upscaling Toolkit, source code](https://vedovino.gumroad.com/l/xrrtln) ($9), prices as listed on 2026-11-13.

## License

MIT, see `LICENSE`.
