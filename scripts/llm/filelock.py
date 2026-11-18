"""ファイルロックの移植層（POSIX は fcntl.flock、Windows は msvcrt.locking）。

どちらも「開いたファイルの先頭 1 バイト」への排他ロック。プロセスが死ねば OS が解除する（残骸が残らない）。
  lock(f)                 取れるまで待つ
  lock(f, blocking=False) 取れなければ OSError
  unlock(f)               解除（close でも解除される）
"""
import os
import time

if os.name == "nt":
    import msvcrt

    def lock(f, blocking=True):
        while True:
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if not blocking:
                    raise
                time.sleep(0.1)

    def unlock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def lock(f, blocking=True):
        fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))

    def unlock(f):
        fcntl.flock(f, fcntl.LOCK_UN)
