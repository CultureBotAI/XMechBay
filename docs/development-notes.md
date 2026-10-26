# Infrastructure extraction

The initial reusable edition contains only general infrastructure and synthetic
examples. Deployment records, machine inventories, personal settings, and live
session content belong outside the versioned source tree.

The terminal controller checks the selected screen before input, scopes tmux
operations to a configured session, and requires verification after delivery.
The session inspector separates observed process metadata from screen-derived
status. The permission inspector reports diagnostic evidence without changing
policy or claiming to reproduce an agent's authorization decision.

Validation uses isolated fixtures and mocked transports. It does not send input
to live sessions. Mirror checks include hidden and ignored files; the publication
gate checks staged content and reachable Git history in addition to working files.
Passing a pattern scan supports review but cannot prove the absence of all private
information or audit remote GitHub history and caches.
