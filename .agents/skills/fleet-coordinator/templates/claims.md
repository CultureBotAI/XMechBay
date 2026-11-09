# Work claims (kept by the coordinator, outside any repository checkout)

Check this file before editing a file another stream may share. The coordinator updates it when a stream starts, changes or finishes.

| Stream | Worker | Host | Owns | Must not touch |
|---|---|---|---|---|
| A: example feature | worker session in window 101 | worker-a.example.invalid | repository example/RepoA; PR example/RepoA#12 | the shared registry files |
| B: example deployment | worker session in window 202 | worker-b.example.invalid | the deployment checkout on that host | any merge |

Updated YYYY-MM-DD by the coordinator.
