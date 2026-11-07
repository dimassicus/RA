"""Bounded public-source retrieval. No cookies, credentials, JS or paywall bypass.

Network and parsing run in a disposable subprocess with a wall-clock deadline.
Connections pin a validated public IP while retaining TLS hostname verification.
"""
from __future__ import annotations

import gzip
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

READER_VERSION = "public-source-v4"
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


def fetch_bytes(url, *, post=None):
    from .publisher_lookup import RPC
    if post is not None and url != RPC:
        raise ValueError('Invalid Google News handoff endpoint')
    for _ in range(4):
        host, address = public_target(url)
        connection = http.client.HTTPSConnection(host, timeout=6)
        # HTTPSConnection wraps this socket using the ORIGINAL hostname for SNI/TLS.
        connection._create_connection = lambda *args, **kwargs: socket.create_connection((address, 443), timeout=6)
        parts = urlsplit(url)
        try:
            connection.request('POST' if post is not None else 'GET', urlunsplit(('', '', parts.path or '/', parts.query, '')), body=post,
                               headers={'User-Agent': 'RA-Credit-Monitor/0.2 (public article research)',
                                        **({'Content-Type':'application/x-www-form-urlencoded'} if post is not None else {}),
                                        'Accept-Encoding': 'identity', 'Accept': 'text/html,application/pdf'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                if post is not None:
                    raise ValueError('Unexpected Google handoff redirect')
                location = response.getheader('Location')
                if not location:
                    raise ValueError('Redirect has no destination')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f'Source returned HTTP {response.status}; access was not bypassed')
            encoding = response.getheader('Content-Encoding', 'identity').lower()
            if encoding not in ('identity', 'gzip'):
                raise ValueError('Source uses an unsupported content encoding')
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('Source exceeds the 2 MB retrieval limit')
            if encoding == 'gzip':
                with gzip.GzipFile(fileobj=BytesIO(raw)) as zipped:
                    raw = zipped.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError('Decompressed source exceeds the 2 MB retrieval limit')
            return raw, response.getheader('Content-Type', ''), url
        finally:
            connection.close()
    raise ValueError('Source exceeded the redirect limit')


class ArticleHTML(HTMLParser):
    """Keep article/main paragraphs only; exclude navigation and related-story cards."""
    def __init__(self, explicit_only=False):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []
        self.structured = []
        self.script = None
        self.explicit_only = explicit_only
        self.article_count = 0
        self.page_titles = []
        self.page_dates = []

    def handle_starttag(self, tag, attrs):
        attrs = {key: value or '' for key, value in attrs}
        if tag == 'meta' and attrs.get('property') == 'og:title':
            self.page_titles.append(attrs.get('content',''))
        if tag == 'meta' and (attrs.get('property') in ('article:published_time', 'og:article:published_time')
                              or 'datePublished' in (attrs.get('itemprop'), attrs.get('name'))):
            self.page_dates.append(attrs.get('content', ''))
        parent = self.stack[-1][1:] if self.stack else (False, False)
        in_article, blocked = parent
        marked = attrs.get('itemprop') == 'articleBody'
        if not blocked and ((self.explicit_only and marked) or
                (not self.explicit_only and tag == 'article' and not any(row[0]=='article' for row in self.stack))):
            self.article_count += 1
        in_article = in_article or marked or (not self.explicit_only and tag in ('article', 'main'))
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


def structured_articles(node):
    if isinstance(node, list):
        for entry in node:
            yield from structured_articles(entry)
    elif isinstance(node, dict):
        kind = node.get('@type', '')
        kinds = kind if isinstance(kind, list) else [kind]
        if any(k in ('NewsArticle', 'Article', 'ReportageNewsArticle') for k in kinds):
            if node.get('isAccessibleForFree') in (False, 'False', 'false'):
                raise ValueError('Publisher marks this article as restricted')
            yield node
        for key in ('@graph', 'mainEntity'):
            yield from structured_articles(node.get(key))


def parse_html(raw, content_type):
    match = re.search(r'charset=["\']?([\w-]+)', content_type)
    text = raw.decode(match[1] if match else 'utf-8', errors='replace')
    parser = ArticleHTML()
    parser.feed(text)
    for item in structured_articles(parser.structured):
        if isinstance(item.get('datePublished'), str):
            parser.page_dates.append(item['datePublished'])
        if isinstance(item.get('headline'), str):
            parser.page_titles.append(item['headline'])
    return parser, text


def extract(raw, content_type, parsed=None):
    if raw.startswith(b'%PDF-') or 'application/pdf' in content_type:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(raw))
        if reader.is_encrypted or len(reader.pages) > 30:
            raise ValueError('Encrypted PDF or document over 30 pages; analyst reading required')
        body = '\n\n'.join(page.extract_text() or '' for page in reader.pages)
        method = 'PDF text (no OCR)'
    elif 'html' in content_type:
        parser, text = parsed or parse_html(raw, content_type)
        bodies = [item['articleBody'] for item in structured_articles(parser.structured)
                  if isinstance(item.get('articleBody'), str)]
        if not bodies and parser.article_count > 1:
            primary = ArticleHTML(explicit_only=True)
            primary.feed(text)
            if primary.article_count == 1:
                parser = primary
        if len(bodies) > 1 or (not bodies and parser.article_count > 1):
            raise ValueError('Multiple articles on one page; a direct article URL is required')
        body = max(bodies, key=len) if bodies else ''.join(parser.parts)
        method = 'JSON-LD articleBody' if bodies else 'HTML article/main text'
        if bodies:
            structured = ArticleHTML()
            structured.feed('<article>' + body + '</article>')
            body = ''.join(structured.parts)
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


def retrieve(url, hint=None):
    """Fail closed on access failures without leaking URLs/tokens in exceptions."""
    try:
        result = subprocess.run([sys.executable, '-m', 'ra.article_content', url],
                                input=json.dumps(hint or {}), capture_output=True, text=True, timeout=120, check=True)
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, ValueError):
        return {'status': 'unavailable', 'reason': 'Source retrieval or parsing failed / timed out'}


def _retrieve(url, hint=None):
    from .publisher_lookup import google_destination, search_destination
    from . import recovery_providers as providers
    from xml.etree.ElementTree import ParseError
    hint = hint or {}
    route = ''
    destination = ''
    review_only = False
    google_hosts = ('news.google.com', 'consent.google.com')
    try:
        try:
            raw, content_type, final_url = fetch_bytes(url)
        except (OSError, ValueError) as exc:
            if not hint or (urlsplit(url).hostname != 'news.google.com' and not isinstance(exc, TimeoutError)):
                raise
            raw, content_type, final_url = b'', '', url
        if urlsplit(final_url).hostname in google_hosts:
            if urlsplit(final_url).hostname == 'news.google.com':
                try:
                    destination = google_destination(raw, url, fetch_bytes)
                except (OSError, ValueError):
                    destination = ''
            route = 'Google News publisher link'
            if not destination:
                route = 'original-headline search'
                try:
                    destination = search_destination(hint, fetch_bytes)
                except (ValueError, OSError, ParseError):
                    destination = ''
            if not destination and providers.configured('Brave'):
                route = 'Brave original-headline search'
                destination = providers.brave_destination(hint)
            if not destination:
                raise ValueError('No matching original publisher article found')
            try:
                raw, content_type, final_url = fetch_bytes(destination)
            except TimeoutError:
                raw, content_type, final_url = b'', '', destination
            if urlsplit(final_url).hostname in google_hosts:
                raise ValueError('Publisher link returned to Google News')
            if route.endswith('headline search') and urlsplit(final_url).hostname != urlsplit(destination).hostname:
                destination = ''
                raise ValueError('Search match redirected to a different host')
        destination = final_url
        parsed = parse_html(raw, content_type) if 'html' in content_type else None
        page = parsed[0] if parsed else ArticleHTML()
        if raw and route.endswith('headline search'):
            if not any(providers.matches_title(title, hint) for title in page.page_titles):
                destination = ''
                raise ValueError('Publisher page does not confirm the original headline')
            if route.startswith('Brave') and (not page.page_dates or not all(
                    providers.matches_date(date, hint) for date in page.page_dates)):
                raise ValueError('Publisher page does not confirm the publication date')
        try:
            if not raw and not content_type:
                raise TimeoutError('Publisher timed out')
            body, method = extract(raw, content_type, parsed)
        except (ValueError, TimeoutError) as exc:
            recoverable = isinstance(exc, TimeoutError) or str(exc).startswith(('Too little article text', 'Multiple articles'))
            if not recoverable or not all(hint.get(k) for k in ('title', 'publisher', 'published_at')):
                raise
            if page.page_dates and not all(providers.matches_date(d, hint) for d in page.page_dates):
                raise ValueError('Publisher page does not confirm the publication date')
            if page.page_titles and not any(providers.matches_title(t, hint) for t in page.page_titles):
                raise ValueError('Publisher page does not confirm the original headline')
            body, method = providers.read_publisher(destination, hint, exc, page.page_dates)
            review_only = True
        from .language import detect_language
        return {'status': 'available', 'body': body, 'final_url': canonical_url(final_url),
                'language': detect_language(body[:4000]), 'review_only': review_only, 'method': method, 'recovery':route,
                'sha256': hashlib.sha256(body.encode()).hexdigest(), 'reason': ''}
    except (socket.timeout, TimeoutError):
        reason = 'Publisher or search endpoint timed out'
    except socket.gaierror:
        reason = 'Publisher or search hostname could not be resolved'
    except OSError as exc:
        reason = 'Public source connection failed: ' + type(exc).__name__
    except ValueError as exc:
        # Only deliberately authored validation messages, never exception URLs.
        reason = str(exc) if type(exc) is ValueError else 'Source parsing failed'
        if reason.startswith(('Only public HTTPS', 'Source resolves to a non-public')):
            destination = ''
    except Exception:
        reason = 'Source retrieval or parsing failed'
    return {'status':'unavailable','reason':reason,'final_url':destination or url,'recovery':route}


if __name__ == '__main__':
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    print(json.dumps(_retrieve(sys.argv[1], json.loads(sys.stdin.read(10000) or '{}'))))
