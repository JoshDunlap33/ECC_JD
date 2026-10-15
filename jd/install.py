"""Install jd/claude/ into the Claude profile (~/.claude).

Run from the repo root after `python jd/build.py`:
    python jd/install.py --skip-hooks            # Phase 1: no Node.js needed
    python jd/install.py                         # Phase 2: needs node and npm
    add --dry-run to either to show what would change and write nothing

What it does:
  1. Copies skills, agents, commands, and rules into ~/.claude. It never
     overwrites an item it did not install itself (tracked in
     ~/.claude/ecc-installed.json); a name collision is skipped and reported.
     With --skip-hooks, Phase 2 items (they need Node.js) are not copied.
  2. Phase 2 only: replaces the hook runtime at ~/.claude/ecc/ and installs
     its pinned npm dependencies with `npm ci --omit=dev --ignore-scripts`.
     With --skip-hooks, a runtime left by an earlier Phase 2 install is removed.
  3. Backs up settings.json, then replaces only ECC hooks, the env keys and
     skillOverrides a previous install set. A value Josh set or changed
     himself is kept. Every other hook and setting is kept as it is.
Close Claude Code first: a running hook can lock files in ~/.claude/ecc/.
"""
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

if sys.version_info < (3, 12):
    sys.exit("install.py needs Python 3.12 or later (shutil.rmtree onexc).")

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "jd" / "claude"
CLAUDE = Path.home() / ".claude"
ROOT = CLAUDE / "ecc"
# Outside ROOT, so replacing the runtime can never delete the ledger.
LEDGER = CLAUDE / "ecc-installed.json"
ROOT_TOKEN = "__ECC_ROOT__"
KINDS = ["skills", "agents", "commands"]


def log(msg):
    print(f"[install] {msg}")


def writable(path):
    """Add write permission (clears the Windows ReadOnly flag) without
    touching any other permission bit."""
    os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)


def remove(path):
    """Delete a file, folder, or link. Files copied out of OneDrive keep its
    ReadOnly flag, which blocks deletion on Windows: clear it and retry."""
    def retry_writable(func, p, _exc):
        writable(p)
        func(p)
    if path.is_symlink() or path.is_junction():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path, onexc=retry_writable)
    else:
        writable(path)
        path.unlink()


def copy(src, dst):
    """Copy, then clear the ReadOnly flag OneDrive left on the source, so
    the installed copy can be edited and replaced later."""
    if src.is_dir():
        shutil.copytree(src, dst)
        for p in [dst, *dst.rglob("*")]:
            writable(p)
    else:
        shutil.copy2(src, dst)
        writable(dst)


def load_ledger():
    led = (json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists()
           else {"items": []})
    led.setdefault("env_keys", [])
    led.setdefault("overrides", {})
    return led


def save_ledger(ledger, dry):
    if not dry:
        LEDGER.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")


def plan_items(skip):
    """Relative paths (under ~/.claude) of every item to install."""
    items = []
    for kind in KINDS:
        items += [f"{kind}/{p.name}" for p in sorted((SRC / kind).iterdir())]
    items += [f"rules/{p.relative_to(SRC / 'rules').as_posix()}"
              for p in sorted((SRC / "rules").rglob("*")) if p.is_file()]
    return [i for i in items if i not in skip]


def copy_items(items, owned, dry):
    installed, collisions = [], []
    for rel in items:
        src, dst = SRC / rel, CLAUDE / rel
        if dst.exists() and rel not in owned:
            collisions.append(rel)
            continue
        installed.append(rel)
        if dry:
            continue
        if dst.exists() or dst.is_symlink():
            remove(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        copy(src, dst)
    # Remove items a previous install put there that are no longer planned.
    removed = sorted(owned - set(items))
    for rel in removed:
        dst = CLAUDE / rel
        if not dry and (dst.exists() or dst.is_symlink()):
            remove(dst)
    return installed, collisions, removed


def install_runtime(npm, dry):
    if dry:
        return
    if ROOT.exists():
        remove(ROOT)
    copy(SRC / "runtime", ROOT)
    log("npm ci --omit=dev --ignore-scripts (pinned by package-lock.json)")
    subprocess.run([npm, "ci", "--omit=dev", "--ignore-scripts", "--no-audit",
                    "--no-fund"], cwd=ROOT, check=True)


def is_ecc_hook(hook):
    cmd = hook.get("command", "")
    return ROOT_TOKEN in cmd or f"const r='{ROOT.as_posix()}'" in cmd


def installed_names(installed):
    """Skill and command names from ledger paths (skills/x, commands/x.md)."""
    names = set()
    for rel in installed:
        kind, _, rest = rel.partition("/")
        if kind == "skills":
            names.add(rest)
        elif kind == "commands":
            names.add(rest[:-3])
    return names


def merge_overrides(settings, ledger, names):
    """Set skillOverrides for installed ECC items.

    The ledger records the value this installer set for each key. A key whose
    current value differs from that record, or that this installer never set,
    belongs to Josh and is kept as it is."""
    tiers = json.loads((SRC / "skill-overrides.json").read_text(encoding="utf-8"))
    ov = settings.setdefault("skillOverrides", {})
    prev = ledger["overrides"]
    for key, val in prev.items():
        if ov.get(key) == val:
            ov.pop(key)
    now, kept_user = {}, []
    for name, value in tiers.items():
        if name not in names:
            continue
        if name in ov:
            kept_user.append(name)
            continue
        ov[name] = value
        now[name] = value
    if not ov:
        del settings["skillOverrides"]
    ledger["overrides"] = now
    log(f"skillOverrides set: {dict(Counter(now.values()))}")
    if kept_user:
        log(f"skillOverrides kept as you set them: {kept_user}")


def merge_settings(ledger, names, with_hooks, dry):
    path = CLAUDE / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    ecc = json.loads((SRC / "hooks.ecc.json").read_text(encoding="utf-8"))
    if not with_hooks:
        ecc = {"env": {}, "hooks": {}}
    root = ROOT.as_posix()

    # Drop ECC hooks one by one, so a user hook sharing an entry survives.
    hooks = settings.setdefault("hooks", {})
    removed = 0
    for event in list(hooks):
        kept_entries = []
        for entry in hooks[event]:
            inner = entry.get("hooks", [])
            mine = [h for h in inner if not is_ecc_hook(h)]
            removed += len(inner) - len(mine)
            if mine or not inner:
                kept_entries.append({**entry, "hooks": mine} if inner else entry)
        hooks[event] = kept_entries
    added = 0
    for event, entries in ecc["hooks"].items():
        for entry in entries:
            entry = json.loads(json.dumps(entry))
            for h in entry["hooks"]:
                h.pop("_ecc_id", None)
                h["command"] = h["command"].replace(ROOT_TOKEN, root)
            hooks.setdefault(event, []).append(entry)
            added += len(entry["hooks"])
    for event in [e for e, v in hooks.items() if not v]:
        del hooks[event]
    if not hooks:
        del settings["hooks"]

    # Env: remove keys a previous install set, then set the current ones.
    env = settings.setdefault("env", {})
    for key in ledger["env_keys"]:
        env.pop(key, None)
    for key, val in ecc["env"].items():
        if key in env and env[key] != val:
            log(f"WARNING: replacing your own env value {key}={env[key]!r} with {val!r}")
        env[key] = val
    if not env:
        del settings["env"]
    ledger["env_keys"] = sorted(ecc["env"])
    merge_overrides(settings, ledger, names)

    log(f"settings.json: removed {removed} old ECC hooks, added {added}")
    if dry:
        return
    if path.exists():
        backup = path.with_name(f"settings.json.ecc-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        log(f"backup: {backup}")
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-hooks", action="store_true",
                    help="Phase 1: no Node runtime, no hooks, no Phase 2 items")
    args = ap.parse_args()
    manifest_path = SRC / "MANIFEST.json"
    if not manifest_path.exists():
        sys.exit("jd/claude/ is missing. Run: python jd/build.py")
    npm = shutil.which("npm")
    if not (args.dry_run or args.skip_hooks) and not (shutil.which("node") and npm):
        sys.exit("node or npm not found on PATH. Install Node.js LTS (Phase 2), "
                 "or run with --skip-hooks.")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    skip = set(manifest["phase2_paths"]) if args.skip_hooks else set()
    ledger = load_ledger()
    owned = set(ledger["items"])
    installed, collisions, removed = copy_items(plan_items(skip), owned, args.dry_run)
    # Record ownership before the steps that can fail, so a rerun can recover.
    ledger["items"] = installed
    save_ledger(ledger, args.dry_run)
    if args.skip_hooks:
        if ROOT.exists():
            log(f"removing hook runtime from an earlier Phase 2 install: {ROOT}")
            if not args.dry_run:
                remove(ROOT)
    else:
        install_runtime(npm, args.dry_run)
    merge_settings(ledger, installed_names(installed), not args.skip_hooks, args.dry_run)
    save_ledger(ledger, args.dry_run)

    log(f"items installed: {len(installed)}  removed: {len(removed)}")
    if collisions:
        log(f"SKIPPED, name already used by a non-ECC item: {collisions}")
    if args.dry_run:
        log("dry run: nothing written")


if __name__ == "__main__":
    main()
