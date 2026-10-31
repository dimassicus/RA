# Brazil source pack

Checked 31 October 2026. **API** here means a provider-published interface or a public machine-readable dataset. “Web” means browser access only. Prices are in the provider's currency and can change.

## Official company and financial records

| Source | Useful information | Access and entry cost | Pilot note |
| --- | --- | --- | --- |
| [Receita Federal / REDESIM CNPJ consultation](https://www.gov.br/pt-br/servicos/consultar-cadastro-nacional-de-pessoas-juridicas) | Registration status, legal identity, shareholder/administrator register (QSA) | **Free web** for citizens; GOV.BR login may be required. No free general-purpose API established from this page. | Verify a CNPJ manually; do not confuse with paid Serpro API. |
| [Serpro Consulta CNPJ API](https://www.gov.br/conecta/catalogo/apis/consulta-cnpj/swagger_api_cnpj.md/swagger_view) | Automated CNPJ and QSA queries | **Paid API**; volume-based commercial price, no reliable public entry price found. | Commercial access/credentials required; [Serpro service description](https://www.transparencia.serpro.gov.br/acesso-a-informacao/acoes-e-programas/carta-de-servicos/imagens-e-arquivos/despublicados/cartadeservicos.pdf/%40%40download/file/cartadeservicos.pdf). |
| [CVM listed-company register](https://dados.cvm.gov.br/dataset/cia_aberta-cad) | CNPJ, legal name, CVM code, registration status | **Free CSV download**, updated daily. | Published [CSV directory](https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS/); direct fetch from this host returned 403, so adapter pending. |
| [CVM annual statements (DFP)](https://dados.cvm.gov.br/dataset/cia_aberta-doc-dfp) | Balance sheet, income, cash flow, capital | **Free ZIP/CSV download**, dataset updated weekly. | Best primary structured financial source for listed firms. |
| [CVM quarterly statements (ITR)](https://dados.cvm.gov.br/dataset/cia_aberta-doc-itr) | Interim financials | **Free ZIP/CSV download**, dataset updated weekly. | Separate from annual DFP. |
| [CVM reference form (FRE)](https://dados.cvm.gov.br/dataset/cia_aberta-doc-fre) | Activities, risks, debt, management, ownership-related disclosures | **Free ZIP/CSV download**, dataset updated weekly. | High-value source for entity profile and creditor review. |
| [CVM filing portal](https://sistemas.cvm.gov.br/cias-abertas.asp) | Individual notices and filings | **Free web**; no general API verified for this portal. | Use underlying document links as evidence. |
| [B3 listed-company page](https://sistemaswebb3-listados.b3.com.br/listedCompaniesPage/main/20621/FHER/overview?language=pt-br) | Issuer identity, ticker, filings | **Free web**. Bulk/market-data products require a separate [B3 licence](https://www.b3.com.br/en_us/market-data-and-indices/data-services/up2data/faq/contracting-and-prices.htm); price depends on product and use. | Do not treat a public page as a licensed market-data feed. |
| [Heringer investor relations — notices](https://ri.heringer.com.br/informacoes-financeiras/comunicados-fatos-relevantes/) | Company material facts and market notices | **Free web and public website JSON endpoint**; no published API price or SLA. | **Integrated in pilot**. The [site-used JSON endpoint](https://apicatalog.mziq.com/filemanager/company/7e25b347-2f69-452f-b9e6-19237e0e6ea5/filter/categories/year/meta) returned 2025–2026 notice metadata, including document URLs. |

## Legal, regulatory, sector, and macro sources

| Source | Useful information | Access and entry cost | Pilot note |
| --- | --- | --- | --- |
| [Diário Oficial da União](https://www.in.gov.br/) | Federal official notices | **Free web search**; the [Imprensa Nacional](https://www.gov.br/imprensanacional/pt-br/acesso-a-informacao/dados-abertos) says pages are freely searchable. | The government API found is for **publishing** to the DOU, not a verified reading/search API. |
| [CNJ DataJud public API](https://www.cnj.jus.br/sistemas/datajud/api-publica/) | Public case metadata and movements across Brazilian courts | **Free public API** subject to its terms and published access method. | Case metadata is not a substitute for court documents or legal review. |
| [TJSP e-SAJ](https://esaj.tjsp.jus.br/esaj?servico=190090) | São Paulo case status and filings | **Free web** for non-sealed cases; no public API verified. | Relevant court depends on the entity and matter. |
| [CADE open data](https://www.gov.br/cade/pt-br/acesso-a-informacao/dados-abertos) | Competition matters and merger reviews | **Free web/open datasets**; a stable case-search API is not yet verified. | Use for competition/regulatory signals. |
| [MAPA fertilizer registrations](https://www.gov.br/agricultura/pt-br/assuntos/registro-de-produtos-e-estabelecimentos/fertilizantes-inoculantes-e-corretivos) | Fertilizer product and establishment registration | **Free public dashboard/web**; no API verified. | Sector-specific for Heringer. |
| [IBAMA enforcement open data](https://dadosabertos.ibama.gov.br/dataset/fiscalizacao-auto-de-infracao) | Environmental fines and infractions | **Free CSV/JSON downloads**; [public consultation](https://www.gov.br/pt-br/servicos/consultar-autuacoes-ambientais-e-embargos) is also free, with possible GOV.BR login. | Match by CNPJ and track final status; an infraction record is not necessarily a final penalty. |
| [Banco Central SGS](https://dadosabertos.bcb.gov.br/dataset/22797-sgs) | Rates and macro/sector time series | **Free JSON/CSV service**. | Adds country context rather than company-specific events. |
| [IBGE SIDRA](https://www.ibge.gov.br/np_download/novoportal/documentos_institucionais/Plano_de_Dados_Abertos_IBGE_2024_2025.pdf) | Brazilian economic and industry statistics | **Free public API**; [IBGE API entry](https://apisidra.ibge.gov.br/). | API exists per IBGE; endpoint was not tested from this host. |

## Local business news and private credit data

| Source | Useful information | Access and entry cost | Pilot note |
| --- | --- | --- | --- |
| [Agência Brasil](https://agenciabrasil.ebc.com.br/feed/) | Public-service national and economy news | **Free website and RSS**. | Useful low-cost monitoring input; check article reuse terms. |
| [Brazil Journal](https://braziljournal.com/) | Business and market reporting | **Public web**; no official API or machine-feed terms verified. | Read original article before reporting. |
| [AgFeed](https://agfeed.com.br/) | Agriculture and fertilizer sector reporting | **Public web observed**; API and subscription terms not verified. | A Heringer management headline appeared in the pilot feed. |
| [Globo Rural](https://globorural.globo.com/) | Agriculture and company reporting | **Web, some access may be subscription-controlled**; no API price verified. | A Heringer/EuroChem litigation headline appeared in the pilot feed. |
| [Valor Econômico](https://assine.vendas.valor.globo.com/) | Brazilian business and finance news | **Paid reader web**: advertised Valor Digital at **R$29.90/month for 12 months** on an annual promotion, then **R$69.90/month** or prevailing standard price. API/redistribution not included. | Offer is promotional; check checkout before buying. |
| [Serasa Experian](https://empresas.serasaexperian.com.br/consulta-serasa) | Credit reports, debts, score, shareholders | **Paid web reports**: “Consulta Sob Medida” **R$29.99 per query**; “Essencial” **R$130/month**. [API products](https://developer.serasaexperian.com.br/api) exist, but API entry pricing requires a quote. | Do not infer API rights from a web report plan. |

**Suggested pilot order:** Heringer RI and CVM material filings first; CVM financial/register datasets next; then DataJud and IBAMA; then locally relevant news. A source failing access should be logged explicitly rather than silently omitted.
