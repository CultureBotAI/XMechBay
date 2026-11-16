# Working on XMechBay

This repository contains reusable tools, documentation, and synthetic examples.
Keep deployments and operational evidence separate from the distributable source.
These instructions are read by every coding agent: Codex and Antigravity read
this file directly and Claude Code reads it through the `CLAUDE.md` symlink
(`docs/agent-infra-contract.md`).

- Read `README.md`, `docs/fleet-control-contract.md` and
  `docs/agent-infra-contract.md` before changing behavior.
- Work on a `public/<topic>` branch; use `[public-source] <summary>` for every
  commit message, including the space after the bracket. Review changes
  adversarially through a pull request. File
  review findings through the owner's approved issue workflow. Merging always
  needs the owner's explicit approval for that pull request.
- This public source is the sole maintained implementation. Private operations
  pin a reviewed source commit and keep configuration, goals, evidence and
  deployment records separately. Do not merge, mirror or push private history
  into this repository, even if the current files appear clean.
- Before publishing from a fresh clone, follow
  `docs/publication-migration.md` and install the publication guard. Verify the
  destination's current API identity, visibility and immutable repository ID;
  a remembered name, remote URL or stale instruction is insufficient.
- The guard must scan the actual outgoing commit ancestry and historical Git
  objects before upload. CI runs after upload and cannot prevent first exposure.
  Do not disable hooks, use `--no-verify`, mirror refs, publish tags, or use an API
  upload to bypass this boundary. A guard refusal requires investigation.
- `.agents/skills/` is the only skills tree; `.claude/skills` is a symlink to it.
  `python3 scripts/sync_skill_mirrors.py --check` verifies that and
  `python3 scripts/agent_infra_audit.py .` must report `OK` before publishing.
- Keep helper scripts in `scripts/` or the relevant skill's `scripts/` directory.
  Document their purpose, inputs, and outputs. Tests belong in `tests/`.
- Record live operations under ignored `.local/records/`, or outside the checkout.
  Commit only synthetic examples. Do not copy real screens, settings, credentials,
  account identifiers, repository inventories, or personal paths into test fixtures.
- Synthetic examples must be written independently, use reserved example names
  and relative paths, and receive semantic review. Renaming fields in a real
  transcript or export does not make it synthetic.
- A claim that data is absent must cover hidden and ignored files as appropriate;
  publication checks must also inspect the actual Git objects being published.
- Run `python3 -B -m unittest discover -s tests -v`, the mirror check, and
  `python3 scripts/check_public_content.py` before proposing a release.
- Never send input to live sessions merely to test a code change. Use mock
  transports. A requested live operation needs a current target/screen check and
  verification after the input.
- Never notify or mention an account, send messages, invite or share on behalf of
  the operator without the specific authorization their instructions require.

## Antigravity (agy)

Permission audits are read-only: an audit result does not authorize changing a
running agent's permission settings or answering a prompt. The maintained
workflows are the skills under `.agents/skills/`.
