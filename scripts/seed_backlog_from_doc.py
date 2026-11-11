#!/usr/bin/env python3
"""Seed the ideas board once from the team's metric brainstorm doc.

    python3 scripts/seed_backlog_from_doc.py --doc metrics.txt        # dry run: table + seed/seed_plan.json
    python3 scripts/seed_backlog_from_doc.py --create                 # open issues for partial + new rows

The dry run reads the doc's txt export (or downloads it with --download), keeps only the eleven domain
sections and "Views", and asks gpt-6-luna to match each bullet against the live catalog at
www.wardwise.org/api/metrics (measures) or the site's views.json (stories). Each row comes back
`live` (already measured: no issue), `partial` (something close exists: the issue names the gap) or
`new`. It prints a markdown table and writes seed/seed_plan.json. Nothing touches GitHub.

--create never calls the LLM. It reads the reviewed seed/seed_plan.json and runs `gh issue create`
for every partial and new row, skipping rows already on the board (each body carries a seed marker).

The doc's Team Members / Interested Observers blocks hold personal emails. The parser drops every
line before the first domain heading and stops at any team heading, so those lines are never parsed,
sent to the LLM or written anywhere. Credits ("- scott", "(debbie)") become first names only.

LLM calls are cached in seed/llm_cache.jsonl: the request (the catalog replaced by its sha256; the
catalog itself is in seed/catalog_snapshot.json), the raw response, the model and the token usage.
A rerun on the same doc and catalog costs nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "seed"
PLAN_PATH = SEED_DIR / "seed_plan.json"
CACHE_PATH = SEED_DIR / "llm_cache.jsonl"
CATALOG_SNAPSHOT = SEED_DIR / "catalog_snapshot.json"
PREVIEW_PATH = ROOT / "SEED_PREVIEW.md"
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"

DOC_EXPORT_URL = ("https://docs.google.com/document/d/1eEpqLBliyaX0rq6i5rOMCSjC7bdsRqgWzFIg2gCcgaw"
                  "/export?format=txt")
METRICS_URL = "https://www.wardwise.org/api/metrics"
VIEWS_URL = "https://raw.githubusercontent.com/ward-wise/ward-wise-front-end-v2/HEAD/views.json"
SITE = "https://www.wardwise.org"
REPO = "ward-wise/ideas"

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
# Same key lookup as ward-wise-civic-tech (pipelines/civic_data/license_names.py): env first, then creds.json.
CREDS_PATH = ROOT.parent / "ward-wise-civic-tech" / "pipelines" / "creds.json"
MODEL = os.environ.get("SEED_MODEL", "gpt-6-luna")
PRICES = {"gpt-6-luna": {"in": 0.10, "out": 0.50}}  # $/1M tokens, 2026-10-27; cached input billed at 10%
REASONING_EFFORT = "high"  # a false "live" hides an idea from the board, so pay a little more thinking
CALL_TIMEOUT = 300

# Doc heading (lowercased, stripped) → domain id. Ids match the API's `category` values and the
# domain:<id> labels in labels.json.
HEADINGS = {
    "psychological wellbeing": "psychological_wellbeing",
    "social connectedness": "social_connectedness",
    "material wellbeing": "material_wellbeing",
    "health": "health",
    "time balance": "time_balance",
    "religion/spiritual": "religion_spiritual",
    "religion/spirituality": "religion_spiritual",
    "life long learning": "lifelong_learning",
    "lifelong learning": "lifelong_learning",
    "good governance": "good_governance",
    "community vitality": "community_vitality",
    "physical environment": "physical_environment",
    "culture": "culture",
    "views": "views",
}
STOP_HEADINGS = {"other notes / ideas", "participatory budgeting simulations"}
# Never parse past these, wherever they sit in the doc: they list people and their emails.
PRIVATE_HEADINGS = {"team info", "team members", "interested observers"}

GEOGRAPHY_DEFAULT = "Not sure"


# --- Parsing ----------------------------------------------------------------------------------

def _balanced(text: str) -> bool:
    return text.count("(") == text.count(")")


def split_credit(text: str) -> tuple[str, list[dict]]:
    """("chain vs non-chain percentage of businesses", [{"name": "Debbie"}]) from "... (debbie)".

    Handles a trailing " - name [more]" (the last dash outside parentheses, short tail, not a URL)
    and a trailing single-word "(name)". A ward number in the tail ("5th ward", "(49 / Rogers Park)")
    is kept; surnames, neighbourhoods and anything else are dropped.
    """
    text = text.strip()
    for match in reversed(list(re.finditer(r"\s+-\s+", text))):
        head, tail = text[:match.start()].rstrip(), text[match.end():].strip()
        first = tail.split()[0] if tail else ""
        if (len(tail) <= 30 and re.fullmatch(r"[A-Za-z]+", first) and "://" not in tail
                and _balanced(head) and _balanced(tail)):
            ward = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+ward\b", tail, re.I) or \
                re.search(r"\(\s*(\d{1,2})\s*/", tail)
            credit = {"name": first.capitalize()}
            if ward:
                credit["ward"] = str(int(ward.group(1)))
            return head, [credit]
    paren = re.search(r"\s*\(([A-Za-z]+)\)\s*$", text)
    if paren and paren.group(1).lower() not in {"challenging", "optional", "maybe", "tbd"}:
        return text[:paren.start()].rstrip(), [{"name": paren.group(1).capitalize()}]
    return text, []


def parse_doc(raw: str) -> list[dict]:
    """Top-level bullets (and stray non-bullet lines) inside domain sections, with their sub-bullets.

    In "Views" the top bullet is a framing ("clicking on a ward opens up an in-depth ward report") and
    each sub-bullet is its own story, so those split into one item per child.
    """
    lines = raw.replace("\ufeff", "").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip().lower() in HEADINGS), None)
    if start is None:
        raise SystemExit("No domain heading found in the doc; has its structure changed?")
    lines = lines[start:]  # everything above (team, observers, goals) is dropped unread

    items: list[dict] = []
    section = None
    current = None
    for line in lines:
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered in PRIVATE_HEADINGS or lowered in STOP_HEADINGS:
            break
        if "@" in stripped:
            continue  # an email never belongs in a public issue; skip the whole line
        if lowered in HEADINGS:
            section, current = HEADINGS[lowered], None
            continue
        if not stripped or section is None:
            continue
        bullet = re.match(r"^(\s*)\*\s*(.*)$", line)
        if bullet:
            depth = len(bullet.group(1).expandtabs(3)) // 3
            text = bullet.group(2).strip()
            if not text:
                continue
            if depth == 0:
                current = {"section": section, "text": text, "children": []}
                items.append(current)
            elif current is not None:
                current["children"].append({"depth": depth, "text": text})
        else:  # a stray line under a heading ("access to resources (...) - sandra") is an idea too
            current = {"section": section, "text": stripped, "children": []}
            items.append(current)

    out: list[dict] = []
    for item in items:
        if item["section"] == "views" and item["children"]:
            parent = item["text"]
            for child in item["children"]:
                if child["depth"] == 1:
                    out.append({"section": "views", "text": child["text"], "context": parent, "children": []})
                elif out:
                    out[-1]["children"].append({"depth": child["depth"] - 1, "text": child["text"]})
        else:
            out.append(item)

    counters: dict[str, int] = {}
    for item in out:
        item["text"], credits = split_credit(item["text"])
        for child in item["children"]:
            child["text"], child_credits = split_credit(child["text"])
            credits += child_credits
        seen, unique = set(), []
        for credit in credits:
            if credit["name"] not in seen:
                seen.add(credit["name"])
                unique.append(credit)
        item["credits"] = unique
        item["kind"] = "story" if item["section"] == "views" else "measure"
        counters[item["section"]] = counters.get(item["section"], 0) + 1
        item["id"] = f"{item['section']}-{counters[item['section']]:02d}"
        basis = item["section"] + "|" + item["text"].lower()
        item["seed_key"] = hashlib.sha1(basis.encode()).hexdigest()[:10]
    assert not any("@" in json.dumps(item) for item in out), "an email leaked into the parsed items"
    return out


# --- Catalogs ---------------------------------------------------------------------------------

def fetch_json(url: str) -> dict:
    with urlopen(Request(url, headers={"Accept": "application/json", "User-Agent": "ward-wise-ideas-seed"}),
                 timeout=120) as response:
        return json.loads(response.read().decode())


def load_catalogs() -> dict:
    metrics = fetch_json(METRICS_URL)["metrics"]
    views = fetch_json(VIEWS_URL)["views"]
    measures = []
    for m in sorted(metrics, key=lambda m: m["metric_id"]):
        nominator = (m.get("nominator") or {}).get("name") or ""
        measures.append({
            "metric_id": m["metric_id"],
            "label": m.get("label") or "",
            "category": m.get("category") or "",
            "description": m.get("description") or "",
            "source": m.get("source") or "",
            "nominator_first_name": nominator.split()[0] if nominator else "",
        })
    pages = [{"path": v["path"], "label": v.get("label") or "", "blurb": v.get("blurb") or ""}
             for v in views if v.get("public", True)]
    catalog = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "metrics_url": METRICS_URL, "views_url": VIEWS_URL, "measures": measures, "pages": pages}
    stable = json.dumps({"measures": measures, "pages": pages}, sort_keys=True)
    catalog["sha256"] = hashlib.sha256(stable.encode()).hexdigest()
    return catalog


def catalog_text(catalog: dict, kind: str) -> str:
    if kind == "story":
        return "\n".join(f"{p['path']} | {p['label']} | {p['blurb']}" for p in catalog["pages"])
    rows = []
    for m in catalog["measures"]:
        row = f"{m['metric_id']} | {m['label']} | {m['category']} | {m['description']} | source: {m['source']}"
        if m["nominator_first_name"]:
            row += f" | nominated by {m['nominator_first_name']}"
        rows.append(row)
    return "\n".join(rows)


# --- LLM matching -----------------------------------------------------------------------------

PROMPT_MEASURE = """You triage a Chicago civic group's brainstorm of ward-level wellbeing measures against the
measures its site Ward Wise already publishes. Each idea is a short, informal bullet from a shared doc,
sometimes with sub-bullets that list concrete variants or notes. The catalog below lists every live measure:
metric_id | label | category | description | source | (who nominated it, if anyone).

For each idea return:
- status: "live" only if one or more catalog measures already answer the idea AS STATED (including its
  sub-bullets). "partial" if something close exists but a real part of the idea is missing (another variant,
  a different angle, a sub-bullet nobody built). "new" if nothing in the catalog is close. When unsure
  between live and partial, choose partial: a wrong "live" hides the idea from volunteers.
- matches: the metric_ids that cover it or come closest (empty for most "new"). Use exact ids from the catalog.
- gap: for partial/new, one or two plain sentences on what a new measure would add. Empty for live.
- title: a plain issue title under 70 characters naming the measure to build (for partial, name the missing
  part). No prefixes, no quotes.
- likely_source: a public dataset or agency that could feed it at ward or finer geography, or "" if you
  don't know one. Prefer City of Chicago, Cook County, CDPH, CPS, ACS and other open sources.
- needs_data: true if no public source at ward-or-finer geography plausibly exists.
- expert_wanted: true if the method, framing or definition needs expert advice before anyone builds
  (survey questions, sentiment, "beauty", performance ratings).
- good_first_issue: true only for a simple count or distance from an open point dataset.
- reason: one short sentence on why you chose the status.

Catalog:
"""

PROMPT_STORY = """You triage a Chicago civic group's brainstorm of story/dashboard ideas against the pages its site
Ward Wise already has. The page list below gives path | label | blurb.

For each idea return:
- status: "live" only if an existing page already does what the idea asks. "partial" if a page covers part
  of it. "new" if none does. When unsure between live and partial, choose partial.
- matches: the paths of the pages that cover it or come closest. Use exact paths from the list.
- gap: for partial/new, one or two plain sentences on what the new page or section would add.
- title: a plain issue title under 70 characters naming the page or section to build.
- likely_source: "" (not used for stories).
- needs_data, expert_wanted, good_first_issue: as for any build ticket; usually false.
- audience: the kind of person the page is for, in a few words.
- question: the question that person needs answered, in their words, one sentence.
- reason: one short sentence on why you chose the status.

Pages:
"""


def response_schema(kind: str) -> dict:
    properties = {
        "id": {"type": "string"},
        "status": {"type": "string", "enum": ["live", "partial", "new"]},
        "matches": {"type": "array", "items": {"type": "string"}},
        "gap": {"type": "string"},
        "title": {"type": "string"},
        "likely_source": {"type": "string"},
        "needs_data": {"type": "boolean"},
        "expert_wanted": {"type": "boolean"},
        "good_first_issue": {"type": "boolean"},
        "reason": {"type": "string"},
    }
    if kind == "story":
        properties["audience"] = {"type": "string"}
        properties["question"] = {"type": "string"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": f"seed_{kind}_triage",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"results": {"type": "array", "items": {
                    "type": "object", "properties": properties,
                    "required": list(properties), "additionalProperties": False}}},
                "required": ["results"],
                "additionalProperties": False,
            },
        },
    }


def openai_api_key() -> str | None:
    if os.getenv("OPENAI_API_KEY"):
        return os.getenv("OPENAI_API_KEY")
    try:
        creds = json.loads(CREDS_PATH.read_text())
    except Exception:
        return None
    section = creds.get("openai") or {}
    return section.get("api_key") or section.get("key") or creds.get("openai_api_key")


def _load_cache() -> dict[str, dict]:
    cache = {}
    if CACHE_PATH.exists():
        for line in CACHE_PATH.read_text().splitlines():
            try:
                row = json.loads(line)
                cache[row["key"]] = row
            except (ValueError, KeyError):
                continue
    return cache


def call_cost(usage: dict, model: str) -> float:
    price = PRICES.get(model)
    if not price or not usage:
        return 0.0
    prompt = usage.get("prompt_tokens", 0)
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    return ((prompt - cached) * price["in"] + cached * price["in"] * 0.1 + completion * price["out"]) / 1e6


def llm_payload(item: dict) -> dict:
    """What the model sees: the idea's words without anyone's name."""
    payload = {"id": item["id"], "domain": item["section"], "idea": item["text"],
               "sub_bullets": ["  " * (c["depth"] - 1) + c["text"] for c in item["children"]]}
    if item.get("context"):
        payload["context"] = item["context"]
    return payload


def triage(items: list[dict], catalog: dict) -> tuple[dict[str, dict], dict]:
    """{item id: result} for every item, one call per doc section, cached by request content."""
    cache = _load_cache()
    api_key = openai_api_key()
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item["section"], []).append(item)

    jobs = []
    for section, group in groups.items():
        kind = group[0]["kind"]
        system = (PROMPT_STORY if kind == "story" else PROMPT_MEASURE) + catalog_text(catalog, kind)
        user = json.dumps([llm_payload(item) for item in group], ensure_ascii=False)
        key = hashlib.sha256(json.dumps([MODEL, REASONING_EFFORT, system, user]).encode()).hexdigest()
        jobs.append((section, kind, system, user, key))

    def run(job):
        section, kind, system, user, key = job
        if key in cache:
            return cache[key], False
        if not api_key:
            raise SystemExit("No OpenAI key (OPENAI_API_KEY or ward-wise-civic-tech/pipelines/creds.json).")
        body = {"model": MODEL, "reasoning_effort": REASONING_EFFORT,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": response_schema(kind)}
        request = Request(OPENAI_URL, data=json.dumps(body).encode(),
                          headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        with urlopen(request, timeout=CALL_TIMEOUT) as response:
            raw = response.read().decode()
        payload = json.loads(raw)
        logged = dict(body)
        logged["messages"] = [{"role": "system", "content": system.split("\n\n")[0] + "\n\n<catalog sha256:"
                               + catalog["sha256"] + " — see seed/catalog_snapshot.json>"},
                              {"role": "user", "content": user}]
        row = {"key": key, "section": section, "model": payload.get("model") or MODEL, "requested_model": MODEL,
               "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "catalog_sha256": catalog["sha256"], "request": logged, "response_raw": raw,
               "usage": payload.get("usage") or {}}
        row["cost_usd"] = round(call_cost(row["usage"], MODEL), 6)
        return row, True

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(run, jobs))

    SEED_DIR.mkdir(exist_ok=True)
    stats = {"calls": len(jobs), "cached": 0, "new_calls": 0, "cost_this_run_usd": 0.0, "cost_recorded_usd": 0.0}
    results: dict[str, dict] = {}
    with CACHE_PATH.open("a") as out:
        for row, fresh in outcomes:
            if fresh:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                stats["new_calls"] += 1
                stats["cost_this_run_usd"] += row["cost_usd"]
            else:
                stats["cached"] += 1
            stats["cost_recorded_usd"] += row.get("cost_usd", 0.0)
            content = json.loads(json.loads(row["response_raw"])["choices"][0]["message"]["content"])
            for result in content.get("results", []):
                result["model"] = row["model"]
                result["llm_call_key"] = row["key"]
                results[result["id"]] = result
    stats["cost_this_run_usd"] = round(stats["cost_this_run_usd"], 4)
    stats["cost_recorded_usd"] = round(stats["cost_recorded_usd"], 4)
    return results, stats


def check_result(item: dict, result: dict | None, catalog: dict) -> dict:
    """Code checks on the model's answer: ids must exist; live/partial need at least one real match."""
    if result is None:
        return {"status": "new", "matches": [], "gap": "", "title": item["text"][:70], "likely_source": "",
                "needs_data": False, "expert_wanted": True, "good_first_issue": False,
                "reason": "No model answer for this row; review by hand.", "checks": ["missing_answer"]}
    known = ({p["path"] for p in catalog["pages"]} if item["kind"] == "story"
             else {m["metric_id"] for m in catalog["measures"]})
    checks = []
    unknown = [m for m in result["matches"] if m not in known]
    if unknown:
        checks.append("dropped_unknown_matches:" + ",".join(unknown))
    result["matches"] = [m for m in result["matches"] if m in known]
    if result["status"] in ("live", "partial") and not result["matches"]:
        checks.append(f"{result['status']}_without_match→new")
        result["status"] = "new"
    if item["kind"] == "measure" and result["status"] == "partial":
        # The doc credits "lead in parks - scott"; the catalog has Scott's park-fountain lead measure. The
        # nominee's own idea shipped, so the row is live. Only for single-credit, single-line ideas: a
        # multi-part bullet ("alderman performance") can still have unbuilt parts.
        nominated = {m["metric_id"]: m["nominator_first_name"].lower() for m in catalog["measures"]}
        names = {c["name"].lower() for c in item["credits"]}
        shipped = [m for m in result["matches"] if nominated.get(m) in names]
        if shipped and len(item["credits"]) == 1 and not item["children"]:
            checks.append("nominator_measure_live:" + ",".join(shipped) + "→live")
            result["status"] = "live"
    if result["good_first_issue"] and (result["needs_data"] or result["expert_wanted"]):
        result["good_first_issue"] = False  # not a first issue if it waits on data or advice
    result["checks"] = checks
    return result


# --- Output -----------------------------------------------------------------------------------

def labels_for(row: dict) -> list[str]:
    labels = ["story-idea" if row["kind"] == "story" else "measure-idea", "open"]
    if row["kind"] == "measure":
        labels.append(f"domain:{row['section']}")
    if row["needs_data"]:
        labels.append("needs-data")
    if row["expert_wanted"]:
        labels.append("expert-wanted")
    if row["good_first_issue"]:
        labels.append("good first issue")
    return labels


def match_link(row: dict, match: str) -> str:
    if row["kind"] == "story":
        return f"[{match}]({SITE}{match})"
    return f"[`{match}`]({SITE}/dictionary#metric-entry-{match})"


def credit_text(credits: list[dict]) -> str:
    return ", ".join(c["name"] + (f" (ward {c['ward']})" if c.get("ward") else "") for c in credits)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_preview(plan: dict) -> str:
    rows = plan["rows"]
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("live", "partial", "new")}
    llm = plan["llm"]
    lines = [
        "# Seed preview",
        "",
        f"Dry run of `scripts/seed_backlog_from_doc.py` on {plan['generated_at'][:10]}. Nothing has been posted.",
        f"Catalog: {plan['catalog']['measures']} live measures from {METRICS_URL} and "
        f"{plan['catalog']['pages']} public pages (fetched {plan['catalog']['fetched_at']}).",
        f"Matching: {plan['model']} (reasoning effort {REASONING_EFFORT}), {llm['calls']} calls, "
        f"recorded cost ${llm['cost_recorded_usd']:.4f}. Every match below is the model's call, checked only "
        "for ids that exist. Review the statuses before running `--create`.",
        "",
        f"**{len(rows)} ideas: {counts['live']} live (no issue), {counts['partial']} partial, {counts['new']} new.** "
        f"`--create` would open {counts['partial'] + counts['new']} issues.",
        "",
        "| # | Domain | Idea (doc text) | Status | Issue title | Closest live | Credit | Labels |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        idea = row["text"] + (f" — {'; '.join(c['text'] for c in row['children'])}" if row["children"] else "")
        if len(idea) > 160:
            idea = idea[:157] + "…"
        matches = ", ".join(match_link(row, m) for m in row["matches"][:4]) + (" …" if len(row["matches"]) > 4 else "")
        title = "" if row["status"] == "live" else row["title"]
        labels = "" if row["status"] == "live" else " ".join(f"`{l}`" for l in row["labels"])
        flag = " ⚠" if row["checks"] else ""
        lines.append(f"| {row['id']} | {row['section']} | {_cell(idea)} | **{row['status']}**{flag} | {_cell(title)} | "
                     f"{matches} | {credit_text(row['credits'])} | {labels} |")
    flagged = [r for r in rows if r["checks"]]
    if flagged:
        lines += ["", "⚠ = a code check changed the model's answer:", ""]
        lines += [f"- {r['id']}: {', '.join(r['checks'])}" for r in flagged]
    lines += ["", "Each row's reason, gap and suggested source are in `seed/seed_plan.json`.", ""]
    return "\n".join(lines)


def form_labels(name: str) -> dict[str, str]:
    """{field id: label} from an issue form, so seeded bodies use the exact headings the forms produce."""
    labels, current = {}, None
    for line in (TEMPLATES / name).read_text().splitlines():
        found = re.match(r"\s+id:\s*(\S+)", line)
        if found:
            current = found.group(1)
        found = re.match(r"\s+label:\s*(.+)$", line)
        if found and current:
            labels[current] = found.group(1).strip().strip('"')
            current = None
    return labels


def issue_body(row: dict) -> str:
    """Same `### Label` sections an issue-form submission produces, so the site reads both alike."""
    sections: list[tuple[str, str]] = []
    credits = row["credits"]
    nominator = ", ".join(c["name"] for c in credits) or "_No response_"
    ward = ", ".join(c["ward"] for c in credits if c.get("ward")) or "_No response_"
    related = "\n".join(f"- {match_link(row, m)}" for m in row["matches"]) or "_No response_"
    doc_words = row["text"] + "".join(f"\n{'  ' * (c['depth'] - 1)}- {c['text']}" for c in row["children"])
    if row["kind"] == "measure":
        f = form_labels("measure-idea.yml")
        what = f"From the team's brainstorm doc:\n\n> {doc_words.replace(chr(10), chr(10) + '> ')}"
        if row["gap"]:
            what += f"\n\nWhat a new measure would add (drafted at seeding by {row['model']}, unverified): {row['gap']}"
        source = (f"{row['likely_source']} (suggested at seeding by {row['model']}, unverified)"
                  if row["likely_source"] else "_No response_")
        sections = [
            (f["what"], what),
            (f["why"], "_Not written yet. If this matters to your ward, add a comment saying why._"),
            (f["source"], source),
            (f["geography"], GEOGRAPHY_DEFAULT),
            (f["domain"], row["section"].replace("_", " ").replace("religion spiritual", "religion/spirituality")),
            (f["related"], related),
            (f["nominator"], nominator),
            (f["ward"], ward),
            (f["work_lives_at"], "_No response_"),
        ]
    else:
        f = form_labels("story-idea.yml")
        what = f"From the team's brainstorm doc:\n\n> {(row.get('context') or '') + ': ' if row.get('context') else ''}{doc_words}"
        sections = [
            (f["audience"], row.get("audience") or "_No response_"),
            (f["question"], (row.get("question") or "") + "\n\n" + what
             + (f"\n\nWhat the page would add (drafted at seeding by {row['model']}, unverified): {row['gap']}"
                if row["gap"] else "")),
            (f["measures"], "_Not sure yet._"),
            (f["related"], related),
            (f["nominator"], nominator),
            (f["ward"], ward),
            (f["work_lives_at"], "_No response_"),
        ]
    body = "\n\n".join(f"### {label}\n\n{value}" for label, value in sections)
    return body + f"\n\n<!-- seed:{row['seed_key']} -->\n"


# --- Commands ---------------------------------------------------------------------------------

def dry_run(doc_path: Path) -> None:
    items = parse_doc(doc_path.read_text(encoding="utf-8"))
    catalog = load_catalogs()
    SEED_DIR.mkdir(exist_ok=True)
    CATALOG_SNAPSHOT.write_text(json.dumps(catalog, indent=1, ensure_ascii=False) + "\n")
    results, stats = triage(items, catalog)
    rows = []
    for item in items:
        result = check_result(item, results.get(item["id"]), catalog)
        row = {**item, **{k: v for k, v in result.items() if k != "id"}}
        row.setdefault("model", MODEL)
        row["labels"] = labels_for(row)
        rows.append(row)
    plan = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "catalog": {"sha256": catalog["sha256"], "fetched_at": catalog["fetched_at"],
                    "measures": len(catalog["measures"]), "pages": len(catalog["pages"])},
        "llm": stats,
        "rows": rows,
    }
    assert "@" not in json.dumps(plan["rows"]), "an email leaked into the plan"
    PLAN_PATH.write_text(json.dumps(plan, indent=1, ensure_ascii=False) + "\n")
    preview = render_preview(plan)
    PREVIEW_PATH.write_text(preview)
    print(preview)
    print(f"LLM: {stats['calls']} calls ({stats['cached']} cached), ${stats['cost_this_run_usd']:.4f} this run, "
          f"${stats['cost_recorded_usd']:.4f} recorded.", file=sys.stderr)


def existing_seed_keys(repo: str) -> set[str]:
    out = subprocess.run(["gh", "issue", "list", "--repo", repo, "--state", "all", "--limit", "1000",
                          "--json", "body"], check=True, capture_output=True, text=True).stdout
    return set(re.findall(r"<!-- seed:([0-9a-f]+) -->", " ".join(i["body"] or "" for i in json.loads(out))))


def create(repo: str, skip: set[str], only: set[str]) -> None:
    plan = json.loads(PLAN_PATH.read_text())
    done = existing_seed_keys(repo)
    created = 0
    for row in plan["rows"]:
        if row["status"] == "live" or row["id"] in skip or (only and row["id"] not in only):
            continue
        if row["seed_key"] in done:
            print(f"skip {row['id']}: already on the board")
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write(issue_body(row))
        command = ["gh", "issue", "create", "--repo", repo, "--title", row["title"], "--body-file", handle.name]
        for label in row["labels"]:
            command += ["--label", label]
        url = subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()
        os.unlink(handle.name)
        row["issue_url"] = url
        created += 1
        print(f"{row['id']}: {url}")
        PLAN_PATH.write_text(json.dumps(plan, indent=1, ensure_ascii=False) + "\n")  # keep URLs if a later call fails
    print(f"Created {created} issues on {repo}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--doc", type=Path, help="the Google Doc's txt export")
    parser.add_argument("--download", action="store_true", help="download the txt export to a temp file first")
    parser.add_argument("--create", action="store_true", help="open issues from the reviewed seed/seed_plan.json")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--skip", default="", help="comma-separated row ids to leave out of --create")
    parser.add_argument("--only", default="", help="comma-separated row ids to create (default: all partial + new)")
    parser.add_argument("--print-body", metavar="ID", help="print the issue body a row would get, then exit")
    args = parser.parse_args()

    if args.print_body:
        row = next(r for r in json.loads(PLAN_PATH.read_text())["rows"] if r["id"] == args.print_body)
        print(f"# {row['title']}\nlabels: {', '.join(row['labels'])}\n\n{issue_body(row)}")
        return
    if args.create:
        create(args.repo, {s for s in args.skip.split(",") if s}, {s for s in args.only.split(",") if s})
        return
    if args.download:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.txt"  # never kept: the doc's top lists people's emails
            with urlopen(DOC_EXPORT_URL, timeout=60) as response:
                path.write_bytes(response.read())
            dry_run(path)
        return
    if not args.doc:
        parser.error("pass --doc <txt export> or --download")
    dry_run(args.doc)


if __name__ == "__main__":
    main()
