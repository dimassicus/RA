"""Public governance disclosures matched to the watched legal entity."""
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .aliases import contains, normalized
from .news_topics import excluded_story
from .article_content import fetch_bytes, retrieve
from .preliminary import country_code, id_equal


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lines = []
        self.hidden = False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.hidden = True

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.hidden = False

    def handle_data(self, data):
        line = ' '.join(data.split())
        if line and not self.hidden:
            self.lines.append(line)


def section(lines, start, end):
    begin = lines.index(start) + 1
    return lines[begin:lines.index(end, begin)]


def add(entries, name, role):
    if not all(isinstance(v, str) and 1 < len(v.strip()) < 160 for v in (name, role)):
        raise ValueError('Incomplete name or role')
    key = normalized(name) + '|' + normalized(role)
    if key in entries:
        raise ValueError('Duplicate entry')
    entries[key] = f'{name} · {role}'


def athens(page, lines):
    entries = {}
    for line in section(lines, 'Full Name (Title)', 'Results'):
        match = re.fullmatch(r'([^()]+) \(([^()]+)\)', line)
        if not match:
            raise ValueError('Incomplete Athens board row')
        add(entries, *match.groups())
    holders = section(lines, 'Voting Rights (>=5%)', 'Company Staff Number')
    if not entries or not holders:
        raise ValueError('Missing Athens board or voting-rights section')
    for line in holders:
        match = re.fullmatch(r'(.+) \((\d+(?:\.\d+)?)%\)', line)
        if not match or not 5 <= Decimal(match[2]) <= 100:
            raise ValueError('Incomplete disclosed voting rights')
        entries['holding|' + normalized(match[1])] = f'{match[1]} · {match[2]}% voting rights'
    return 'Euronext Athens · board and voting rights of 5% or more', entries


def cms(page, lines):
    rows = section(lines, 'BOARD OF DIRECTORS', 'INVESTOR RELATIONS')
    if not rows or len(rows) % 2:
        raise ValueError('Incomplete CMS board')
    entries = {}
    for name, role in zip(rows[::2], rows[1::2]):
        if not re.search('President|Member', role):
            raise ValueError('Unrecognised CMS board role')
        add(entries, name, role)
    return 'CMS Jant ve Makina · issuer board', entries


def pvfcco(page, lines):
    entries = {}
    for start, end in [('Board of members', 'Board of Supervisors'), ('Board of Directors', None)]:
        if end:
            rows = section(lines, start, end)
        else:
            tail = lines[lines.index(start) + 1:]
            boundary = next((i for i, line in enumerate(tail) if re.fullmatch(r'[+()\d -]{8,25}', line)), None)
            if boundary is None:
                raise ValueError('Missing end of executive roster')
            rows = tail[:boundary]
        if not rows or len(rows) % 3:
            raise ValueError('Incomplete PVFCCo board section')
        for prefix, name, role in zip(rows[::3], rows[1::3], rows[2::3]):
            if prefix not in ('Mr.', 'Ms.', 'Mrs.') or not re.search(r'BOD|President|CEO', role):
                raise ValueError('Unrecognised PVFCCo member row')
            add(entries, name, role)
    return 'PVFCCo · board members and executive management', entries


def bulletin_holdings(page, lines):
    links = []
    for url, text in re.findall(r'<a\s+href="(https://dpm\.vn/en/ban-tin-nha-dau-tu/[^"#]+)"[^>]*>(.*?)</a>', page, re.S):
        date = re.search(r'class="file-date">(\d{2}/\d{2}/\d{4})<', text)
        if date:
            links.append((datetime.strptime(date[1], '%d/%m/%Y'), url))
    if not links:
        raise ValueError('Issuer bulletin list unavailable')
    _, url = max(links)
    raw, mime, final = fetch_bytes(url)
    if final != url or 'html' not in mime:
        raise ValueError('Issuer bulletin page unavailable')
    files = set(re.findall(r'(?:src|href)="(/public_folder/files_upload/[^"?#]+\.pdf)"', raw.decode('utf-8'), re.I))
    if len(files) != 1:
        raise ValueError('Missing or ambiguous issuer bulletin attachment')
    pdf = 'https://dpm.vn' + files.pop()
    doc = retrieve(pdf)
    if doc.get('status') != 'available' or doc.get('final_url') != pdf or doc.get('method') != 'PDF text (no OCR)':
        raise ValueError('Issuer bulletin PDF could not be verified')
    label, holdings = parse_bulletin(doc['body'])
    return label, holdings, pdf


def parse_bulletin(body):
    if 'PetroVietnam Fertilizer and Chemicals Corporation' not in body or 'HOSE: DPM' not in body:
        raise ValueError('Unexpected bulletin issuer')
    table = re.search(r'Key shareholders\s*\((\d{2}/\d{2}/\d{4})\):\s*(.*?)\s*I\.\s+MESSAGE TO INVESTORS', body, re.S)
    if not table:
        raise ValueError('Incomplete dated key shareholder section')
    as_of = datetime.strptime(table[1], '%d/%m/%Y').date()
    entries = {}
    total = Decimal(0)
    for row in table[2].split('•'):
        if not row.strip():
            continue
        match = re.fullmatch(r'([^:]{2,150}):\s*(\d+(?:\.\d+)?)%', ' '.join(row.split()))
        if not match or Decimal(match[2]) > 100:
            raise ValueError('Incomplete shareholder row in issuer bulletin')
        name, pct = match.groups()
        total += Decimal(pct)
        if normalized(name) in ('foreign ownership', 'other', 'others'):
            continue
        key = normalized(name)
        if key in entries:
            raise ValueError('Duplicate holder in issuer bulletin')
        entries[key] = f'{name} · {pct}%'
    if not entries or total > Decimal('100.05'):
        raise ValueError('Invalid disclosed key shareholder figures')
    return f'PVFCCo investor bulletin · named key shareholders as of {as_of}', entries


def delemar(page, lines):
    body = section(lines, 'Our Leadership', 'Dele')
    entries = {}
    # The page also contains biographies and links. Compare only its named chief officers.
    for i, line in enumerate(body):
        if re.fullmatch('Chief [A-Za-z &-]+ Officer', line) and i:
            add(entries, body[i-1], line)
    if not any(v.endswith(' · Chief Executive Officer') for v in entries.values()):
        raise ValueError('Missing group chief executive')
    return 'Delemar Industrial Group · group chief officers only', entries


def delemar_entity(page, lines):
    rows = section(lines, 'المسؤولين', 'الموبايل')
    if len(rows) != 2 or rows[0] != 'المدير العام' or 'www.delemaralu.com' not in lines:
        raise ValueError('Incomplete entity-specific manager listing')
    entries = {}
    add(entries, rows[1], 'General manager')
    return 'EGYDIR directory · profiles-production entity · undated, verify with ITDA', entries


def balton(page, lines):
    if lines[lines.index('ИНН') + 1] != '201079149':
        raise ValueError('Balton tax number differs')
    manager = section(lines, 'Управленческая информация', 'Учредители')
    founders = section(lines, 'Учредители', 'Товарные знаки организации')
    if len(manager) != 2 or manager[0] != 'Руководитель' or not founders or len(founders) % 2:
        raise ValueError('Incomplete director or founders section')
    entries = {}
    add(entries, manager[1], 'Director')
    total = Decimal(0)
    for name, share in zip(founders[::2], founders[1::2]):
        match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*%', share)
        if not match or not 0 <= Decimal(match[1]) <= 100 or normalized(name) in entries:
            raise ValueError('Invalid founder share')
        total += Decimal(match[1])
        entries[normalized(name)] = f'{name} · founder · {match[1]}%'
    notice = lines[lines.index('Внимание', lines.index('Учредители')) + 1]
    date = re.search(r'актуальна на (\d{2}\.\d{2}\.\d{4})', notice)
    if not date or total != 100:
        raise ValueError('Missing source date or incomplete ownership total')
    return f'Orginfo directory · director and founders · source dated {date[1]} · verify with register', entries


def kap(page, lines):
    # KAP embeds the filed table, date and reconciliation row in its public HTML.
    field = '"itemKey":"kpy41_acc5_ortaklik_yapisi","value":'
    for match in re.finditer(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', page):
        chunk = json.loads(match[1])
        if field not in chunk:
            continue
        data = chunk.split(field, 1)[1]
        rows, offset = json.JSONDecoder().raw_decode(data)
        date = re.match(r',"disclosureIndex":\d+,"creationDate":"(\d{2}/\d{2}/\d{4}) ', data[offset:])
        if not date or not isinstance(rows, list) or len(rows) < 2 or rows[-1].get('shareholder') != 'TOPLAM':
            break
        entries = {}
        total = Decimal(0)
        capital = 0
        for row in rows[:-1]:
            name, pct, amount = row['shareholder'], row['ratioInCapital'], row['shareInCapital']
            if (not name or normalized(name) in entries or not re.fullmatch(r'\d+(?:\.\d+)?', pct)
                    or Decimal(pct) > 100 or not amount.isdecimal()):
                raise ValueError('Incomplete KAP shareholding')
            entries[normalized(name)] = f'{name} · {pct}%'
            total += Decimal(pct)
            capital += int(amount)
        if abs(total - 100) > Decimal('0.05') or capital != int(rows[-1]['shareInCapital']):
            raise ValueError('KAP ownership totals disagree')
        return 'KAP · shareholder structure · filed ' + date[1], entries
    raise ValueError('Dated KAP shareholding table unavailable')


def tiryaki(page, lines):
    rows = section(lines, 'BOARD OF DIRECTORS', 'Executive Board')
    entries = {}
    rows = [r for r in rows if r != 'Risk and Audit Committee']
    if not rows or len(rows) % 2:
        raise ValueError('Incomplete Tiryaki board')
    for name, role in zip(rows[::2], rows[1::2]):
        if role not in ('Board Member', 'Chairman', 'CEO', 'Internal Audit Director'):
            raise ValueError('Unexpected Tiryaki board role')
        add(entries, name, role)
    if 'CEO' not in rows:
        raise ValueError('Missing executive section')
    return 'Tiryaki Agro · published board and CEO', entries


# Country, legal-name phrase and registry/tax ID bind each curated URL to one issuer.
SOURCES = [
    ('GR', 'Elvalhalcor', '094061318', 'https://athens.euronext.com/en/market-data/issuers/377',
     'ELVALHALCOR HELLENIC COPPER AND ALUMINIUM INDUSTRY', athens),
    ('EG', 'Delemar Aluminium Profiles Production', '36360', 'https://delemargroup.com/meet-the-team/',
     'Delemar Industrial Group', delemar),
    ('EG', 'Delemar Aluminium Profiles Production', '36360',
     'https://www.egydir.com/ar/9746/' + quote('دلمار-لإنتاج-قطاعات-الألمنيوم') + '/about',
     'دلمار لإنتاج قطاعات الألمنيوم', delemar_entity),
    ('TR', 'CMS Jant ve Makina', '2060074145',
     'https://www.cms.com.tr/en/corporate/group-companies/cms-jant-ve-makina-san-a-s', 'CMS Jant ve Makina Sanayii', cms),
    ('TR', 'Tiryaki Agro', 'TR8450302285', 'https://tiryaki.com.tr/en/about-us/management-team/organization-chart',
     'Tiryaki Agro Gıda San.', tiryaki),
    ('TR', 'Tiryaki Agro', 'TR8450302285', 'https://www.kap.org.tr/en/sirket-bilgileri/genel/2433-tiryaki-agro-gida-sanayi-ve-ticaret-a-s',
     'TİRYAKİ AGRO GIDA SANAYİ VE TİCARET', kap),
    ('VN', 'PetroVietnam Fertilizer and Chemicals', '0303165480', 'https://dpm.vn/en/so-do-to-chuc',
     'PetroVietnam Fertilizer and Chemicals Corporation', pvfcco),
    ('VN', 'PetroVietnam Fertilizer and Chemicals', '0303165480', 'https://dpm.vn/en/quan-he-nha-dau-tu',
     'PetroVietnam Fertilizer and Chemicals Corporation', bulletin_holdings),
    ('UZ', 'Balton Trading', '201079149', 'https://orginfo.uz/ru/organization/49cc9b43f7af/', 'Balton Trading (Asia)', balton),
]


def issuer_matches(subject, country, phrase, identifier):
    return (country_code(subject.get('country_registration', '')) == country
            and contains(normalized(subject['name']), normalized(phrase))
            and any(id_equal(subject.get(k) or '', identifier) for k in ('tax_id', 'registration_number')))


def read_page(url, identity, reader):
    try:
        raw, mime, final = fetch_bytes(url)
    except TimeoutError:
        raw, mime, final = fetch_bytes(url)
    if final != url or 'html' not in mime:
        raise ValueError('Issuer page moved or unavailable')
    page = raw.decode('utf-8')
    parser = VisibleText()
    parser.feed(page)
    if not any(contains(normalized(line), normalized(identity)) for line in parser.lines):
        raise ValueError('Published issuer identity could not be confirmed')
    try:
        return reader(page, parser.lines)
    except (IndexError, KeyError, TypeError, AttributeError, ArithmeticError, ValueError) as exc:
        raise ValueError(f'Could not validate disclosure: {exc}') from exc


def notice_source(subject):
    if issuer_matches(subject, 'VN', 'PetroVietnam Fertilizer and Chemicals', '0303165480'):
        return 'PVFCCo investor disclosures', 'https://dpm.vn/quan-he-nha-dau-tu'
    if country_code(subject.get('country_registration', '')) == 'AZ':
        tin = subject.get('tax_id') or subject.get('registration_number') or ''
        if re.fullmatch(r'[0-9]{10}', tin):
            return 'Vergilər official notices', f'https://vergiler.az/announcements/?search_text={tin}&search_type=voen'
    return None


def notice_rows(subject, publisher, page):
    if publisher == 'PVFCCo investor disclosures':
        if 'pvfcco.com.vn' not in page or 'file-item-name' not in page:
            raise ValueError('Missing PVFCCo disclosure index')
        found = False
        for url, body in re.findall(r'<a\s+href="(https://dpm\.vn/(?:cong-bo-thong-tin|bao-cao-tai-chinh)/[^"#]+)"[^>]*>(.*?)</a>', page, re.S):
            if 'file-item-name' not in body:
                continue
            title = re.search(r'class="file-item-name">([^<]+)</span>', body)
            date = re.search(r'class="file-date">(\d{2}/\d{2}/\d{4})</span>', body)
            if not title or not date:
                raise ValueError('Incomplete PVFCCo notice')
            found = True
            yield unescape(title[1]).strip(), url, datetime.strptime(date[1], '%d/%m/%Y'), 'vi'
        if not found:
            raise ValueError('No readable issuer disclosure rows')
        return
    if 'announcement-wrapper' not in page:
        if 'axtarışa uyğun elanlar tapılmadı' in page:
            return
        raise ValueError('Unrecognised Vergilər search response')
    months = 'yanvar fevral mart aprel may iyun iyul avqust sentyabr oktyabr noyabr dekabr'.split()
    tin = subject.get('tax_id') or subject['registration_number']
    for article in re.findall(r'<article\b[^>]*>(.*?)</article>', page, re.S):
        if 'announcement-wrapper' not in article:
            continue
        parser = VisibleText()
        parser.feed(article)
        lines = parser.lines
        if len(lines) < 4 or lines[1] != 'VÖEN: ' + tin:
            raise ValueError('Gazette result does not match requested tax ID')
        link = re.search(r'href="(/news_print\.php\?announcement_id=\d+)"', article)
        date = re.fullmatch(r'(\d+) (\S+) (\d{4}) (\d+):(\d+)', lines[-1])
        if not link or not date:
            raise ValueError('Incomplete gazette notice')
        day, month, year, hour, minute = date.groups()
        published = datetime(int(year), months.index(normalized(month)) + 1, int(day), int(hour), int(minute))
        yield lines[0] + ' · ' + ' '.join(lines[2:-1])[:450], 'https://vergiler.az' + link[1], published, 'az'


def recent_notices(subject, publisher, url, fetch, now=None, errors=None, *, window=None):
    from .relevance import risk_signals
    gazette = publisher == 'Vergilər official notices'
    zone = ZoneInfo('Asia/Baku' if gazette else 'Asia/Ho_Chi_Minh')
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
    start = today - timedelta(days=6)
    if window:
        start, today = (instant.astimezone(zone).date() for instant in window)
    results = []
    try:
        for number in range(1, 11):
            page = fetch(url if number == 1 else f'{url}&page={number}')
            if len(page) > 2_000_000:
                raise ValueError('Disclosure index exceeds retrieval limit')
            html = page.decode('utf-8')
            reached_older = False
            for title, dest, date, lang in notice_rows(subject, publisher, html):
                if date.date() < start:
                    reached_older = True
                elif (date.date() <= today and not excluded_story(title, dest)
                      and (not window or window[0] <= date.replace(tzinfo=zone) <= window[1])):
                    results.append(dict(title=title, url=dest, publisher=publisher, published_at=date.replace(tzinfo=zone),
                                        query=url, language=lang, source_kind='official', discovery_service=publisher,
                                        signal_terms=risk_signals(title, lang)))
            if not gazette or reached_older or 'class="next page-numbers"' not in html:
                break
        else:
            raise ValueError('More than ten recent gazette result pages; check remaining notices at source')
    except (ValueError, OSError) as exc:
        if errors is None:
            raise
        errors.append(f'{publisher}: {exc}')
    return results
