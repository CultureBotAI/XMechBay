# Source and deployment data

Commit reusable implementation, tests, general guidance, and hand-authored
synthetic examples. Examples use reserved names such as `worker.example.invalid`
and relative paths. They must not be sanitized fragments of real transcripts or
configuration dumps whose remaining fields can identify a deployment.

Keep credentials, personal paths, machine names, email addresses, account and
billing records, screenshots, settings snapshots, terminal histories, real goal
files, repository inventories, and API exports outside the tracked tree. Ignored
`.local/` is available for development, but it is not an access-control mechanism.

Review the staged diff and run `scripts/check_public_content.py`. The check scans
publishable file content and commit metadata and reports locations without
printing matched values. Pattern checks are a backstop, not proof that every
possible identifying detail is absent. A human must also judge the meaning of
examples and documentation.

GitHub service identities and public no-reply account aliases are permitted in
Git identity headers. Real contact addresses and email addresses in source or
commit messages remain subject to the email scan.

## Release history

The entire reachable history must be suitable for publication. A deletion commit
or `.gitignore` rule does not remove earlier content. Do not merge a private
deployment branch into the public source history. Use a separate clean checkout
when importing reusable changes from a private deployment, then review the diff.

For a repository that previously held private material, verify branches, tags,
pull requests, comments and their edit history, Actions logs/artifacts, releases,
wiki content, and forks before changing visibility. GitHub can retain cached
commits and read-only pull-request references after a Git history rewrite.
Those references require a separate server-side purge; see
[GitHub's removal guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).

Keep such a repository private until the server-side removal is verified. A clean
working tree alone is not a publication gate. Keep any recovery archive offline
or in access-controlled private storage outside the public checkout.
