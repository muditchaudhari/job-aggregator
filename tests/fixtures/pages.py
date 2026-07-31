"""Sample payloads for extraction tests.

Trimmed but structurally faithful to what the real platforms return, including
the awkward bits — HTML-escaped description bodies, epoch-millisecond dates,
navigation links mixed into the listing.
"""

from __future__ import annotations

GREENHOUSE_API = {
    "jobs": [
        {
            "id": 4012345,
            "title": "Senior Backend Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/4012345",
            "location": {"name": "Bengaluru, India"},
            "updated_at": "2026-07-20T10:30:00-04:00",
            "content": "&lt;p&gt;We use &lt;strong&gt;Python&lt;/strong&gt; and AWS.&lt;/p&gt;",
            "departments": [{"id": 1, "name": "Engineering"}],
            "metadata": [{"name": "Employment Type", "value": "Full-time"}],
        },
        {
            "id": 4012346,
            "title": "Product Designer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/4012346",
            "location": {"name": "Remote - US"},
            "updated_at": "2026-07-25T09:00:00-04:00",
            "content": "&lt;p&gt;Figma, design systems.&lt;/p&gt;",
            "departments": [{"id": 2, "name": "Design"}],
            "metadata": [],
        },
    ]
}

LEVER_API = [
    {
        "id": "abc-123",
        "text": "Staff Software Engineer",
        "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        "categories": {
            "location": "Bangalore, Karnataka",
            "team": "Platform",
            "commitment": "Full-time",
        },
        "descriptionPlain": "Build distributed systems in Java and Kotlin.",
        "lists": [
            {"text": "Requirements", "content": "<li>5+ years experience</li>"}
        ],
        # Lever reports epoch milliseconds, not ISO.
        "createdAt": 1753000000000,
        "workplaceType": "hybrid",
    }
]

WORKDAY_CXS = {
    "total": 2,
    "jobPostings": [
        {
            "title": "Software Engineer II",
            "externalPath": "/job/Bengaluru/Software-Engineer-II_R-12345",
            "locationsText": "Bengaluru, India",
            "postedOn": "Posted 3 Days Ago",
            "bulletFields": ["R-12345"],
            "timeType": "Full time",
        },
        {
            "title": "Data Analyst",
            "externalPath": "/job/Pune/Data-Analyst_R-12346",
            "locationsText": "Pune, India",
            "postedOn": "Posted Yesterday",
            "bulletFields": ["R-12346"],
            "timeType": "Full time",
        },
    ],
}

#: A conventional server-rendered listing: repeating <li>, one link each.
GENERIC_LISTING_HTML = """
<!doctype html>
<html><head><title>Careers</title></head>
<body>
  <nav><a href="/about">About</a><a href="/contact">Contact</a></nav>
  <main>
    <h1>Open positions</h1>
    <ul class="job-list">
      <li class="job-item">
        <a href="/careers/jobs/backend-engineer">Backend Engineer</a>
        <span class="job-location">Bengaluru, India</span>
        <time class="job-date" datetime="2026-07-28">3 days ago</time>
      </li>
      <li class="job-item">
        <a href="/careers/jobs/frontend-engineer">Frontend Engineer</a>
        <span class="job-location">Remote</span>
        <time class="job-date" datetime="2026-07-29">2 days ago</time>
      </li>
      <li class="job-item">
        <a href="/careers/jobs/devops-engineer">DevOps Engineer</a>
        <span class="job-location">Pune, India</span>
        <time class="job-date" datetime="2026-07-30">Today</time>
      </li>
    </ul>
  </main>
  <footer><a href="/privacy">Privacy policy</a></footer>
</body></html>
"""

JSON_LD_HTML = """
<!doctype html>
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "JobPosting",
  "title": "Machine Learning Engineer",
  "url": "https://widgets.example.com/careers/ml-engineer",
  "datePosted": "2026-07-22",
  "employmentType": "FULL_TIME",
  "description": "<p>PyTorch, Python, distributed training.</p>",
  "identifier": {"@type": "PropertyValue", "value": "ML-77"},
  "jobLocation": {
    "@type": "Place",
    "address": {
      "@type": "PostalAddress",
      "addressLocality": "Bengaluru",
      "addressRegion": "Karnataka",
      "addressCountry": "India"
    }
  },
  "baseSalary": {
    "@type": "MonetaryAmount",
    "currency": "INR",
    "value": {"@type": "QuantitativeValue", "minValue": 2500000, "maxValue": 3500000}
  }
}
</script>
</head><body><div id="root"></div></body></html>
"""

#: A client-rendered shell: framework marker present, no content. Should be
#: detected as an SPA and escalated to a render.
SPA_SHELL_HTML = """
<!doctype html>
<html><head><title>Careers</title></head>
<body><div id="root"></div>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"jobs":[
  {"id":"j1","title":"Cloud Engineer","url":"/jobs/j1","location":"Hyderabad, India"},
  {"id":"j2","title":"Security Engineer","url":"/jobs/j2","location":"Remote"}
]}}}
</script>
</body></html>
"""

#: Nothing resembling a listing — the case that must score 0 rather than
#: returning navigation links as "jobs".
NO_JOBS_HTML = """
<!doctype html>
<html><body>
  <nav><a href="/home">Home</a><a href="/about">About</a></nav>
  <h1>We have no openings right now</h1>
  <p>Check back soon.</p>
</body></html>
"""

#: What a bot wall looks like: HTTP 200, valid HTML, no content.
BOT_WALL_HTML = """
<!doctype html>
<html><body><h1>Checking your browser before accessing</h1>
<p>Please enable JavaScript and cookies to continue.</p>
<div class="cf-browser-verification"></div></body></html>
"""

LLM_SELECTOR_RESPONSE = """```json
{
  "container_selector": "li.job-item",
  "title_selector": "a",
  "url_selector": "a",
  "location_selector": "span.job-location",
  "department_selector": null,
  "date_selector": "time.job-date",
  "description_selector": null,
  "requires_render": false,
  "confidence": 0.92,
  "notes": "Simple unordered list, one anchor per posting."
}
```"""

#: Selectors that parse fine but match nothing — the case that must be
#: rejected rather than stored.
LLM_BAD_SELECTOR_RESPONSE = """
{
  "container_selector": "div.does-not-exist",
  "title_selector": "h4.nope",
  "url_selector": "a",
  "location_selector": null,
  "date_selector": null,
  "requires_render": false,
  "confidence": 0.95,
  "notes": "Confident but wrong."
}
"""
