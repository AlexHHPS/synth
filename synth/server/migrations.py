"""Run schema initialization using separately supplied migration credentials."""
from .db import migrate

if __name__ == '__main__':
    migrate()
    print('Schema initialized; use a restricted runtime role for API and worker.')
