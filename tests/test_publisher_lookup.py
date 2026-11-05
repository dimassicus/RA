from datetime import datetime, timezone
from html import escape
import json
from urllib.parse import parse_qs
import pytest

from ra import article_content as reader
from ra.publisher_lookup import RPC, google_destination, search_destination

TITLE = "Clube mantém taxa em 13,75%"
HINT = {'title':TITLE + ' - Jornal', 'publisher':'Jornal','published_at':'2026-11-04T12:00:00+00:00'}
URL = 'https://publisher.example/credit'
BODY = 'The company issued a public statement about its quarterly financial position. ' * 6


def test_google_handoff_is_bound_to_the_original_article_and_destination_is_not_fetched_here():
    token = 'CBMi0123456789'
    fields = [['en-US'],token,0,1,None,False,123,'sig']
    raw = ('<div data-p="'+escape('%.'+'@.'+json.dumps(fields)[1:],quote=True)+'">').encode()
    def fetch(url, *, post):
        assert url == RPC
        row = json.loads(parse_qs(post.decode())['f.req'][0])[0][0]
        assert json.loads(row[1]) == ['garturlreq',['en-US'],token,123,'sig']
        return (b")]}'\n\n" + json.dumps([['wrb.fr','Fbv4je',json.dumps(['garturlres', URL])]]).encode(), '', url)
    assert google_destination(raw,'https://news.google.com/articles/'+token,fetch) == URL
    assert not google_destination(raw,'https://news.google.com/articles/anotherToken',fetch)


def rss(title=TITLE, publisher='Jornal', date='Wed, 04 Nov 2026 14:00:00 GMT', url=URL):
    return ('<item><title>'+escape(title)+'</title><source>'+publisher+'</source><pubDate>'+date+
            '</pubDate><link>'+escape(url)+'</link></item>')


@pytest.mark.parametrize('items, expected', [(rss(),URL),(rss(publisher='Syndicate'),''),
    (rss(title=TITLE.replace('13','14')),''),(rss(date='Wed, 04 Nov 2020 14:00:00 GMT'),''),
    (rss()+rss(url=URL+'-other'),'')])
def test_search_requires_exact_original_title_publisher_date_and_unique_destination(items, expected):
    fetch = lambda url:(('<rss><channel>'+items+'</channel></rss>').encode(),'application/xml',url)
    assert search_destination(HINT,fetch) == expected


def test_extraction_uses_one_marked_body_and_excludes_article_cards():
    raw=('<article><div itemprop="articleBody">'+BODY+'</div></article><article>Unrelated default</article>').encode()
    assert reader.extract(raw,'text/html')[0] == BODY.strip()
    with pytest.raises(ValueError,match='Multiple articles'):
        reader.extract(raw+raw,'text/html')


def test_search_hit_cannot_supply_evidence_if_publisher_page_title_disagrees(monkeypatch):
    def fetch(url, **kw):
        if url.startswith('https://www.bing.com/'):return ('<rss><channel>'+rss()+'</channel></rss>').encode(),'text/xml',url
        return ('<meta property="og:title" content="Another story"><article>'+BODY+'</article>').encode(),'text/html',url
    monkeypatch.setattr(reader,'fetch_bytes',fetch)
    result=reader._retrieve('https://news.google.com/rss/articles/unresolved',HINT)
    assert result['status']=='unavailable' and 'does not confirm' in result['reason']


def test_empty_html_attributes_do_not_discard_readable_article():
    body, _ = reader.extract(('<article><p class id>'+BODY+'</p></article>').encode(),'text/html')
    assert body == BODY.strip()
