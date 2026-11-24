"""Poll the online D1 media queue; this process keeps no local queue or cursor."""
import argparse
import sys
import time

import nightly_media as media


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--favicon-limit', type=int, default=100)
    ap.add_argument('--thumbnail-limit', type=int, default=10)
    ap.add_argument('--interval', type=int, default=300)
    ap.add_argument('--once', action='store_true')
    a = ap.parse_args(argv)
    if sys.platform != 'win32' or not 0 <= a.favicon_limit <= 100 or not 0 <= a.thumbnail_limit <= 100 or a.interval < 60:
        ap.error('Windows 専用。各画像の上限は 0–100、間隔は 60 秒以上')
    while True:
        rc = media.main(['--favicon-limit', str(a.favicon_limit), '--thumbnail-limit', str(a.thumbnail_limit)])
        if rc or a.once:
            return rc
        time.sleep(a.interval)


if __name__ == '__main__':
    sys.exit(main())
