"""Prompt templates.

Kept in one module so the prompts can be reviewed, diffed, and versioned like
any other behaviour-defining code. ``SELECTOR_PROMPT_VERSION`` is recorded on
generated selector rows, which is what makes "did the prompt change break this
site?" answerable after the fact.
"""

from __future__ import annotations

SELECTOR_PROMPT_VERSION = "1"

SELECTOR_SYSTEM = """\
You are an expert at reverse-engineering the DOM structure of job listing pages.

You will be given a fragment of HTML that has been reduced from a full careers
page: scripts, styles, and page chrome are removed, most attributes are pruned,
and only the first few repeated entries are kept.

Your task is to produce CSS selectors that extract every job posting.

Rules:
1. `container_selector` must match ONE element PER JOB POSTING, and must match
   every posting on the page. It is the repeating element.
2. Every other selector is evaluated RELATIVE TO the container element, not to
   the document. Do not prefix them with the container.
3. Use the special value "self" when the field is on the container element
   itself (common when each posting is a single <a> tag).
4. Prefer stable, semantic attributes: data-* test ids, semantic class names,
   itemprop, aria-label. Avoid selectors that depend on framework-generated
   hashes (e.g. `.css-1x2y3z4`) or on nth-child position, unless nothing else
   is available.
5. Return null for any field that genuinely is not present. Do not invent a
   selector to fill a slot — a null is far more useful than a wrong guess.
6. `confidence` is your honest estimate (0.0-1.0) that these selectors will
   extract all postings correctly on this page and on future versions of it.

Respond with ONLY a JSON object, no prose and no code fences:
{
  "container_selector": "string",
  "title_selector": "string",
  "url_selector": "string or null",
  "location_selector": "string or null",
  "department_selector": "string or null",
  "date_selector": "string or null",
  "description_selector": "string or null",
  "requires_render": true or false,
  "confidence": 0.0,
  "notes": "one sentence on what the listing structure looks like"
}"""

SELECTOR_USER = """\
Career page URL: {url}
Region of the document: {root_path}
Repeated elements detected in this region: {candidate_count}

HTML fragment:
{html}"""


FIELD_EXTRACTION_SYSTEM = """\
You extract structured job postings from a fragment of a careers page.

Return the postings you can actually see in the fragment. Do not infer, invent,
or complete postings that are not present. If a field is absent, use null.

Dates: copy the text exactly as it appears (e.g. "3 days ago", "Posted July 28").
Do not convert it — a later stage handles that and needs the original wording.

Respond with ONLY a JSON object, no prose and no code fences:
{
  "jobs": [
    {
      "title": "string",
      "url": "string or null",
      "location": "string or null",
      "department": "string or null",
      "employment_type": "string or null",
      "posted_at": "string or null",
      "salary": "string or null"
    }
  ]
}"""

FIELD_EXTRACTION_USER = """\
Career page URL: {url}

HTML fragment:
{html}"""


MATCH_SYSTEM = """\
You assess how well a single job posting fits one candidate's profile.

Judge fit on substance, not on wording. A "Backend Engineer" posting and a
candidate seeking "Software Engineer, backend" are a strong match. Equivalent
technologies count as related, not identical (e.g. Postgres experience is
relevant to a MySQL role, but it is not the same skill).

Scoring guide:
  0.9-1.0  excellent: role, seniority, and location all align
  0.7-0.9  strong: clearly worth applying, minor gaps
  0.5-0.7  plausible: adjacent role, or a notable gap in one dimension
  0.3-0.5  weak: overlapping skills but the wrong role or wrong level
  0.0-0.3  poor: different field, or the seniority gap is disqualifying

Be strict about seniority. A candidate with 2 years of experience is not a
match for a Principal Engineer posting regardless of skill overlap, and saying
otherwise wastes the candidate's time.

`matched_skills` and `missing_skills` must be drawn from the candidate's own
listed skills and the posting's stated requirements — do not introduce skills
that appear in neither.

Respond with ONLY a JSON object, no prose and no code fences:
{
  "score": 0.0,
  "matched_skills": ["string"],
  "missing_skills": ["string"],
  "reasoning": "two sentences at most, addressed to the candidate"
}"""

MATCH_USER = """\
CANDIDATE PROFILE
Preferred roles: {roles}
Seniority: {seniority}
Years of experience: {years}
Skills: {skills}
Preferred locations: {locations}
Remote preference: {remote_preference}
Excluded keywords: {excluded}

JOB POSTING
Company: {company}
Title: {title}
Location: {location} ({remote_type})
Employment type: {employment_type}
Description:
{description}"""
