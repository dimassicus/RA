"""Company-specific adverse discovery vocabulary; phrases are leads, not findings.

Families follow FATF offence categories, Wolfsberg negative-news guidance,
Basel credit monitoring and OECD responsible-business due diligence.
Existing keyword names own their expansions so settings and translations survive.
"""

FAMILIES = {
    "financial distress": {
        "bankruptcy": ("filed for bankruptcy", "bankruptcy petition", "declared bankrupt"),
        "insolvency": ("declared insolvent", "unable to pay debts"),
        "default": ("debt default", "payment default", "missed interest payment", "bond default", "loan default"),
        "covenant breach": ("breach of financial covenants", "covenant violation"),
        "distressed debt restructuring": ("distressed exchange", "creditor haircut", "emergency debt restructuring"),
        "compulsory liquidation": ("winding-up petition", "winding-up order", "receiver appointed"),
        "liquidity crisis": ("cash shortfall", "liquidity shortfall", "funding crisis"),
        "downgrade": ("credit rating downgrade", "credit downgrade", "negative rating outlook", "downgrade watch"),
        "profit warning": ("earnings warning", "slashed profit forecast", "unexpected financial loss"),
        "going-concern uncertainty": ("substantial doubt about ability to continue", "going-concern warning"),
    },
    "enforcement": {
        "asset seizure": ("assets seized", "property attachment", "asset arrest"),
        "asset freeze": ("freezing order", "bank accounts frozen", "funds frozen"),
        "asset forfeiture": ("confiscation order", "forfeiture proceedings", "proceeds confiscated"),
        "criminal investigation": ("criminal probe", "investigated for fraud", "suspected criminal activity"),
        "police raid": ("premises raided", "offices searched by police", "search warrant executed"),
        "arrest": ("arrested", "arrest warrant", "detained by police"),
        "criminal charges": ("charged with", "indicted", "criminal prosecution"),
        "conviction": ("criminal conviction", "convicted of", "found guilty", "pleaded guilty", "sentenced to prison"),
        "lawsuit": ("lawsuit alleging misconduct", "sued for fraud", "class action alleging", "damages claim against"),
        "regulatory penalty": ("fined by regulator", "civil monetary penalty", "enforcement action against"),
        "licence revocation": ("license revocation", "licence revoked", "license revoked", "operating permit suspended", "authorisation withdrawn", "authorization withdrawn"),
        "debarment": ("barred from public contracts", "procurement ban", "disqualified from tenders"),
        "sanctions designation": ("added to sanctions list", "designated under sanctions", "sanctioned entity"),
        "sanctions evasion": ("sanctions circumvention", "sanctions violation", "embargo violation"),
        "export-control violation": ("illegal export", "unlicensed export", "export-control evasion"),
    },
    "financial crime": {
        "fraud": ("fraudulent scheme", "investment scam", "ponzi scheme"),
        "accounting fraud": ("falsified accounts", "fabricated revenue", "fraudulent financial reporting"),
        "embezzlement": ("embezzled funds", "funds siphoned off"),
        "misappropriation of assets": ("misappropriated funds", "asset stripping", "diversion of company funds"),
        "corruption": ("bribery", "bribes paid", "kickback scheme", "corrupt payments"),
        "tax evasion": ("tax fraud", "fraudulent VAT refund", "tax-evasion scheme"),
        "money laundering": ("laundering criminal proceeds", "trade-based money laundering"),
        "terrorist financing": ("financing terrorism", "funding a terrorist organisation", "funding a terrorist organization"),
        "illegal insider trading": ("insider dealing", "unlawful use of inside information"),
        "market manipulation": ("price manipulation", "pump-and-dump scheme", "market rigging"),
        "bid rigging": ("tender collusion", "price-fixing conspiracy", "cartel infringement"),
    },
    "criminal activity": {
        "smuggling": ("contraband trafficking", "customs fraud", "customs-duty evasion"),
        "drug trafficking": ("drug dealing", "narcotics trafficking", "drug smuggling", "cocaine trafficking"),
        "arms trafficking": ("weapons smuggling", "illicit arms dealing", "illegal weapons trade"),
        "human trafficking": ("trafficking in persons", "sex trafficking"),
        "migrant smuggling": ("people smuggling", "migrant-smuggling network"),
        "organised crime involvement": ("organized crime involvement", "organised crime links", "organized crime links", "racketeering", "mafia ties", "criminal syndicate"),
        "extortion": ("blackmail", "protection racket", "extortion scheme"),
        "counterfeiting": ("counterfeit goods", "counterfeit currency", "counterfeit medicines"),
        "forgery": ("forged documents", "falsified certificates", "forged signatures"),
        "trafficking in stolen goods": ("handling stolen goods", "receiving stolen property", "cargo theft"),
        "violent crime": ("murder", "homicide", "aggravated assault", "kidnapping", "hostage-taking"),
        "sexual misconduct": ("sexual assault", "sexual exploitation", "sexual harassment"),
    },
    "labour and environment": {
        "forced labour": ("forced labor", "modern slavery", "debt bondage"),
        "child labour violations": ("child labor violations", "illegal child labor", "illegal child labour", "unlawful employment of minors"),
        "wage theft": ("unpaid wages", "withheld salaries", "illegal wage deductions"),
        "environmental crime": ("illegal dumping", "waste trafficking", "illegal logging", "illegal mining", "wildlife trafficking"),
        "pollution incident": ("toxic spill", "illegal discharge", "groundwater contamination"),
    },
    "operational misconduct and harm": {
        "fatal workplace accident": ("worker killed", "workplace fatality", "fatal safety breach"),
        "plant fire": ("industrial fire", "industrial explosion", "factory fire", "plant explosion", "refinery explosion"),
        "forced operational shutdown": ("plant ordered closed", "production halted after accident", "compulsory closure"),
        "unsafe product recall": ("safety recall", "contaminated product", "defective product causing injury"),
        "cyberattack": ("cyber attack", "ransomware attack", "systems compromised", "cyber extortion"),
        "data breach": ("customer data stolen", "personal data leaked", "unauthorised data disclosure", "unauthorized data disclosure"),
        "executive removal for misconduct": ("director disqualified", "CEO fired for misconduct", "executive suspended over allegations"),
    },
}

RELATED_PHRASES = {term: phrases for families in FAMILIES.values() for term, phrases in families.items()}

# Titles are neutral identity context and must always be anchored to a company.
PERSON_TITLES = {
    "chief executive officer": ("CEO", "chief executive", "managing director", "general director"),
    "chief financial officer": ("CFO", "finance director"),
    "chief operating officer": ("COO",),
    "board of directors": ("chairman", "chairwoman", "chairperson", "board chair", "board director"),
    "top management": ("senior executive", "executive director"),
    "major shareholder": ("main shareholder", "largest shareholder", "controlling shareholder", "majority shareholder"),
    "beneficial owner": ("ultimate beneficial owner", "UBO", "owner", "co-owner"),
    "founder": ("co-founder",),
}
