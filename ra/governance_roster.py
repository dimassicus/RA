"""Compare issuer-matched governance observations, separate from article scores."""
import re
from functools import partial
from decimal import Decimal
from html import unescape

from . import db, governance_sources, preliminary, search
from .article_content import fetch_bytes
from .aliases import normalized


def registry_url(subject):
    if preliminary.country_code(subject.get('country_registration', '')) != 'BR':
        return ''
    ids = preliminary.brazil_cnpjs(' '.join([subject.get('tax_id') or '', subject.get('registration_number') or '']))
    return 'https://brasilapi.com.br/api/cnpj/v1/' + ids[0] if len(ids) == 1 else ''


def read_members(subject, url, fetcher):
    record = fetcher(url)
    if not isinstance(record, dict):
        raise ValueError('Invalid registry response')
    if (not preliminary.id_equal(record.get('cnpj') or '', url.rsplit('/', 1)[1])
            or preliminary.name_score(subject['name'], record.get('razao_social') or '') < 0.68):
        raise ValueError('Registry identity could not be confirmed')
    entries = record.get('qsa')
    if not isinstance(entries, list) or not entries:
        raise ValueError('No comparable registry roster supplied')
    members = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Incomplete registry roster')
        name, role = entry.get('nome_socio'), entry.get('qualificacao_socio')
        if not all(isinstance(v, str) and v.strip() for v in (name, role)):
            raise ValueError('Incomplete registry roster')
        members[normalized(name) + '|' + normalized(role)] = name.strip() + ' · ' + role.strip()
    return members


HERINGER_OWNERS = "https://ri.heringer.com.br/en/corporate-governance/ownership-breakdown/"


def ownership_table(url):
    raw, mime, final = fetch_bytes(url)
    if final != url or 'html' not in mime:
        raise ValueError('Issuer ownership page unavailable')
    page = raw.decode('utf-8')
    for table in re.finditer(r"<table\b[^>]*>(.*?)</table>", page, re.I | re.S):
        rows = []
        for tr in re.findall(r'<tr\b[^>]*>(.*?)</tr>', table[1], re.I | re.S):
            cells = re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', tr, re.I | re.S)
            rows.append([' '.join(unescape(re.sub('<[^>]+>', '', cell)).split()) for cell in cells])
        if not rows or rows[0] != ['Shareholders', 'Common Shares', 'Total (In %)']:
            continue
        date = re.search(r'Updated on ([^<]{1,50})<', page[table.end():table.end()+300])
        if not date or len(rows) < 3 or rows[-1][0:1] != ['Total']:
            break
        holdings = {}
        count = 0
        total_pct = Decimal(0)
        for row in rows[1:]:
            if len(row) != 3:
                raise ValueError('Incomplete issuer ownership table')
            name, shares, fraction = row
            if (not name or not re.fullmatch(r'\d{1,3}(?:\.\d{3})*|\d+', shares)
                    or not re.fullmatch(r'\d{1,3}(?:,\d+)?%', fraction)):
                raise ValueError('Unrecognised issuer holdings format')
            number = int(shares.replace('.', ''))
            pct = Decimal(fraction[:-1].replace(',', '.'))
            if name == 'Total':
                if number != count or pct != 100 or abs(total_pct - 100) > Decimal('0.1'):
                    raise ValueError('Issuer ownership totals disagree')
            else:
                key = normalized(name)
                if key in holdings or pct > 100:
                    raise ValueError('Ambiguous issuer ownership row')
                count += number
                total_pct += pct
                holdings[key] = f'{name} · {pct.normalize():f}% · {number:,} common shares'
        return 'Issuer ownership table · updated ' + date[1].rstrip('.'), holdings
    raise ValueError('Comparable dated issuer ownership table unavailable')


def record(run_id, subject_key, subject, url, read):
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM governance_rosters WHERE run_id=%s AND subject_key=%s AND source_url=%s AND coverage_note=''",
                        (run_id, subject_key, url)).fetchone():
            return
    try:
        label, members, *evidence = read(url)
    except (OSError, ValueError) as exc:
        save_note(run_id, subject_key, subject, url, 'Source check failed', str(exc))
        raise
    with db.connect() as conn:
        conn.execute('INSERT INTO governance_rosters (run_id,subject_key,company_name,source_url,source_label,members,evidence_url) '
                     "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (run_id,subject_key,source_url) DO UPDATE "
                     "SET evidence_url=EXCLUDED.evidence_url,members=EXCLUDED.members,source_label=EXCLUDED.source_label,coverage_note='',"
                     "observed_at=EXCLUDED.observed_at WHERE governance_rosters.coverage_note<>''",
                     (run_id, subject_key, subject['name'], url, label, db.Jsonb(members), evidence[0] if evidence else ''))


def save_note(run_id, key, subject, url, label, note):
    with db.connect() as conn:
        conn.execute("INSERT INTO governance_rosters (run_id,subject_key,company_name,source_url,source_label,members,coverage_note) "
                     "VALUES (%s,%s,%s,%s,%s,'{}',%s) ON CONFLICT (run_id,subject_key,source_url) DO UPDATE "
                     "SET coverage_note=EXCLUDED.coverage_note,observed_at=EXCLUDED.observed_at "
                     "WHERE governance_rosters.coverage_note<>''", (run_id, key, subject['name'], url, label, note))


def check(run_id, subject_key, subject, fetcher=preliminary.fetch_json):
    url = registry_url(subject)
    sources = []
    if url:
        sources.append((url, lambda u: ('Brazilian registry roster', read_members(subject, u, fetcher))))
    if search.is_heringer(subject):
        sources.append((HERINGER_OWNERS, ownership_table))
    readers = []
    for country, phrase, identifier, page, identity, reader in governance_sources.SOURCES:
        if governance_sources.issuer_matches(subject, country, phrase, identifier):
            sources.append((page, partial(governance_sources.read_page, identity=identity, reader=reader)))
            readers.append(reader)
    code = preliminary.country_code(subject.get('country_registration', ''))
    registry = preliminary.MANUAL_REGISTRIES.get(code)
    gap = ''
    if governance_sources.delemar in readers:
        gap = 'Confirm the operating entity’s directors and owners with an ITDA extract. Group officers and the directory entry have separate scopes.'
    elif governance_sources.balton in readers:
        gap = 'The directory is secondary evidence. Confirm current directors and ownership using an official extract.'
    elif governance_sources.cms in readers:
        registry = ('CMS Jant ve Makina · MKK issuer record', 'https://e-sirket.mkk.com.tr/esir/Dashboard.jsp#/sirketbilgileri/10518')
        gap = 'Obtain shareholder disclosures to confirm major stakes and percentages.'
    elif governance_sources.issuer_matches(subject, 'BR', 'Cibrafertil', '00117842000128'):
        registry = ('Cibrafértil · issuer financial statements', 'https://www.cibra.com/informacoes-aos-debenturistas/')
        gap = 'Check the ownership note in the latest signed financial statements. The CNPJ roster does not establish ownership percentages.'
    elif url and not search.is_heringer(subject):
        gap = 'Obtain shareholder disclosures to confirm major stakes and percentages.'
    elif not sources:
        gap = 'Obtain a company-specific register extract for current directors and major owners.'
    if gap and registry:
        label, link = registry
        lookup = subject.get('tax_id') or subject.get('registration_number') or subject['name']
        save_note(run_id, subject_key, subject, link, label + ' · verification needed', f'{gap} Lookup: {lookup}')
    errors = []
    for url, read in sources:
        try:
            record(run_id, subject_key, subject, url, read)
        except (OSError, ValueError) as exc:
            errors.append(f"{url}: {exc}")
    if errors:
        raise ValueError('; '.join(errors))


def observations(run_id, subject_key=None):
    with db.connect() as conn:
        rows = conn.execute("""
            SELECT cur.*, prev.members AS previous, prev.observed_at AS previous_at
            FROM governance_rosters cur LEFT JOIN LATERAL (
                SELECT members,observed_at FROM governance_rosters
                WHERE source_url=cur.source_url AND subject_key=cur.subject_key AND run_id<cur.run_id AND observed_at<cur.observed_at
                  AND coverage_note='' AND cur.coverage_note=''
                ORDER BY observed_at DESC LIMIT 1
            ) prev ON true WHERE cur.run_id=%s AND (%s::text IS NULL OR cur.subject_key=%s)
            ORDER BY cur.company_name,(cur.coverage_note<>''),cur.source_url
        """, (run_id, subject_key, subject_key)).fetchall()
    for row in rows:
        row['source_url'] = row['evidence_url'] or row['source_url']
        new, old = row['members'], row['previous']
        if row['coverage_note']:
            row['notes'] = [row['coverage_note']]
        elif old is None:
            row['notes'] = ['First observation · ' + entry for entry in sorted(new.values())]
        else:
            edits = [f"{old[k]} → {new[k]}" for k in sorted(new.keys() & old.keys())
                     if normalized(old[k]) != normalized(new[k])]
            row['notes'] = (edits + ['Newly listed · ' + new[k] for k in sorted(new.keys() - old.keys())]
                            + ['Previously listed · ' + old[k] for k in sorted(old.keys() - new.keys())]
                            or ['Same disclosed entries as the previous observation.'])
    return rows
