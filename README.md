# Ward Wise ideas

This repo is the idea board for [Ward Wise](https://www.wardwise.org), which measures wellbeing in every
Chicago ward. It holds no code. Each issue is a ticket for a measure or a story somebody could build, and
the work itself lives in its own repo.

- A **measure idea** (`measure-idea`) becomes a new public repo that publishes the measure under the
  [byop/v1 contract](https://github.com/ward-wise/ward-wise-measures/blob/main/CONTRACT.md), plus one pull
  request that pins it in `ward-wise-measures/measures.json`.
- A **story idea** (`story-idea`) is a page, report or dashboard for a particular kind of person, such as a
  block-club captain's one-page brief. It becomes a page PR in
  [ward-wise-front-end-v2](https://github.com/ward-wise/ward-wise-front-end-v2) or a standalone site built on
  the [public API](https://www.wardwise.org/api-docs).

Before you open an idea, check the [metric dictionary](https://www.wardwise.org/dictionary). If you don't have
a GitHub account, use [wardwise.org/suggest-metric](https://www.wardwise.org/suggest-metric) and a volunteer
copies your idea here.

## Two ways to help

**Advise.** You know your ward, or you know a field: public health, schools, housing, surveys. You don't
write code. Open an idea with the issue form, or comment on one that's already here. Tell us why it matters,
where the data might be, or what would make the number misleading. Ideas labelled `expert-wanted` need this
kind of help before anyone builds them.

**Build.** You write code or work with data. Pick an open idea and claim it (see below), then start from a
template so you don't begin from a blank page:

- [`ward-wise/ward-wise-measure-template`](https://github.com/ward-wise/ward-wise-measure-template) gives you an
  `aggregation_spec.json` skeleton, a tiny example layer and a publish workflow. Follow
  [ADDING_A_MEASURE.md](https://github.com/ward-wise/ward-wise-measures/blob/main/ADDING_A_MEASURE.md).
- [`ward-wise/ward-wise-story-template`](https://github.com/ward-wise/ward-wise-story-template) gives you a static page that calls
  the public API, one example chart and a GitHub Pages deploy.

Both template repos are still being set up and the links above don't work yet. Until they do, start a
measure from ADDING_A_MEASURE.md, and start a story from the [API docs](https://www.wardwise.org/api-docs).

Ideas labelled `good first issue` are small builds from an open dataset.

## How an idea moves

1. **Open.** Someone files it with the form. It carries the `open` label.
2. **Claimed.** You comment "I'll take this". A maintainer assigns you, or links you if you're outside the org.
3. **In progress.** You put your new repo's URL (or your page PR) in the issue's **Work lives at** field. A
   maintainer swaps `open` for `in-progress`.
4. **Closed.** A maintainer closes the issue once the measure is pinned in
   [`measures.json`](https://github.com/ward-wise/ward-wise-measures/blob/main/measures.json), or once the page or
   site is live. The closing comment links the live URL.

Two labels park an idea without closing it. `needs-data` means it waits on someone finding or freeing a
data source. `not-feasible` means nobody can measure it at ward level with data we can get. The reason goes
in a comment.

Each idea also carries one `domain:<id>` label for the wellbeing domain it serves. The ids match the
`category` values in the Ward Wise API: `psychological_wellbeing`, `social_connectedness`,
`material_wellbeing`, `health`, `time_balance`, `religion_spiritual`, `lifelong_learning`, `good_governance`,
`community_vitality`, `physical_environment`, `culture`.

## What's in this repo

| Path | What it does |
|---|---|
| `.github/ISSUE_TEMPLATE/` | The two issue forms. wardwise.org reads their fields, so keep the labels and field headings stable. |
| `labels.json`, `scripts/create_labels.sh` | Every label, with colours. `scripts/create_labels.sh` creates or updates them on GitHub. |
| `scripts/seed_backlog_from_doc.py` | One-time seeding from the team's brainstorm doc (below). |
| `SEED_PREVIEW.md`, `seed/` | The reviewed seeding plan, the catalog it was matched against, and the raw model output. |

## Seeding from the brainstorm doc

The board starts from the team's metric brainstorm, a Google Doc. The script reads the doc's text export,
keeps only the eleven domain sections and "Views", and never parses the team and observer lists at the top.
gpt-6-luna matches each bullet against the live catalog (`/api/metrics`) and the site's pages. Each bullet
comes back `live` (already measured, so no issue), `partial` (the issue names the missing part) or `new`.
Credits in the doc ("- scott", "(debbie)") carry over as first names only.

```bash
python3 scripts/seed_backlog_from_doc.py --download                # dry run: SEED_PREVIEW.md + seed/seed_plan.json
python3 scripts/seed_backlog_from_doc.py --print-body health-02    # see one issue before it exists
scripts/create_labels.sh                                           # labels first
python3 scripts/seed_backlog_from_doc.py --create                  # open issues for partial + new rows
python3 scripts/seed_backlog_from_doc.py --create --skip culture-03,views-01   # leave rows out
```

`--create` reads the reviewed `seed/seed_plan.json` and never calls the model. Every seeded issue carries a
hidden `seed:` marker, so a rerun skips issues that already exist. Model calls are cached in
`seed/llm_cache.jsonl` with the model name, token usage and cost. A rerun on the same doc and catalog costs
nothing. Text the model wrote (the gap, the suggested source) says so in the issue body and is unverified.
