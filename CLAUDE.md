# CLAUDE.md

@AGENTS.md

## Claude Code specifics

- Personal, machine-specific instructions live in `CLAUDE.local.md` (untracked), which Claude Code loads
  automatically next to this file.
- Project skills live in `.claude/skills/` and the project hooks in `.claude/settings.json`. Hooks are
  guardrails: they do not replace the validation in `AGENTS.md` or the maintainer's approvals.
- When the user types `/graphify`, use the graphify skill before anything else.
