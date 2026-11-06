"""Exclude sports and arts coverage at discovery, before processing leads."""
import re
from urllib.parse import unquote, urlsplit

from .aliases import normalized


# Short query exclusions leave room for the issuer, local market and risk terms.
QUERY_TERMS = {
    "en": "sports football cinema festival", "pt": "esportes futebol cinema festival",
    "el": "αθλητισμός ποδόσφαιρο κινηματογράφος", "ar": "الرياضة كرة_القدم سينما مهرجان",
    "az": "idman futbol kino festival", "uz": "sport futbol kino festival",
    "ru": "спорт футбол кино фестиваль", "tr": "spor futbol sinema festival",
    "vi": "bóng_đá điện_ảnh lễ_hội", "es": "deportes fútbol cine festival",
    "fr": "sports football cinéma festival", "id": "olahraga sepak_bola festival",
    "th": "กีฬา ฟุตบอล ภาพยนตร์",
}

# Categories and URL sections can identify a story even when its headline names only people.
SECTIONS = {
    normalized(term) for term in (
        "sport sports esport esports arts art culture cultural entertainment music film theatre cinema showbiz "
        "esportes esporte desporto desportos cultura entretenimento esportes deportes espectáculos "
        "spor sanat kültür idman mədəniyyət madaniyat sanʼat спорт культура искусство "
        "αθλητικά αθλητισμός πολιτισμός τεχνες الرياضة رياضة ثقافة الثقافة فنون الفنون "
    "budaya olahraga divertissement loisirs văn-hóa thể-thao giải-trí kultur sanat kultur-sanat arts-culture"
    ).split()
}
TOPIC_PUBLISHERS = {
    "espn", "espn brasil", "espn deportes", "sky sports", "bbc sport", "globo esporte",
    "eurosport", "bein sports", "sporx", "sport24", "football az", "bongdaplus",
    "the art newspaper", "billboard", "pitchfork", "hollywood reporter", "the hollywood reporter",
}

# Search all languages: the feed edition is often different from the headline language.
CUES = """
sport|sports|esports|athletics|football|soccer|basketball|volleyball|baseball|rugby|cricket match|tennis|grand prix|world cup|champions league|
national team|premier league|hat trick|olympics|olympic games|wins match|wins the match|touchdown|boxing|formula 1|
concert|concerts|orchestra|symphony|film premiere|film industry|film director|movie|box office|music|festival|festivals|art gallery|art exhibition|
performing arts|museum|ballet|theatre|theater|opera house|culture|cultural|arts and culture|
film|cinematography|filmmaker|film awards|grammys|oscars|art exhibition|art show|literary prize|poetry|
olympic|marathon race|nba|uefa|fifa|semifinal|wimbledon|goalkeeper|matchday|scored a hat trick|
arts council|cultural centre|cultural center|cultural events|cultural exchange|pop culture|art fair|fine art|new album|
desporto|esportes|esporte|património cultural|festa cultural|ministerio da cultura|centro cultural|
futebol|seleção brasileira|copa do mundo|campeonato brasileiro|libertadores|basquete|voleibol|olimpíadas|
cinema|filme|filmes|teatro|museu|música|exposição de arte|carnaval|patrimônio cultural|
fútbol|baloncesto|película|concierto|museo|arte y cultura|torneo de tenis|deportes|
futbol|futbolcu|süper lig|basketbol|voleybol|şampiyonlar ligi|sinema|konser|tiyatro|sanat sergisi|kültür festivali|
idman|çempionat|milli komanda|incəsənət|mədəniyyət|teatr|muzey|konsert|chempionat|terma jamoa|madaniyat|
sanat|kültür|müze|müzik|futbolda|futbol üzrə|kino|kinoteatr|spor|futbolchilar|sanʼat|
чемпионлар лигаси|футбол бўйича|терма жамоа|маданият|концерт дастури|sanat festivali|kültür sanat|
ποδόσφαιρο|ποδοσφαίρου|μπάσκετ|πρωτάθλημα|ολυμπιακοί αγώνες|θέατρο|κινηματογράφος|συναυλία|μουσείο|φεστιβάλ|πολιτισμός|πολιτισμού|
كرة القدم|لكرة القدم|الدوري الممتاز|كأس العالم|كرة السلة|الألعاب الأولمبية|مهرجان|مهرجانات|سينما|السينما|حفل موسيقي|مسرح|
bóng đá|bóng chuyền|bóng rổ|đội tuyển|thể thao|điện ảnh|liên hoan phim|âm nhạc|lễ hội|nghệ thuật|bảo tàng|văn hóa|
футбол|футболу|футбола|чемпионат|чемпионата|баскетбол|спортсмен|кино|театр|концерт|фестиваль|музей|
coupe du monde|championnat|concerts|musée|théâtre|sepak bola|olahraga|pertandingan sepak bola|musik|budaya
"""
_terms = {normalized(term) for term in CUES.replace("\n", "").split("|") if term}
TOPIC = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, sorted(_terms))) + r")(?!\w)")
THAI = re.compile("กีฬา|ฟุตบอล|ภาพยนตร์|ดนตรี|วัฒนธรรม|เทศกาล")
# These phrases use an overlapping word in its business, technical or legal sense.
BUSINESS_USAGE = re.compile(
    r"\b(?:(?:risk|safety|corporate|company|workplace|organizational|compliance|credit|cell|tissue|bacterial) culture"
    r"|culture (?:medium|media)|(?:thin|polymer|plastic|packaging|protective|polyethylene) film"
    r"|(?:acting|act|acted) in concert|(?:cultura (?:de seguranca|corporativa|organizacional))"
    r"|van hoa doanh nghiep)\b"
)


def excluded_story(title, url, *, publisher="", categories=(), summary=""):
    if normalized(publisher) in TOPIC_PUBLISHERS:
        return True
    if isinstance(categories, str):
        categories = [categories]
    try:
        path = unquote(urlsplit(url).path)
    except ValueError:
        return True
    sections = [*categories, *path.split("/")[:-1]]
    if any(normalized(label) in SECTIONS for label in sections if isinstance(label, str)):
        return True
    text = title.removesuffix(" - " + publisher) + " " + summary
    return bool(TOPIC.search(BUSINESS_USAGE.sub(" ", normalized(text))) or THAI.search(text))


def excluded_query_terms(language):
    words = QUERY_TERMS.get(language.split("-")[0], "")
    return tuple(dict.fromkeys(term.replace("_", " ") for term in (QUERY_TERMS["en"] + " " + words).split()))
