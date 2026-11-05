"""Recover original links, without using snippets or syndicated copies as evidence."""
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import parse_qs, urlencode, urlsplit
from xml.etree import ElementTree
import json
import re
import unicodedata

RPC = "https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je"


def google_destination(raw, wrapper, fetch):
    """Read the same article-specific URL handoff supplied to the Google News page."""
    token = urlsplit(wrapper).path.rstrip('/').rsplit('/', 1)[-1]
    if not re.fullmatch(r'[\w-]{10,3000}', token):
        return ''
    for match in re.finditer(rb'\bdata-p="([^"]{5,12000})"', raw):
        payload = unescape(match[1].decode('utf-8', errors='replace'))
        if not payload.startswith('%.@.'):
            continue
        try:
            data = json.loads('[' + payload[4:])
            if data[1] != token or not isinstance(data[-2], int) or not isinstance(data[-1], str):
                continue
            req = json.dumps(['garturlreq',data[0],token,data[-2],data[-1]])
            body = urlencode({'f.req':json.dumps([[['Fbv4je',req,None,'generic']]])}).encode()
            result, _, _ = fetch(RPC, post=body)
            for row in json.loads(result.decode().removeprefix(")]}'").strip()):
                if len(row) > 2 and row[:2] == ['wrb.fr','Fbv4je']:
                    dest = json.loads(row[2])
                    if dest[0] == 'garturlres' and isinstance(dest[1], str) and dest[1].startswith('https://'):
                        return dest[1]
        except (ValueError, TypeError, IndexError, KeyError):
            continue
    return ''


def normalized(text):
    return ' '.join(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', text).casefold()))


def original_title(hint):
    title = hint.get('title', '')
    publisher = hint.get('publisher', '')
    return title.removesuffix(' - ' + publisher) if publisher else title


def search_destination(hint, fetch):
    title, publisher = original_title(hint), hint.get('publisher','')
    if not title or not publisher or not hint.get('published_at'):
        return ''
    query = '"' + title[:400].replace('"',' ') + '" ' + publisher[:100]
    url = 'https://www.bing.com/news/search?' + urlencode({'q':query, 'format':'rss'})
    raw, _, _ = fetch(url)
    hits = set()
    for item in ElementTree.fromstring(raw).findall('./channel/item')[:15]:
        source = next((v.text or '' for v in item if v.tag.rsplit('}',1)[-1].lower()=='source'), '')
        if normalized(item.findtext('title','')) != normalized(title) or normalized(source) != normalized(publisher):
            continue
        try:
            delta = parsedate_to_datetime(item.findtext('pubDate','')) - datetime.fromisoformat(hint['published_at'])
        except (ValueError, TypeError, OverflowError):
            continue
        if abs(delta.total_seconds()) > 86400:
            continue
        dest = item.findtext('link','')
        parts = urlsplit(dest)
        if parts.hostname in ('www.bing.com','bing.com') and parts.path.casefold() == '/news/apiclick.aspx':
            dest = parse_qs(parts.query).get('url',[''])[0]
        if dest.startswith('https://') and urlsplit(dest).hostname not in ('news.google.com','www.bing.com','bing.com'):
            hits.add(dest)
    return hits.pop() if len(hits) == 1 else ''
