"""Portal-health dashboard.

A static page the hosted workflow publishes after every scan, so "is
everything still working" is a browser refresh rather than a trip through the
Actions log. Generated from the database alone: no network access, no secrets,
and nothing here that is not already in the public run logs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.enums import ScrapeStatus
from app.models.job import Job
from app.models.match import JobMatch
from app.models.scrape_run import ScrapeRun
from app.normalization.dates import humanize_age
from app.repositories.company import CompanyRepository
from app.repositories.scrape_run import ScrapeRunRepository
from app.repositories.user import UserProfileRepository
from app.utils.time import as_aware, utcnow

#: A portal whose last run is older than this is "stale" even if that run
#: succeeded: the scheduler has stopped reaching it, which is its own failure.
_STALE_AFTER = timedelta(hours=3)


def build_summary(session: Session, *, match_threshold: float) -> dict[str, Any]:
    """Everything the page shows, as plain data.

    Kept separate from rendering so the same dict can be written as JSON —
    a future dashboard elsewhere reads that rather than scraping the HTML.
    """
    now = utcnow()
    companies = CompanyRepository(session).list_filtered(is_active=True, limit=500)
    runs = ScrapeRunRepository(session)
    profiles = UserProfileRepository(session).list_all()
    profile_id = profiles[0].id if profiles else None

    stored = dict(
        session.execute(
            sa.select(Job.company_id, sa.func.count())
            .where(Job.is_active.is_(True))
            .group_by(Job.company_id)
        ).all()
    )
    matched: dict[Any, int] = {}
    if profile_id is not None:
        matched = dict(
            session.execute(
                sa.select(Job.company_id, sa.func.count())
                .join(JobMatch, JobMatch.job_id == Job.id)
                .where(
                    JobMatch.profile_id == profile_id,
                    JobMatch.score >= match_threshold,
                    Job.is_active.is_(True),
                )
                .group_by(Job.company_id)
            ).all()
        )

    portals = [
        _portal_row(c, runs.latest_for_company(c.id), stored, matched, now) for c in companies
    ]
    portals.sort(key=lambda p: (p["health"] != "failing", p["health"] != "stale", p["name"]))

    return {
        "generated_at": now.isoformat(),
        "match_threshold": match_threshold,
        "totals": {
            "portals": len(portals),
            "working": sum(1 for p in portals if p["health"] == "ok"),
            "failing": sum(1 for p in portals if p["health"] == "failing"),
            "stale": sum(1 for p in portals if p["health"] == "stale"),
            "jobs_stored": sum(stored.values()),
            "matches": sum(matched.values()),
        },
        "portals": portals,
    }


def _portal_row(
    company: Company,
    last: ScrapeRun | None,
    stored: dict[Any, int],
    matched: dict[Any, int],
    now: datetime,
) -> dict[str, Any]:
    started = as_aware(last.started_at) if last else None
    if last is None or started is None:
        health = "never"
    elif last.status is ScrapeStatus.FAILED:
        health = "failing"
    elif now - started > _STALE_AFTER:
        health = "stale"
    else:
        health = "ok"

    return {
        "name": company.name,
        "url": company.career_url,
        "ats": str(company.ats_type),
        "health": health,
        "last_run_at": started.isoformat() if started else None,
        "last_run_age": humanize_age(started, now=now) if started else "never",
        "last_status": str(last.status) if last else None,
        "tier": str(last.extraction_tier) if last and last.extraction_tier else None,
        "jobs_found": last.jobs_found if last else 0,
        "jobs_new": last.jobs_new if last else 0,
        "duration_s": round(last.total_ms / 1000, 1) if last and last.total_ms else None,
        "jobs_stored": stored.get(company.id, 0),
        "matches": matched.get(company.id, 0),
        "consecutive_failures": company.consecutive_failures,
        # One line, so a wall of traceback never lands on the page.
        "error": (last.error or "").strip().splitlines()[0][:160] if last and last.error else None,
    }


def write_site(summary: dict[str, Any], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "data.json").write_text(json.dumps(summary, indent=2))
    (out / "index.html").write_text(render_html(summary))
    # GitHub Pages runs Jekyll by default and drops files it decides are
    # internal; this file switches that off.
    (out / ".nojekyll").write_text("")


# --- Rendering ---------------------------------------------------------------

_HEALTH_LABEL = {
    "ok": "working",
    "stale": "stale",
    "failing": "failing",
    "never": "not yet run",
}


def render_html(summary: dict[str, Any]) -> str:
    t = summary["totals"]
    generated = datetime.fromisoformat(summary["generated_at"])
    rows = "\n".join(_row_html(p) for p in summary["portals"])
    banner_class = "bad" if t["failing"] else ("warn" if t["stale"] else "good")
    return _PAGE.format(
        banner_class=banner_class,
        working=t["working"],
        portals=t["portals"],
        failing=t["failing"],
        stale=t["stale"],
        jobs=f"{t['jobs_stored']:,}",
        matches=t["matches"],
        threshold=summary["match_threshold"],
        generated=generated.strftime("%d %b %Y, %H:%M UTC"),
        generated_iso=escape(summary["generated_at"]),
        rows=rows,
    )


def _row_html(p: dict[str, Any]) -> str:
    detail = ""
    if p["error"]:
        detail = f'<div class="err">{escape(p["error"])}</div>'
    elif p["tier"]:
        detail = f'<div class="meta">via {escape(p["tier"])}</div>'
    duration = f'{p["duration_s"]}s' if p["duration_s"] is not None else "–"
    new = f'<span class="new">+{p["jobs_new"]}</span>' if p["jobs_new"] else ""
    return (
        f'<tr class="{p["health"]}">'
        f'<td><span class="dot"></span>{_HEALTH_LABEL[p["health"]]}</td>'
        f'<td><a href="{escape(p["url"])}" rel="noopener">{escape(p["name"])}</a>'
        f'<div class="meta">{escape(p["ats"])}</div></td>'
        f'<td class="num">{p["jobs_found"]} {new}{detail}</td>'
        f'<td class="num">{p["jobs_stored"]}</td>'
        f'<td class="num match">{p["matches"] or ""}</td>'
        f'<td class="age" title="{escape(p["last_run_at"] or "")}">{escape(p["last_run_age"])}'
        f'<div class="meta">{duration}</div></td>'
        f"</tr>"
    )


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>Portal health</title>
<style>
 :root{{--bg:#fafafa;--fg:#111;--mute:#6b6b70;--line:#e4e4e8;--card:#fff;
       --ok:#137333;--warn:#b45309;--bad:#b3261e;--link:#1a56db}}
 @media (prefers-color-scheme:dark){{
   :root{{--bg:#161618;--fg:#e8e8ea;--mute:#9a9aa0;--line:#2c2c30;--card:#1e1e21;
          --ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--link:#7aa7ff}}}}
 body{{font-family:system-ui,-apple-system,sans-serif;margin:0 auto;max-width:960px;
      padding:1.5rem 1rem 3rem;color:var(--fg);background:var(--bg)}}
 h1{{font-size:1.3rem;margin:0 0 .25rem}}
 .sub{{color:var(--mute);font-size:.9rem;margin-bottom:1.25rem}}
 .banner{{border-radius:10px;padding:.9rem 1.1rem;margin-bottom:1.25rem;font-weight:600;
   background:var(--card);border-left:5px solid var(--ok)}}
 .banner.warn{{border-left-color:var(--warn)}} .banner.bad{{border-left-color:var(--bad)}}
 .banner small{{display:block;font-weight:400;color:var(--mute);margin-top:.2rem}}
 .tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.6rem;
   margin-bottom:1.5rem}}
 .tile{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.7rem .9rem}}
 .tile b{{display:block;font-size:1.5rem;font-variant-numeric:tabular-nums}}
 .tile span{{color:var(--mute);font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}}
 table{{border-collapse:collapse;width:100%;background:var(--card);border-radius:10px;
   overflow:hidden;box-shadow:0 1px 3px #0001}}
 th,td{{padding:.6rem .75rem;border-bottom:1px solid var(--line);text-align:left;
   vertical-align:top}}
 th{{font-size:.7rem;text-transform:uppercase;letter-spacing:.04em;color:var(--mute)}}
 td.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
 th.num{{text-align:right}}
 td.match{{color:var(--ok);font-weight:600}}
 .dot{{display:inline-block;width:.6rem;height:.6rem;border-radius:50%;margin-right:.45rem;
   background:var(--mute);vertical-align:middle}}
 tr.ok .dot{{background:var(--ok)}} tr.stale .dot{{background:var(--warn)}}
 tr.failing .dot{{background:var(--bad)}}
 tr.failing td:first-child{{color:var(--bad);font-weight:600}}
 tr.stale td:first-child{{color:var(--warn)}}
 .meta{{color:var(--mute);font-size:.78rem;margin-top:.15rem}}
 .err{{color:var(--bad);font-size:.78rem;margin-top:.15rem;max-width:34ch;white-space:normal}}
 .new{{color:var(--ok);font-size:.8rem;font-weight:600}}
 .age{{white-space:nowrap;color:var(--mute);font-size:.88rem}}
 a{{color:var(--link);text-decoration:none}} a:hover{{text-decoration:underline}}
 .foot{{color:var(--mute);font-size:.8rem;margin-top:1.25rem}}
 @media (max-width:560px){{ th:nth-child(4),td:nth-child(4){{display:none}} }}
</style></head><body>
<h1>Portal health</h1>
<div class="sub">Every portal in <code>config/portals.txt</code>, as of the last scan.
 Refresh for the latest; the page also reloads itself every 15 minutes.</div>

<div class="banner {banner_class}">{working} of {portals} portals working
 <small>{failing} failing · {stale} stale · last scan {generated}</small></div>

<div class="tiles">
 <div class="tile"><b>{jobs}</b><span>jobs tracked</span></div>
 <div class="tile"><b>{matches}</b><span>matches ≥ {threshold}</span></div>
 <div class="tile"><b>{working}</b><span>portals working</span></div>
 <div class="tile"><b>{failing}</b><span>portals failing</span></div>
</div>

<table>
<thead><tr><th>Status</th><th>Portal</th><th class="num">Found last run</th>
<th class="num">Tracked</th><th class="num">Matches</th><th>Last run</th></tr></thead>
<tbody>
{rows}
</tbody></table>

<div class="foot">Full match report: <a href="results.html">results.html</a> ·
 machine-readable: <a href="data.json">data.json</a> ·
 <time datetime="{generated_iso}">generated {generated}</time></div>
</body></html>"""
