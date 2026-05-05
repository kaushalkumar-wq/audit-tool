import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from pymongo import MongoClient


app = Flask(__name__)
DATA_PATH = Path(__file__).parent / "data" / "audit_data.csv"
MONGO_URI_ENV = "TRANSFER_ORDERS_MONGO_URI"
MAY_START = datetime(2026, 5, 1, tzinfo=timezone.utc)
MAY_END = datetime(2026, 6, 1, tzinfo=timezone.utc)
SHEET_ID = "1TVXKiZrqH42ogu5dm1IQF8I0xplAmvaUrvdHLZWVP_Y"
SHEET_TAB = "Audit"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?gid=0#gid=0"
SHARE_EMAILS = ["kaushal.kumar@vetic.in", "sami.uddin@vetic.in"]
GOOGLE_SERVICE_ACCOUNT_ENV = "GOOGLE_SERVICE_ACCOUNT_JSON"


def get_mongo_client():
    mongo_uri = (
        os.environ.get(MONGO_URI_ENV)
        or os.environ.get("MONGO_URI")
        or os.environ.get("MONGODB_URI")
    )
    if not mongo_uri:
        raise RuntimeError(f"{MONGO_URI_ENV}, MONGO_URI, or MONGODB_URI is not configured")
    return MongoClient(mongo_uri, serverSelectionTimeoutMS=8000, connectTimeoutMS=8000)


def load_audit_rows():
    if not DATA_PATH.exists():
        return []

    with DATA_PATH.open(newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))

    for row in rows:
        row["In Hand Quantity"] = parse_number(row.get("In Hand Quantity"))
        row["Bad Inventory Count"] = parse_number(row.get("Bad Inventory Count"))

    return rows


def load_live_audit_rows():
    script = [
        {
            "$match": {
                "inventory_movement_at": {
                    "$gte": MAY_START,
                    "$lt": MAY_END,
                },
                "reason_of_inventory_movement": "Not found in Q COM order",
            }
        }
    ]

    with get_mongo_client() as client:
        vms_db = client["vetic_vms"]
        qc_db = client["quick_commerce"]

        bad_counts = list(
            vms_db.bad_inventory_data.aggregate(
                script
                + [
                    {
                        "$group": {
                            "_id": "$vetic_variant_id",
                            "bad_inventory_count": {"$sum": 1},
                        }
                    }
                ]
            )
        )

        bad_count_by_variant = {
            row["_id"]: row["bad_inventory_count"]
            for row in bad_counts
            if row.get("_id")
        }
        variant_ids = list(bad_count_by_variant.keys())

        variants = qc_db.variants.find(
            {"vetic_variant_id": {"$in": variant_ids}},
            {
                "vetic_variant_id": 1,
                "vetbuddy_plan_item_name": 1,
                "item_name": 1,
                "general_details.name": 1,
            },
        )
        variant_names = {}
        for variant in variants:
            variant_names[variant["vetic_variant_id"]] = (
                variant.get("vetbuddy_plan_item_name")
                or variant.get("item_name")
                or variant.get("general_details", {}).get("name")
                or ""
            )

        stock_rows = list(
            qc_db.stock_data.aggregate(
                [
                    {
                        "$match": {
                            "vetic_variant_id": {"$in": variant_ids},
                            "in_hand_quantity": {"$gt": 0},
                        }
                    },
                    {
                        "$group": {
                            "_id": {
                                "vetic_variant_id": "$vetic_variant_id",
                                "clinic_id": "$clinic_id",
                            },
                            "in_hand_quantity": {"$sum": "$in_hand_quantity"},
                        }
                    },
                    {"$match": {"in_hand_quantity": {"$gt": 0}}},
                ]
            )
        )

        clinic_ids = list(
            {
                row["_id"].get("clinic_id")
                for row in stock_rows
                if row.get("_id", {}).get("clinic_id")
            }
        )
        clinic_rows = list(
            vms_db.bad_inventory_data.aggregate(
                [
                    {"$match": {"clinic_details.ship_to_clinic_id": {"$in": clinic_ids}}},
                    {
                        "$group": {
                            "_id": "$clinic_details.ship_to_clinic_id",
                            "clinic_name": {"$first": "$clinic_details.ship_to_clinic_name"},
                        }
                    },
                ]
            )
        )
        clinic_names = {row["_id"]: row.get("clinic_name", "") for row in clinic_rows}

    rows = []
    for stock_row in stock_rows:
        row_id = stock_row["_id"]
        variant_id = row_id["vetic_variant_id"]
        clinic_id = row_id["clinic_id"]
        clinic_name = clinic_names.get(clinic_id, "")
        bad_count = bad_count_by_variant.get(variant_id, 0)

        rows.append(
            {
                "Name": variant_names.get(variant_id, ""),
                "Vetic Variant ID": variant_id,
                "Clinic ID": clinic_id,
                "Clinic Name": clinic_name,
                "Mapped Clinic Name": map_clinic_name(clinic_name),
                "In Hand Quantity": parse_number(stock_row.get("in_hand_quantity")),
                "Bad Inventory Count": bad_count,
                "Priority": priority_for_count(bad_count),
            }
        )

    rows.sort(
        key=lambda item: (
            -item["Bad Inventory Count"],
            item["Name"],
            item["Vetic Variant ID"],
            item["Mapped Clinic Name"],
            item["Clinic ID"],
        )
    )
    return rows


def priority_for_count(count):
    if count >= 3:
        return "Very High"
    if count == 2:
        return "High"
    return "Normal"


def map_clinic_name(clinic_name):
    if not clinic_name:
        return ""

    normalized = " ".join(str(clinic_name).replace("\u00a0", " ").split())
    rules = [
        ("Sector 82A", "GGN82A"),
        ("Sector 57", "GGN57"),
        ("Sector 45", "GGN45"),
        ("Sector 15", "GGN15"),
        ("Sector 20", "Noida20"),
        ("Noida Sec 20", "Noida20"),
        ("Sector 49", "Noida49"),
        ("Sec 49", "Noida49"),
        ("Golf Course Road", "GCR"),
        ("GCR", "GCR"),
        ("DLF Galleria", "Galleria"),
        ("Greater Kailash", "Greater Kailash"),
        ("SDA Market", "SDA Market"),
        ("Southern Avenue", "Southern Avenue"),
        ("Thane West", "Thane"),
        ("Thane West Part A", "Thane"),
        ("Hitec City", "Hitec City"),
        ("HITEC City", "Hitec City"),
        ("Electronic City", "Electronic"),
        ("Mira Raod", "Mira Road"),
        ("Mira Road", "Mira Road"),
        ("Sadashiva Nagar", "Sadashiva"),
        ("Salt Lake", "Salt Lake"),
        ("Sanpada", "Sanpada"),
        ("Santacruz", "Santacruz"),
        ("Sarjapur", "Sarjapur"),
        ("Secunderabad", "Secunderabad"),
        ("Sohna Road", "Sohna"),
        ("Vasant Kunj", "Vasant Kunj"),
        ("Whitefield", "Whitefield"),
        ("Yelahanka", "Yelahanka"),
        ("Adyar", "Adyar"),
        ("Anand Vihar", "Anand Vihar"),
        ("Andheri", "Andheri"),
        ("Anna Nagar", "Anna Nagar"),
        ("Aundh", "Aundh"),
        ("Banjara Hills", "Banjara Hills"),
        ("Banashankari", "Banashankari"),
        ("Bannerghatta", "Bannerghatta"),
        ("Behala", "Behala"),
        ("Borivali", "Borivali"),
        ("Chembur", "Chembur"),
        ("Defence Colony", "Defence Colony"),
        ("Domlur", "Domlur"),
        ("Dwarka Sector 17", "Dwarka Sector 17"),
        ("Faridabad", "Faridabad"),
        ("Goregaon", "Goregaon"),
        ("Himayat Nagar", "Himayat Nagar"),
        ("HSR", "HSR"),
        ("Indirapuram", "Indirapuram"),
        ("Kalyan Nagar", "Kalyan Nagar"),
        ("Kalyani Nagar", "Kalyani Nagar"),
        ("Kandivali", "Kandivali"),
        ("Koramangala", "Koramangala"),
        ("Kukatpally", "Kukatpally"),
        ("Lower Parel", "Lower Parel"),
        ("Malviya Nagar", "Malviya Nagar"),
        ("Manikonda", "Manikonda"),
        ("Mulund", "Mulund"),
        ("Nagarbhavi", "Nagarbhavi"),
        ("NIBM", "NIBM"),
        ("Paschim Vihar", "Paschim Vihar"),
        ("Pitampura", "Pitampura"),
        ("Powai", "Powai"),
        ("Rohini Sec 24", "Rohini Sec 24"),
        ("Rohini Sec-8", "Rohini Sec-8"),
        ("Derawal", "Derawal"),
        ("InventoryDemo - Clinic", "InventoryDemo - Clinic"),
        ("InventoryDemo - TAMS", "InventoryDemo - TAMS"),
    ]

    for needle, mapped_name in rules:
        if needle in normalized:
            return mapped_name

    return normalized


def parse_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0

    return int(number) if number.is_integer() else number


def get_current_rows():
    try:
        return load_live_audit_rows(), "live", ""
    except Exception as exc:
        return load_audit_rows(), "csv_fallback", str(exc)


def filter_rows_by_mapped_clinic(rows, mapped_clinic):
    if not mapped_clinic:
        return rows
    return [row for row in rows if row.get("Mapped Clinic Name") == mapped_clinic]


def get_google_credentials():
    service_account_json = os.environ.get(GOOGLE_SERVICE_ACCOUNT_ENV)
    if not service_account_json:
        raise RuntimeError(f"{GOOGLE_SERVICE_ACCOUNT_ENV} is not configured")

    from google.oauth2 import service_account

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    info = json.loads(service_account_json)
    return service_account.Credentials.from_service_account_info(info, scopes=scopes)


def get_google_service(api_name, api_version):
    from googleapiclient.discovery import build

    return build(api_name, api_version, credentials=get_google_credentials(), cache_discovery=False)


def ensure_sheet_tab(sheets_service):
    spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    sheets = spreadsheet.get("sheets", [])

    if any(sheet.get("properties", {}).get("title") == SHEET_TAB for sheet in sheets):
        return

    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": SHEET_TAB}}}]},
    ).execute()


def update_google_sheet(rows):
    sheets_service = get_google_service("sheets", "v4")
    ensure_sheet_tab(sheets_service)

    headers = [
        "Name",
        "Vetic Variant ID",
        "Clinic ID",
        "Clinic Name",
        "Mapped Clinic Name",
        "In Hand Quantity",
        "Bad Inventory Count",
        "Priority",
    ]
    values = [headers]
    values.extend([[row.get(header, "") for header in headers] for row in rows])

    sheets_service.spreadsheets().values().clear(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB}!A:Z",
        body={},
    ).execute()

    sheets_service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB}!A1",
        valueInputOption="RAW",
        body={"values": values},
    ).execute()

    share_google_sheet()
    return SHEET_URL


def share_google_sheet():
    drive_service = get_google_service("drive", "v3")

    for email in SHARE_EMAILS:
        try:
            drive_service.permissions().create(
                fileId=SHEET_ID,
                body={"type": "user", "role": "writer", "emailAddress": email},
                sendNotificationEmail=True,
            ).execute()
        except Exception:
            pass


@app.route("/")
def dashboard():
    rows, _, _ = get_current_rows()
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
    rows, source, error = get_current_rows()
    response = {"source": source, "rows": rows}
    if error:
        response["error"] = error
    return jsonify(response)


@app.route("/api/update-sheet", methods=["POST"])
def update_sheet():
    payload = request.get_json(silent=True) or {}
    mapped_clinic = payload.get("mapped_clinic", "")
    rows, source, error = get_current_rows()
    rows = filter_rows_by_mapped_clinic(rows, mapped_clinic)

    try:
        sheet_url = update_google_sheet(rows)
    except Exception as exc:
        return jsonify({"error": str(exc), "source": source, "fallback_error": error}), 500

    return jsonify(
        {
            "rows": len(rows),
            "source": source,
            "sheet_url": sheet_url,
            "shared_with": SHARE_EMAILS,
        }
    )


if __name__ == "__main__":
    app.run()
