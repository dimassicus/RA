"""Initialize the pilot from its workbook and independently sourced CNPJ."""

from . import db
from .importer import import_workbook


CVM_IDENTITY_SOURCE = (
    "https://www.rad.cvm.gov.br/ENETCONSULTA/frmDownloadDocumento.aspx?"
    "CodigoInstituicao=1&Tela=99&descTipo=IPE&numProtocolo=1566373&"
    "numSequencia=1091079&numVersao=1"
)


def bootstrap():
    db.init_schema()
    added, skipped = import_workbook("companies.xlsx")
    company = db.find_company("Fertilizantes Heringer", "Brazil")
    if company:
        with db.connect() as conn:
            if not company["registration_number"]:
                conn.execute(
                    "UPDATE companies SET registration_number = %s, updated_at = now() "
                    "WHERE id = %s", ("22.266.175/0001-88", company["id"]),
                )
            if company["registration_number"] in ("", "22.266.175/0001-88"):
                conn.execute(
                    "INSERT INTO company_identity_evidence "
                    "(company_id, identifier_type, identifier_value, source_name, source_url) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (company["id"], "CNPJ", "22.266.175/0001-88", "CVM material fact", CVM_IDENTITY_SOURCE),
                )
    return added, skipped


if __name__ == "__main__":
    print("Workbook added/already present:", bootstrap())
