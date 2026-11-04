"""Bounded public-source retrieval. No cookies, credentials, JS or paywall bypass.

Network and parsing run in a disposable subprocess with a wall-clock deadline.
Connections pin a validated public IP while retaining TLS hostname verification.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import subprocess
import sys
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

READER_VERSION = "public-source-v1"
MAX_BYTES = 2_000_000
MAX_TEXT = 100_000


def canonical_url(url):
    """Only remove known tracking parameters; preserve article-identifying queries."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid', 'mc_cid', 'mc_eid')]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', urlencode(query), ''))


def public_target(url, resolver=socket.getaddrinfo):
    parts = urlsplit(url)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.port not in (None, 443) or len(url) > 4096):
        raise ValueError('Only public HTTPS source URLs on port 443 are supported')
    host = parts.hostname.encode('idna').decode('ascii')
    addresses = resolver(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('Source resolves to a non-public address')
    return host, addresses[0][4][0]


def fetch_bytes(url):
    for _ in range(4):
        host, address = public_target(url)
        connection = http.client.HTTPSConnection(host, timeout=6)
        # HTTPSConnection wraps this socket using the ORIGINAL hostname for SNI/TLS.
        connection._create_connection = lambda *args, **kwargs: socket.create_connection((address, 443), timeout=6)
        parts = urlsplit(url)
        try:
            connection.request('GET', urlunsplit(('', '', parts.path or '/', parts.query, '')),
                               headers={'User-Agent': 'RA-Credit-Monitor/0.2 (public article research)',
                                        'Accept-Encoding': 'identity', 'Accept': 'text/html,application/pdf'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location:
                    raise ValueError('Redirect has no destination')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f'Source returned HTTP {response.status}; access was not bypassed')
            if response.getheader('Content-Encoding', 'identity') != 'identity':
                raise ValueError('Compressed source response is not supported')
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('Source exceeds the 2 MB retrieval limit')
            return raw, response.getheader('Content-Type', ''), url
        finally:
            connection.close()
    raise ValueError('Source exceeded the redirect limit')


class ArticleHTML(HTMLParser):
    """Keep article/main paragraphs only; exclude navigation and related-story cards."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []
        self.structured = []
        self.script = None
        self.article_count = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        parent = self.stack[-1][1:] if self.stack else (False, False)
        in_article, blocked = parent
        if tag == "article" and not any(row[0] == "article" for row in self.stack) and not blocked:
            self.article_count += 1
        in_article = in_article or tag in ('article', 'main') or attrs.get('itemprop') == 'articleBody'
        blocked = blocked or tag in ('nav', 'aside', 'footer', 'header', 'script', 'style', 'form', 'noscript', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6') or bool(
            re.search(r'\b(?:related|recommend|recommended|comment|comments|share|newsletter|advert|menu)\b',
                      attrs.get('class', '') + ' ' + attrs.get('id', ''), re.I))
        if tag == 'script' and attrs.get('type') == 'application/ld+json':
            self.script = []
        if tag not in ('meta', 'link', 'img', 'br', 'hr', 'input', 'source', 'wbr'):
            self.stack.append((tag, in_article, blocked))
        if tag in ('p', 'div', 'br', 'li', 'h1', 'h2') and in_article and not blocked:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag == 'script' and self.script is not None:
            try:
                self.structured.append(json.loads(''.join(self.script)))
            except (ValueError, RecursionError):
                pass
            self.script = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                if tag in ('p', 'div', 'li', 'h1', 'h2'):
                    self.parts.append('\n')
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if self.stack and self.stack[-1][1] and not self.stack[-1][2]:
            self.parts.append(data)


def structured_bodies(node):
    if isinstance(node, list):
        for entry in node:
            yield from structured_bodies(entry)
    elif isinstance(node, dict):
        kind = node.get('@type', '')
        kinds = kind if isinstance(kind, list) else [kind]
        if any(k in ('NewsArticle', 'Article', 'ReportageNewsArticle') for k in kinds):
            if node.get('isAccessibleForFree') in (False, 'False', 'false'):
                raise ValueError('Publisher marks this article as restricted')
            if isinstance(node.get('articleBody'), str):
                yield node['articleBody']
        for key in ('@graph', 'mainEntity'):
            yield from structured_bodies(node.get(key))


def extract(raw, content_type):
    if raw.startswith(b'%PDF-') or 'application/pdf' in content_type:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(raw))
        if reader.is_encrypted or len(reader.pages) > 30:
            raise ValueError('Encrypted PDF or document over 30 pages; analyst reading required')
        body = '\n\n'.join(page.extract_text() or '' for page in reader.pages)
        method = 'PDF text (no OCR)'
    elif 'html' in content_type:
        match = re.search(r'charset=["\']?([\w-]+)', content_type)
        encoding = match.group(1) if match else 'utf-8'
        text = raw.decode(encoding, errors='replace')
        parser = ArticleHTML()
        parser.feed(text)
        bodies = list(structured_bodies(parser.structured))
        if len(bodies) > 1 or (not bodies and parser.article_count > 1):
            raise ValueError('Multiple articles on one page; a direct article URL is required')
        body = max(bodies, key=len) if bodies else ''.join(parser.parts)
        method = 'JSON-LD articleBody' if bodies else 'HTML article/main text'
        if re.search(r'(?:subscribe|sign in|log in) to (?:continue|read|access)|enable javascript|verify you are human', body, re.I):
            raise ValueError('Access gate detected; source needs manual review')
    else:
        raise ValueError('Source is not readable HTML or PDF')
    body = '\n'.join(re.sub(r'[^\S\n]+', ' ', line).strip() for line in body.splitlines())
    body = re.sub(r'\n{3,}', '\n\n', body).strip()
    if len(body) < 200 or len(body.split()) < 30:
        raise ValueError('Too little article text; preview, scan or extraction failure')
    if len(body) > MAX_TEXT:
        raise ValueError('Article exceeds the 100,000 character analysis limit')
    return body, method


def retrieve(url):
    """Fail closed on access failures without leaking URLs/tokens in exceptions."""
    try:
        result = subprocess.run([sys.executable, '-m', 'ra.article_content', url],
                                capture_output=True, text=True, timeout=25, check=True)
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, ValueError):
        return {'status': 'unavailable', 'reason': 'Source retrieval or parsing failed / timed out'}


def _retrieve(url):
    try:
        raw, content_type, final_url = fetch_bytes(url)
        if urlsplit(final_url).hostname in ('news.google.com', 'consent.google.com'):
            raise ValueError('Google News did not redirect to the publisher; open the original manually')
        body, method = extract(raw, content_type)
        from .language import detect_language
        return {'status': 'available', 'body': body, 'final_url': canonical_url(final_url),
                'language': detect_language(body[:4000]), 'method': method,
                'sha256': hashlib.sha256(body.encode()).hexdigest(), 'reason': ''}
    except ValueError as exc:
        # Only deliberately authored validation messages, never exception URLs.
        return {'status': 'unavailable', 'reason': str(exc) if type(exc) is ValueError else 'Source parsing failed'}
    except Exception:
        return {'status': 'unavailable', 'reason': 'Source retrieval or parsing failed'}


if __name__ == '__main__':
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    print(json.dumps(_retrieve(sys.argv[1])))
