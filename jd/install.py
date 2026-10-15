"""Install jd/claude/ into the Claude profile (~/.claude).

Run from the repo root after `python jd/build.py`:
    python jd/install.py            # install or update
    python jd/install.py --dry-run  # show what would change, write nothing

What it does:
  1. Copies skills, agents, commands, and rules into ~/.claude. It never
     overwrites an item it did not install itself (tracked in
     ~/.claude/ecc-installed.json); a name collision is skipped and reported.
  2. Replaces the hook runtime at ~/.claude/ecc/ and installs its pinned npm
     dependencies with `npm ci --omit=dev --ignore-scripts`.
  3. Backs up settings.json, then replaces only ECC hooks and the env keys a
     previous install set. Every other hook and setting is kept as it is.
Close Claude Code first: a running hook can lock files in ~/.claude/ecc/.
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

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


def load_ledger():
    if LEDGER.exists():
        led = json.loads(LEDGER.read_text(encoding="utf-8"))
        led.setdefault("env_keys", [])
        return led
    return {"items": [], "env_keys": []}


def save_ledger(ledger, dry):
    if not dry:
        LEDGER.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")


def plan_items():
    """Relative paths (under ~/.claude) of every item to install."""
    items = []
    for kind in KINDS:
        items += [f"{kind}/{p.name}" for p in sorted((SRC / kind).iterdir())]
    items += [f"rules/{p.relative_to(SRC / 'rules').as_posix()}"
              for p in sorted((SRC / "rules").rglob("*")) if p.is_file()]
    return items


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
        if dst.is_dir():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    # Remove items a previous install put there that are no longer adopted.
    removed = sorted(owned - set(items))
    for rel in removed:
        dst = CLAUDE / rel
        if not dry and dst.exists():
            shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
    return installed, collisions, removed


def install_runtime(npm, dry):
    if dry:
        return
    if ROOT.exists():
        shutil.rmtree(ROOT)
    shutil.copytree(SRC / "runtime", ROOT)
    log("npm ci --omit=dev --ignore-scripts (pinned by package-lock.json)")
    subprocess.run([npm, "ci", "--omit=dev", "--ignore-scripts", "--no-audit",
                    "--no-fund"], cwd=ROOT, check=True)


def is_ecc_hook(hook):
    cmd = hook.get("command", "")
    return ROOT_TOKEN in cmd or f"const r='{ROOT.as_posix()}'" in cmd


def merge_settings(ledger, dry):
    path = CLAUDE / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    ecc = json.loads((SRC / "hooks.ecc.json").read_text(encoding="utf-8"))
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
    args = ap.parse_args()
    if not (SRC / "MANIFEST.json").exists():
        sys.exit("jd/claude/ is missing. Run: python jd/build.py")
    npm = shutil.which("npm")
    if not args.dry_run and not (shutil.which("node") and npm):
        sys.exit("node or npm not found on PATH. Install Node.js LTS, restart VS Code, run again.")

    ledger = load_ledger()
    owned = set(ledger["items"])
    installed, collisions, removed = copy_items(plan_items(), owned, args.dry_run)
    # Record ownership before the steps that can fail, so a rerun can recover.
    ledger["items"] = installed
    save_ledger(ledger, args.dry_run)
    install_runtime(npm, args.dry_run)
    merge_settings(ledger, args.dry_run)
    save_ledger(ledger, args.dry_run)

    log(f"items installed: {len(installed)}  removed: {len(removed)}")
    if collisions:
        log(f"SKIPPED, name already used by a non-ECC item: {collisions}")
    if args.dry_run:
        log("dry run: nothing written")


if __name__ == "__main__":
    main()
