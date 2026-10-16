"""Build jd/claude/ from upstream ECC using jd/adoption.json.

Run from the repo root:  python jd/build.py
Output is deterministic: jd/claude/ is deleted and rebuilt every run, so a
git diff of jd/claude/ shows exactly what an upstream merge changed.
"""
import json
import os
import re
import shutil
import stat
import sys
import time
from collections import Counter
from pathlib import Path

if sys.version_info < (3, 12):
    sys.exit("build.py needs Python 3.12 or later (shutil.rmtree onexc).")

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "jd" / "claude"
CFG = json.loads((REPO / "jd" / "adoption.json").read_text(encoding="utf-8"))
EXC = CFG["exclude"]
REN = CFG["rename"]

# Runtime files the Node hooks load. Copied whole so require() paths resolve.
RUNTIME_DIRS = ["scripts", "schemas", "hooks",
                "skills/continuous-learning-v2", "skills/continuous-learning"]
RUNTIME_FILES = ["package.json", "package-lock.json", "LICENSE", "VERSION"]

# The hook commands locate ECC with an inline resolver. Swap it for a fixed
# root that jd/install.py fills in.
RESOLVER = re.compile(r"const r=\(function\(\)\{.*?return d\}\)\(\);")
ROOT_TOKEN = "__ECC_ROOT__"

# Agents must run only when Josh asks (AI Team CLAUDE.md, subagent rule).
AGENT_EDITS = [
    (re.compile(r"Use PROACTIVELY\b"), "Use on request"),
    (re.compile(r"\bProactively reviews\b"), "Reviews"),
    (re.compile(r"Use immediately after writing or modifying code\.[ \t]*"), ""),
    (re.compile(r"[ \t]*MUST BE USED[^.\n]*\.[ \t]*"), ""),
    (re.compile(r"\bUse for (all|any) "), r"Use on request for \1 "),
]
TEXT_EDITS = {
    "rules/common/security.md": [
        ("2. Use **security-reviewer** agent",
         "2. Tell the user and offer the **security-reviewer** agent"),
    ],
    # HCR and MVB do not use black or ruff; reformatting whole files breaks
    # "surgical changes" in AI Team CLAUDE.md.
    "rules/python/coding-style.md": [
        ("- **black** for code formatting",
         "- **black** for code formatting, only if the project already uses it"),
        ("- **ruff** for linting",
         "- **ruff** for linting, only if the project already uses it"),
    ],
    "rules/python/hooks.md": [
        ("- **black/ruff**: Auto-format `.py` files after edit",
         "- **black/ruff**: Auto-format `.py` files after edit, only in a project that already uses them"),
    ],
    "rules/react-native/testing.md": [
        ("Use the `tdd-guide` agent proactively for new features",
         "Offer the `tdd-guide` agent for new features"),
    ],
}


def read(path):
    """Read without newline translation, so line endings match upstream."""
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def frontmatter_keys(text):
    """Field names in the leading --- block, in order."""
    lines = text.splitlines()
    end = lines.index("---", 1)
    return [ln.split(":", 1)[0] for ln in lines[1:end] if re.match(r"^\w[\w-]*:", ln)]


def fail(msg):
    sys.exit(f"build.py: {msg}")


def check_exclusions_exist():
    """A typo in adoption.json would silently adopt the item. Stop instead."""
    up_skills = {p.name for p in (REPO / "skills").iterdir() if p.is_dir()}
    up_agents = {p.stem for p in (REPO / "agents").glob("*.md")}
    up_cmds = {p.stem for p in (REPO / "commands").glob("*.md")}
    up_common = {p.name for p in (REPO / "rules" / "common").glob("*.md")}
    for kind, have in [("skills", up_skills), ("agents", up_agents),
                       ("commands", up_cmds), ("rules_common", up_common)]:
        missing = set(EXC[kind]) - have
        if missing:
            fail(f"adoption.json excludes {kind} not found upstream: {sorted(missing)}")
    # A missed rename would install over a built-in command name.
    for kind, have in [("skills", up_skills), ("commands", up_cmds)]:
        missing = set(REN[kind]) - have
        if missing:
            fail(f"adoption.json renames {kind} not found upstream: {sorted(missing)}")
    # Only rules/README.md is expected at the top level; anything else is new.
    loose = {p.name for p in (REPO / "rules").iterdir() if p.is_file()} - {"README.md"}
    if loose:
        fail(f"new top-level rule files need a decision: {sorted(loose)}")
    return up_skills, up_agents, up_cmds


def apply_edits(path, rel):
    text = read(path)
    new = text
    for old, rep in TEXT_EDITS.get(rel, []):
        if old not in new:
            fail(f"expected text not found in {rel}: {old!r}")
        new = new.replace(old, rep)
    if new != text:
        write(path, new)


def build_skills(up_skills):
    adopted = []
    for name in sorted(up_skills - set(EXC["skills"])):
        target = REN["skills"].get(name, name)
        dst = OUT / "skills" / target
        shutil.copytree(REPO / "skills" / name, dst)
        if target != name:
            md = dst / "SKILL.md"
            text = read(md)
            text, n = re.subn(rf"^name:\s*{re.escape(name)}\s*$",
                              f"name: {target}", text, count=1, flags=re.M)
            if n != 1:
                fail(f"could not rename frontmatter in skill {name}")
            write(md, text)
        adopted.append(target)
    return adopted


def build_agents(up_agents):
    adopted, edited = [], []
    (OUT / "agents").mkdir(parents=True)
    for name in sorted(up_agents - set(EXC["agents"])):
        text = read(REPO / "agents" / f"{name}.md")
        new = text
        for pat, rep in AGENT_EDITS:
            new = pat.sub(rep, new)
        if new != text:
            edited.append(name)
        write(OUT / "agents" / f"{name}.md", new)
        adopted.append(name)
    left = [a for a in adopted
            if re.search(r"PROACTIVELY|MUST BE USED",
                         read(OUT / "agents" / f"{a}.md"))]
    if left:
        fail(f"proactive wording still present in agents: {left}")
    for a in adopted:
        if (frontmatter_keys(read(REPO / "agents" / f"{a}.md"))
                != frontmatter_keys(read(OUT / "agents" / f"{a}.md"))):
            fail(f"edit changed the frontmatter fields of agent {a}")
    return adopted, edited


def build_commands(up_cmds, skill_names):
    adopted, shadowed = [], []
    (OUT / "commands").mkdir(parents=True)
    for name in sorted(up_cmds - set(EXC["commands"])):
        target = REN["commands"].get(name, name)
        # A skill of the same name is invocable as /name; keep the skill.
        if target in skill_names:
            shadowed.append(name)
            continue
        shutil.copy2(REPO / "commands" / f"{name}.md",
                     OUT / "commands" / f"{target}.md")
        adopted.append(target)
    return adopted, shadowed


def build_rules():
    src = REPO / "rules"
    dst = OUT / "rules"
    for d in sorted(p for p in src.iterdir() if p.is_dir()):
        if d.name == "common":
            (dst / "common").mkdir(parents=True)
            for f in sorted(d.glob("*.md")):
                if f.name not in EXC["rules_common"]:
                    shutil.copy2(f, dst / "common" / f.name)
        else:
            shutil.copytree(d, dst / d.name)
    for rel in TEXT_EDITS:
        if rel.startswith("rules/"):
            apply_edits(OUT / rel, rel)
    return sorted(str(p.relative_to(dst)).replace("\\", "/")
                  for p in dst.rglob("*.md"))


def build_runtime():
    dst = OUT / "runtime"
    for rel in RUNTIME_DIRS:
        shutil.copytree(REPO / rel, dst / rel,
                        ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
    for rel in RUNTIME_FILES:
        shutil.copy2(REPO / rel, dst / rel)


def build_hooks():
    hooks = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    meta = json.loads((REPO / "hooks" / "hooks.metadata.json").read_text(encoding="utf-8"))
    omit = CFG["hooks"]["omit_entries"]
    out, kept, dropped = {}, [], []
    for event, entries in hooks["hooks"].items():
        ids = [m["id"] for m in meta["entries"][event]]
        if len(ids) != len(entries):
            fail(f"hooks.metadata.json out of step with hooks.json for {event}")
        for hid, entry in zip(ids, entries):
            if hid in omit:
                dropped.append(hid)
                continue
            entry = json.loads(json.dumps(entry))
            for h in entry["hooks"]:
                cmd, n = RESOLVER.subn(f"const r='{ROOT_TOKEN}';", h["command"])
                if n != 1:
                    fail(f"resolver not found exactly once in hook {hid}")
                h["command"] = cmd
                h["_ecc_id"] = hid
            out.setdefault(event, []).append(entry)
            kept.append(hid)
    unknown = set(omit) - set(dropped)
    if unknown:
        fail(f"adoption.json omits hook ids not found upstream: {sorted(unknown)}")
    write(OUT / "hooks.ecc.json",
          json.dumps({"env": CFG["hooks"]["env"], "hooks": out}, indent=2) + "\n")
    return kept, dropped


def build_overrides(skills, cmds):
    """skillOverrides for settings.json, plus the Phase 2 path list.

    Every adopted skill and command gets a value. off_overlap items are off;
    the "on" list is on; everything else, Phase 2 items included, is
    user-invocable-only (runs with /name, no description loaded every turn).
    Phase 2 items are not copied until Node.js is installed (install.py)."""
    tiers = CFG["tiers"]
    p2 = tiers["phase2_node"]
    on, off = set(tiers["on"]), set(tiers["off_overlap"])
    p2_names = set(p2["skills"]) | set(p2["commands"])
    unknown = ((on | off | set(p2["skills"])) - skills) | (set(p2["commands"]) - cmds)
    if unknown:
        fail(f"tiers name items that are not adopted: {sorted(unknown)}")
    clash = (on & off) | ((on | off) & p2_names)
    if clash:
        fail(f"items in more than one tier: {sorted(clash)}")
    out = {n: ("on" if n in on else "off" if n in off else "user-invocable-only")
           for n in sorted(skills | cmds)}
    write(OUT / "skill-overrides.json", json.dumps(out, indent=2) + "\n")
    p2_paths = sorted([f"skills/{n}" for n in p2["skills"]]
                      + [f"commands/{n}.md" for n in p2["commands"]])
    return out, p2_paths


def agent_skip_paths(agents):
    """Adopted agents not on the install list stay in jd/claude/agents only."""
    keep = set(CFG["tiers"]["agents_install"]["names"])
    unknown = keep - set(agents)
    if unknown:
        fail(f"agents_install names agents that are not adopted: {sorted(unknown)}")
    return sorted(f"agents/{a}.md" for a in agents if a not in keep)


def clear_out():
    """Delete the old build. OneDrive marks synced folders ReadOnly, which
    blocks deletion on Windows: clear the flag and retry that item. Also
    retry the whole delete briefly while OneDrive holds a file."""
    def make_writable(func, path, _exc):
        os.chmod(path, stat.S_IWRITE)
        func(path)

    for attempt in range(5):
        try:
            if OUT.exists():
                shutil.rmtree(OUT, onexc=make_writable)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(2)


def main():
    clear_out()
    OUT.mkdir(parents=True)
    up_skills, up_agents, up_cmds = check_exclusions_exist()
    skills = build_skills(up_skills)
    agents, edited = build_agents(up_agents)
    cmds, shadowed = build_commands(up_cmds, set(skills))
    rules = build_rules()
    build_runtime()
    kept, dropped = build_hooks()
    overrides, p2_paths = build_overrides(set(skills), set(cmds))

    # Coverage: every upstream item is adopted, excluded, or shadowed.
    renamed_back = {v: k for k, v in REN["skills"].items()}
    accounted = {renamed_back.get(s, s) for s in skills} | set(EXC["skills"])
    if accounted != up_skills:
        fail(f"skills not accounted for: {sorted(up_skills ^ accounted)}")
    cmd_back = {v: k for k, v in REN["commands"].items()}
    accounted = {cmd_back.get(c, c) for c in cmds} | set(EXC["commands"]) | set(shadowed)
    if accounted != up_cmds:
        fail(f"commands not accounted for: {sorted(up_cmds ^ accounted)}")

    manifest = {
        "upstream_commit": CFG["upstream_commit"],
        "counts": {"skills": len(skills), "agents": len(agents),
                   "commands": len(cmds), "rules": len(rules),
                   "hooks_kept": len(kept), "hooks_omitted": len(dropped),
                   "overrides": dict(Counter(overrides.values()))},
        "phase2_paths": p2_paths,
        "agent_skip_paths": agent_skip_paths(agents),
        "agents_edited_for_proactive_wording": edited,
        "commands_shadowed_by_same_name_skill": shadowed,
        "skills": skills, "agents": agents, "commands": cmds,
        "rules": rules, "hooks_kept": kept, "hooks_omitted": dropped,
    }
    write(OUT / "MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest["counts"]))
    print(f"agents edited: {len(edited)}  commands shadowed: {shadowed}")


if __name__ == "__main__":
    main()
