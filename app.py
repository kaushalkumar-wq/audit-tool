import csv
from pathlib import Path

from flask import Flask, jsonify, render_template


app = Flask(__name__)
DATA_PATH = Path(__file__).parent / "data" / "audit_data.csv"


def load_audit_rows():
    if not DATA_PATH.exists():
        return []

    with DATA_PATH.open(newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))

    for row in rows:
        row["In Hand Quantity"] = parse_number(row.get("In Hand Quantity"))
        row["Bad Inventory Count"] = parse_number(row.get("Bad Inventory Count"))

    return rows


def parse_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0

    return int(number) if number.is_integer() else number


@app.route("/")
def dashboard():
    rows = load_audit_rows()
    clinics = sorted({row.get("Mapped Clinic Name", "") for row in rows if row.get("Mapped Clinic Name")})
    priorities = ["Very High", "High", "Normal"]

    return render_template(
        "dashboard.html",
        rows=rows,
        clinics=clinics,
        priorities=priorities,
        total_rows=len(rows),
    )


@app.route("/api/audit-data")
def audit_data():
    return jsonify({"rows": load_audit_rows()})


if __name__ == "__main__":
    app.run()
