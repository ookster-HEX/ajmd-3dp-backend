import sys
import sqlite3
from pathlib import Path


def promote_user_to_admin(user_id: str, db_path: str = "./3dp.db") -> None:
    db_file = Path(db_path)
    if not db_file.is_file():
        print(f"Error: Database file '{db_file}' not found.")
        sys.exit(1)

    conn = sqlite3.connect(str(db_file))
    try:
        cursor = conn.cursor()

        # Ensure the 'admin' column exists on the users table
        cursor.execute("PRAGMA table_info(users)")
        columns = {row[1] for row in cursor.fetchall()}
        if "admin" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN admin BOOLEAN NOT NULL DEFAULT 0")
            print("Added missing 'admin' column to 'users' table.")

        # Verify user exists and update admin = 1 (True)
        cursor.execute("SELECT id, username, admin FROM users WHERE id = ?", (user_id.strip(),))
        user_row = cursor.fetchone()
        if not user_row:
            print(f"Error: No user found with id '{user_id}'.")
            sys.exit(1)

        cursor.execute("UPDATE users SET admin = 1 WHERE id = ?", (user_id.strip(),))
        conn.commit()
        print(f"Success: User '{user_row[1]}' ({user_row[0]}) is now an admin (admin=True).")
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: uv run python make_admin.py <user_id> [path_to_3dp.db]")
        sys.exit(1)

    target_user_id = sys.argv[1]
    database_file = sys.argv[2] if len(sys.argv) > 2 else "./3dp.db"
    promote_user_to_admin(target_user_id, database_file)