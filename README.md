# ReproBot

Record a bug. Get a report an engineer can trust, because an agent already reproduced it in a real browser.

Built at the Gemini 4 NYC Hackathon (Nov 21, 2026). Problem statement: **5 Sigma**.

## The problem

Bug reports from users are vague ("it's broken"), often describe expected behavior, and cost engineers
hours to reproduce. A model that writes a nicer report from a screen recording doesn't fix that: it can
also misread the video and report bugs that don't exist.

## What it does

1. **Gemini 4 Neon** watches the screen recording with agentic video understanding and writes a
   structured report: steps with timestamps, expected vs actual, and one automatable check.
2. **Gemini Decision 1** (Decisions API) scores the report in under a second: reproducible from text
   alone? checkable? how severe? If not, the reporter is asked for what's missing and no agent time is spent.
3. A **Gemini Managed Agent** (Antigravity, Google-hosted Linux sandbox) writes a Playwright script from
   the report, runs it up to three times in real Chrome, and takes a screenshot.
4. **Gemini 4 Hydrogen** turns the agent's log into a strict verdict: reproduced, not reproduced, or blocked.

The agent only reads pages inside its sandbox. Nothing is filed or changed without a person.

## Does it work

15 screen recordings of real pages, each with a vague user note. Question: is this a real, reproducible bug?
Several recordings show correct behavior that a user mistook for a bug.

| | Correct | False alarms |
|---|---|---|
| ReproBot (report, gate, reproduce) | **13 / 15** | 0 |
| Baseline (one Hydrogen call on the video) | 10 / 15 | 0 |

What the numbers hide:

- The baseline missed every intermittent bug (typo, disappearing menu item, shifting menu). Rerunning the
  page three times in the sandbox is what caught them.
- Twice, Neon itself misread a recording and reported a bug that isn't there (`dynamic_controls`,
  `status_500`). The agent could not reproduce either one, so neither reached an engineer.
- Our two misses: `status_500` and `notification` are labeled as bugs in our test set, and the system
  called them expected behavior. Both labels are debatable; we left them as first written.
- Limits: the test pages come from one public practice site and we chose them. It needs the page URL,
  since a tab recording has no address bar. Pages behind a login are reported as blocked.
  One run takes one to four minutes, mostly the sandbox step.

### The fix step

A second test, on a small restaurant ordering site with 10 planted bugs (`docs/`, live on GitHub Pages) plus 3
recordings of correct behavior. Input per case: one screen recording and one vague sentence. The pipeline never
sees the bug list.

| | Result |
|---|---|
| Bug or not a bug | 13 / 13 |
| Reproduced in the sandbox | 10 / 10 |
| Fixed, and the fix independently verified | **10 / 10** |
| Median time per bug | 6 minutes |

"Independently verified" means each diff was applied to a clean copy of the site and tested with a separate,
stricter script than the agent's own.

What the numbers hide: these are one-line faults in an 800-line site with full source, one run each, and the
recordings are cleaner than real ones, so treat 10 of 10 as an upper bound. The watcher misread one recording
and that bug was reproduced partly by luck. The run also exposed two bugs in our own harness (cases sharing a
sandbox, and file downloads returning corrupt bytes for 3 diffs); both are fixed. The case list and verifier
are kept out of this repository because the fix agent clones it.

Reproduce the evaluation: `python eval/record.py && python eval/run.py` (results in `data/eval.json`).

## Run it

```
python -m venv .venv && .venv/bin/pip install -U "google-genai>=3.2.0" fastapi "uvicorn[standard]" python-multipart playwright
.venv/bin/python -m playwright install chromium     # only needed to record evaluation cases
export GEMINI_API_KEY=...
.venv/bin/uvicorn app.server:app --port 8000
```

Open http://localhost:8000, record a tab, paste the page URL, and press Verify.

## Layout

- `app/pipeline.py`: the four steps and the baseline
- `app/server.py`: upload, job status, screenshot
- `web/index.html`: the page
- `eval/`: test cases, recorder, scorer
