"""Notification rendering.

One payload, three presentations. Kept apart from the channels so that fixing a
wording problem does not mean touching SMTP code, and so the rendered output
can be asserted on in tests without any network stubbing.
"""

from __future__ import annotations

from html import escape

from app.notifications.base import NotificationPayload

_EMAIL_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
line-height:1.5;color:#1a1a1a;margin:0;padding:24px;background:#f6f7f9}
.card{max-width:600px;margin:0 auto;background:#fff;border:1px solid #e4e6eb;
border-radius:10px;overflow:hidden}
.header{padding:20px 24px;border-bottom:1px solid #e4e6eb}
.title{font-size:19px;font-weight:600;margin:0 0 4px}
.company{color:#5c6470;font-size:14px;margin:0}
.body{padding:20px 24px}
.row{margin:0 0 10px;font-size:14px}
.label{color:#5c6470;display:inline-block;min-width:96px}
.score{display:inline-block;padding:3px 10px;border-radius:999px;
font-size:13px;font-weight:600;background:#e8f5e9;color:#1b5e20}
.score.mid{background:#fff8e1;color:#8d6e00}
.why{background:#f6f7f9;border-left:3px solid #c9ced6;padding:12px 14px;
border-radius:0 6px 6px 0;font-size:14px;margin:16px 0}
.skills{font-size:13px;color:#5c6470;margin:4px 0}
.cta{display:inline-block;margin-top:8px;padding:10px 20px;background:#1a73e8;
color:#fff !important;text-decoration:none;border-radius:6px;font-weight:600;font-size:14px}
.footer{padding:14px 24px;border-top:1px solid #e4e6eb;font-size:12px;color:#8b929c}
"""


def render_email_subject(payload: NotificationPayload) -> str:
    return f"[{int(payload.match_score * 100)}% match] {payload.job_title} — {payload.company_name}"


def render_email_html(payload: NotificationPayload) -> str:
    score_class = "score" if payload.match_score >= 0.75 else "score mid"
    rows = [
        ("Location", payload.location),
        ("Type", _humanise(payload.employment_type)),
        ("Remote", _humanise(payload.remote_type)),
    ]
    if payload.salary:
        rows.append(("Salary", payload.salary))
    if payload.posted_date:
        rows.append(("Posted", payload.posted_date))

    row_html = "".join(
        f'<p class="row"><span class="label">{escape(label)}</span>{escape(str(value))}</p>'
        for label, value in rows
    )

    why = ""
    if payload.reasoning:
        why = (
            '<div class="why"><strong>Why this matched</strong><br>'
            f"{escape(payload.reasoning)}</div>"
        )

    skills = ""
    if payload.matched_skills:
        skills += (
            f'<p class="skills"><strong>Your skills mentioned:</strong> '
            f"{escape(', '.join(payload.matched_skills[:8]))}</p>"
        )
    if payload.missing_skills:
        skills += (
            f'<p class="skills"><strong>Not mentioned:</strong> '
            f"{escape(', '.join(payload.missing_skills[:6]))}</p>"
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>{_EMAIL_CSS}</style></head>
<body><div class="card">
  <div class="header">
    <p class="title">{escape(payload.job_title)}</p>
    <p class="company">{escape(payload.company_name)}</p>
  </div>
  <div class="body">
    <p><span class="{score_class}">{int(payload.match_score * 100)}% match</span></p>
    {row_html}
    {why}
    {skills}
    <a class="cta" href="{escape(payload.url)}">View &amp; apply</a>
  </div>
  <div class="footer">Sent by your job aggregation platform.</div>
</div></body></html>"""


def render_email_text(payload: NotificationPayload) -> str:
    """Plain-text alternative.

    Not optional: a multipart email without a text part is a strong spam
    signal, and some clients show the raw HTML source without one.
    """
    lines = [
        f"{payload.job_title} — {payload.company_name}",
        "",
        f"Match:    {int(payload.match_score * 100)}%",
        f"Location: {payload.location}",
        f"Type:     {_humanise(payload.employment_type)}",
        f"Remote:   {_humanise(payload.remote_type)}",
    ]
    if payload.salary:
        lines.append(f"Salary:   {payload.salary}")
    if payload.posted_date:
        lines.append(f"Posted:   {payload.posted_date}")
    if payload.reasoning:
        lines += ["", "Why this matched:", payload.reasoning]
    if payload.matched_skills:
        lines += ["", f"Your skills mentioned: {', '.join(payload.matched_skills[:8])}"]
    lines += ["", f"Apply: {payload.url}"]
    return "\n".join(lines)


def render_slack_blocks(payload: NotificationPayload) -> dict:
    fields = [
        {"type": "mrkdwn", "text": f"*Location*\n{payload.location}"},
        {"type": "mrkdwn", "text": f"*Match*\n{int(payload.match_score * 100)}%"},
    ]
    if payload.salary:
        fields.append({"type": "mrkdwn", "text": f"*Salary*\n{payload.salary}"})
    if payload.posted_date:
        fields.append({"type": "mrkdwn", "text": f"*Posted*\n{payload.posted_date}"})

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": _truncate(payload.job_title, 150)},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{payload.company_name}*"},
            "fields": fields,
        },
    ]
    if payload.reasoning:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"_{_truncate(payload.reasoning, 500)}_"},
            }
        )
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View & apply"},
                    "url": payload.url,
                    "style": "primary",
                }
            ],
        }
    )
    return {
        "text": f"{payload.job_title} at {payload.company_name}",  # notification fallback
        "blocks": blocks,
    }


def render_telegram(payload: NotificationPayload) -> str:
    """Telegram HTML. Only a small tag subset is supported by the API."""
    parts = [
        f"<b>{escape(payload.job_title)}</b>",
        f"{escape(payload.company_name)}",
        "",
        f"📍 {escape(payload.location)}",
        f"🎯 {int(payload.match_score * 100)}% match",
    ]
    if payload.salary:
        parts.append(f"💰 {escape(payload.salary)}")
    if payload.posted_date:
        parts.append(f"🗓 {escape(payload.posted_date)}")
    if payload.reasoning:
        parts += ["", f"<i>{escape(_truncate(payload.reasoning, 400))}</i>"]
    parts += ["", f'<a href="{escape(payload.url)}">View &amp; apply</a>']
    return "\n".join(parts)


def _humanise(value: str) -> str:
    return value.replace("_", " ").title()


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"
