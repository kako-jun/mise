#!/usr/bin/env python3
"""Small, explicit-seed collection trial; does not follow page links or write a database."""
import datetime
import html.parser
import json
import sys
import subprocess
import email.parser
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

USER_AGENT = 'MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)'
SEEDS = ['https://www.hyuki.com/', 'https://www.kmonos.net/']

class Metadata(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(); self.title = ''; self.in_title = False; self.description = None; self.robots = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'title': self.in_title = True
        if tag == 'meta' and attrs.get('name', '').lower() == 'description': self.description = attrs.get('content')
        if tag == 'meta' and attrs.get('name', '').lower() in ('robots', 'miseresearchbot'): self.robots.append(attrs.get('content', ''))
    def handle_endtag(self, tag):
        if tag == 'title': self.in_title = False
    def handle_data(self, data):
        if self.in_title: self.title += data

def fetch(url):
    # curl uses the host CA configuration; never follow redirects without checking robots again.
    result = subprocess.run(['curl', '--silent', '--show-error', '--max-time', '15', '--max-filesize', '1048576', '--user-agent', USER_AGENT, '--dump-header', '-', url], check=True, capture_output=True)
    raw_headers, body = result.stdout.split(b'\r\n\r\n', 1)
    # Proxies can prepend a CONNECT response.
    if raw_headers.startswith(b'HTTP/1.1 200 Connection established'):
        raw_headers, body = body.split(b'\r\n\r\n', 1)
    first, _, rest = raw_headers.partition(b'\r\n')
    status = int(first.split()[1]); headers = email.parser.BytesParser().parsebytes(rest)
    if status >= 400: raise urllib.error.HTTPError(url, status, 'HTTP error', headers, None)
    if status != 200: raise ValueError('Redirect or unexpected status requires separate robots check')
    if len(body) > 1024 * 1024: raise ValueError('Response exceeds 1 MiB')
    return status, headers.get_content_type(), headers, body

def collect(url):
    parts = urllib.parse.urlsplit(url)
    robots_url = f'{parts.scheme}://{parts.netloc}/robots.txt'
    try:
        status, _, _, body = fetch(robots_url)
        if status != 200: raise ValueError('Unexpected robots status')
        rules = body.decode('utf-8', errors='replace').splitlines()
    except urllib.error.HTTPError as e:
        if e.code not in (404, 410): raise
        status = e.code; rules = []
    parser = urllib.robotparser.RobotFileParser(); parser.parse(rules)
    if rules and not parser.can_fetch(USER_AGENT, url): raise ValueError('Disallowed by robots.txt')
    time.sleep(1)
    code, content_type, headers, body = fetch(url)
    if content_type not in ('text/html', 'application/xhtml+xml'): raise ValueError('Not HTML')
    charset = headers.get_content_charset() or 'utf-8'
    parsed = Metadata(); parsed.feed(body.decode(charset, errors='replace'))
    if any('noindex' in value.lower() or 'none' in value.lower() for value in [headers.get('X-Robots-Tag', ''), *parsed.robots]):
        raise ValueError('Page requests noindex')
    return {'url':url, 'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'), 'robots_url':robots_url, 'robots_http_status':status, 'page_http_status':code, 'title':parsed.title.strip(), 'meta_description':parsed.description}

if __name__ == '__main__':
    results = []
    for seed in SEEDS:
        try: results.append(collect(seed))
        except (ValueError, urllib.error.URLError, OSError, subprocess.CalledProcessError) as exc: print(f'{seed}: {exc}', file=sys.stderr)
        time.sleep(1)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if len(results) != len(SEEDS): sys.exit(1)
