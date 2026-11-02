"""Initialize the schema and stage workbook rows for analyst review."""

from . import db
from .importer import stage_workbook


def bootstrap():
    db.init_schema()
    return stage_workbook("companies.xlsx")


if __name__ == "__main__":
    print("Workbook staged/already staged:", bootstrap())
