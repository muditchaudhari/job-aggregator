"""Command line entry point.

    make run        scan every portal in config/portals.txt and print results

Runs everything in this one process — no worker, no scheduler, no queue. The
Celery path exists for running unattended later; this is the one you use while
you are still deciding whether the thing fetches your portals correctly.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import PlatformError
from app.core.logging import configure_logging
from app.database.session import ensure_schema, session_scope
from app.models.company import Company
from app.models.enums import ATSType, ScrapeFrequency
from app.models.user import UserProfile
from app.normalization.dates import humanize_age
from app.repositories.company import CompanyRepository
from app.repositories.job import JobRepository
from app.repositories.match import JobMatchRepository
from app.repositories.user import UserProfileRepository, UserRepository
from app.scrapers.detection import detect
from app.scrapers.fetcher import Fetcher
from app.services.company import CompanyService
from app.services.scan import ScanService
from app.userconfig import ConfigError, UserConfig, load, profile_fields
from app.utils.time import utcnow
from app.utils.urls import canonicalize_url

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"
)
RULE = "─" * 78


# --- setup ----------------------------------------------------------------


def _sync_profile(session: Session, config: UserConfig) -> UserProfile:
    users = UserRepository(session)
    profiles = UserProfileRepository(session)

    user = users.get_or_create(config.email, config.full_name)
    if config.full_name:
        user.full_name = config.full_name

    fields = profile_fields(config)
    existing = profiles.get_for_user(user.id)
    if existing is None:
        return profiles.add(UserProfile(user_id=user.id, **fields))
    return profiles.update(existing, **fields)


def _sync_portals(session: Session, config: UserConfig) -> list[Company]:
    """Register anything new, and return the portals in file order."""
    service = CompanyService(session)
    repository = CompanyRepository(session)
    companies: list[Company] = []

    for portal in config.portals:
        canonical = canonicalize_url(portal.url)
        existing = repository.get_by_url(canonical)
        if existing is None:
            existing = service.register(career_url=canonical, name=portal.name)
        elif portal.name and existing.name != portal.name:
            repository.update(existing, name=portal.name)

        # Keep the cadence in step with preferences.yml. Without this the
        # interval is whatever it was when the portal was first registered,
        # and editing the file would appear to do nothing.
        if existing.scrape_interval_minutes != config.scan_every_minutes:
            repository.update(
                existing,
                scrape_frequency=ScrapeFrequency.CUSTOM,
                scrape_interval_minutes=config.scan_every_minutes,
            )
        companies.append(existing)

    session.flush()
    return companies


# --- run ------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    try:
        config = load(Path(args.config))
    except ConfigError as exc:
        print(f"{RED}config error{RESET}: {exc}", file=sys.stderr)
        return 2

    settings = get_settings()

    # Semantic scoring is off unless asked for. It costs one model call per
    # shortlisted job, and on Gemini's free tier (~10/min) five portals spend
    # most of a run asleep in rate-limit backoff. The rule matcher scores
    # everything instantly and for free, which is what you want while you are
    # still checking that the portals fetch at all.
    settings.match_semantic_enabled = args.semantic
    scoring = "semantic (slow, uses the LLM)" if args.semantic else "rules (fast, free)"

    print(f"\n{BOLD}Job scan{RESET}")
    print(f"{DIM}  {len(config.portals)} portals · {len(config.skills)} skills · "
          f"threshold {config.match_threshold} · scoring: {scoring}{RESET}")
    if args.semantic:
        print(f"{DIM}  llm {settings.llm_provider}/{settings.llm_model}{RESET}")
    print()

    rows: list[dict[str, Any]] = []

    with session_scope() as session:
        profile = _sync_profile(session, config)
        companies = _sync_portals(session, config)
        profile_id = profile.id

        if args.due:
            # Skip portals whose own interval has not elapsed. The database
            # already knows when each was last scanned, so a repeat `make run`
            # costs nothing for boards that cannot have changed much.
            fresh = [c for c in companies if c.next_scrape_at and c.next_scrape_at > utcnow()]
            for company in fresh:
                due_in = company.next_scrape_at - utcnow()
                hours = due_in.total_seconds() / 3600
                print(f"{DIM}[skip] {company.name} — scanned recently, due in "
                      f"{hours:.1f}h{RESET}")
                rows.append({
                    "name": company.name, "ats": str(company.ats_type), "found": 0,
                    "new": 0, "tier": "skipped", "llm": 0, "secs": 0.0, "error": None,
                })
            companies = [c for c in companies if c not in fresh]
            if fresh:
                print()

        targets = [(c.id, c.name) for c in companies]

    # Each portal gets its own thread, session and HTTP client. Companies are
    # independent, and the rate limiter is keyed per *domain*, so running them
    # together stays as polite to each site while turning a sum of latencies
    # into a maximum. Serial scanning was the whole reason a run felt hung.
    workers = max(1, min(args.jobs, len(targets))) if targets else 1
    print(f"{DIM}  scanning {len(targets)} portals, {workers} at a time{RESET}\n")

    lock = threading.Lock()
    done = 0

    def scan_one(company_id: Any, name: str) -> dict[str, Any]:
        nonlocal done
        started = time.monotonic()
        try:
            with session_scope() as session, Fetcher() as fetcher:
                if args.redetect or _needs_detection(session, company_id):
                    _detect_into(session, company_id, fetcher)
                report = ScanService(session, fetcher=fetcher).scan_company(
                    company_id, force_llm=args.force_llm, notify=args.notify
                )
                row = {
                    "name": name, "ats": report.extraction_tier or "-",
                    "found": report.jobs_found, "new": report.jobs_new,
                    "tier": report.extraction_tier or "-", "llm": report.llm_calls,
                    "secs": report.duration_ms / 1000, "error": report.error,
                    "ok": report.succeeded, "baseline": report.baseline,
                }
        except Exception as exc:
            row = {
                "name": name, "ats": "-", "found": 0, "new": 0, "tier": "-",
                "llm": 0, "secs": time.monotonic() - started,
                "error": f"{type(exc).__name__}: {exc}"[:70], "ok": False,
                "baseline": False,
            }
        with lock:
            done += 1
            mark = GREEN + "ok" + RESET if row["ok"] else RED + "fail" + RESET
            detail = (
                f'{row["found"]} jobs, {row["new"]} new'
                if row["ok"]
                else str(row["error"])[:52]
            )
            print(
                f'  [{done}/{len(targets)}] {mark}  {row["name"][:18]:20}'
                f'{detail:34} {DIM}{row["secs"]:.1f}s{RESET}'
            )
        return row

    if targets:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(scan_one, cid, name) for cid, name in targets]
            rows.extend(future.result() for future in as_completed(futures))
        rows.sort(key=lambda r: -r["found"])

    with session_scope() as session:
        profile = _sync_profile(session, config)
        profile_id = profile.id
        _fill_ats(session, rows)
        _print_summary(rows)
        _print_matches(session, profile_id, config, limit=args.top)

    scanned = [r for r in rows if r["tier"] != "skipped"]
    return 0 if (not scanned or any(r["found"] for r in scanned)) else 1


def _needs_detection(session: Session, company_id: Any) -> bool:
    company = CompanyRepository(session).get(company_id)
    return company is not None and company.ats_type is ATSType.UNKNOWN


def _detect_into(session: Session, company_id: Any, fetcher: Fetcher) -> None:
    company = CompanyRepository(session).get(company_id)
    if company is None:
        return
    try:
        found = detect(company.career_url, fetcher)
    except PlatformError:
        return
    company.ats_type = found.ats_type
    company.scraping_strategy = found.strategy
    if found.board_token:
        company.board_token = found.board_token
    session.flush()


def _fill_ats(session: Session, rows: list[dict[str, Any]]) -> None:
    """Label each row with the platform we ended up using."""
    by_name = {c.name: c for c in CompanyRepository(session).list_filtered(limit=500)}
    for row in rows:
        company = by_name.get(row["name"])
        if company is not None:
            row["ats"] = str(company.ats_type)


def _print_summary(rows: list[dict[str, Any]]) -> None:
    print(RULE)
    header = (f"{'PORTAL':22}{'TYPE':17}{'JOBS':>6}{'NEW':>6}"
              f"{'TIER':>15}{'LLM':>5}{'TIME':>7}")
    print(f"{BOLD}{header}{RESET}")
    print(RULE)
    for row in rows:
        print(f"{row['name'][:21]:22}{row['ats'][:16]:17}{row['found']:>6}{row['new']:>6}"
              f"{str(row['tier'])[:14]:>15}{row['llm']:>5}{row['secs']:>6.1f}s")
        if row.get("error"):
            print(f"  {RED}└─ {str(row['error'])[:70]}{RESET}")
    print(RULE)

    scanned = [r for r in rows if r["tier"] != "skipped"]
    ok = sum(1 for r in scanned if r["found"])
    print(f"{BOLD}{ok}/{len(scanned)} portals returned jobs · "
          f"{sum(r['found'] for r in rows)} found · {sum(r['new'] for r in rows)} new · "
          f"{sum(r['llm'] for r in rows)} LLM calls{RESET}\n")


def _print_matches(session: Session, profile_id: Any, config: UserConfig, *, limit: int) -> None:
    jobs, _ = JobRepository(session).list_filtered(limit=2000)
    if not jobs:
        return

    scores = JobMatchRepository(session).scores_for_jobs([j.id for j in jobs], profile_id)
    ranked = sorted(
        ((scores.get(j.id) or 0.0, j) for j in jobs), key=lambda pair: -pair[0]
    )
    above = [pair for pair in ranked if pair[0] >= config.match_threshold]

    counted = f"({len(above)} at or above {config.match_threshold})"
    print(f"{BOLD}Top matches{RESET} {DIM}{counted}{RESET}")
    print(RULE)
    for score, job in ranked[:limit]:
        flag = GREEN if score >= config.match_threshold else DIM
        company = job.company.name if job.company else "?"
        print(f"{flag}{score:.2f}{RESET}  {job.title[:46]:46} {DIM}{company[:12]:12} "
              f"{(job.location_raw or '-')[:22]}{RESET}")
    print(RULE)
    if not above:
        print(f"{DIM}Nothing cleared the threshold. Lower match_threshold in "
              f"config/preferences.yml, or widen roles/locations.{RESET}")
    print()


# --- status ---------------------------------------------------------------


def cmd_sync(args: argparse.Namespace) -> int:
    """Apply the config files without scanning anything.

    Used by the background stack at start-up so the scheduler always reflects
    the current portals.txt and preferences.yml — including the scan cadence,
    which is otherwise frozen at whatever it was when a portal was registered.
    """
    config = load(Path(args.config))
    with session_scope() as session:
        _sync_profile(session, config)
        companies = _sync_portals(session, config)
        names = [c.name for c in companies]
    print(f"profile {config.email} · {len(names)} portals · "
          f"every {config.scan_every_minutes} min")
    for name in names:
        print(f"  {name}")
    return 0


def cmd_test_email(args: argparse.Namespace) -> int:
    """Send one sample alert through every configured channel.

    The delivery path is the last thing a scan exercises and the first thing a
    misconfiguration breaks, but it only runs when a genuinely new posting
    clears the threshold — which can be days. This makes it testable on
    demand, with an obviously fake posting so a test can never be mistaken for
    a real opening.
    """
    from app.models.enums import NotificationChannel
    from app.notifications.base import NotificationPayload
    from app.notifications.channels import build_sender

    config = load(Path(args.config))
    settings = get_settings()

    payload = NotificationPayload(
        job_title="TEST — delivery check, not a real vacancy",
        company_name="Job Aggregator",
        location="Bengaluru, India",
        url="https://github.com/muditchaudhari/job-aggregator",
        match_score=1.0,
        reasoning=(
            "This is a test message confirming that email delivery works. "
            "Real alerts look like this one and arrive only when a new posting "
            "clears your match threshold."
        ),
        matched_skills=["python", "postgresql"],
        missing_skills=[],
        posted_date=None,
        salary=None,
        remote_type="unknown",
        employment_type="full_time",
    )

    with session_scope() as session:
        user = UserRepository(session).get_or_create(config.email, config.full_name)
        channels = settings.enabled_notification_channels
        if not channels:
            print(f"{RED}no channels enabled{RESET} — set NOTIFY_CHANNELS")
            return 1

        print(f"sending to {BOLD}{user.email}{RESET} via: {', '.join(channels)}\n")
        failures = 0
        for name in channels:
            try:
                sender = build_sender(NotificationChannel(name))
            except (ValueError, PlatformError) as exc:
                print(f"  {RED}✗{RESET} {name}: {exc}")
                failures += 1
                continue

            if not sender.is_configured():
                print(f"  {RED}✗{RESET} {name}: not configured (missing credentials)")
                failures += 1
                continue

            try:
                sender.send(user, payload)
            except PlatformError as exc:
                print(f"  {RED}✗{RESET} {name}: {exc}")
                failures += 1
            else:
                print(f"  {GREEN}✓{RESET} {name}: accepted for delivery")

    if failures:
        print(f"\n{RED}{failures} channel(s) failed{RESET}")
        return 1
    print(f"\n{GREEN}sent{RESET} — check your inbox, and your spam folder if it is not there")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from app.repositories.scrape_run import ScrapeRunRepository

    with session_scope() as session:
        companies = CompanyRepository(session).list_filtered(is_active=True, limit=500)
        runs = ScrapeRunRepository(session)
        print(f"\n{len(companies)} portals · {JobRepository(session).count()} jobs stored\n")
        print(f"{'PORTAL':22}{'TYPE':17}{'LAST RUN':10}{'JOBS':>6}")
        print(RULE)
        for company in companies:
            last = runs.latest_for_company(company.id)
            print(f"{company.name[:21]:22}{str(company.ats_type)[:16]:17}"
                  f"{(str(last.status) if last else '-'):10}{(last.jobs_found if last else 0):>6}")
        print()
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    """Wipe scanned data. Config files and learned selectors are untouched
    unless --all is given."""
    from app.models.job import Job
    from app.models.scrape_run import ScrapeRun
    from app.models.selector import Selector

    with session_scope() as session:
        session.query(Job).delete()
        session.query(ScrapeRun).delete()
        session.query(Company).delete()
        removed = "portals and jobs"
        if args.all:
            session.query(Selector).delete()
            removed += " and learned selectors"
    print(f"cleared {removed}")
    return 0


# --- results --------------------------------------------------------------


def _ranked_matches(session: Session, profile_id: Any, args: argparse.Namespace) -> list:
    """Jobs joined to their score, filtered and ranked. No network access."""
    from datetime import timedelta

    from app.models.job import Job
    from app.models.match import JobMatch

    query = (
        session.query(Job, JobMatch.score, JobMatch.reasoning, JobMatch.matched_skills)
        .join(JobMatch, JobMatch.job_id == Job.id)
        .filter(JobMatch.profile_id == profile_id, Job.is_active.is_(True))
    )
    # Default to what cleared the bar. Seeing 1,000 near-misses by default
    # buries the handful worth acting on.
    if args.min is not None:
        query = query.filter(JobMatch.score >= args.min)
    elif not args.all:
        from app.userconfig import load as _load

        query = query.filter(JobMatch.score >= _load(Path(args.config)).match_threshold)
    if args.company:
        query = query.join(Company, Company.id == Job.company_id).filter(
            Company.name.ilike(f"%{args.company}%")
        )
    if args.location:
        query = query.filter(Job.location_raw.ilike(f"%{args.location}%"))
    if args.title:
        query = query.filter(Job.title.ilike(f"%{args.title}%"))
    if args.new:
        query = query.filter(Job.first_seen_at >= utcnow() - timedelta(hours=args.new))

    return query.order_by(JobMatch.score.desc()).limit(args.limit).all()


def _group_by_company(rows: list) -> list[tuple[str, list]]:
    """Company-wise, each company's best match first, companies ranked by
    their strongest role — so the most promising employer leads."""
    buckets: dict[str, list] = {}
    for entry in rows:
        job = entry[0]
        name = job.company.name if job.company else "Unknown"
        buckets.setdefault(name, []).append(entry)
    for entries in buckets.values():
        entries.sort(key=lambda e: -e[1])
    return sorted(buckets.items(), key=lambda kv: -max(e[1] for e in kv[1]))


def cmd_results(args: argparse.Namespace) -> int:
    """Show what has already been found. Never scans."""
    config = load(Path(args.config))

    with session_scope() as session:
        profiles = UserProfileRepository(session).list_all()
        if not profiles:
            print("no profile yet — run `make run` once")
            return 1
        rows = _ranked_matches(session, profiles[0].id, args)

        if not rows:
            print("\nNothing matched those filters.")
            print(f"{DIM}Try a lower --min, or `make run` if you have not scanned yet.{RESET}")
            return 1

        threshold = args.min if args.min is not None else config.match_threshold
        grouped = _group_by_company(rows)

        print(f"\n{BOLD}{len(rows)} jobs across {len(grouped)} companies{RESET} "
              f"{DIM}(threshold {threshold}){RESET}")

        for company, entries in grouped:
            best = max(score for _, score, _, _ in entries)
            print(f"\n{RULE}")
            print(f"{BOLD}{company}{RESET}  {DIM}{len(entries)} roles · "
                  f"best {best:.2f}{RESET}")
            print(RULE)
            for job, score, reasoning, skills in entries:
                colour = GREEN if score >= config.match_threshold else DIM
                age = humanize_age(job.posted_at, job.posted_date)
                print(f"  {colour}{score:.2f}{RESET}  {BOLD}{job.title[:58]}{RESET}")
                print(f"        {DIM}{(job.location_raw or 'n/a')[:40]} · {age}{RESET}")
                if args.why and reasoning:
                    print(f"        {DIM}{reasoning[:150]}{RESET}")
                if skills:
                    print(f"        {DIM}skills seen: {', '.join(list(skills)[:8])}{RESET}")
                print(f"        {job.url}")
            print()

        if args.html:
            path = Path(args.html)
            path.write_text(_render_html(rows, config), encoding="utf-8")
            print(f"wrote {path.resolve()}")
            print(f"{DIM}open it with:  open {path}{RESET}")
    return 0


def _render_html(rows: list, config: UserConfig) -> str:
    """A single self-contained page — no server, no assets, just open it."""

    threshold = config.match_threshold
    strong_sections, weak_sections, weak_companies = [], [], 0

    for company, entries in _group_by_company(rows):
        green = [e for e in entries if e[1] >= threshold]
        rest = [e for e in entries if e[1] < threshold]

        if not green:
            # Nothing here clears the bar. Fold the whole company away rather
            # than making you scroll past it to reach one that does.
            weak_companies += 1
            weak_sections.append(_company_html(company, [], rest, config))
            continue
        strong_sections.append(_company_html(company, green, rest, config))

    body = "".join(strong_sections)
    if not strong_sections:
        body = (
            '<p class="empty">Nothing cleared the threshold. Lower '
            "<code>match_threshold</code> in config/preferences.yml, or widen "
            "roles and locations.</p>"
        )
    if weak_sections:
        body += (
            f'<details class="rest"><summary>{weak_companies} more companies '
            "with no matches above the threshold</summary>"
            f'{"".join(weak_sections)}</details>'
        )

    strong = sum(1 for _, score, _, _ in rows if score >= threshold)
    return _PAGE.format(
        count=strong,
        total=len(rows),
        companies=len(_group_by_company(rows)),
        threshold=threshold,
        generated=f"{utcnow():%Y-%m-%d %H:%M} UTC",
        body=body,
    )


def _company_html(company: str, green: list, rest: list, config: UserConfig) -> str:
    """One company block: matches shown, everything else behind a disclosure."""
    from html import escape

    head = '<tr><th>Score</th><th>Role</th><th>Location</th><th>Posted</th></tr>'
    parts = [f'<h2>{escape(company)} <span class="count">']
    parts.append(
        f"{len(green)} match{'' if len(green) == 1 else 'es'}"
        if green
        else f"{len(rest)} roles, none above {config.match_threshold}"
    )
    parts.append("</span></h2>")

    if green:
        parts.append(f"<table><thead>{head}</thead><tbody>")
        parts.append("".join(_row_html(e, config) for e in green))
        parts.append("</tbody></table>")

    if rest:
        label = (
            f"Show {len(rest)} more below {config.match_threshold}"
            if green
            else f"Show {len(rest)} roles"
        )
        parts.append(
            f"<details><summary>{label}</summary>"
            f"<table><thead>{head}</thead><tbody>"
            f'{"".join(_row_html(e, config) for e in rest)}'
            "</tbody></table></details>"
        )
    return "".join(parts)


def _row_html(entry: tuple, config: UserConfig) -> str:
    from html import escape

    job, score, reasoning, skills = entry
    good = "good" if score >= config.match_threshold else ""
    matched = ", ".join(list(skills)[:10]) if skills else ""
    detail = escape(reasoning or "")
    if matched:
        detail += f' <span class="skills">{escape(matched)}</span>'
    age = humanize_age(job.posted_at, job.posted_date)
    fresh = "fresh" if age.endswith(("hours ago", "hour ago", "minutes ago")) or age in (
        "today", "just now"
    ) else ""
    return f"""<tr class="{good}">
  <td class="score">{score:.2f}</td>
  <td><a href="{escape(job.url)}" target="_blank" rel="noopener">{escape(job.title)}</a>
      <div class="meta">{detail}</div></td>
  <td>{escape(job.location_raw or '—')}</td>
  <td class="age {fresh}">{escape(age)}</td>
</tr>"""


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job matches</title>
<style>
 body{{font-family:system-ui,-apple-system,sans-serif;margin:2rem auto;max-width:1000px;
      padding:0 1rem;color:#111;background:#fafafa}}
 h1{{font-size:1.35rem;margin:0 0 .2rem}}
 h2{{font-size:1rem;margin:2rem 0 .5rem;padding-bottom:.3rem;
     border-bottom:2px solid #d8d8dc}}
 .count{{font-weight:400;color:#777;font-size:.8rem}}
 .sub{{color:#666;font-size:.9rem;margin-bottom:.5rem}}
 table{{border-collapse:collapse;width:100%;background:#fff;box-shadow:0 1px 3px #0001}}
 th,td{{padding:.55rem .7rem;border-bottom:1px solid #eee;text-align:left;
        vertical-align:top}}
 th{{background:#f4f4f5;font-size:.7rem;text-transform:uppercase;
     letter-spacing:.04em;color:#555}}
 .score{{font-variant-numeric:tabular-nums;font-weight:600;color:#888;width:3.2rem}}
 tr.good .score{{color:#137333}} tr.good{{background:#f6fbf7}}
 .age{{white-space:nowrap;color:#777;font-size:.85rem;width:7rem}}
 .age.fresh{{color:#b45309;font-weight:600}}
 a{{color:#1a56db;text-decoration:none}} a:hover{{text-decoration:underline}}
 .meta{{color:#777;font-size:.8rem;margin-top:.2rem;max-width:70ch}}
 .skills{{color:#137333;font-weight:500}}
 details{{margin:.4rem 0 0}}
 details>summary{{cursor:pointer;color:#555;font-size:.85rem;padding:.45rem .6rem;
   background:#f1f1f3;border-radius:6px;user-select:none;list-style:none}}
 details>summary::-webkit-details-marker{{display:none}}
 details>summary::before{{content:"▸ ";color:#999}}
 details[open]>summary::before{{content:"▾ "}}
 details>summary:hover{{background:#e8e8ec}}
 details.rest{{margin-top:2.5rem}}
 .empty{{color:#666;background:#fff;padding:1rem;border-radius:8px}}
 @media (prefers-color-scheme: dark){{
   body{{background:#161618;color:#e8e8ea}} table{{background:#1e1e21}}
   th{{background:#26262a;color:#aaa}} td{{border-color:#2c2c30}}
   h2{{border-color:#33333a}} tr.good{{background:#16211a}}
   a{{color:#7aa7ff}} .age.fresh{{color:#fbbf24}}
   details>summary{{background:#26262a;color:#bbb}}
   details>summary:hover{{background:#303036}}
   .empty{{background:#1e1e21;color:#aaa}}
 }}
</style></head><body>
<h1>Job matches</h1>
<div class="sub">{count} matches above {threshold} · {total} roles scanned across
 {companies} companies · scored on title, experience and location ·
 generated {generated}</div>
{body}
</body></html>"""


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="scan every portal and print the results")
    run.add_argument("--config", default="config", help="config directory")
    run.add_argument("--top", type=int, default=15, help="how many matches to list")
    run.add_argument("--notify", action="store_true", help="also send notifications")
    run.add_argument("--semantic", action="store_true",
                     help="score with the LLM for reasoning (slower, rate limited)")
    run.add_argument("--force-llm", action="store_true", help="relearn selectors")
    run.add_argument("--redetect", action="store_true", help="re-run site detection")
    run.add_argument("--jobs", type=int, default=4, metavar="N",
                     help="portals to scan in parallel (default 4)")
    run.add_argument("--due", action="store_true",
                     help="skip portals scanned within their interval (fewer requests)")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="what is registered and what it found")
    status.set_defaults(func=cmd_status)

    res = sub.add_parser("results", help="show matches already found (no scanning)")
    res.add_argument("--config", default="config")
    res.add_argument("--min", type=float, help="minimum score (default: your threshold)")
    res.add_argument("--limit", type=int, default=40)
    res.add_argument("--company", help="filter by company name")
    res.add_argument("--location", help="filter by location text")
    res.add_argument("--title", help="filter by words in the title")
    res.add_argument("--new", type=int, metavar="HOURS", help="only jobs first seen recently")
    res.add_argument("--why", action="store_true", help="show the match reasoning")
    res.add_argument("--all", action="store_true",
                     help="include roles below the threshold (default: matches only)")
    res.add_argument("--html", metavar="PATH", help="also write a browsable HTML page")
    res.set_defaults(func=cmd_results)

    sync = sub.add_parser("sync", help="apply config files without scanning")
    sync.add_argument("--config", default="config")
    sync.set_defaults(func=cmd_sync)

    test_email = sub.add_parser(
        "test-email", help="send one sample alert to check delivery works"
    )
    test_email.add_argument("--config", default="config")
    test_email.set_defaults(func=cmd_test_email)

    reset = sub.add_parser("reset", help="clear scanned data and start over")
    reset.add_argument("--all", action="store_true", help="also drop learned selectors")
    reset.set_defaults(func=cmd_reset)

    args = parser.parse_args(argv)
    try:
        # Cheap on an existing database and the difference between working and
        # not on a fresh one, which is the normal state of a cron runner whose
        # cached database was evicted.
        ensure_schema()
        return int(args.func(args))
    except ConfigError as exc:
        print(f"{RED}config error{RESET}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
