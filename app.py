import csv
import io
import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from flask import Flask, render_template, request, redirect, session, jsonify
import pandas as pd
from pymongo import MongoClient

app = Flask(__name__)
app.secret_key = "vetic-secret"

# Google Sheet CSV URL
sheet_url = "https://docs.google.com/spreadsheets/d/1gsxI3pBhT4EjM6qpmN7qE4B1q-xUw3QGB9UPe_h7G3U/export?format=csv"
AUDIT_EMAIL_TO = "Kaushal.Kumar@vetic.in"
MONGO_URI_ENV = "TRANSFER_ORDERS_MONGO_URI"


def get_mongo_client():
    mongo_uri = os.environ.get(MONGO_URI_ENV)
    if not mongo_uri:
        raise RuntimeError(f"{MONGO_URI_ENV} is not configured")
    return MongoClient(mongo_uri)


def get_bad_inventory_variant_ids(vms_db):
    return vms_db.bad_inventory_data.distinct(
        "vetic_variant_id",
        {
            "inventory_movement_at": {
                "$gte": datetime(2026, 5, 1, tzinfo=timezone.utc),
                "$lt": datetime(2026, 6, 1, tzinfo=timezone.utc),
            },
            "reason_of_inventory_movement": "Not found in Q COM order",
        },
    )


def get_clinic_names(vms_db, clinic_ids):
    clinic_rows = vms_db.bad_inventory_data.aggregate(
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
    return {row["_id"]: row.get("clinic_name", "") for row in clinic_rows}


def get_variant_names(qc_db, variant_ids):
    variants = qc_db.variants.find(
        {"vetic_variant_id": {"$in": variant_ids}},
        {
            "vetic_variant_id": 1,
            "vetbuddy_plan_item_name": 1,
            "item_name": 1,
            "general_details.name": 1,
        },
    )

    names = {}
    for variant in variants:
        names[variant["vetic_variant_id"]] = (
            variant.get("vetbuddy_plan_item_name")
            or variant.get("item_name")
            or variant.get("general_details", {}).get("name")
            or ""
        )
    return names


def build_in_hand_rows_for_clinic(clinic_id):
    with get_mongo_client() as client:
        vms_db = client["vetic_vms"]
        qc_db = client["quick_commerce"]
        variant_ids = get_bad_inventory_variant_ids(vms_db)
        variant_names = get_variant_names(qc_db, variant_ids)
        clinic_names = get_clinic_names(vms_db, [clinic_id])

        stock_rows = qc_db.stock_data.aggregate(
            [
                {
                    "$match": {
                        "clinic_id": clinic_id,
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
                {"$sort": {"_id.vetic_variant_id": 1}},
            ]
        )

        rows = []
        for row in stock_rows:
            vetic_variant_id = row["_id"]["vetic_variant_id"]
            rows.append(
                {
                    "Name": variant_names.get(vetic_variant_id, ""),
                    "Vetic Variant ID": vetic_variant_id,
                    "Clinic ID": row["_id"]["clinic_id"],
                    "Clinic Name": clinic_names.get(row["_id"]["clinic_id"], ""),
                    "In Hand Quantity": row["in_hand_quantity"],
                }
            )

        rows.sort(key=lambda item: (item["Name"], item["Vetic Variant ID"]))
        return rows


def rows_to_csv(rows):
    output = io.StringIO()
    fieldnames = [
        "Name",
        "Vetic Variant ID",
        "Clinic ID",
        "Clinic Name",
        "In Hand Quantity",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def send_audit_email(csv_text, clinic_name):
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_user = os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    smtp_from = os.environ.get("SMTP_FROM", smtp_user)
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    if not all([smtp_host, smtp_user, smtp_password, smtp_from]):
        return False

    msg = EmailMessage()
    msg["Subject"] = f"QCOM In Hand Audit - {clinic_name}"
    msg["From"] = smtp_from
    msg["To"] = AUDIT_EMAIL_TO
    msg.set_content("Attached is the QCOM in_hand_quantity audit for the selected clinic.")
    msg.add_attachment(
        csv_text,
        filename="qcom_in_hand_audit.csv",
        subtype="csv",
        maintype="text",
    )

    with smtplib.SMTP(smtp_host, smtp_port) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)

    return True

@app.route("/", methods=["GET", "POST"])
def login():
    msg = ""

    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        try:
            df = pd.read_csv(sheet_url)

            user = df[
                (df["Username"].astype(str).str.strip() == username) &
                (df["Password"].astype(str).str.strip() == password) &
                (df["Status"].astype(str).str.strip().str.lower() == "active")
            ]

            if not user.empty:
                session["user"] = username
                session["role"] = user.iloc[0]["Role"]
                session["name"] = user.iloc[0]["Name"]
                return redirect("/dashboard")
            else:
                msg = "Invalid Credentials"

        except:
            msg = "Sheet Connection Error"

    return render_template("login.html", msg=msg)


@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/")

    return render_template(
        "dashboard.html",
        user=session["name"],
        role=session["role"]
    )


@app.route("/audit-clinics")
def audit_clinics():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        with get_mongo_client() as client:
            vms_db = client["vetic_vms"]
            qc_db = client["quick_commerce"]
            variant_ids = get_bad_inventory_variant_ids(vms_db)

            clinic_ids = qc_db.stock_data.distinct(
                "clinic_id",
                {
                    "vetic_variant_id": {"$in": variant_ids},
                    "in_hand_quantity": {"$gt": 0},
                },
            )
            clinic_names = get_clinic_names(vms_db, clinic_ids)

            clinics = [
                {"id": clinic_id, "name": clinic_names.get(clinic_id, clinic_id)}
                for clinic_id in clinic_ids
            ]
            clinics.sort(key=lambda clinic: clinic["name"])

        return jsonify({"clinics": clinics})
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/run-audit", methods=["POST"])
def run_audit():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or request.form
    clinic_id = data.get("clinic_id")
    clinic_name = data.get("clinic_name", clinic_id)

    if not clinic_id:
        return jsonify({"error": "Clinic is required"}), 400

    try:
        rows = build_in_hand_rows_for_clinic(clinic_id)
        csv_text = rows_to_csv(rows)
        email_sent = send_audit_email(csv_text, clinic_name)

        return jsonify(
            {
                "rows": len(rows),
                "email_to": AUDIT_EMAIL_TO,
                "email_sent": email_sent,
                "message": (
                    "Audit sent successfully"
                    if email_sent
                    else "Audit generated. SMTP is not configured, so email was not sent."
                ),
            }
        )
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


if __name__ == "__main__":
    app.run()
