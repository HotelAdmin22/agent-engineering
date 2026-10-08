"""build_manifest fails loudly instead of guessing; splits are deterministic.

A synthetic Spider2 layout with 6 tasks stands in for the clone: the rules are
the same whether the target is 64 of 68 or 4 of 6.
"""

import json

import pytest

from evals.dbt.scripts import build_manifest as bm
from evals.dbt.scripts import make_splits
from evals.dbt.tasks import dir_sha256, load_manifest, load_splits


def _task(root, iid, gold_name="db.duckdb", start=True, gold=True, func="duckdb_match"):
    ex = root / "examples" / iid
    ex.mkdir(parents=True)
    (ex / "dbt_project.yml").write_text(f"name: {iid}\n", encoding="utf-8")
    if start:
        (ex / "db.duckdb").write_bytes(b"start-" + iid.encode())
    g = root / "evaluation_suite" / "gold" / iid
    if gold:
        g.mkdir(parents=True)
        (g / "db.duckdb").write_bytes(b"gold-" + iid.encode())
    return ({"instance_id": iid, "instruction": f"do {iid}", "type": "DBT"},
            {"instance_id": iid, "evaluation": {"func": func, "parameters": {
                "gold": gold_name, "condition_tabs": ["t"]}}})


@pytest.fixture
def spider(tmp_path):
    root = tmp_path / "spider2-dbt"
    ex, ev = [], []
    for i in range(6):
        a, b = _task(root, f"t{i}")
        ex.append(a)
        ev.append(b)
    (root / "examples" / "spider2-dbt.jsonl").write_text(
        "\n".join(json.dumps(e) for e in ex), encoding="utf-8")
    (root / "evaluation_suite" / "gold" / "spider2_eval.jsonl").write_text(
        "\n".join(json.dumps(e) for e in ev), encoding="utf-8")
    return root


def excluded(ids, total=6):
    return {"source_task_count": total,
            "excluded": [{"instance_id": i, "reason": "r"} for i in ids]}


def test_builds_in_source_order_with_hashes(spider):
    m, checks = bm.build(spider, excluded(["t1", "t4"]), target=4)
    assert [t["instance_id"] for t in m["tasks"]] == ["t0", "t2", "t3", "t5"]
    assert m["task_count"] == 4 and m["source_task_count"] == 6
    t0 = m["tasks"][0]
    assert t0["start_db"] == "db.duckdb" and t0["instruction"] == "do t0"
    assert t0["example_dir_sha256"] == dir_sha256(spider / "examples" / "t0")
    assert checks["gold"]["t0"] == t0["gold_dir_sha256"]


def test_rebuild_is_byte_identical(spider):
    a = bm.dump(bm.build(spider, excluded(["t1", "t4"]), target=4)[0])
    b = bm.dump(bm.build(spider, excluded(["t1", "t4"]), target=4)[0])
    assert a == b


@pytest.mark.parametrize("ids,msg", [
    (["t1"], "exactly 2"),
    (["t1", "t1"], "duplicate"),
    (["t1", "nope"], "not in the benchmark"),
])
def test_bad_exclusions_fail(spider, ids, msg):
    with pytest.raises(bm.ManifestError, match=msg):
        bm.build(spider, excluded(ids), target=4)


def test_exclusion_needs_a_reason(spider):
    e = excluded(["t1", "t2"])
    e["excluded"][0]["reason"] = ""
    with pytest.raises(bm.ManifestError, match="reason"):
        bm.build(spider, e, target=4)


def test_source_count_must_match(spider):
    with pytest.raises(bm.ManifestError, match="source_task_count"):
        bm.build(spider, excluded(["t1", "t2"], total=68), target=4)


def test_non_duckdb_match_stops_for_review(tmp_path):
    root = tmp_path / "s"
    a, b = _task(root, "x0", func="table_match")
    (root / "examples" / "spider2-dbt.jsonl").write_text(json.dumps(a), encoding="utf-8")
    (root / "evaluation_suite" / "gold" / "spider2_eval.jsonl").write_text(json.dumps(b),
                                                                           encoding="utf-8")
    with pytest.raises(bm.ManifestError, match="owner review"):
        bm.build(root, excluded([], total=1), target=1)


def test_missing_start_db_or_gold_fails(tmp_path):
    for n, (kwargs, msg) in enumerate([({"start": False}, "start .duckdb"),
                                       ({"gold": False}, "gold")]):
        root = tmp_path / f"case{n}"
        a, b = _task(root, "x0", **kwargs)
        (root / "examples" / "spider2-dbt.jsonl").write_text(json.dumps(a), encoding="utf-8")
        (root / "evaluation_suite" / "gold").mkdir(parents=True, exist_ok=True)
        (root / "evaluation_suite" / "gold" / "spider2_eval.jsonl").write_text(
            json.dumps(b), encoding="utf-8")
        with pytest.raises(bm.ManifestError, match=msg):
            bm.build(root, excluded([], total=1), target=1)


def test_gold_name_mismatch_is_flagged_not_fatal(tmp_path):
    root = tmp_path / "s"
    a, b = _task(root, "x0", gold_name="other.duckdb")
    (root / "examples" / "spider2-dbt.jsonl").write_text(json.dumps(a), encoding="utf-8")
    (root / "evaluation_suite" / "gold" / "spider2_eval.jsonl").write_text(json.dumps(b),
                                                                           encoding="utf-8")
    m, _ = bm.build(root, excluded([], total=1), target=1)
    assert m["tasks"][0]["gold_file_present"] is False


def test_splits_are_deterministic_and_disjoint(spider, tmp_path):
    m, _ = bm.build(spider, excluded(["t1", "t4"]), target=4)
    path = tmp_path / "tasks.json"
    path.write_bytes(bm.dump(m).encode())
    s1 = make_splits.make(path, held_out=1)
    s2 = make_splits.make(path, held_out=1)
    assert s1 == s2 and len(s1["held_out"]) == 1 and len(s1["dev"]) == 3
    assert not set(s1["held_out"]) & set(s1["dev"])
    sp = tmp_path / "splits.json"
    sp.write_text(json.dumps(s1), encoding="utf-8")
    assert set(load_splits(sp, path).values()) == {"dev", "held_out"}
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="different manifest"):
        load_splits(sp, path)


def test_committed_manifest_and_splits_agree():
    from evals.dbt.tasks import ROOT
    mpath = ROOT / "evals/dbt/manifests/tasks_64.json"
    data, tasks = load_manifest(mpath)
    assert data["task_count"] == len(tasks) == 64
    splits = load_splits(ROOT / "evals/dbt/manifests/splits.json", mpath)
    assert len(splits) == 64 and sum(v == "held_out" for v in splits.values()) == 13
