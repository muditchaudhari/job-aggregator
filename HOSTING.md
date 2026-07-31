# Running it in the cloud, for free

The scanner emails you new postings every 30 minutes without your laptop being
on. Total cost: nothing.

## Why GitHub Actions and not a server

A scan is a batch job. It starts, reads twelve portals, sends mail if anything
is new, and exits — about a minute of work, then nothing for the next 29. Paying
for a machine that is idle 97% of the time is the wrong shape, and every free
tier that offers an always-on machine (Render, Fly, Railway) either sleeps it,
bills for it, or expires the trial.

GitHub Actions bills by the minute of actual execution, and for a **public**
repository those minutes are unmetered. Private repositories get 2,000
minutes/month free, and this uses roughly 1,500 (48 runs/day × ~1 min), so a
private repo fits but with little headroom — make the repo public if you would
rather not think about it. Nothing secret lives in it; the API keys are stored
as GitHub Secrets, which are not part of the repository.

No Redis and no Postgres are needed. The scan process does all the work itself:
SQLite holds the state, and the LLM budget falls back to a per-process counter.

## Setup

### 1. Get a Resend API key

Mail goes over Resend's HTTPS API rather than SMTP, because GitHub blocks
outbound SMTP ports on its runners — a mail server would simply never connect.

1. Sign up at <https://resend.com> **with the address you want the alerts sent
   to** (`muditchaudhari@gmail.com`). This matters: see the limitation below.
2. Create an API key at <https://resend.com/api-keys> with **Sending access**.
3. Keep the key on your clipboard for step 3. Don't paste it into a file that
   gets committed, and don't paste it into a chat window.

The free tier gives 3,000 emails/month capped at 100/day. This sends at most a
few a day, and only when something new appears.

**The one limitation:** without your own domain, Resend only lets you send from
`onboarding@resend.dev`, and that sender may only deliver **to the address on
your Resend account**. That is exactly the setup here, so it works — but it is
why the signup address has to be the address you want mail at. If you later
verify a domain of your own, set a repository variable `RESEND_FROM` to an
address on it and the restriction disappears.

### 2. Push the repository

```bash
cd ~/Desktop/job-aggregator-platform && git add -A && git commit -m "Job aggregation platform" && gh repo create job-aggregator --public --source=. --push
```

(If you don't have the `gh` CLI, create an empty repo on github.com and follow
the `git remote add` instructions it shows you.)

`.env` is gitignored, so your local Gemini key does not go up with it.

### 3. Add the key as a secret

On the repository page: **Settings → Secrets and variables → Actions → New
repository secret**.

| Name | Value | Required |
| --- | --- | --- |
| `RESEND_API_KEY` | the key from step 1 | yes |
| `GEMINI_API_KEY` | your existing Gemini key | no |

Gemini is optional. It is only used when all four deterministic extraction tiers
fail on a portal, which none of the currently configured twelve do. Add it and
the self-learning path is available if a site redesigns; leave it out and that
portal just reports a failure until you look at it.

### 4. Turn the schedule on

**Actions** tab → if prompted, enable workflows → select **Scan job portals** →
**Run workflow** to fire one immediately rather than waiting for the next
half-hour boundary.

The first run for each company records its board as a **baseline** and sends no
mail — otherwise your first email would contain every job at all twelve
companies. From the second run onward you get only genuinely new postings.

## What happens on each run

1. Restores the SQLite database from the Actions cache (the record of what
   you have already been shown).
2. Scans all twelve portals, four at a time.
3. Scores each posting on title, experience and location, per `preferences.yml`.
4. Emails you anything new at or above your `match_threshold`.
5. Saves the database back to the cache, and once a day pushes a one-line
   heartbeat commit.

## Changing what it looks for

Edit `config/preferences.yml`, `config/portals.txt` or `config/skills.txt`,
commit, and push. The next scheduled run picks the change up — there is nothing
to redeploy.

## Two GitHub behaviours worth knowing

**Cron is best-effort.** Runs are queued on shared infrastructure and can be
delayed, sometimes by 10–15 minutes at the top of the hour. `*/30` means "about
every half hour", not "on the hour and the half hour".

**Scheduled workflows are disabled after 60 days of repository inactivity.**
This is why the workflow pushes a dated one-line file once a day: that counts as
activity, so the schedule never lapses. If you ever do get GitHub's "workflow
disabled" email, one click on **Enable workflow** restores it.

## If the mail stops arriving

Check the **Actions** tab first — a red run tells you which portal broke, and
the log line names the tier that failed. The likelier causes, in order:

- **Nothing new.** Silence is the normal state most days. Confirm by
  downloading the `results` artifact from the latest run, which is the same
  report `make report` produces locally.
- **Resend rejected it.** The step log carries Resend's own explanation; an
  unverified sender is the usual one.
- **A portal started refusing datacentre traffic.** Some sites treat a cloud IP
  more suspiciously than a home one. That portal fails, the other eleven carry
  on, and the run stays green.
