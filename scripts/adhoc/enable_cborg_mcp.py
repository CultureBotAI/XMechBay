#!/usr/bin/env python3
"""2026-11-07: Apply user-approved CBORG activation; config in, backup/config out.
Provenance: XMechBay CBORG MCP setup PR. Retains prompts and tool exclusions.
"""
from pathlib import Path
import re
import shutil

config = Path.home() / '.codex/config.toml'
backup = config.with_name(config.name + '.bak-cborg-enable-20261107')
text = config.read_text()
for name in ('tavily', 'context7', 'valency'):
    pattern = r'(\[mcp_servers\.' + name + r'\]\n)(.*?)(?=\n\[|\Z)'
    match = re.search(pattern, text, flags=re.S)
    if not match or 'default_tools_approval_mode = "prompt"' not in match[2]:
        raise SystemExit('Expected prompted server configuration missing: ' + name)
    body, count = re.subn(r'^enabled = false$', 'enabled = true', match[2], flags=re.M)
    if count != 1:
        raise SystemExit('Expected disabled server: ' + name)
    text = text[:match.start(2)] + body + text[match.end(2):]
if backup.exists():
    raise SystemExit('Refusing to overwrite backup')
shutil.copy2(config, backup)
config.write_text(text)
assert config.read_text() == text
print('Enabled tavily, context7, valency; prompts retained. Backup:', backup)
