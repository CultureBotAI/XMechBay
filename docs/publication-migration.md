# Publishing setup and deployment migration

Use a fresh clone of the intended public source for development. Do not change
the remote of an operational checkout to point at public source, graft private
history, copy its Git directory, or push its tags/backup branches. Repository
names and redirecting URLs are not stable identity checks.

## Initial source publication

An empty-root `main` has no installer or implementation yet. The initial source
pull request is a review candidate, not a deployed release. Its publisher must
use the reviewed candidate's installer and approved identity/root contract
before the first upload; the initial publication is not a hook-bypass exception.
The owner decides separately whether to merge that PR. General users and private
deployments should install from a fresh clone only after that approval and merge,
then pin the reviewed public source commit. Do not switch live deployments to an
unmerged initial candidate merely because its PR exists or its CI is green.

## Configure a publishing clone

Obtain the approved public repository name, immutable numeric repository ID and
clean root commit through the maintainer's verified release information. Confirm
the current API reports that same name and ID and `private: false`. These are
deployment-specific values; never copy a private repository's identity into a
public example.

After reading the installer and guard, run it from the fresh public checkout.
This command is a synthetic example; replace its values with the verified
public identity and actual approved root:

```bash
python3 scripts/install_publication_guard.py \
  --repository example-org/fleet-tools \
  --repository-id 123456 \
  --clean-root 0123456789abcdef0123456789abcdef01234567
```

If private repository names or other deployment identifiers need additional
protection, add `--deny-patterns ../private-publication-patterns.txt` to the
installation command. This is a placeholder path outside the checkout. Keep
the actual UTF-8 file in private storage, with one regular expression per line;
blank lines and lines starting with `#` are ignored. Use case-insensitive
patterns where the identifiers have case variants. Do not put the private
patterns or their matched values in public source, fixtures, or CI output.

The installer stores the contract and pinned guard/scanner/hook copies in the
Git common directory and configures the local pre-push hook. The hook does not
run the guard from a changing working-tree branch. Installation is per clone;
pulling updates does not silently replace the installed guard. An identical
installation is idempotent; changed code or contract values are refused. When
configured, the private patterns are copied as `deny-patterns.txt` beside the
installed guard and their SHA256 is pinned in the local contract. A missing,
changed or malformed configured copy refuses publication. Changing the external
file alone does not update an installation. To upgrade the guard, revise the
private patterns, or change an approved repository name, review the change,
create a fresh public clone, and install the reviewed version there with the
verified contract. The installer does not perform in-place upgrades. Keep the
old clone unpublished until its disposition is reviewed. A refusal or identity
mismatch needs investigation, not a hook bypass.

Use a `public/<topic>` branch and `[public-source] <summary>` commit-message
format, with a space after the closing bracket. Run
the local tests, mirror/audit checks and publication scanner in the
[policy](publication-policy.md). Inspect the actual outgoing objects before a
push. Keep the first push bounded to one reviewed branch, verify the resulting
remote ref and CI checks, then open the PR for independent review. Merge only
after the owner explicitly approves that PR. Tags and mirror uploads remain
blocked by default.

## Configure server enforcement

Review the four generic templates in `config/publication/` against the current
GitHub ruleset API, apply them through an authorized administrator, then read
back active rules and compare their targets, enforcement and bypass lists.
Confirm required check names against actual successful check runs. Where the
platform permits, bind each required check to the verified GitHub Actions app
identity; do not guess an integration ID or substitute a repository ID.

Use an approved synthetic canary to verify a compliant branch works and that
prohibited branch namespaces, tag creation, non-fast-forward updates and direct
main updates are rejected by their applicable server rules. Verify the stricter
prefix, branch grammar and clean-root checks with local guard regressions;
these are not claimed as server metadata enforcement. Do not test with private
data. A server may retain uploaded objects even if it rejects the reference
update. Keep receipts private. A template, UI label or local dry run alone does
not prove active server enforcement or prevent first disclosure.

## Move private operations to a pinned source version

Record the public source commit and reviewed configuration schema in a private
deployment manifest. Keep live hosts, paths, credentials, goals, snapshots,
reports and migration receipts in that private workspace. Call tools from a
dedicated checkout of the pinned source version; do not maintain another copy
of their implementation in the operations repository.

Keep deployment-specific repository classification in the external fleet
configuration's `role_overrides`. For example, the invented configuration below
keeps an enclosing repository in the `other` group even if its branch name
resembles a Mech:

```json
{"role_overrides": {"ExampleRepository": {"role": "other", "project": "Example Repository"}}}
```

Override keys match path components and repository labels case-insensitively;
configure distinct worktree aliases explicitly. They are deployment data, not
new entries in the public classifier's built-in repository inventory.

Before changing a live deployment, inventory its current entrypoints and source
pin, preserve the prior manifest/configuration, and verify one authorized canary
using the same launcher and environment as the eventual rollout. Check the
actual resulting session/artifact, not only the command exit status. Upgrade
the private pin and configuration only after that review; rollback selects the
previous preserved pin. A repository rename is not authorization to restart,
resume or send input to active sessions.

Never copy existing operational history into a source PR. If operations reveal
a reusable defect, reproduce it with a newly authored synthetic fixture in the
public source, review that fix, and update the private deployment pin separately.
