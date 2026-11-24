"""Process due D1 media jobs and store image bytes directly in R2."""
import argparse
import sqlite3
import sys

from media_online import Cloudflare, run_kind


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--favicon-limit', type=int, default=100)
    ap.add_argument('--thumbnail-limit', type=int, default=10)
    a = ap.parse_args(argv)
    if not all(0 <= n <= 100 for n in (a.favicon_limit, a.thumbnail_limit)):
        ap.error('各画像の上限は 0–100 件')
    if a.thumbnail_limit and sys.platform not in ('linux', 'win32'):
        ap.error('サムネイル撮影は Linux または Windows が必要')
    try:
        client = Cloudflare()
        for kind, limit in (('favicon', a.favicon_limit), ('thumbnail', a.thumbnail_limit)):
            if limit:
                count = run_kind(client, kind, limit)
                print(f'{kind}: processed {count} D1 jobs')
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(f'media: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
