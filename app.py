from flask import Flask, render_template, request, redirect, session
import pandas as pd

app = Flask(__name__)
app.secret_key = "vetic-secret"

# Google Sheet CSV URL
sheet_url = "https://docs.google.com/spreadsheets/d/1gsxI3pBhT4EjM6qpmN7qE4B1q-xUw3QGB9UPe_h7G3U/export?format=csv"

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


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


if __name__ == "__main__":
    app.run()
