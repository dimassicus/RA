"""Optional, quota-limited recovery of original public articles."""
import http.client
import json
import os
import re
from datetime import datetime
from html import unescape
from urllib.parse import quote, urlencode, urlsplit

from . import db
from .article_content import MAX_BYTES, MAX_TEXT, canonical_url, public_target
from .publisher_lookup import normalized, original_title

HOSTS = {'Brave': 'api.search.brave.com', 'Jina': 'r.jina.ai', 'Firecrawl': 'api.firecrawl.dev'}


def configured(provider):
    return bool(os.environ.get(f'RA_{provider.upper()}_API_KEY', '').strip())


def request(provider, path, *, headers=None, body=None):
    """Fixed TLS destination, no redirects, one atomic reservation before sending."""
    prefix = f'RA_{provider.upper()}'
    key = os.environ[prefix + '_API_KEY'].strip()
    if provider == 'Firecrawl':
        reserved_day = db.reserve_firecrawl_credit()
        if reserved_day is None:
            raise ValueError('Firecrawl monthly credit limit reached')
    elif provider == 'Jina':
        db.reserve_news_api_request('jina', None)
    else:
        try:
            limit = int(os.environ.get(prefix + '_DAILY_LIMIT', '20'))
        except ValueError:
            limit = 0
        if not db.reserve_news_api_request(provider.lower(), limit):
            raise ValueError(f'{provider} daily request limit reached')
    auth = {'X-Subscription-Token': key} if provider == 'Brave' else {'Authorization': 'Bearer ' + key}
    connection = http.client.HTTPSConnection(HOSTS[provider], timeout=22)
    try:
        try:
            connection.request('POST' if body else 'GET', path, json.dumps(body) if body else None,
                               {'Accept': 'application/json', 'Content-Type': 'application/json', **auth, **(headers or {})})
            response = connection.getresponse()
        except (ValueError, http.client.HTTPException, OSError):
            raise ValueError(f'{provider} request failed') from None
        if response.status != 200:
            # These API rejections precede a scrape. Retain the reservation on ambiguous failures.
            if provider == 'Firecrawl' and response.status in (400, 401, 402, 403, 422, 429):
                db.release_firecrawl_credit(reserved_day)
            raise ValueError(f'{provider} HTTP {response.status}')
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError(f'{provider} response exceeded size limit')
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            result = None
        if not isinstance(result, dict):
            raise ValueError(f'{provider} returned invalid data')
        if provider == 'Firecrawl' and isinstance(result.get('data'), dict):
            metadata = result['data'].get('metadata')
            if isinstance(metadata, dict) and metadata.get('creditsUsed') == 0:
                db.release_firecrawl_credit(reserved_day)
        return result
    finally:
        connection.close()


def matches_title(title, hint):
    expected = normalized(original_title(hint))
    if not expected or not isinstance(title, str):
        return False
    title = unescape(title).strip()
    if normalized(title) == expected:
        return True
    # Common publisher branding appended to the HTML title.
    parts = re.split(r'\s+[|–—-]\s+', title)
    return (len(parts) > 1 and normalized(parts[-1]) == normalized(hint['publisher'])
            and normalized(' '.join(parts[:-1])) == expected)


def matches_date(date, hint):
    try:
        actual = datetime.fromisoformat(date.replace('Z', '+00:00'))
        saved = datetime.fromisoformat(hint['published_at'])
        return abs((saved - actual).total_seconds()) <= 86400
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
        return False


def brave_destination(hint):
    if not configured('Brave') or not all(hint.get(k) for k in ('title', 'publisher', 'published_at')):
        return ''
    query = original_title(hint)[:380] + ' ' + hint['publisher'][:100]
    result = request('Brave', '/res/v1/web/search?' + urlencode({'q':query, 'count':5, 'country':'ALL', 'spellcheck':'false'}))
    matches = set()
    for hit in (result.get('web') or {}).get('results', [])[:5]:
        if not isinstance(hit, dict):
            continue
        url = hit.get('url', '')
        host = urlsplit(url).hostname or ''
        profile = hit.get('profile') or {}
        publisher = normalized(hint['publisher'])
        if (url.startswith('https://') and host not in ('news.google.com', 'www.bing.com')
                and publisher in (normalized(profile.get('name') or ''), normalized(host.removeprefix('www.')))
                and matches_title(hit.get('title'), hint)):
            matches.add(canonical_url(url))
    return matches.pop() if len(matches) == 1 else ''


GATE = re.compile(r'(?:subscribe|sign in|log in) to (?:continue|read|access)|unlock full access|verify you are human|checking your browser', re.I)


def read_publisher(url, hint, original_error, page_dates=()):
    """Provider Markdown may contain nearby stories. Retain it for review only."""
    attempts = []
    for provider in ('Jina', 'Firecrawl'):
        if not configured(provider):
            continue
        public_target(url)
        try:
            if provider == 'Jina':
                data = request(provider, '/' + quote(url, safe=':/?&=%+;,@!$()*~-._'),
                               headers={'X-Token-Budget':'15000', 'X-Timeout':'15', 'X-Retain-Images':'none'}).get('data') or {}
                if not isinstance(data, dict):
                    raise ValueError('invalid reader response')
                title, date, final, text = (data.get(k) for k in ('title','publishedTime','url','content'))
            else:
                data = request(provider, '/v2/scrape', body={'url':url, 'formats':['markdown'], 'onlyMainContent':True,
                                                           'proxy':'basic', 'timeout':15000}).get('data') or {}
                if not isinstance(data, dict) or not isinstance(data.get('metadata'), dict):
                    raise ValueError('invalid reader response')
                meta = data['metadata']
                if meta.get('statusCode') != 200:
                    raise ValueError('publisher response unavailable')
                title = meta.get('og:title') or meta.get('title')
                date = meta.get('article:published_time') or meta.get('publishedTime')
                final, text = meta.get('url'), data.get('markdown')
            if not isinstance(final, str) or canonical_url(final) != canonical_url(url) or not matches_title(title, hint):
                raise ValueError('publisher URL or headline mismatch')
            dates = [date] if date else list(page_dates)
            if not dates or not all(matches_date(d, hint) for d in dates):
                raise ValueError('publication date unconfirmed')
            if isinstance(text, str) and GATE.search(text):
                raise PermissionError('Publisher access gate detected')
            if not isinstance(text, str) or len(text) > MAX_TEXT:
                raise ValueError('unusable text')
            # Omit linked cards, headings, embedded images and short interface labels.
            lines = [line.strip() for line in text.splitlines() if len(line.split()) >= 8 and
                     not line.lstrip().startswith(('#','* ','- ','!','|')) and '](http' not in line]
            body = '\n'.join(lines)
            if len(body) < 200 or len(body.split()) < 30:
                raise ValueError('insufficient publisher paragraphs')
            return body, provider + ' publisher text (analyst review)'
        except PermissionError:
            raise ValueError(f'{provider}: publisher access gate detected') from None
        except (ValueError, OSError, http.client.HTTPException) as exc:
            reason = str(exc) if type(exc) is ValueError else 'request failed'
            attempts.append(f'{provider}: {reason}')
    if attempts:
        raise ValueError(str(original_error) + '. ' + '. '.join(attempts))
    raise original_error
