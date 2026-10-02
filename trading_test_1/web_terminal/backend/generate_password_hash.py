"""
Run this once to generate the ADMIN_PASSWORD_HASH value for your .env file.

Usage:
    python generate_password_hash.py
"""
import getpass
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

if __name__ == "__main__":
    password = getpass.getpass("Choose a password for the web terminal admin login: ")
    confirm = getpass.getpass("Confirm password: ")

    if password != confirm:
        print("Passwords did not match. Try again.")
    else:
        hashed = pwd_context.hash(password)
        print("\nAdd this line to your .env file:\n")
        print(f"ADMIN_PASSWORD_HASH={hashed}\n")
