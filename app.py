import sqlite3
import os
import secrets
from datetime import datetime, date, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
# For local college demos, generate a new key each run so old browser sessions
# are invalidated when the Flask server is restarted. Set HABITLOOP_SECRET_KEY
# to a stable secret if you later deploy this app for real users.
app.secret_key = os.environ.get("HABITLOOP_SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

@app.after_request
def prevent_auth_page_caching(response):
    # Avoid restoring login/register pages with stale form values from browser cache.
    if request.endpoint in {"login", "register", "logout"}:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "habitloop.db")


def get_db():
    # A longer timeout helps when two requests briefly access SQLite together.
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


def init_db():
    conn = get_db()
    # WAL mode allows readers and a writer to coexist more reliably for this local app.
    conn.execute("PRAGMA journal_mode = WAL;")
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS User (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            is_admin BOOLEAN DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS Habit (
            habit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            frequency TEXT DEFAULT 'Daily',
            start_date DATE NOT NULL,
            reminder_time TIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES User(user_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS HabitLog (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            completion_date DATE NOT NULL,
            status BOOLEAN NOT NULL DEFAULT 1,
            notes TEXT,
            FOREIGN KEY (habit_id) REFERENCES Habit(habit_id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES User(user_id) ON DELETE CASCADE,
            UNIQUE (habit_id, completion_date)
        );

        CREATE TABLE IF NOT EXISTS Streak (
            streak_id INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_id INTEGER NOT NULL UNIQUE,
            current_streak INTEGER DEFAULT 0,
            longest_streak INTEGER DEFAULT 0,
            last_completed_date DATE,
            FOREIGN KEY (habit_id) REFERENCES Habit(habit_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS Notification (
            notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            habit_id INTEGER,
            message TEXT NOT NULL,
            reminder_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'unread',
            FOREIGN KEY (user_id) REFERENCES User(user_id) ON DELETE CASCADE,
            FOREIGN KEY (habit_id) REFERENCES Habit(habit_id) ON DELETE SET NULL
        );
    """)

    # Create default Admin if not exists
    cursor.execute("SELECT * FROM User WHERE email = ?", ("admin@habitloop.com",))
    if not cursor.fetchone():
        hashed_pwd = generate_password_hash("admin123")
        cursor.execute(
            "INSERT INTO User (name, email, password, is_admin) VALUES (?, ?, ?, 1)",
            ("System Admin", "admin@habitloop.com", hashed_pwd)
        )
    conn.commit()
    conn.close()


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("is_admin"):
            flash("Administrator access required.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated_function


def recalculate_streak(conn, habit_id):
    """Calculates current and longest streaks based on logs."""
    logs = conn.execute(
        "SELECT completion_date FROM HabitLog WHERE habit_id = ? AND status = 1 ORDER BY completion_date ASC",
        (habit_id,)
    ).fetchall()

    if not logs:
        conn.execute(
            "UPDATE Streak SET current_streak = 0, last_completed_date = NULL WHERE habit_id = ?",
            (habit_id,)
        )
        return

    dates = sorted(list(set([datetime.strptime(row["completion_date"], "%Y-%m-%d").date() for row in logs])))
    
    longest = 0
    current = 0
    streak = 0
    prev_date = None

    for d in dates:
        if prev_date is None or d == prev_date + timedelta(days=1):
            streak += 1
        else:
            streak = 1
        if streak > longest:
            longest = streak
        prev_date = d

    # Current streak check
    today = date.today()
    yesterday = today - timedelta(days=1)
    last_date = dates[-1]

    if last_date == today or last_date == yesterday:
        # Trace backwards from last completion
        current = 0
        target = last_date
        for d in reversed(dates):
            if d == target:
                current += 1
                target -= timedelta(days=1)
            else:
                break
    else:
        current = 0

    conn.execute("""
        UPDATE Streak 
        SET current_streak = ?, longest_streak = ?, last_completed_date = ?
        WHERE habit_id = ?
    """, (current, longest, last_date.strftime("%Y-%m-%d"), habit_id))


# ---------------- Routes ---------------- #

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


import re  # Agar pehle se nahi hai toh top par import karein

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        # 1. Email Format Validation (.com / extension check)
        email_pattern = r"^[\w\.-]+@[\w\.-]+\.\w+$"
        if not re.match(email_pattern, email):
            flash("Please enter a valid email address (e.g., name@example.com).", "danger")
            return redirect(url_for("register"))

        # 2. Minimum Length Check (High Security)
        if len(password) < 8:
            flash("Password must be at least 8 characters long.", "danger")
            return redirect(url_for("register"))
        
        # 3. Lowercase (Small letter) Check
        elif not any(c.islower() for c in password):
            flash("Password must contain at least one lowercase letter (a-z).", "danger")
            return redirect(url_for("register"))
        
        # 4. Uppercase (Capital letter) Check
        elif not any(c.isupper() for c in password):
            flash("Password must contain at least one uppercase letter (A-Z).", "danger")
            return redirect(url_for("register"))
        
        # 5. Special Character Check
        elif not re.search(r"[!@#$%^&*(),.?\":{}|<>]", password):
            flash("Password must contain at least one special character (e.g. @, #, $, !).", "danger")
            return redirect(url_for("register"))

        # Agar saare checks pass ho jayein, tab database me save hoga
        conn = get_db()
        try:
            hashed_pwd = generate_password_hash(password)
            conn.execute(
                "INSERT INTO User (name, email, password) VALUES (?, ?, ?)",
                (name, email, hashed_pwd)
            )
            conn.commit()
            flash("Account registered successfully! Please log in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Email is already registered.", "danger")
        finally:
            conn.close()

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        conn = get_db()
        user = conn.execute("SELECT * FROM User WHERE email = ?", (email,)).fetchone()
        conn.close()

        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["user_id"]
            session["name"] = user["name"]
            session["is_admin"] = bool(user["is_admin"])
            flash(f"Welcome back, {user['name']}!", "success")
            if session["is_admin"]:
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid email or password.", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    today_str = date.today().strftime("%Y-%m-%d")

    # Fetch habits and their streak + completion status for today
    habits = conn.execute("""
        SELECT 
            h.*, 
            COALESCE(s.current_streak, 0) AS current_streak,
            COALESCE(s.longest_streak, 0) AS longest_streak,
            CASE WHEN hl.log_id IS NOT NULL THEN 1 ELSE 0 END AS completed_today
        FROM Habit h
        LEFT JOIN Streak s ON h.habit_id = s.habit_id
        LEFT JOIN HabitLog hl ON h.habit_id = hl.habit_id AND hl.completion_date = ?
        WHERE h.user_id = ?
        ORDER BY h.created_at DESC
    """, (today_str, session["user_id"])).fetchall()

    notifications = conn.execute("""
        SELECT * FROM Notification
        WHERE user_id = ? AND status = 'unread'
        ORDER BY reminder_time DESC LIMIT 5
    """, (session["user_id"],)).fetchall()

    total_habits = len(habits)
    completed_today = sum(1 for h in habits if h["completed_today"])
    completion_percent = round((completed_today / total_habits) * 100) if total_habits else 0
    best_streak = max((h["longest_streak"] for h in habits), default=0)

    # Explainable, data-driven coaching (rule-based, not a trained ML model).
    coach_tips = []
    for h in habits:
        recent = conn.execute("""
            SELECT COUNT(*) AS completed
            FROM HabitLog
            WHERE habit_id = ? AND user_id = ? AND status = 1
              AND completion_date >= ? AND completion_date <= ?
        """, (h["habit_id"], session["user_id"],
              (date.today() - timedelta(days=6)).strftime("%Y-%m-%d"), today_str)).fetchone()["completed"]
        if h["completed_today"]:
            coach_tips.append({"title": h["title"], "kind": "success",
                               "message": f"Great work — you completed this habit today. Keep the routine going!"})
        elif recent <= 2:
            coach_tips.append({"title": h["title"], "kind": "focus",
                               "message": f"This habit was completed {recent} time(s) in the last 7 days. Try a smaller 2-minute version today."})
        else:
            coach_tips.append({"title": h["title"], "kind": "tip",
                               "message": f"You completed this {recent} time(s) in the last 7 days. Pick a consistent time to make it easier to repeat."})
    coach_tips = coach_tips[:3]

    conn.close()
    return render_template("dashboard.html", habits=habits, notifications=notifications,
                           today=today_str, total_habits=total_habits,
                           completed_today=completed_today, completion_percent=completion_percent,
                           best_streak=best_streak, coach_tips=coach_tips)


@app.route("/habits/create", methods=["GET", "POST"])
@login_required
def create_habit():
    if request.method == "POST":
        title = request.form["title"].strip()
        description = request.form.get("description", "").strip()
        frequency = request.form.get("frequency", "Daily")
        start_date = request.form.get("start_date") or date.today().strftime("%Y-%m-%d")
        reminder_time = request.form.get("reminder_time") or "08:00"

        if not title:
            flash("Please enter a habit name.", "danger")
            return redirect(url_for("create_habit"))

        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO Habit (user_id, title, description, frequency, start_date, reminder_time)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (session["user_id"], title, description, frequency, start_date, reminder_time))
            habit_id = cursor.lastrowid
            cursor.execute("INSERT INTO Streak (habit_id, current_streak, longest_streak) VALUES (?, 0, 0)", (habit_id,))
            cursor.execute("""
                INSERT INTO Notification (user_id, habit_id, message)
                VALUES (?, ?, ?)
            """, (session["user_id"], habit_id, f"Reminder set for habit: {title} at {reminder_time}"))
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if "locked" in str(exc).lower():
                flash("The database is busy. Please wait a moment and try again.", "warning")
                return redirect(url_for("create_habit"))
            raise
        finally:
            conn.close()
        flash("Habit created successfully!", "success")
        return redirect(url_for("dashboard"))

    return render_template("add_habit.html")


@app.route("/habits/<int:habit_id>/toggle", methods=["POST"])
@login_required
def toggle_habit(habit_id):
    conn = get_db()
    today_str = date.today().strftime("%Y-%m-%d")

    # Verify ownership
    habit = conn.execute("SELECT * FROM Habit WHERE habit_id = ? AND user_id = ?", (habit_id, session["user_id"])).fetchone()
    if not habit:
        conn.close()
        return jsonify({"success": False, "error": "Habit not found"}), 404

    existing_log = conn.execute(
        "SELECT * FROM HabitLog WHERE habit_id = ? AND completion_date = ?",
        (habit_id, today_str)
    ).fetchone()

    if existing_log:
        conn.execute("DELETE FROM HabitLog WHERE log_id = ?", (existing_log["log_id"],))
        completed = False
    else:
        conn.execute(
            "INSERT INTO HabitLog (habit_id, user_id, completion_date, status) VALUES (?, ?, ?, 1)",
            (habit_id, session["user_id"], today_str)
        )
        completed = True

    recalculate_streak(conn, habit_id)
    conn.commit()

    streak_row = conn.execute("SELECT current_streak FROM Streak WHERE habit_id = ?", (habit_id,)).fetchone()
    conn.close()

    return jsonify({"success": True, "completed": completed, "current_streak": streak_row["current_streak"] if streak_row else 0})


@app.route("/habits/<int:habit_id>/delete", methods=["POST"])
@login_required
def delete_habit(habit_id):
    conn = get_db()
    conn.execute("DELETE FROM Habit WHERE habit_id = ? AND user_id = ?", (habit_id, session["user_id"]))
    conn.commit()
    conn.close()
    flash("Habit deleted.", "info")
    return redirect(url_for("dashboard"))


@app.route("/progress")
@login_required
def progress():
    conn = get_db()
    user_id = session["user_id"]
    today = date.today()

    # Past 7 days completion chart
    last_7_days = [(today - timedelta(days=i)) for i in range(6, -1, -1)]
    labels = [d.strftime("%a (%d %b)") for d in last_7_days]
    date_strs = [d.strftime("%Y-%m-%d") for d in last_7_days]

    counts = []
    for d_str in date_strs:
        row = conn.execute("""
            SELECT COUNT(*) AS cnt FROM HabitLog 
            WHERE user_id = ? AND completion_date = ? AND status = 1
        """, (user_id, d_str)).fetchone()
        counts.append(row["cnt"])

    # Overall habit analytics
    habit_stats = conn.execute("""
        SELECT 
            h.habit_id, h.title, h.frequency,
            COALESCE(s.current_streak, 0) AS current_streak,
            COALESCE(s.longest_streak, 0) AS longest_streak,
            COUNT(hl.log_id) AS total_completed
        FROM Habit h
        LEFT JOIN Streak s ON h.habit_id = s.habit_id
        LEFT JOIN HabitLog hl ON h.habit_id = hl.habit_id AND hl.status = 1
        WHERE h.user_id = ?
        GROUP BY h.habit_id
    """, (user_id,)).fetchall()

    conn.close()
    return render_template("progress.html", labels=labels, counts=counts, habit_stats=habit_stats)


@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_db()
    users = conn.execute("SELECT user_id, name, email, is_admin, created_at FROM User").fetchall()
    total_users = len(users)
    total_habits = conn.execute("SELECT COUNT(*) AS cnt FROM Habit").fetchone()["cnt"]
    total_logs = conn.execute("SELECT COUNT(*) AS cnt FROM HabitLog").fetchone()["cnt"]
    conn.close()

    return render_template("admin.html", users=users, total_users=total_users, total_habits=total_habits, total_logs=total_logs)


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True, use_reloader=False)