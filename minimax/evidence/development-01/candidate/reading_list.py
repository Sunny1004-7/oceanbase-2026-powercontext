"""Reading list: status/tag selection and deterministic ordering."""
import argparse
import json
from pathlib import Path

STATUSES = ('unread', 'reading', 'done')

def _normalize_tag(tag):
    """Strip a query tag; blank or None means 'no tag filter'."""
    if tag is None:
        return None
    if not isinstance(tag, str):
        raise ValueError('tag must be a string')
    cleaned = tag.strip()
    return cleaned or None

def select_books(books, status=None, tag=None, **filters):
    """Select by status and (optionally) by an exact tag; filters intersect."""
    if status is not None and status not in STATUSES:
        raise ValueError('unknown status')
    wanted = _normalize_tag(tag)
    selected = []
    for book in books:
        if status is not None and book['status'] != status:
            continue
        if wanted is not None:
            tags = book.get('tags') or []
            if wanted not in tags:
                continue
        selected.append(book)
    return sorted(selected, key=lambda book: (book['added'], book['id']))

def main():
    parser = argparse.ArgumentParser(description='阅读清单')
    parser.add_argument('data', type=Path)
    parser.add_argument('--status', choices=STATUSES)
    parser.add_argument('--tag')
    args = parser.parse_args()
    books = json.loads(args.data.read_text(encoding='utf-8'))
    print(json.dumps(select_books(books, status=args.status, tag=args.tag), ensure_ascii=False))

if __name__ == '__main__':
    main()
