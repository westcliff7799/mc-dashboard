"""Generate the ADMIN_PASSWORD_HASH value for .env.

    python -m app.hashpw

Reads the password from a hidden prompt so it never lands in shell history.
"""

import getpass
import sys

from .auth import hash_password


def main() -> int:
    password = getpass.getpass("New dashboard password: ")
    if len(password) < 12:
        print("Refusing: use at least 12 characters — this login faces the internet.")
        return 1
    if password != getpass.getpass("Confirm: "):
        print("Passwords did not match.")
        return 1

    print("\nAdd this line to your .env file:\n")
    print(f"ADMIN_PASSWORD_HASH={hash_password(password)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
