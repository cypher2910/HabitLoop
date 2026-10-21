# HabitLoop

HabitLoop is a Flask + SQLite habit-tracking web app.

## Run on Windows
1. Extract the ZIP to a normal folder (not inside OneDrive or a synced/network folder if SQLite locking persists).
2. Open the folder containing `app.py` in VS Code.
3. In the VS Code terminal run:

   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   python -m pip install -r requirements.txt
   python app.py
   ```
4. Visit http://127.0.0.1:5000

The app creates/updates `habitloop.db` automatically. The default admin login is `admin@habitloop.com` / `admin123`; change this before using the app beyond a local college demo.

## What's improved
- SQLite connection timeout and busy timeout to reduce transient `database is locked` errors.
- SQLite WAL mode to improve reader/writer coexistence.
- Absolute database path so the database does not accidentally get created in a different working directory.
- Disabled Flask's debug reloader to avoid a second development process competing for the database.
- Dashboard summary cards for today's completion percentage, completion count, and longest streak.
- Smart Habit Coach with transparent, rule-based suggestions derived from the user's last seven days of logs.

## Important note about the coach
The coach is a transparent rule-based recommendation feature, not a trained machine-learning model. Do not describe it as K-means, deep learning, or a generative AI model. A future version could add a real ML model if required by the project brief.

## Database locking note
Stop old Flask/Python instances before restarting the app. Keep the database on a local drive and do not delete `habitloop.db`, `habitloop.db-wal`, or `habitloop.db-shm` while the app is running.


### Fresh start / account data
The ZIP intentionally does not include a pre-populated `habitloop.db`. On first run, the app creates a fresh database and default admin account. Existing user accounts remain in your local database until you back it up and remove that database file. Browser autofill is controlled by Chrome/password-manager settings; the login/register forms request that autofill be disabled, but some browsers may override this.
