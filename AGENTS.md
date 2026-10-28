# Working on XMechBay

This repository contains reusable tools, documentation, and synthetic examples.
Keep deployments and operational evidence separate from the distributable source.
These instructions are read by every coding agent: Codex and Antigravity read
this file directly and Claude Code reads it through the `CLAUDE.md` symlink
(`docs/agent-infra-contract.md`).

- Read `README.md`, `docs/fleet-control-contract.md` and
  `docs/agent-infra-contract.md` before changing behavior.
- Work on a topic branch and review changes through a pull request. Merging is a
  separate user decision.
- `.agents/skills/` is the only skills tree; `.claude/skills` is a symlink to it.
  `python3 scripts/sync_skill_mirrors.py --check` verifies that and
  `python3 scripts/agent_infra_audit.py .` must report `OK` before publishing.
- Keep helper scripts in `scripts/` or the relevant skill's `scripts/` directory.
  Document their purpose, inputs, and outputs. Tests belong in `tests/`.
- Record live operations under ignored `.local/records/`, or outside the checkout.
  Commit only synthetic examples. Do not copy real screens, settings, credentials,
  account identifiers, repository inventories, or personal paths into test fixtures.
- A claim that data is absent must cover hidden and ignored files as appropriate;
  publication checks must also inspect the actual Git objects being published.
- Run `python3 -B -m unittest discover -s tests -v`, the mirror check, and
  `python3 scripts/check_public_content.py` before proposing a release.
- Never send input to live sessions merely to test a code change. Use mock
  transports. A requested live operation needs a current target/screen check and
  verification after the input.

## Antigravity (agy)

Permission audits are read-only: an audit result does not authorize changing a
running agent's permission settings or answering a prompt. The maintained
workflows are the skills under `.agents/skills/`.
