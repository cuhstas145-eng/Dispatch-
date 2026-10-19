"""Create the first CargoSync workspace administrator. Run once on the server."""
import getpass
import re

from app import create_admin_user, connect


def main() -> None:
    with connect() as c:
        existing = c.execute("SELECT id,username FROM users WHERE role='admin' LIMIT 1").fetchone()
    if existing:
        print(f"An administrator already exists ({existing['username']}). No account was created.")
        return
    print("Create the first CargoSync administrator. Passwords are hashed on the server; they are not stored in plaintext.")
    name = input("Admin display name [Workspace Admin]: ").strip() or "Workspace Admin"
    username = input("Admin login [admin]: ").strip().lower() or "admin"
    while not re.fullmatch(r"[a-z0-9._]{3,32}", username):
        username = input("Use 3–32 letters, numbers, dots or underscores. Login: ").strip().lower()
    while True:
        password = getpass.getpass("Password (minimum 12 characters): ")
        confirm = getpass.getpass("Repeat password: ")
        if len(password) < 12:
            print("Use at least 12 characters.")
        elif password != confirm:
            print("Passwords do not match.")
        else:
            break
    try:
        uid = create_admin_user(name, username, password)
    except ValueError as exc:
        print(f"Could not create administrator: {exc}")
        raise SystemExit(1)
    print(f"Administrator created (id: {uid}). Start CargoSync and sign in with {username}.")


if __name__ == "__main__":
    main()