# M5 repository-freshness runtime inspection

Observed 2026-11-10, final host timestamp 07:31:02 UTC. Scope: read-only SSH
inspection of M5 (`marcins-MacBook-Pro-3.local`) for the coordinator's approved
freshness work. No session input, restart, provider request, auth/keychain read,
Git fetch, configuration edit, profile/freeze change or adoption was performed.

## Result

The actual M5 Mech entry path is a native interactive Codex goal, launched from
GUI-origin shells. The inspected account wrappers and tmux launcher do not
implement per-goal or post-merge gates. The installed CLI exposes its hooks
feature as stable and enabled, but neither installed freshness hooks nor their
execution at native goal boundaries were demonstrated. Native integration is
therefore an explicit implementation and canary task, not an unidentified host
or runner task. Candidate documentation alone is not mechanical enforcement.

## Runtime and ownership

- `/opt/homebrew/bin/tmux -V`: `tmux 3.8`.
- Codex binary (below): `codex-cli 0.167.0`; package.json agrees.
- Binary path: `/Users/marcin/.nvm/versions/node/v22.16.0/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex`.
- `codex features list` reports `goals stable true`, `hooks stable true`, and
  `plugin_hooks removed false`. This proves the installed feature flag, not
  event ordering or coverage.
- `~/.codex-accounts.sh:19-20`: `codex-work` and `codex-personal` set CODEX_HOME
  to `~/.codex` and `~/.codex-pro`, respectively, then invoke `command codex`.
  Neither function wraps a goal or commit sequence.
- `~/mechs-tmux-start.sh` creates `mechs`, sets history/mouse, and lists
  sessions. It does not launch a workflow supervisor.
- These files and the binary are owned by `marcin`; both config.toml files are
  mode 0600. Config contents and account identities are intentionally omitted.

`tmux list-panes -a` and `ps` establish shell -> node -> Codex ancestry in the
`mechs` server (server PID 74259). All twelve running Mech/PFAS panes below use
`codex resume --last --no-daemon`. Linked `xmb-view-*` sessions share the same
pane IDs; they are viewers, not additional agents.

| Window | Pane | Shell PID | Codex PID |
|---|---|---:|---:|
| CultureMech | %3 | 87705 | 92813 |
| TraitMech | %4 | 87707 | 92817 |
| ProteinTraitsMech | %5 | 87709 | 92825 |
| PathwayMech | %6 | 87712 | 93915 |
| NaturalProductMech | %7 | 87718 | 95510 |
| HabitatMech | %8 | 87729 | 96431 |
| DUFMech | %9 | 87742 | 97162 |
| CommunityMech | %10 | 87760 | 98002 |
| AntibioticMech | %11 | 87775 | 98916 |
| AIMech | %12 | 87795 | 99522 |
| CMMMech | %13 | 87830 | 534 |
| PFASCommunityAgents | %14 | 87861 | 1625 |

Native Terminal sessions outside those panes must be included in any adoption
map: CSM PID 70797 (`resume --last --no-daemon`), MIM PID 92477 (`resume
01a15303-31b2-73d1-aed2-227cb0d2063f --no-daemon`), Taxon PID 36965
(`--no-daemon`), and claw PID 48304 (`resume
01a15d35-8b73-7a50-9ce9-35555ff87d78 --no-daemon`). Their actual checkout CWDs
were verified with `lsof -a -p PID -d cwd -Fn`. MIM's old `mechs` pane %1 is an
idle shell after an earlier Documents-access failure, not the live native MIM
session. `--last` alone is not a verified conversation identity.

Separate `mim-unmatched-*` sessions launch
`MediaIngredientMech/.temp/all-unmatched-20261107/scripts/launch_codex_work.sh`.
`ptm-structure-20261028-029cc7` launches claw's
`workspace/reports/ptm-structure-launch-20261028/worker.sh`. These worker
launchers are distinct from native Mech goals. Other tmux sockets, EdgeMech
runtime, and every worker's internal lifecycle were outside this bounded audit.

## Actual goal and closure instruction paths

M1 XMechBay's `goals/mech-review-goal-20261018.txt` begins with native `/goal`
and selects unreviewed scientific records. Its scope says fresh origin/main,
but it has no packaged freshness call or receipt gate. The visible TraitMech
pane still displays this goal's text.

`goals/m5-idle-mechs-commit-sequence-2026-10-27.txt` is the standalone closure
entry instruction. It points to claw's
`src/kg_microbe_workflows/templates/mech-cycle.yaml` closure_policy and
`src/kg_microbe_governance/artifacts/prompts/backlog-loop-goal.md` sections 3-4.
It allows a standalone report when there is no real supervisor context and
explicitly forbids starting/resuming the next goal. The M5 claw template exists;
closure_policy starts at line 45. M5 claw checkout HEAD was
`a88116ff44b18f42a08bd80e59701cdc336c4d42`.

The actual coordinator transport is XMechBay's
`.agents/skills/mac-orchestration/scripts/mac_term_control.py:261` (`paste`),
with exact-pane resolution and prompt-state guards. It transports instructions;
its inspected body performs no freshness execution or receipt validation.

On M5, CM/PTM/MIM `prompts/backlog-loop-goal.md` still describe the generic
workflow: inspect current remote state, fetch before an isolated worktree, and
refresh before merging. They do not invoke the packaged freshness CLI. PTM's
`.claude/skills/proteintraitsmech-evolve-workflow/SKILL.md` requires supervisor
status, pins, native goal identity and receipt validation when real context
exists. It says the supervisor alone advances a cycle. That instruction is not
evidence of an attached live supervisor for the directly launched native loops.

## Scoped configuration findings and limits

Ignored-inclusive `rg --files -uu --max-depth 3` over `~/.codex` and
`~/.codex-pro`, restricted to config.toml/requirements.toml/hooks.json, found
the two active config.toml files and two history-backup configs, with no
hooks.json in that scope. An explicit header/key scan of the active files
found goals enabled and no hook/notify/plugin configuration matches. Filesystem
globs found no `.codex` directory under immediate `Mechs/*` roots or the M5
claw main checkout. Ignored-inclusive scoped searches of CM/PTM/MIM instructions
and prompts found no `pre_goal`, `post_merge` or packaged freshness invocation.
An ignored-inclusive depth-four source filename search of the M5 claw `src/`
found no freshness-named files. These are bounded findings, not proof that no
plugin, system configuration, worktree hook or unrelated runner exists anywhere.
Credentials, auth stores, keychains, broad transcript contents, and plugin
internals were not inspected.

## Minimal implementation handoff

1. Keep native identity separate from the Intel supervisor. Bind M5 host,
   verified session/pane or Terminal identity, repository, owned checkout,
   boundary, adoption reference and predecessor closure in a new native adapter.
   Preserve existing goals as unadopted; do not synthesize supervisor IDs.
2. Use the released shared CLI, not an extra pull implementation:
   `python -m kg_microbe_workflows.freshness_cli run --request REQUEST.json --apply`,
   followed by `python -m kg_microbe_workflows.freshness_cli verify --request REQUEST.json`.
   Request operation is `pre_goal` before scope/inventory consumption, then
   `post_merge` with verified actual merge identity or `no_change` at closure.
   A hold blocks closure acknowledgment and the next goal's dispatch.
3. Put receipt validation in a dedicated native coordinator dispatch/closure
   adapter. Merely changing `codex-work`, a prompt, or generic paste cannot
   enforce later native goals inside the same process. Do not overload the
   generic transport with inferred workflow identities.
4. Stage a Codex hook adapter, but first prove the exact native paths. Official
   hook documentation describes UserPromptSubmit, PreToolUse and Stop, rather
   than a dedicated Goal event. Hooks need trusted definitions; some tool paths
   bypass hooks, and errors can fail open. Thus flags plus documentation do not
   establish a complete barrier. No hook or trust bypass was installed here.
5. Start with offline fixture/replay tests. Then obtain coordinator authorization
   for one isolated same-binary native-session canary before any live adoption.
   Exercise `/goal` creation, model-created goals, automatic continuation,
   inventory consumption, actual-merge/no-change closure, hold/error/timeout,
   changed/untrusted hook and missing/mismatched receipt cases. Establish which
   events actually run and whether completion is prevented. Offline adapters
   alone cannot establish native runtime enforcement. Only then propose the
   reviewed adoption map, beginning with one owned checkout in the GUI-origin
   environment; SSH GitHub/keychain behavior is not an equivalent canary.

Primary documentation consulted: [Codex hooks](https://learn.chatgpt.com/docs/hooks)
and [Goals in Codex](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex).
The current hooks feature is also directly observed in the installed binary;
event semantics above are documented candidates, not locally exercised behavior.

## SHA-256 evidence

| M5 artifact | SHA-256 |
|---|---|
| `~/.codex-accounts.sh` | `0fb636e17cb6dc740e5d48c3493bab75e538d2f8830b9509736be4874cf0b972` |
| `~/mechs-tmux-start.sh` | `7509dbbf44977962ad285886b8d2b37168d1adfc87476ed4841d4bdfc48ad28c` |
| `~/.codex/config.toml` | `ca15d940012df1ea575dd508696637ba697a96808654b07309dd909c68bc3663` |
| `~/.codex-pro/config.toml` | `2bc5a7d5a0dfa175199c549fe8e8b2433205b96f8792b5b35fe13bc2873c1187` |
| Codex npm package.json | `a673c6cd2211c83378ebd3d24f08285a94322fd6b588255396c0f675ef74b1b5` |
| Codex native binary | `030316ecfbb41f4e7946e584b0063ef333b7a36cd312ba349f2f6869e39b62cc` |
| PTM evolve-workflow skill | `ce1b9f03d6f2831a0d2e1c747f22e398d884700d8a07a5fcec9f6a591dbae16b` |

All commands were invoked from the XMechBay checkout. Inspection used existing
read-only shell tools; no helper scripts were created. This report is the only
new file and was written in the coordinator's isolated branch worktree.
