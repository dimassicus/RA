from pathlib import Path

from ra import db, preliminary, search
from ra.app import create_app
from ra.importer import read_workbook


ROOT = Path(__file__).resolve().parents[1]


def test_expanded_workbook_fields_are_preserved():
    rows = read_workbook(ROOT / "companies.xlsx")
    assert len(rows) == 9
    assert rows[0][1]["short_name"] == "Heringer"
    assert "22.266.175/0001-88" in rows[0][1]["registration_number"]
    assert "Paulinia" in rows[0][1]["legal_address"]
    assert rows[6][1]["registration_number"] == "ИП-00121 / 201079149"


def test_case_accents_typos_and_padded_identifiers():
    assert preliminary.name_score("FERTILIZANTES HERINGER SOCIEDADE ANÔNIMA",
                                  "Fertilizantes Heringer S.A.") >= 0.9
    assert preliminary.name_score("Elvalhalcor", "ELVALHALCOR") == 1
    assert preliminary.name_score("ELVALHALCOR", "ELVALHALCORR") >= 0.8
    assert preliminary.id_equal("0303401000", "000303401000")
    assert "ИП00121" in db.identifier_tokens("ИП-00121 / 201079149", "Uzbekistan")


def test_verified_registry_match_fills_only_empty_fields(monkeypatch):
    monkeypatch.setattr(db, "list_companies", lambda active=None: [])
    source = {
        "name": "CIBRAFERTIL COMPANHIA BRASILEIRA DE FERTILIZANTES SOCIEDADE ANÔNIMA FECHADA",
        "short_name": "", "country_registration": "Brazil", "registration_number": "00.117.842/0001-28",
        "legal_address": "User supplied address", "top_management": "", "key_owners": "",
    }
    record = {"cnpj": "00117842000128", "razao_social": "CIBRAFERTIL COMPANHIA BRASILEIRA DE FERTILIZANTES",
              "nome_fantasia": "CIBRA", "logradouro": "ALFA", "numero": "1428",
              "qsa": [{"nome_socio": "A Director", "qualificacao_socio": "Diretor"}]}
    proposed, findings, error = preliminary.research_company(
        source, json_fetcher=lambda url: record if "brasilapi" in url else {"data": []},
        feed_fetcher=lambda url: b"<rss><channel></channel></rss>")
    assert not error
    assert proposed["short_name"] == "CIBRA"
    assert proposed["top_management"] == "A Director (Diretor)"
    assert proposed["legal_address"] == source["legal_address"]
    assert findings["identity_evidence"][0]["identifier_value"] == "00117842000128"


def test_name_only_registry_hit_does_not_fill(monkeypatch):
    monkeypatch.setattr(db, "list_companies", lambda active=None: [])
    source = {"name": "Acme Limited", "short_name": "", "country_registration": "Greece",
              "registration_number": "12345", "legal_address": ""}
    record = {"data": [{"id": "LEI123", "attributes": {"entity": {
        "legalName": {"name": "Acme Limited"}, "jurisdiction": "GR", "registeredAs": "99999",
        "legalAddress": {"addressLines": ["Example Street"], "country": "GR"}}}}]}
    proposed, findings, _ = preliminary.research_company(
        source, json_fetcher=lambda url: record,
        feed_fetcher=lambda url: b"<rss><channel></channel></rss>")
    assert proposed["legal_address"] == ""
    assert findings["matches"][0]["verdict"] == "Identifier mismatch"
    assert not findings["identity_evidence"]


def test_add_route_stages_before_saving(monkeypatch):
    saved = []
    monkeypatch.setattr(db, "find_company", lambda name, country: None)
    monkeypatch.setattr(db, "stage_company", lambda data, source: 42)
    monkeypatch.setattr(db, "save_company", lambda *args: saved.append(args))
    monkeypatch.setattr("ra.app.intake_worker.enqueue", lambda intake_id: None)
    app = create_app()
    with app.test_client() as client:
        client.get("/") if False else None
        with client.session_transaction() as session:
            session["csrf_token"] = "test-token"
        response = client.post("/companies", data={"csrf_token": "test-token", "name": "Acme",
                                                  "country_registration": "Greece"})
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/intake/42")
        assert not saved


def test_new_country_news_locales():
    for country, edition in (("Egypt", "EG:ar"), ("Greece", "GR:el"), ("Vietnam", "VN:vi"),
                             ("Azerbaijan", "AZ:az"), ("Uzbekistan", "UZ:uz"), ("Turkey", "TR:tr")):
        url, _ = search.feed_url('"Company"', country)
        assert edition.replace(":", "%3A") in url
