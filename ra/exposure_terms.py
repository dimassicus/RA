"""Native search terms for the watchlist's country, region and sector exposures."""

TRADE = "import costs and access to customers"
FUNDING = "financing costs and access to credit"
CURRENCY = "foreign-currency payments and purchasing power"
OPERATIONS = "continuity of operations"
SUPPLY = "input costs and production"
PRICES = "selling prices and margins"

# Keys match canonical workbook topics. Alternatives include common headline inflections.
NAMES = {
    "pt": {"brazil": "Brasil", "portugal": "Portugal", "latin america": "América Latina",
           "fertilizer": "fertilizantes|fertilizante|adubos", "aluminium": "alumínio",
           "copper": "cobre", "glass": "vidro", "agriculture": "agricultura"},
    "es": {"spain": "España", "mexico": "México", "latin america": "América Latina|Latinoamérica",
           "fertilizer": "fertilizantes|fertilizante|abonos", "agriculture": "agricultura"},
    "ar": {"egypt": "مصر|المصرية", "aluminium": "الألومنيوم|الألمنيوم|الألمنيوم المصري",
           "glass": "الزجاج|زجاج"},
    "el": {"greece": "Ελλάδα|Ελλάδας|ελληνική", "aluminium": "αλουμίνιο|αλουμινίου",
           "copper": "χαλκός|χαλκού|χαλκό"},
    "vi": {"vietnam": "Việt Nam", "fertilizer": "phân bón"},
    "az": {"azerbaijan": "Azərbaycan|Azərbaycanda|Azərbaycanın", "pipe production": "boru istehsalı|polad borular"},
    "uz": {"uzbekistan": "O‘zbekiston|O‘zbekistonda|Ўзбекистон|Ўзбекистонда",
           "wholesale trading": "ulgurji savdo|улгуржи савдо"},
    "ru": {"azerbaijan": "Азербайджан|Азербайджане|Азербайджана",
           "uzbekistan": "Узбекистан|Узбекистане|Узбекистана",
           "pipe production": "производство труб|стальные трубы|стальных труб",
           "wholesale trading": "оптовая торговля|оптовой торговли|оптовом рынке"},
    "tr": {"turkey": "Türkiye|Türkiye'de", "car accessories": "oto aksesuar|oto aksesuarları|otomotiv yan sanayi",
           "agriculture": "tarım|tarımda|tarımsal", "food processing": "gıda işleme|gıda sanayi|gıda sektörü"},
    "fr": {"france": "France", "fertilizer": "engrais", "aluminium": "aluminium", "copper": "cuivre"},
    "id": {"indonesia": "Indonesia", "fertilizer": "pupuk", "agriculture": "pertanian"},
    "th": {"thailand": "ไทย|ประเทศไทย", "fertilizer": "ปุ๋ย", "agriculture": "เกษตร"},
}

CHANNEL_TERMS = {
    "pt": {TRADE: "tarifa|tarifas|sanções", FUNDING: "juros|Selic", CURRENCY: "inflação|desvalorização",
           OPERATIONS: "apagão|greve geral", SUPPLY: "escassez|paralisação", PRICES: "preço|preços|demanda"},
    "es": {TRADE: "arancel|aranceles|sanciones", FUNDING: "tasas de interés|tipos de interés", CURRENCY: "inflación|devaluación",
           OPERATIONS: "apagón|huelga general", SUPPLY: "escasez|paralización", PRICES: "precio|precios|demanda"},
    "ar": {TRADE: "رسوم جمركية|الرسوم الجمركية|عقوبات", FUNDING: "الفائدة|أسعار الفائدة", CURRENCY: "التضخم|خفض العملة",
           OPERATIONS: "انقطاع الكهرباء|إضراب عام", SUPPLY: "نقص|توقف الإنتاج", PRICES: "أسعار|سعر|الطلب"},
    "el": {TRADE: "δασμοί|δασμούς|κυρώσεις", FUNDING: "επιτόκια|επιτοκίων", CURRENCY: "πληθωρισμός|πληθωρισμού",
           OPERATIONS: "γενική απεργία|διακοπές ρεύματος", SUPPLY: "ελλείψεις|έλλειψη", PRICES: "τιμές|τιμή|ζήτηση"},
    "vi": {TRADE: "thuế quan|thuế nhập khẩu", FUNDING: "lãi suất", CURRENCY: "lạm phát|mất giá",
           OPERATIONS: "cắt điện|mất điện", SUPPLY: "thiếu hụt|ngừng sản xuất", PRICES: "giá|nhu cầu"},
    "az": {TRADE: "gömrük rüsumları|sanksiyalar", FUNDING: "faiz dərəcəsi", CURRENCY: "inflyasiya|devalvasiya",
           OPERATIONS: "elektrik kəsintisi", SUPPLY: "qıtlıq|istehsalın dayanması", PRICES: "qiymət|qiymətlər|tələb"},
    "uz": {TRADE: "bojxona boji|божхона божи", FUNDING: "foiz stavkasi|фоиз ставкаси", CURRENCY: "inflyatsiya|инфляция",
           OPERATIONS: "elektr uzilishi|электр узилиши", SUPPLY: "taqchillik|тақчиллик", PRICES: "narx|narxlar|talab|нархлар|талаб"},
    "ru": {TRADE: "пошлины|пошлин|санкции", FUNDING: "ключевая ставка|ключевой ставки", CURRENCY: "инфляция|инфляции|девальвация",
           OPERATIONS: "отключения электроэнергии|забастовка", SUPPLY: "дефицит|остановка производства", PRICES: "цены|цен|спрос"},
    "tr": {TRADE: "gümrük vergisi|yaptırımlar", FUNDING: "faiz|faiz oranı", CURRENCY: "enflasyon|devalüasyon",
           OPERATIONS: "elektrik kesintisi|genel grev", SUPPLY: "kıtlık|üretim durdu", PRICES: "fiyat|fiyatları|fiyatlar|talep"},
    "fr": {TRADE: "droits de douane|sanctions", FUNDING: "taux d’intérêt", CURRENCY: "inflation|dévaluation",
           OPERATIONS: "grève générale", SUPPLY: "pénurie|pénuries", PRICES: "prix|demande"},
    "id": {TRADE: "tarif impor|sanksi", FUNDING: "suku bunga", CURRENCY: "inflasi|devaluasi",
           OPERATIONS: "pemadaman listrik", SUPPLY: "kelangkaan", PRICES: "harga|permintaan"},
    "th": {TRADE: "ภาษีนำเข้า", FUNDING: "ดอกเบี้ย", CURRENCY: "เงินเฟ้อ", OPERATIONS: "ไฟฟ้าดับ",
           SUPPLY: "ขาดแคลน", PRICES: "ราคา|ความต้องการ"},
}

MARKET_ALIASES = {
    "brazil": "Brazilian|brasileiro|brasileira|brasileiros|brasileiras|brasileño|brasileña",
    "vietnam": "Vietnamese", "greece": "Greek|ελληνικό|ελληνικός", "egypt": "Egyptian|المصري",
    "turkey": "Turkish|Türk", "azerbaijan": "Azerbaijani", "uzbekistan": "Uzbek",
    "latin america": "Latin American|LatAm|latinoamericano|latinoamericana",
    "united kingdom": "UK|British|United Kingdom", "united states": "USA|United States",
    "india": "Indian|भारत", "bangladesh": "Bangladeshi|বাংলাদেশ",
}
# A recorded regional presence can match an explicitly named market within the region.
REGION_MARKETS = {"latin america": (
    "Argentina", "Bolivia", "Brazil", "Brasil", "Chile", "Colombia", "Costa Rica", "Cuba",
    "Dominican Republic", "Ecuador", "El Salvador", "Guatemala", "Haiti", "Honduras", "Mexico",
    "Nicaragua", "Panama", "Paraguay", "Peru", "Uruguay", "Venezuela",
)}
