"""Create the database and the seed accounts:  python -m e2_persistence.seed"""
import config
from .repository import Repository


def main():
    config.ensure_dirs()
    repo = Repository()
    repo.init()
    for username, password, role in config.SEED_USERS:
        try:
            repo.create_user(username, password, role)
            print(f"created {role}: {username}")
        except Exception:
            print(f"exists  {role}: {username}")
    repo.close()


if __name__ == "__main__":
    main()
