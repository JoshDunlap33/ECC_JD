# jd/ : Josh's policy-screened ECC

This folder holds the parts of ECC ("Everything Claude Code") that passed the
APFM AI Acceptable Use Policy screen, with Josh's edits. The rest of the repo
is upstream ECC, unchanged, so upstream updates can be reviewed with
`git diff`.

Approval record: `Josh_ObsidianVault/To Do List/Claude Skill Adoption/2026-10-15 - ECC Policy-Clean Adoption.md`.

AI-generated: the files in `jd/` (scripts, manifest, edits) were written by
Claude Code and reviewed before use.

## Files

| File | What it is |
|---|---|
| `adoption.json` | The screen. Every excluded skill, agent, command, rule, and hook, each with its reason. Renames and hook settings. |
| `build.py` | Rebuilds `claude/` from upstream using `adoption.json`. Stops if any upstream item is unaccounted for, if an exclusion names a missing item, or if an agent edit breaks frontmatter. |
| `install.py` | Copies `claude/` into `~/.claude`. Backs up `settings.json`, replaces only ECC hook entries, and never overwrites an item it did not install. |
| `claude/` | Build output: the adopted, edited content and the hook runtime. Do not edit by hand. |
| `claude/MANIFEST.json` | What the last build adopted, edited, and omitted. |

## Install or update

Needs Python 3.12 and Node.js LTS (`node`, `npm` on PATH).

```powershell
python jd/build.py
python jd/install.py --dry-run
python jd/install.py
```

Restart Claude Code after an install. Skills, agents, and hooks load at
session start.

## Take an upstream update

1. `git fetch upstream` (add it once: `git remote add upstream https://github.com/affaan-m/ecc.git`).
2. `git merge upstream/main` on a branch.
3. Read the diff of new or changed skills, agents, commands, hooks, and
   `scripts/`. Screen anything new against the AI policy. Add exclusions to
   `adoption.json` with a reason.
4. `python jd/build.py`, then read `git diff jd/claude/`.
5. Install, test, and commit.

Never use ECC's `/auto-update` or `install.ps1`; they skip this review.

## Edits made to upstream content

- Agents: "Use PROACTIVELY", "MUST BE USED", and "Use for all ..." wording
  changed to on-request wording. Claude runs an agent only when asked.
- Renamed to avoid built-in command names: skill `security-review` to
  `ecc-security-review`; commands `code-review` to `ecc-code-review`, `plan`
  to `ecc-plan`.
- Commands that share a name with a skill are not installed; the skill is
  invocable by the same `/name`.
- `rules/common/`: only `security.md` (one edit: offer the security-reviewer
  agent rather than launch it) and `hooks.md`. The other eight conflict with
  AI Team `CLAUDE.md`.
- Hooks: GateGuard, governance capture, desktop notify, doc file warning,
  and tmux hooks are off. `ECC_SKIP_LLM_SUMMARY=1` stops a hidden `claude -p`
  summary call.

## License

ECC is MIT-licensed, Copyright (c) 2026 Affaan Mustafa. See `../LICENSE`. The
license file is also copied into the installed runtime.
