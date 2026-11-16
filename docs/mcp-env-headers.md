# MCP authentication from a private env file

Long-lived clients do not inherit credentials added to a shell after they start.
For clients that support a local HTTP-header helper, `scripts/adhoc/mcp_env_headers.py`
reads one literal token from an explicitly selected private file. No secret is
stored in the client configuration or command arguments.

The default invocation checks availability without printing the token:

```sh
python3 scripts/adhoc/mcp_env_headers.py \
  --env-file /opt/example-config/service.env --variable SERVICE_API_KEY
```

The file must be a regular, single-link file owned by the running user with no
group or other access. Symlinks, shell expansion, duplicate assignments and
control characters are refused. Literal unquoted, single-quoted and double-quoted
assignments are accepted. The helper never evaluates a shell profile or inherits
a credential from its parent. Keep the token file outside Git.

For the native `http_headers_helper` interface verified in Codex 0.169, configure
the absolute interpreter and reviewed helper paths as a command string, ending
with the explicit `--emit-headers` option. The client receives a JSON Authorization
header over its private pipe. Do not run emission interactively, redirect its
output to a log, or use it as a diagnostic command; nonterminal stdout is not a
guarantee that a caller will handle secrets correctly.

Replace the selected server's conflicting bearer-environment reference only
after backing up the configuration. Preserve the URL, tool filters and approval
policy. Check the exact installed client's support before applying this interface.
The [native implementation](https://github.com/openai/codex/blob/rust-v0.169.0/codex-rs/rmcp-client/src/http_headers.rs)
bounds the helper, restricts its headers to the configured origin and gives
explicit bearer/OAuth credentials precedence.

Test one server end to end before wider activation. A fresh-process discovery
check does not prove an older session adopted the change. Use the client's
supported configuration refresh and inspect the specific running session's
connection afterward. Preserve a rollback copy and verify unaffected settings;
do not restart a shared backend merely to refresh one service.
