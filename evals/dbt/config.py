"""Load and check a run config (evals/dbt/configs/*.toml).

Configs are complete files, not overlays: baseline.toml and routed.toml each
state every key, and `report --compare` asserts that everything outside the
experiment's own surface (delegate tools, subagent roles) is identical.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Keys allowed to differ between compared configs. Everything else must match.
EXPERIMENT_SURFACE = {"experiment", "roles.main.tools", "roles.explore", "roles.diagnose",
                      "delegation"}


@dataclass
class Role:
    name: str
    model: str
    system_prompt_path: Path
    tools: list[str]
    max_turns: int
    output_schema: str | None = None
    reasoning_effort: str | None = None
    system_prompt: str = field(default="", repr=False)

    def settings_sent(self) -> dict:
        """Exactly the optional request settings this role sends."""
        out: dict = {"store": False}
        if self.reasoning_effort:
            out["reasoning"] = {"effort": self.reasoning_effort}
        return out


def load(path: Path | str) -> dict:
    path = Path(path)
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    cfg["_path"] = str(path)
    _check(cfg)
    return cfg


def _check(cfg: dict) -> None:
    for key in ("experiment", "attempts", "task_manifest", "splits", "spider2_root",
                "runs_dir", "concurrency", "task_timeout_s", "sandbox", "roles",
                "limits", "gateway", "dbt", "pricing", "api"):
        if key not in cfg:
            raise ValueError(f"config missing {key!r}")
    if "main" not in cfg["roles"]:
        raise ValueError("config has no [roles.main]")
    for name, role in cfg["roles"].items():
        if not role.get("model"):
            raise ValueError(f"roles.{name} has no model")
        if role["model"] not in cfg["pricing"]:
            raise ValueError(f"roles.{name}.model {role['model']!r} has no [pricing] entry")
    delegates = {"delegate_explore": "explore", "delegate_diagnose": "diagnose"}
    for tool, role in delegates.items():
        if tool in cfg["roles"]["main"]["tools"] and role not in cfg["roles"]:
            raise ValueError(f"main has {tool} but there is no [roles.{role}]")
        if tool in cfg["roles"]["main"]["tools"] and "delegation" not in cfg:
            raise ValueError("delegate tools need a [delegation] table")


def resolve(cfg: dict, key: str) -> Path:
    p = Path(cfg[key])
    return p if p.is_absolute() else ROOT / p


def roles(cfg: dict) -> dict[str, Role]:
    out = {}
    for name, r in cfg["roles"].items():
        prompt_path = Path(r["system_prompt"])
        prompt_path = prompt_path if prompt_path.is_absolute() else ROOT / prompt_path
        out[name] = Role(
            name=name,
            model=r["model"],
            system_prompt_path=prompt_path,
            tools=list(r["tools"]),
            max_turns=int(r["max_turns"]),
            output_schema=r.get("output_schema"),
            reasoning_effort=r.get("reasoning_effort") or None,
            system_prompt=prompt_path.read_text(encoding="utf-8"),
        )
    return out


def flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        if k.startswith("_"):
            continue
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def differences(a: dict, b: dict) -> list[str]:
    """Keys that differ between two configs, outside the experiment surface."""
    fa, fb = flatten(a), flatten(b)
    diffs = []
    for key in sorted(set(fa) | set(fb)):
        if any(key == s or key.startswith(s + ".") for s in EXPERIMENT_SURFACE):
            continue
        if fa.get(key) != fb.get(key):
            diffs.append(f"{key}: {fa.get(key)!r} != {fb.get(key)!r}")
    return diffs
