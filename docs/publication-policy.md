# Public source and private operations

Commit reusable implementation, tests, general guidance and independently
written synthetic examples. This repository is the maintained implementation;
private operations consume a reviewed commit and retain their own configuration,
goals, deployment manifests and evidence. They must not become a second
implementation branch that is periodically merged or mirrored here.

Keep credentials, personal paths, machine names, contact addresses, account and
billing records, screenshots, settings snapshots, terminal histories, real goal
files, repository inventories, API exports and research results outside public
Git history. Use an access-controlled operations workspace. Ignored `.local/`
can hold local development state, but gitignore is not access control and does
not remove any already committed object.

## Examples and review

Write examples from scratch using reserved names such as
`worker.example.invalid`, synthetic identifiers and relative paths. Do not start
from a real transcript, settings dump or export and merely replace familiar
names. Review the meaning of all remaining fields, command arguments and embedded
payloads. A test fixture's usefulness does not authorize publishing its source.

Use `public/<topic>` branches and start each commit message with
`[public-source] `, including the trailing space before the summary. Keep commit
text public too. GitHub service identities and
public no-reply account aliases are permitted only in Git identity headers;
source text and messages remain subject to the email scan. Review the staged
diff and the full reachable history, including deleted versions and metadata.
Every pull request needs independent review and explicit human approval before
merging. No helper or CI job grants merge authorization.

## Checks before upload

Install the local guard in every publishing clone as described in
[publication-migration.md](publication-migration.md). Its local contract pins the
expected GitHub repository name, immutable repository ID and clean root commit.
Before a push it checks the current destination identity and public visibility
through the API, the canonical `origin` push URL, permitted branch refs and
outgoing ancestry. The hook uses the object IDs supplied by Git, not just the
current working tree. It scans historical blobs, paths and commit metadata
before Git uploads the proposed update.

Generic patterns cannot identify every private project name. Configure known
private identifiers through the installer's optional external `--deny-patterns`
file. The installer keeps a private copy in the Git common directory and pins
its SHA256 in the local contract. The guard applies those patterns to destination
refs, commit metadata, tree paths and historical blobs; a missing, changed or
malformed configured copy fails closed. Review pattern updates and install them
in a fresh clone. Public CI uses generic checks and does not receive that private
file, so its success does not establish private-pattern coverage.

A familiar repository name can refer to a different repository after a rename
or recreation. A cached instruction saying a repository is private is not
evidence of its current visibility. Stop if the expected immutable ID, name,
visibility or root differs; investigate rather than repinning to silence the
failure.

Run the local source checks before requesting publication:

```bash
python3 -B -m unittest discover -s tests -v
python3 scripts/sync_skill_mirrors.py --check
python3 scripts/agent_infra_audit.py .
python3 scripts/check_public_content.py
```

The scanner covers index/worktree publication inputs and reachable Git objects;
its default history scan includes all local refs. Large, non-UTF-8 and unsupported
objects are findings, not silently skipped data. Pattern checks are a backstop,
not proof that every identifier, encoded payload or scientific result is safe.
Inspect the reported scope and perform semantic review. An ignore-independent
working-tree inventory is also required when deciding whether local material is
absent; it does not replace the outgoing-object scan.

Do not push private deployment ancestry, tags, mirrors, all refs or backup refs.
Do not use unattended synchronization into the public repository. Migrate a
generic implementation change by independently preparing and reviewing it in a
fresh public clone, without importing private Git history or copying operational
artifacts.

## Server rules and their limits

The reviewed REST templates in `config/publication/` describe four proposed active
rulesets with empty bypass lists. A repository administrator applies and verifies
them separately; a JSON file in this tree does not establish enforcement.

| Target | Required rules |
| --- | --- |
| Every branch | Linear history and no force pushes |
| Branches outside `main` and `public/` descendants | Creation and updates restricted with no bypass actors |
| Every tag | Creation, updates and deletion restricted with no bypass actors |
| `main` | Pull request, resolved review threads, linear history, no force push/deletion, and both required CI checks against the current base |

Required check contexts are exactly `checks` and `Python 3.9 stdlib checks`.
The main template permits zero formal GitHub approving reviews so a sole
maintainer can merge an independently reviewed change after human approval. It
still requires a PR and resolved threads. A team may raise the formal approval
count when a separate authorized reviewer is available. The repository must
permit squash or rebase merging for linear history; preserve the required
message prefix in the resulting commit.

The exact lowercase branch grammar, commit-message prefix, immutable clean root
and historical content scan are enforced by the installed local guard. Do not
claim server-side enforcement of that prefix or ancestry check. These templates
intentionally omit metadata restrictions: configuration readback alone does not
establish that a server evaluates them for a particular push.
The server namespace rule deliberately permits broad `public/` descendants,
while the local guard imposes the stricter lowercase grammar.

Server rules constrain reference updates, main's PR/check requirements and tags.
They are not an object-disclosure barrier: uploaded Git objects may be retained
even when a reference update is rejected. Administrators can change server rules;
any such change requires separate review and authorization.

GitHub documents these branch/tag controls and the REST field names in its
[ruleset API](https://docs.github.com/en/rest/repos/rules),
[branch-targeting guide](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository#using-fnmatch-syntax),
and [available rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets).
Do not describe these templates as arbitrary server-side content filters.
GitHub's path/extension/size push rules are a separate product capability with
plan and repository-visibility constraints; they are not enabled by these
branch/tag templates.

The local hook is cooperative protection: Git does not install it during clone,
and a client can bypass it with `--no-verify`, change `core.hooksPath`, edit its
local files or upload through another API/client. Such a bypass violates this
publication policy. CI runs after a public upload and protects merging, not
first disclosure. Neither a rejected push nor a green CI run proves that private
objects were never uploaded. Read-only CI credentials, restricted repository
write access, verified server rules and pre-upload review remain necessary.

## Existing private history

Keep repositories with operational history private. A deletion commit, history
rewrite or clean working tree does not establish removal of old objects,
pull-request refs, logs, artifacts, releases, comments, forks or outside copies.
Any incident assessment and recovery evidence belongs in private storage.
GitHub's [sensitive-data removal guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)
describes its separate removal process; do not claim a local rewrite proves
server-side erasure. Build new public history only from reviewed source, with
no private ancestors or inherited refs.
