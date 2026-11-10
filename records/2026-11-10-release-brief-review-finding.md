# Clarify separate-PR merge authority in freshness release brief

Independent read-only review of coordinator PR #70 found a narrow wording ambiguity in `goals/claw-intel-freshness-release-scope-clarification-20261110.txt`: its last paragraph says new "unrelated" PRs require separate merge approval. Read alone, this could imply that related separate PRs inherit approval.

The user's authorization names claw #1128. Earlier briefs and the coordinator record already preserve separate approval for every other PR, so no additional merge was authorized or performed by this wording.

Resolution: replace the sentence with "Any separate PR requires its own merge approval; only #1128's merge is authorized." Preserve the already authorized feature release, protected-source proof and all deployment/adoption boundaries. Relay the exact correction to Intel and verify readback. This finding concerns the coordinator brief, not the claw implementation.
