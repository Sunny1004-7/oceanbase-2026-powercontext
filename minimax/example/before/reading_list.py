"""Reading list baseline: status selection and deterministic ordering."""
import argparse
import json
from pathlib import Path

STATUSES = ('unread', 'reading', 'done')

def select_books(books, status=None, **filters):
    """Select by status; other filters are reserved for later CLI extensions."""
    if status is not None and status not in STATUSES:
        raise ValueError('unknown status')
    selected = [book for book in books if status is None or book['status'] == status]
    return sorted(selected, key=lambda book: (book['added'], book['id']))

def main():
    parser = argparse.ArgumentParser(description='阅读清单')
    parser.add_argument('data', type=Path)
    parser.add_argument('--status', choices=STATUSES)
    args = parser.parse_args()
    books = json.loads(args.data.read_text(encoding='utf-8'))
    print(json.dumps(select_books(books, status=args.status), ensure_ascii=False))

if __name__ == '__main__':
    main()
