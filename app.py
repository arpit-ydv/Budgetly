import os
import io
import csv
import json
import calendar
import threading
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, Response
from werkzeug.security import generate_password_hash, check_password_hash
import resend

# Database configuration: PostgreSQL on Render/Neon, fallback to SQLite locally
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    
    if "channel_binding=" in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.replace("&channel_binding=require", "").replace("channel_binding=require", "")
        if DATABASE_URL.endswith("?") or DATABASE_URL.endswith("&"):
            DATABASE_URL = DATABASE_URL[:-1]

    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError:
        import psycopg2_binary as psycopg2
        from psycopg2.extras import RealDictCursor
else:
    import sqlite3

# Resend API Key (Configured in Render -> Environment)
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "super-secret-budgetly-key-change-in-prod")
DB_NAME = "expenses.db"

def get_db():
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    try:
        conn = get_db()
        cursor = conn.cursor()
        if DATABASE_URL:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(100) UNIQUE NOT NULL,
                    email VARCHAR(255) UNIQUE,
                    password_hash TEXT NOT NULL,
                    monthly_budget NUMERIC DEFAULT 10000.0
                );
            """)
            cursor.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='email') THEN
                        ALTER TABLE users ADD COLUMN email VARCHAR(255) UNIQUE;
                    END IF;
                END $$;
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS expenses (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title VARCHAR(255) NOT NULL,
                    amount NUMERIC NOT NULL,
                    category VARCHAR(100) NOT NULL,
                    date DATE DEFAULT CURRENT_DATE
                );
            """)
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE,
                    password_hash TEXT NOT NULL,
                    monthly_budget REAL DEFAULT 10000.0
                );
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
                );
            """)
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN email TEXT UNIQUE")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN monthly_budget REAL DEFAULT 10000.0")
            except sqlite3.OperationalError:
                pass

        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"init_db notice: {e}")

try:
    init_db()
except Exception as err:
    print(f"Deferred init: {err}")

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

def get_ph():
    return "%s" if DATABASE_URL else "?"

# =====================================================================
# EMAIL ALERT DISPATCHER (RESEND HTTPS API - BYPASSES RENDER SMTP BLOCK)
# =====================================================================
def send_budget_alert_async(to_email, username, spent, budget, month_name):
    def _send():
        if not RESEND_API_KEY or not to_email:
            print("Alert skipped: Missing RESEND_API_KEY or recipient email.")
            return
        try:
            html_content = f"""
            <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #07090e; color: #e2e8f0; padding: 30px; border-radius: 20px; max-width: 500px; margin: auto; border: 1px solid rgba(255,255,255,0.1);">
                <h2 style="color: #38bdf8; margin-top: 0; font-size: 24px;">Budgetly<span style="color: #38bdf8;">.</span></h2>
                <h3 style="color: #f43f5e; margin-bottom: 8px;">Spending Limit Exceeded</h3>
                <p style="font-size: 14px; color: #94a3b8;">Hi <strong>{username}</strong>,</p>
                <p style="font-size: 14px; color: #cbd5e1; line-height: 1.6;">
                    Your recorded outlays for <strong>{month_name}</strong> have crossed your target ceiling.
                </p>
                <div style="background: rgba(255, 255, 255, 0.05); padding: 18px; border-radius: 14px; margin: 20px 0; border: 1px solid rgba(255, 255, 255, 0.1);">
                    <div style="font-size: 13px; color: #94a3b8; margin-bottom: 6px;">Total Outlay: <strong style="color: #f43f5e; font-size: 16px;">₹{spent:,.2f}</strong></div>
                    <div style="font-size: 13px; color: #94a3b8;">Allocated Cap: <strong style="color: #ffffff; font-size: 14px;">₹{budget:,.2f}</strong></div>
                </div>
                <p style="font-size: 12px; color: #64748b;">Visit your Budgetly dashboard to view transaction velocity or adjust your monthly cap.</p>
            </div>
            """

            params = {
                "from": "Budgetly <onboarding@resend.dev>",
                "to": [to_email],
                "subject": f"⚠️ Budget Alert: You exceeded your {month_name} budget!",
                "html": html_content,
            }
            res = resend.Emails.send(params)
            print(f"Alert sent successfully via Resend: {res}")
        except Exception as ex:
            print(f"Failed to send email alert via Resend: {ex}")

    threading.Thread(target=_send).start()

# --- Authentication Routes ---

@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not username or not password or not email:
            flash("Username, email, and password are required.", "error")
            return redirect(url_for("register"))

        hashed_pw = generate_password_hash(password)
        conn = get_db()
        cursor = conn.cursor()
        ph = get_ph()

        try:
            cursor.execute(
                f"INSERT INTO users (username, email, password_hash) VALUES ({ph}, {ph}, {ph})",
                (username, email, hashed_pw)
            )
            conn.commit()
            flash("Account initialized! Please sign in.", "success")
            return redirect(url_for("login"))
        except Exception:
            flash("Username or Email is already registered.", "error")
            return redirect(url_for("register"))
        finally:
            cursor.close()
            conn.close()

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        login_input = request.form.get("login_input", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cursor = conn.cursor()
        ph = get_ph()

        cursor.execute(
            f"SELECT * FROM users WHERE LOWER(username) = LOWER({ph}) OR LOWER(email) = LOWER({ph})",
            (login_input, login_input)
        )
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["email"] = user.get("email") if isinstance(user, dict) else user["email"]
            return redirect(url_for("index"))
        else:
            flash("Invalid credentials.", "error")
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
    ph = get_ph()

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(f"SELECT monthly_budget, email FROM users WHERE id = {ph}", (user_id,))
    user = cursor.fetchone()
    budget = float(user["monthly_budget"]) if user and user.get("monthly_budget") else 10000.0
    user_email = user.get("email") if user and user.get("email") else ""

    date_fn = "TO_CHAR(date, 'YYYY-MM')" if DATABASE_URL else "strftime('%Y-%m', date)"

    query = (
        f"SELECT id, title, amount, category, TO_CHAR(date, 'YYYY-MM-DD') as date FROM expenses WHERE user_id = {ph}"
        if DATABASE_URL else
        f"SELECT * FROM expenses WHERE user_id = {ph}"
    )
    params = [user_id]

    if selected_month:
        query += f" AND {date_fn} = {ph}"
        params.append(selected_month)
    if selected_category:
        query += f" AND category = {ph}"
        params.append(selected_category)
    query += " ORDER BY date DESC, id DESC"

    cursor.execute(query, tuple(params))
    expenses = cursor.fetchall()

    total_query = f"SELECT COALESCE(SUM(amount), 0) as total FROM expenses WHERE user_id = {ph}"
    total_params = [user_id]
    if selected_month:
        total_query += f" AND {date_fn} = {ph}"
        total_params.append(selected_month)
    if selected_category:
        total_query += f" AND category = {ph}"
        total_params.append(selected_category)

    cursor.execute(total_query, tuple(total_params))
    total_row = cursor.fetchone()
    total = float(total_row["total"] if DATABASE_URL else total_row[0]) if total_row else 0.0

    current_m = selected_month if selected_month else now.strftime("%Y-%m")
    cursor.execute(
        f"SELECT COALESCE(SUM(amount), 0) as month_total FROM expenses WHERE user_id = {ph} AND {date_fn} = {ph}",
        (user_id, current_m)
    )
    month_total_row = cursor.fetchone()
    monthly_spent = float(month_total_row["month_total"] if DATABASE_URL else month_total_row[0]) if month_total_row else 0.0

    cat_query = f"""
        SELECT category, SUM(amount) as cat_total 
        FROM expenses 
        WHERE user_id = {ph}
    """
    cat_params = [user_id]
    if selected_month:
        cat_query += f" AND {date_fn} = {ph}"
        cat_params.append(selected_month)
    if selected_category:
        cat_query += f" AND category = {ph}"
        cat_params.append(selected_category)
    cat_query += " GROUP BY category ORDER BY cat_total DESC"

    cursor.execute(cat_query, tuple(cat_params))
    cat_data = cursor.fetchall()

    categories = [row["category"] for row in cat_data]
    amounts = [float(row["cat_total"]) for row in cat_data]

    top_category = categories[0] if categories else "None"
    top_cat_amount = amounts[0] if amounts else 0.0
    top_cat_pct = round((top_cat_amount / total * 100), 1) if total > 0 else 0.0

    trend_col = "TO_CHAR(date, 'YYYY-MM-DD')" if DATABASE_URL else "date"
    trend_query = f"""
        SELECT {trend_col} as tx_date, SUM(amount) as daily_total
        FROM expenses
        WHERE user_id = {ph}
    """
    trend_params = [user_id]
    if selected_month:
        trend_query += f" AND {date_fn} = {ph}"
        trend_params.append(selected_month)
    if selected_category:
        trend_query += f" AND category = {ph}"
        trend_params.append(selected_category)
    trend_query += f" GROUP BY {trend_col} ORDER BY tx_date ASC"

    cursor.execute(trend_query, tuple(trend_params))
    trend_data = cursor.fetchall()

    trend_dates = [str(row["tx_date"]) for row in trend_data]
    trend_amounts = [float(row["daily_total"]) for row in trend_data]

    year_val, month_val = map(int, current_m.split("-"))
    total_days_in_month = calendar.monthrange(year_val, month_val)[1]
    elapsed_days = max(now.day, 1) if current_m == now.strftime("%Y-%m") else total_days_in_month
    daily_avg = monthly_spent / elapsed_days if elapsed_days > 0 else 0.0
    projected_spend = daily_avg * total_days_in_month

    cursor.execute(f"""
        SELECT DISTINCT {date_fn} as month_val 
        FROM expenses 
        WHERE user_id = {ph} 
        ORDER BY month_val DESC
    """, (user_id,))
    months_rows = cursor.fetchall()
    available_months = [row["month_val"] for row in months_rows if row.get("month_val") or (not DATABASE_URL and row[0])]

    cursor.close()
    conn.close()

    budget_pct = min(round((monthly_spent / budget) * 100, 1), 100.0) if budget > 0 else 0.0

    return render_template(
        "index.html",
        username=session["username"],
        user_email=user_email,
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

@app.route("/link-email", methods=["POST"])
@login_required
def link_email():
    new_email = request.form.get("email", "").strip().lower()
    user_id = session["user_id"]
    ph = get_ph()

    if new_email:
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute(f"UPDATE users SET email = {ph} WHERE id = {ph}", (new_email, user_id))
            conn.commit()
            session["email"] = new_email
            flash("Email address linked successfully!", "success")
        except Exception:
            flash("That email is already linked to another account.", "error")
        finally:
            cursor.close()
            conn.close()

    return redirect(url_for("index"))

@app.route("/set-budget", methods=["POST"])
@login_required
def set_budget():
    new_budget = request.form.get("budget")
    user_id = session["user_id"]
    ph = get_ph()

    if new_budget:
        try:
            budget_val = float(new_budget)
            if budget_val > 0:
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute(f"UPDATE users SET monthly_budget = {ph} WHERE id = {ph}", (budget_val, user_id))
                conn.commit()
                cursor.close()
                conn.close()
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
    ph = get_ph()

    if title and amount:
        try:
            valid_amount = float(amount)
            if valid_amount > 0:
                conn = get_db()
                cursor = conn.cursor()
                if date:
                    cursor.execute(
                        f"INSERT INTO expenses (user_id, title, amount, category, date) VALUES ({ph}, {ph}, {ph}, {ph}, {ph})",
                        (user_id, title, valid_amount, category, date)
                    )
                else:
                    cursor.execute(
                        f"INSERT INTO expenses (user_id, title, amount, category) VALUES ({ph}, {ph}, {ph}, {ph})",
                        (user_id, title, valid_amount, category)
                    )
                conn.commit()

                # Calculate whether this entry pushed spending past monthly ceiling
                date_fn = "TO_CHAR(date, 'YYYY-MM')" if DATABASE_URL else "strftime('%Y-%m', date)"
                tx_month = date[:7] if date else datetime.now().strftime("%Y-%m")

                cursor.execute(f"SELECT email, monthly_budget, username FROM users WHERE id = {ph}", (user_id,))
                user_info = cursor.fetchone()

                cursor.execute(
                    f"SELECT COALESCE(SUM(amount), 0) as m_sum FROM expenses WHERE user_id = {ph} AND {date_fn} = {ph}",
                    (user_id, tx_month)
                )
                m_sum_row = cursor.fetchone()
                total_month = float(m_sum_row["m_sum"] if DATABASE_URL else m_sum_row[0])

                cursor.close()
                conn.close()

                # Trigger the email alert
                if user_info and user_info.get("email"):
                    budget_limit = float(user_info["monthly_budget"] or 10000.0)
                    user_email = user_info["email"]
                    username = user_info["username"]
                    if total_month > budget_limit and (total_month - valid_amount) <= budget_limit:
                        send_budget_alert_async(user_email, username, total_month, budget_limit, tx_month)
        except ValueError:
            pass

    return redirect(request.referrer or url_for("index"))

@app.route("/edit/<int:expense_id>", methods=["POST"])
@login_required
def edit_expense(expense_id):
    user_id = session["user_id"]
    title = request.form.get("title", "").strip()
    amount = request.form.get("amount")
    category = request.form.get("category", "Other")
    date = request.form.get("date")
    ph = get_ph()

    if title and amount:
        try:
            valid_amount = float(amount)
            if valid_amount > 0:
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute(
                    f"UPDATE expenses SET title = {ph}, amount = {ph}, category = {ph}, date = {ph} WHERE id = {ph} AND user_id = {ph}",
                    (title, valid_amount, category, date, expense_id, user_id)
                )
                conn.commit()
                cursor.close()
                conn.close()
        except ValueError:
            pass

    return redirect(request.referrer or url_for("index"))

@app.route("/delete/<int:expense_id>", methods=["POST", "GET"])
@login_required
def delete_expense(expense_id):
    user_id = session["user_id"]
    conn = get_db()
    cursor = conn.cursor()
    ph = get_ph()
    cursor.execute(f"DELETE FROM expenses WHERE id = {ph} AND user_id = {ph}", (expense_id, user_id))
    conn.commit()
    cursor.close()
    conn.close()
    return redirect(request.referrer or url_for("index"))

@app.route("/export-csv")
@login_required
def export_csv():
    user_id = session["user_id"]
    selected_month = request.args.get("month", "")
    selected_category = request.args.get("category", "")
    ph = get_ph()

    conn = get_db()
    cursor = conn.cursor()
    date_fn = "TO_CHAR(date, 'YYYY-MM')" if DATABASE_URL else "strftime('%Y-%m', date)"
    
    query = (
        f"SELECT id, title, amount, category, TO_CHAR(date, 'YYYY-MM-DD') as date FROM expenses WHERE user_id = {ph}"
        if DATABASE_URL else
        f"SELECT * FROM expenses WHERE user_id = {ph}"
    )
    params = [user_id]
    if selected_month:
        query += f" AND {date_fn} = {ph}"
        params.append(selected_month)
    if selected_category:
        query += f" AND category = {ph}"
        params.append(selected_category)
    query += " ORDER BY date DESC, id DESC"

    cursor.execute(query, tuple(params))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Date", "Description", "Category", "Amount (INR)"])

    for row in rows:
        writer.writerow([row["id"], row["date"], row["title"], row["category"], f"{float(row['amount']):.2f}"])

    output.seek(0)
    filename = f"budgetly_expenses_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={filename}"}
    )

if __name__ == "__main__":
    app.run(debug=True)