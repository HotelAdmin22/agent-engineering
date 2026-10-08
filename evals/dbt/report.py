"""Summaries for one run, and the baseline-vs-routed comparison.

    uv run python -m evals.dbt.report --run runs/dbt/<run_id>
    uv run python -m evals.dbt.report --compare runs/dbt/<baseline_id> runs/dbt/<routed_id>

The denominator is always the run's task count (64 for a full run). A missing
prediction, a timeout or an agent_error is a fail. Any harness_error makes the
run non-reportable until it is rerun (--resume).
"""

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

from . import config as config_mod
from . import records

ROLES = ("main", "explore", "diagnose")
STAGES = ("retrieval", "generation", "execution", "interpretation")


def load_results(run_dir: Path) -> list[dict]:
    return [records.read_json(p) for p in sorted(Path(run_dir).glob("attempt_*/*/result.json"))]


def _rate(n: int, d: int) -> float:
    return round(n / d, 4) if d else 0.0


def pass_metrics(results: list[dict], task_ids: list[str], attempts: int) -> dict:
    n = len(task_ids)
    by_attempt = defaultdict(dict)
    for r in results:
        if r["instance_id"] in task_ids:
            by_attempt[r["attempt"]][r["instance_id"]] = bool(r["passed"])
    per_attempt = [_rate(sum(by_attempt[a].values()), n) for a in range(1, attempts + 1)]
    all_pass = [i for i in task_ids if all(by_attempt[a].get(i) for a in range(1, attempts + 1))]
    return {
        "tasks": n,
        "per_attempt_pass_rate": per_attempt,
        "noise_range": [min(per_attempt), max(per_attempt)] if per_attempt else [0, 0],
        "pass@1": round(statistics.mean(per_attempt), 4) if per_attempt else 0.0,
        f"pass^{attempts}": _rate(len(all_pass), n),
        "tasks_passed_every_attempt": len(all_pass),
    }


def _stat(values: list[float]) -> dict:
    if not values:
        return {"mean": 0, "median": 0}
    return {"mean": round(statistics.mean(values), 4), "median": round(statistics.median(values), 4)}


def triage_rows(run_dir: Path) -> list[dict]:
    p = Path(run_dir) / "triage.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def summarize(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    manifest = records.read_json(run_dir / "run_manifest.json")
    results = load_results(run_dir)
    sel = manifest["selection"]
    task_ids, attempts = sel["tasks"], sel["attempts"]
    expected = len(task_ids) * attempts

    harness = [r for r in results if r["status"] == "harness_error"]
    splits = defaultdict(list)
    for r in results:
        if r["attempt"] == 1:
            splits[r["split"]].append(r["instance_id"])

    by_role = {}
    for role in ROLES:
        rows = [r["usage_by_role"][role] for r in results if role in (r.get("usage_by_role") or {})]
        if not rows:
            continue
        by_role[role] = {
            "model": rows[0]["model"],
            "models_returned": sorted({m for u in rows for m in u.get("models_returned", [])}),
            "task_attempts": len(rows),
            "input_tokens": _stat([u["input_tokens"] for u in rows]),
            "cached_input_tokens": _stat([u["cached_input_tokens"] for u in rows]),
            "output_tokens": _stat([u["output_tokens"] for u in rows]),
            "reasoning_tokens": _stat([u["reasoning_tokens"] for u in rows]),
            "cost_usd": _stat([u["cost_usd"] for u in rows]),
            "cost_usd_total": round(sum(u["cost_usd"] for u in rows), 4),
        }

    sub_calls = defaultdict(list)
    for r in results:
        for k in ("explore", "diagnose", "errors", "budget_stops"):
            sub_calls[k].append(r["subagent_calls"].get(k, 0))
    total_sub = sum(sub_calls["explore"]) + sum(sub_calls["diagnose"])

    tool_by_fn = Counter()
    for r in results:
        tool_by_fn.update(r.get("tool_calls_by_function") or {})

    triage = triage_rows(run_dir)
    stage_counts = Counter(t["stage"] for t in triage if t.get("stage"))
    examples = {}
    for stage in STAGES:
        row = next((t for t in triage if t.get("stage") == stage and t.get("example")), None) \
            or next((t for t in triage if t.get("stage") == stage), None)
        if row:
            examples[stage] = {"instance_id": row["instance_id"], "attempt": row["attempt"],
                               "example": row.get("example", ""), "notes": row.get("notes", "")}
    failures = [r for r in results if not r["passed"]]
    passed = sum(1 for r in results if r["passed"])
    total_cost = sum(r["total_cost_usd"] for r in results)

    reportable_reasons = []
    if manifest.get("git_dirty"):
        reportable_reasons.append("git tree was dirty")
    if sel.get("subset") or sel.get("attempts_override"):
        reportable_reasons.append("task subset or attempts override (smoke run)")
    if manifest["config"]["sandbox"].get("keep_failed_containers"):
        reportable_reasons.append("keep_failed_containers = true")
    if harness:
        reportable_reasons.append(f"{len(harness)} harness_error(s); rerun with --resume")
    if len(results) != expected:
        reportable_reasons.append(f"{len(results)}/{expected} task attempts have results")
    image_ids = {r.get("container", {}).get("image_id") for r in results} - {None}

    return {
        "run_id": manifest["run_id"],
        "experiment": manifest["config"]["experiment"],
        "image_id": manifest["sandbox"]["image_id"],
        "concurrency": manifest["concurrency"],
        "attempts": attempts,
        "task_attempts": {"expected": expected, "present": len(results)},
        "reportable": not reportable_reasons,
        "not_reportable_because": reportable_reasons,
        "overall": pass_metrics(results, task_ids, attempts),
        "splits": {s: pass_metrics(results, ids, attempts) for s, ids in sorted(splits.items())},
        "status_counts": dict(Counter(r["status"] for r in results)),
        "failure_categories": dict(Counter(r["failure_category"] for r in failures)),
        "triage_stages": dict(stage_counts),
        "triage_examples": examples,
        "untriaged_failures": sum(1 for t in triage if not t.get("stage")),
        "by_role": by_role,
        "per_task_attempt": {
            "cost_usd": _stat([r["total_cost_usd"] for r in results]),
            "duration_s": _stat([r["duration_s"] for r in results]),
            "main_turns": _stat([r["main_turns"] for r in results]),
        },
        "cost_usd_total": round(total_cost, 4),
        "cost_per_passed_task_attempt": round(total_cost / passed, 4) if passed else None,
        "delegation": {
            "calls_per_task_attempt": {k: _stat(v) for k, v in sub_calls.items()},
            "error_rate": _rate(sum(sub_calls["errors"]), total_sub),
            "budget_stop_rate": _rate(sum(sub_calls["budget_stops"]), total_sub),
        } if total_sub or manifest["config"]["experiment"] == "routed" else None,
        "tools": {
            "calls_by_function": dict(tool_by_fn),
            "tool_errors": sum(r["tool_errors"] for r in results),
            "gateway_rejections": sum(r["gateway_rejections"] for r in results),
            "dbt_calls": sum(r["dbt_calls"] for r in results),
            "ooms": sum(1 for r in results if r["container"].get("oom_killed")),
        },
        "_image_ids_seen": sorted(image_ids),
    }


def to_markdown(s: dict) -> str:
    o = s["overall"]
    k = s["attempts"]
    lines = [
        f"# Spider2-DBT -- {s['experiment']} ({s['run_id']})",
        "",
        f"**Reportable:** {'yes' if s['reportable'] else 'no -- ' + '; '.join(s['not_reportable_because'])}",
        f"Image `{s['image_id']}`, concurrency {s['concurrency']}, {o['tasks']} tasks x {k} attempts.",
        "",
        "## Pass rates",
        "",
        "| Split | Tasks | Per-attempt | Noise range | pass@1 | pass^" + str(k) + " |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, m in [("all", o), *s["splits"].items()]:
        lines.append(f"| {name} | {m['tasks']} | {', '.join(f'{x:.3f}' for x in m['per_attempt_pass_rate'])} "
                     f"| {m['noise_range'][0]:.3f}-{m['noise_range'][1]:.3f} | {m['pass@1']:.3f} "
                     f"| {m[f'pass^{k}']:.3f} |")
    lines += ["", "## Status and failures", "",
              f"Status: {s['status_counts']}", "",
              f"Failure categories (automatic): {s['failure_categories']}", "",
              f"Triage stages (manual, triage.csv): {s['triage_stages'] or 'none yet'}; "
              f"untriaged failures: {s['untriaged_failures']}", ""]
    for stage, ex in s["triage_examples"].items():
        lines.append(f"- **{stage}**: {ex['instance_id']} a{ex['attempt']} -- {ex['example'] or ex['notes']}")
    lines += ["", "## Cost, tokens and time per task attempt", "",
              "| Role | Model | Input (mean) | Output (mean) | Reasoning (mean) | Cost mean / median |",
              "| --- | --- | --- | --- | --- | --- |"]
    for role, r in s["by_role"].items():
        lines.append(f"| {role} | {r['model']} | {r['input_tokens']['mean']:,.0f} | "
                     f"{r['output_tokens']['mean']:,.0f} | {r['reasoning_tokens']['mean']:,.0f} | "
                     f"${r['cost_usd']['mean']:.4f} / ${r['cost_usd']['median']:.4f} |")
    p = s["per_task_attempt"]
    lines += ["",
              f"Total cost ${s['cost_usd_total']:.4f}; per passed task attempt "
              f"{'$%.4f' % s['cost_per_passed_task_attempt'] if s['cost_per_passed_task_attempt'] else 'n/a'}. "
              f"Duration mean {p['duration_s']['mean']}s, median {p['duration_s']['median']}s. "
              f"Main turns mean {p['main_turns']['mean']}.", ""]
    if s["delegation"]:
        d = s["delegation"]
        lines += ["## Delegation", "",
                  f"Calls per task attempt: explore {d['calls_per_task_attempt']['explore']}, "
                  f"diagnose {d['calls_per_task_attempt']['diagnose']}. "
                  f"Error rate {d['error_rate']}, budget-stop rate {d['budget_stop_rate']}.", ""]
    t = s["tools"]
    lines += ["## Tools", "", f"Calls: {t['calls_by_function']}", "",
              f"Tool errors {t['tool_errors']}, gateway rejections {t['gateway_rejections']}, "
              f"dbt calls {t['dbt_calls']}, OOMs {t['ooms']}.", ""]
    return "\n".join(lines)


def write_summary(run_dir: Path) -> dict:
    s = summarize(run_dir)
    records.write_json_atomic(Path(run_dir) / "summary.json", s)
    (Path(run_dir) / "summary.md").write_text(to_markdown(s), encoding="utf-8")
    return s


# --- compare -------------------------------------------------------------

class CompareRefused(Exception):
    pass


def compare(run_a: Path, run_b: Path) -> dict:
    ma = records.read_json(Path(run_a) / "run_manifest.json")
    mb = records.read_json(Path(run_b) / "run_manifest.json")
    problems = []
    for label, get in [
        ("manifest", lambda m: m["benchmark"]["manifest_sha256"]),
        ("splits", lambda m: m["benchmark"]["splits_sha256"]),
        ("pricing", lambda m: m["pricing"]),
        ("main model", lambda m: m["roles"]["main"]["model"]),
        ("main settings", lambda m: (m["roles"]["main"]["settings_sent"],
                                     m["roles"]["main"]["max_turns"],
                                     m["roles"]["main"]["prompt_sha256"])),
        ("image ID", lambda m: m["sandbox"]["image_id"]),
        ("concurrency", lambda m: m["concurrency"]),
        ("task selection", lambda m: (m["selection"]["tasks"], m["selection"]["attempts"])),
    ]:
        if get(ma) != get(mb):
            problems.append(label)
    cfg_diffs = config_mod.differences(ma["config"], mb["config"])
    cfg_diffs = [d for d in cfg_diffs if not d.startswith(("config_path", "spider2_root_resolved"))]
    if cfg_diffs:
        problems.append(f"config outside the experiment surface: {cfg_diffs[:5]}")
    if problems:
        raise CompareRefused("runs are not comparable: " + "; ".join(problems))

    sa, sb = summarize(run_a), summarize(run_b)
    k = sa["attempts"]
    ra, rb = load_results(run_a), load_results(run_b)

    def majority(results):
        votes = defaultdict(list)
        for r in results:
            votes[r["instance_id"]].append(bool(r["passed"]))
        return {i: sum(v) * 2 > len(v) for i, v in votes.items()}

    va, vb = majority(ra), majority(rb)
    flips_up = sorted(i for i in va if not va[i] and vb.get(i))
    flips_down = sorted(i for i in va if va[i] and vb.get(i) is False)

    def tokens(s, role="main"):
        r = s["by_role"].get(role)
        return (r["input_tokens"]["mean"] + r["output_tokens"]["mean"]) if r else 0

    a1, b1 = sa["overall"]["pass@1"], sb["overall"]["pass@1"]
    lo = min(sa["overall"]["noise_range"][0], sb["overall"]["noise_range"][0])
    hi = max(sa["overall"]["noise_range"][1], sb["overall"]["noise_range"][1])
    within = (sa["overall"]["noise_range"][0] <= b1 <= sa["overall"]["noise_range"][1]
              or sb["overall"]["noise_range"][0] <= a1 <= sb["overall"]["noise_range"][1])
    return {
        "a": {"run_id": sa["run_id"], "experiment": sa["experiment"], "reportable": sa["reportable"]},
        "b": {"run_id": sb["run_id"], "experiment": sb["experiment"], "reportable": sb["reportable"]},
        "pass@1": [a1, b1],
        f"pass^{k}": [sa["overall"][f"pass^{k}"], sb["overall"][f"pass^{k}"]],
        "noise_range": [sa["overall"]["noise_range"], sb["overall"]["noise_range"]],
        "combined_noise_span": [lo, hi],
        "verdict": "no clear change" if within else ("better" if b1 > a1 else "worse"),
        "cost_per_task_attempt": [sa["per_task_attempt"]["cost_usd"]["mean"],
                                  sb["per_task_attempt"]["cost_usd"]["mean"]],
        "cost_per_passed_task_attempt": [sa["cost_per_passed_task_attempt"],
                                         sb["cost_per_passed_task_attempt"]],
        "duration_s_mean": [sa["per_task_attempt"]["duration_s"]["mean"],
                            sb["per_task_attempt"]["duration_s"]["mean"]],
        "main_tokens_mean": [tokens(sa), tokens(sb)],
        "majority_flips_fail_to_pass": flips_up,
        "majority_flips_pass_to_fail": flips_down,
    }


def compare_markdown(c: dict) -> str:
    a, b = c["a"], c["b"]
    k = next(key for key in c if key.startswith("pass^"))
    rows = [
        ("pass@1", *c["pass@1"]),
        (k, *c[k]),
        ("noise range", *[f"{x[0]:.3f}-{x[1]:.3f}" for x in c["noise_range"]]),
        ("cost / task attempt", *[f"${x:.4f}" for x in c["cost_per_task_attempt"]]),
        ("cost / passed task attempt", *[f"${x:.4f}" if x else "n/a"
                                         for x in c["cost_per_passed_task_attempt"]]),
        ("time / task attempt (s)", *c["duration_s_mean"]),
        ("main tokens / task attempt", *[f"{x:,.0f}" for x in c["main_tokens_mean"]]),
    ]
    lines = [f"# Compare: {a['experiment']} ({a['run_id']}) vs {b['experiment']} ({b['run_id']})",
             "", f"**Verdict on pass@1: {c['verdict']}**"
             + ("" if a["reportable"] and b["reportable"] else
                " (at least one run is not reportable)"), "",
             f"| Metric | {a['experiment']} | {b['experiment']} |", "| --- | --- | --- |"]
    lines += [f"| {r[0]} | {r[1]} | {r[2]} |" for r in rows]
    lines += ["", f"Majority fail -> pass: {c['majority_flips_fail_to_pass'] or 'none'}",
              f"Majority pass -> fail: {c['majority_flips_pass_to_fail'] or 'none'}", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", type=Path)
    ap.add_argument("--compare", nargs=2, type=Path, metavar=("BASELINE", "ROUTED"))
    args = ap.parse_args()
    if args.run:
        s = write_summary(args.run)
        print(to_markdown(s))
        return 0
    if args.compare:
        try:
            c = compare(*args.compare)
        except CompareRefused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        out = args.compare[1] / "compare.json"
        records.write_json_atomic(out, c)
        md = compare_markdown(c)
        (args.compare[1] / "compare.md").write_text(md, encoding="utf-8")
        print(md)
        return 0
    ap.error("pass --run or --compare")
    return 2


if __name__ == "__main__":
    sys.exit(main())
