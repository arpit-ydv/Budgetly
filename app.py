import sqlite3
import json
import calendar
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "super-secret-budgetly-key-change-in-prod"
DB_NAME = "expenses.db"

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                monthly_budget REAL DEFAULT 10000.0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                date DATE DEFAULT CURRENT_DATE,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN monthly_budget REAL DEFAULT 10000.0")
        except sqlite3.OperationalError:
            pass
        conn.commit()

init_db()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

# --- Auth Routes ---

@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Username and password are required.", "error")
            return redirect(url_for("register"))

        hashed_pw = generate_password_hash(password)
        try:
            with sqlite3.connect(DB_NAME) as conn:
                conn.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    (username, hashed_pw)
                )
                conn.commit()
            flash("Account created! Please sign in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Username already taken.", "error")
            return redirect(url_for("register"))

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        with sqlite3.connect(DB_NAME) as conn:
            conn.row_factory = sqlite3.Row
            user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("index"))
        else:
            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# --- Dashboard & Insights ---

@app.route("/")
@login_required
def index():
    user_id = session["user_id"]
    selected_month = request.args.get("month", "")
    selected_category = request.args.get("category", "")
    now = datetime.now()

    with sqlite3.connect(DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 1. Budget & Profile
        user = cursor.execute("SELECT monthly_budget FROM users WHERE id = ?", (user_id,)).fetchone()
        budget = user["monthly_budget"] if user and user["monthly_budget"] else 10000.0

        # 2. Base Query Filters
        query = "SELECT * FROM expenses WHERE user_id = ?"
        params = [user_id]
        if selected_month:
            query += " AND strftime('%Y-%m', date) = ?"
            params.append(selected_month)
        if selected_category:
            query += " AND category = ?"
            params.append(selected_category)
        query += " ORDER BY date DESC, id DESC"

        cursor.execute(query, tuple(params))
        expenses = cursor.fetchall()

        # 3. Aggregated Total
        total_query = "SELECT SUM(amount) FROM expenses WHERE user_id = ?"
        total_params = [user_id]
        if selected_month:
            total_query += " AND strftime('%Y-%m', date) = ?"
            total_params.append(selected_month)
        if selected_category:
            total_query += " AND category = ?"
            total_params.append(selected_category)
        cursor.execute(total_query, tuple(total_params))
        total_row = cursor.fetchone()
        total = total_row[0] if total_row[0] else 0.0

        # 4. Active Month Spending for Budget Bar
        current_m = selected_month if selected_month else now.strftime("%Y-%m")
        cursor.execute(
            "SELECT SUM(amount) FROM expenses WHERE user_id = ? AND strftime('%Y-%m', date) = ?",
            (user_id, current_m)
        )
        month_total_row = cursor.fetchone()
        monthly_spent = month_total_row[0] if month_total_row[0] else 0.0

        # 5. Category Breakdown (Doughnut Chart)
        cat_query = """
            SELECT category, SUM(amount) as cat_total 
            FROM expenses 
            WHERE user_id = ?
        """
        cat_params = [user_id]
        if selected_month:
            cat_query += " AND strftime('%Y-%m', date) = ?"
            cat_params.append(selected_month)
        if selected_category:
            cat_query += " AND category = ?"
            cat_params.append(selected_category)
        cat_query += " GROUP BY category ORDER BY cat_total DESC"
        cursor.execute(cat_query, tuple(cat_params))
        cat_data = cursor.fetchall()

        categories = [row["category"] for row in cat_data]
        amounts = [row["cat_total"] for row in cat_data]

        # Top category metric
        top_category = categories[0] if categories else "None"
        top_cat_amount = amounts[0] if amounts else 0.0
        top_cat_pct = round((top_cat_amount / total * 100), 1) if total > 0 else 0.0

        # 6. Timeline Trend (Line Chart: Daily spending chronological)
        trend_query = """
            SELECT date, SUM(amount) as daily_total
            FROM expenses
            WHERE user_id = ?
        """
        trend_params = [user_id]
        if selected_month:
            trend_query += " AND strftime('%Y-%m', date) = ?"
            trend_params.append(selected_month)
        if selected_category:
            trend_query += " AND category = ?"
            trend_params.append(selected_category)
        trend_query += " GROUP BY date ORDER BY date ASC"
        cursor.execute(trend_query, tuple(trend_params))
        trend_data = cursor.fetchall()

        trend_dates = [row["date"] for row in trend_data]
        trend_amounts = [row["daily_total"] for row in trend_data]

        # 7. Burn Rate & Month Projection
        # Determine day count for active month
        year_val, month_val = map(int, current_m.split("-"))
        total_days_in_month = calendar.monthrange(year_val, month_val)[1]
        
        # If current month, divide by elapsed days; else divide by total month days
        if current_m == now.strftime("%Y-%m"):
            elapsed_days = max(now.day, 1)
        else:
            elapsed_days = total_days_in_month

        daily_avg = monthly_spent / elapsed_days if elapsed_days > 0 else 0.0
        projected_spend = daily_avg * total_days_in_month

        # Available months for dropdown
        cursor.execute("""
            SELECT DISTINCT strftime('%Y-%m', date) as month_val 
            FROM expenses 
            WHERE user_id = ? 
            ORDER BY month_val DESC
        """, (user_id,))
        available_months = [row["month_val"] for row in cursor.fetchall() if row["month_val"]]

    budget_pct = min(round((monthly_spent / budget) * 100, 1), 100.0) if budget > 0 else 0.0

    return render_template(
        "index.html",
        username=session["username"],
        expenses=expenses,
        total=total,
        monthly_spent=monthly_spent,
        budget=budget,
        budget_pct=budget_pct,
        top_category=top_category,
        top_cat_pct=top_cat_pct,
        daily_avg=daily_avg,
        projected_spend=projected_spend,
        selected_month=selected_month,
        selected_category=selected_category,
        available_months=available_months,
        categories_json=json.dumps(categories),
        amounts_json=json.dumps(amounts),
        trend_dates_json=json.dumps(trend_dates),
        trend_amounts_json=json.dumps(trend_amounts)
    )

@app.route("/set-budget", methods=["POST"])
@login_required
def set_budget():
    new_budget = request.form.get("budget")
    if new_budget:
        try:
            budget_val = float(new_budget)
            if budget_val > 0:
                with sqlite3.connect(DB_NAME) as conn:
                    conn.execute("UPDATE users SET monthly_budget = ? WHERE id = ?", (budget_val, session["user_id"]))
                    conn.commit()
        except ValueError:
            pass
    return redirect(request.referrer or url_for("index"))

@app.route("/add", methods=["POST"])
@login_required
def add_expense():
    user_id = session["user_id"]
    title = request.form.get("title", "").strip()
    amount = request.form.get("amount")
    category = request.form.get("category", "Other")
    date = request.form.get("date")

    if title and amount:
        try:
            valid_amount = float(amount)
            if valid_amount > 0:
                with sqlite3.connect(DB_NAME) as conn:
                    if date:
                        conn.execute(
                            "INSERT INTO expenses (user_id, title, amount, category, date) VALUES (?, ?, ?, ?, ?)",
                            (user_id, title, valid_amount, category, date)
                        )
                    else:
                        conn.execute(
                            "INSERT INTO expenses (user_id, title, amount, category) VALUES (?, ?, ?, ?)",
                            (user_id, title, valid_amount, category)
                        )
                    conn.commit()
        except ValueError:
            pass

    return redirect(request.referrer or url_for("index"))

@app.route("/delete/<int:expense_id>", methods=["POST", "GET"])
@login_required
def delete_expense(expense_id):
    user_id = session["user_id"]
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user_id))
        conn.commit()
    return redirect(request.referrer or url_for("index"))

if __name__ == "__main__":
    app.run(debug=True)