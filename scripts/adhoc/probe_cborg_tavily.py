#!/usr/bin/env python3
"""2026-11-07: Verify CBORG Tavily for MCP setup; env key in, JSON results out.

Supports discovery and a single explicit tool call. Never prints credentials.
Provenance: XMechBay LBNL Tavily MCP setup PR.
"""
import argparse
import json
import os
import urllib.request
import urllib.error


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', choices=['tavily', 'context7', 'valency'], default='tavily')
    parser.add_argument('--tool')
    parser.add_argument('--arguments', default='{}')
    args = parser.parse_args()
    allowed_tools = {
        'tavily': {'tavily-tavily_search', 'tavily-tavily_extract'},
        'context7': {'context7-resolve-library-id', 'context7-query-docs'},
        'valency': {'valency-list_sources', 'valency-get_author_papers',
                    'valency-search_by_author', 'valency-search_by_title'},
    }
    if args.tool and args.tool not in allowed_tools[args.server]:
        parser.error('Tool refused: only the explicitly reviewed read-only tools '
                     'are allowed. Notification-capable calls are forbidden by '
                     'the standing send-capable-script rule.')
    key = os.environ['CBORG_API_KEY']
    headers = {'Authorization': 'Bearer ' + key,
               'Content-Type': 'application/json',
               'Accept': 'application/json, text/event-stream'}
    opener = urllib.request.build_opener(NoRedirect())

    def rpc(method, params, request_id=None):
        body = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if request_id is not None:
            body['id'] = request_id
        req = urllib.request.Request('https://api.cborg.lbl.gov/mcp/' + args.server,
                                     data=json.dumps(body).encode(), headers=headers)
        with opener.open(req, timeout=60) as response:
            session = response.headers.get('Mcp-Session-Id')
            if session:
                headers['Mcp-Session-Id'] = session
            raw = response.read().decode()
        if not raw.strip():
            return {}
        if raw.lstrip().startswith('{'):
            result = json.loads(raw)
        else:
            events = [json.loads(line[5:].strip()) for line in raw.splitlines()
                      if line.startswith('data:')]
            result = next(event for event in events if event.get('id') == request_id)
        if 'error' in result:
            raise RuntimeError(json.dumps(result['error']).replace(key, '[REDACTED]'))
        return result.get('result', {})

    initialized = rpc('initialize', {'protocolVersion': '2024-11-05',
                      'capabilities': {}, 'clientInfo': {'name': 'xmechbay-probe',
                                                         'version': '1.0'}}, 1)
    headers['MCP-Protocol-Version'] = initialized['protocolVersion']
    rpc('notifications/initialized', {})
    if args.tool:
        result = rpc('tools/call', {'name': args.tool,
                                  'arguments': json.loads(args.arguments)}, 2)
    else:
        result = rpc('tools/list', {}, 2)
    print(json.dumps(result, indent=2).replace(key, '[REDACTED]'))


if __name__ == '__main__':
    try:
        main()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors='replace')
        print(('HTTP %s: %s' % (exc.code, detail)).replace(
            os.environ.get('CBORG_API_KEY', '\0'), '[REDACTED]'))
        raise SystemExit(1)
