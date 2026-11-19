"""ファビコンの R2 キーを「内容ハッシュ名」から「サイト固定のキー」へ移す一回きりの移行(Issue #163)。本番の切替用。

入力は台帳(複数の --db。取得を分割した台帳ぜんぶ)と、内容ハッシュ名のローカル画像(--store-dir。既定 var/favicons/)。サイトには一切アクセスしない。
既定は全部 dry-run(計画の表示だけ)。R2 へ書く/消すのは `--apply` を付けたときだけで、**本番は `--apply --r2-remote` を明示したときだけ**(無ければ wrangler の --local 模擬)。
冪等: 置いた印(台帳の icon_put)があれば飛ばす。途中で止まっても同じコマンドを再実行すればよい。失敗は --retries 回まで再試行して集計し、残りは次回に回る。

  plan        移行の計画(サイト数・置く画像・元の画像が無い/壊れているもの)を表示
  upload      サイト固定のキー(icon/<sha256(site_id)先頭32桁>.webp)で R2 に置く(--jobs 並列)。置いたら台帳に印を付け、手元の var/favicons/ にも同じキーで写す
  sql         D1 切替の UPDATE(sites.favicon_path / favicon_v)。R2 に置いた印のあるサイトだけ出す
  list-old    削除対象の旧キー(内容ハッシュ名)の一覧
  delete-old  旧キーを R2 から消す。D1 の切替と配信の確認が済んでから 1 回だけ(--d1-done 必須)。新キーが置いてあるサイトからしか参照されない旧名だけ消す

使い方の順序(コーディネーター): 新 Worker をデプロイ → `upload --apply --r2-remote` → `sql --out x.sql` を D1 に適用 → 配信を確認 → `delete-old --apply --r2-remote --d1-done`。
実行: uv run --with pillow python3 scripts/favicon/rekey.py --db var/favicons.sqlite --db var/favicons-shard-0.sqlite plan
"""
import argparse
import hashlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import favicon  # noqa: E402

ROOT = favicon.ROOT
DEFAULT_JOBS = 8
DEFAULT_RETRIES = 2


class Item:
    """1 サイト分の移行。content は内容ハッシュ名(旧キー)、name は新キー(サイト固定)、v は favicon_v。"""
    def __init__(self, site_id, db_path, content, checked):
        self.site_id = site_id; self.db_path = db_path; self.content = content; self.checked = checked or ''
        self.name = favicon.site_name(site_id); self.v = favicon.icon_v(content)


def collect(db_paths):
    """台帳(読み取り専用)から、移行するサイト(status ok・画像あり・除外でない)を集める。同じサイトが複数の台帳にあれば、最後に確認した方を採る。
    戻り: (items {site_id: Item}, dups 件数, old_names 旧キー候補の集合)。old_names は icon_hash と、旧台帳が R2 本番に置いた名前(icon_stored)の和集合。"""
    items = {}; dups = 0; old = set()
    for p in db_paths:
        db = favicon.open_readonly(p)
        if db is None:
            raise SystemExit(f'台帳がない: {p}')
        try:
            ex = ' AND exclude=0' if favicon._has_exclude(db) else ''
            for r in db.execute(f"SELECT site_id, icon_hash, last_checked_at FROM favicons WHERE status='ok' AND icon_hash IS NOT NULL{ex} ORDER BY site_id"):
                old.add(r['icon_hash'])
                it = Item(r['site_id'], str(p), r['icon_hash'], r['last_checked_at'])
                cur = items.get(it.site_id)
                if cur is not None:
                    dups += 1
                if cur is None or it.checked > cur.checked:
                    items[it.site_id] = it
            try:
                old.update(r[0] for r in db.execute("SELECT name FROM icon_stored WHERE store='r2-remote'"))
            except Exception:   # 旧い表が無い台帳
                pass
        finally:
            db.close()
    new_names = {i.name for i in items.values()}
    return items, dups, {n for n in old if n not in new_names}   # 新キーと同じ名前は(偶然でも)旧キーとして消さない


def read_source(store_dir, it):
    """内容ハッシュ名のローカル画像を読む。戻り: (bytes, None) または (None, 理由)。中身のハッシュが名前と合わなければ壊れている扱い。"""
    f = Path(store_dir) / it.content
    if not f.is_file():
        return None, 'missing_source'
    data = f.read_bytes()
    if not data:
        return None, 'empty_source'
    if favicon.icon_name(data) != it.content:
        return None, 'bad_source'   # 名前の内容ハッシュと一致しない(壊れている・別の画像)
    return data, None


def _writable(handles, path):
    if path not in handles:
        handles[path] = favicon.connect(path)   # 印の表を持たない旧い台帳には connect() が自動で足す
    return handles[path]


def _marked(db_path, it, label):
    db = favicon.open_readonly(db_path)
    try:
        return favicon._marked(db, it.site_id, label, it.content)
    finally:
        db.close()


def upload(items, store, store_dir, local_dir=None, apply=False, jobs=DEFAULT_JOBS, retries=DEFAULT_RETRIES, sleep=time.sleep, log=print):
    """サイト固定のキーで保存先に置く。戻り: 集計 dict(todo/uploaded/skipped_marked/missing_source/bad_source/failed)。"""
    stats = {'todo': 0, 'uploaded': 0, 'skipped_marked': 0, 'missing_source': 0, 'empty_source': 0, 'bad_source': 0, 'failed': 0}
    todo = []
    for it in sorted(items.values(), key=lambda i: i.site_id):
        if _marked(it.db_path, it, store.label):
            stats['skipped_marked'] += 1; continue   # 置いた印がある(再実行で飛ばす)
        data, why = read_source(store_dir, it)
        if why:
            stats[why] += 1; log(f'{it.site_id}\t{why}\t{it.content}'); continue
        todo.append((it, data))
    stats['todo'] = len(todo)
    log(f'{"upload" if apply else "[dry-run] upload"} {len(todo)} 件 -> {store.label} (印あり {stats["skipped_marked"]}、元画像なし/壊れ {stats["missing_source"] + stats["empty_source"] + stats["bad_source"]})')
    if not apply:
        return stats

    def put(it, data):
        err = None
        for n in range(retries + 1):
            try:
                store.put(it.name, data)
                return None
            except OSError as e:
                err = e
                if n < retries:
                    sleep(1 + n)
        return err

    handles = {}
    try:
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
            futs = {ex.submit(put, it, data): (it, data) for it, data in todo}
            for fu in as_completed(futs):
                it, data = futs[fu]
                err = fu.result()
                if err is not None:
                    stats['failed'] += 1; log(f'{it.site_id}\tupload_failed\t{type(err).__name__}'); continue
                db = _writable(handles, it.db_path)   # 台帳への書き込みはこのスレッドだけ
                favicon._mark(db, it.site_id, store.label, it.content)
                if local_dir is not None:   # 手元の保存先にも新キーで写す(夜間ジョブの取り直しを無駄に起こさない)
                    favicon.LocalDirStore(local_dir).put(it.name, data)
                    favicon._mark(db, it.site_id, 'local', it.content)
                db.commit(); stats['uploaded'] += 1
    finally:
        for h in handles.values():
            h.close()
    return stats


def sql_text(items, label='r2-remote'):
    """D1 切替の UPDATE。置いた印(その保存先に現在の内容)のあるサイトだけ。favicon.sql_updates と同じ形で、favicon_v が違うときだけ書く。"""
    lines = []
    for it in sorted(items.values(), key=lambda i: i.site_id):
        if _marked(it.db_path, it, label):
            lines.append(f"UPDATE sites SET favicon_path={favicon._q(it.name)}, favicon_v={favicon._q(it.v)} WHERE id={favicon._q(it.site_id)} AND favicon_v IS NOT {favicon._q(it.v)};")
    return '\n'.join(lines) + ('\n' if lines else '')


def delete_old(items, old_names, store, label, verify=False, apply=False, jobs=DEFAULT_JOBS, retries=DEFAULT_RETRIES, sleep=time.sleep, log=print):
    """旧キー(内容ハッシュ名)を保存先から消す。その名前を使っていたサイトが全部、新キーに置いた印を持つ名前だけ消す(verify なら新キーが実在することも確かめる)。
    戻り: 集計 dict(todo/deleted/kept_unplaced/failed)。"""
    refs = {}
    for it in items.values():
        refs.setdefault(it.content, []).append(it)
    stats = {'todo': 0, 'deleted': 0, 'kept_unplaced': 0, 'failed': 0}
    todo = []
    for name in sorted(old_names):
        ok = True
        for it in refs.get(name, []):
            if not _marked(it.db_path, it, label) or (verify and not store.exists(it.name)):
                ok = False; break
        if ok:
            todo.append(name)
        else:
            stats['kept_unplaced'] += 1; log(f'{name}\tkept (新キーが未配置のサイトが参照)')
    stats['todo'] = len(todo)
    log(f'{"delete-old" if apply else "[dry-run] delete-old"} {len(todo)} 件 <- {store.label} (新キー未配置で残す {stats["kept_unplaced"]})')
    if not apply:
        return stats

    def rm(name):
        err = None
        for n in range(retries + 1):
            try:
                store.delete(name)
                return None
            except OSError as e:
                err = e
                if n < retries:
                    sleep(1 + n)
        return err

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futs = {ex.submit(rm, n): n for n in todo}
        for fu in as_completed(futs):
            err = fu.result()
            if err is not None:
                stats['failed'] += 1; log(f'{futs[fu]}\tdelete_failed\t{type(err).__name__}')
            else:
                stats['deleted'] += 1
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', action='append', required=True, help='ファビコンの台帳(複数回指定可。var/favicons*.sqlite を 1 本ずつ)。plan/sql/list-old は読み取りのみ。upload --apply は印を書く')
    ap.add_argument('--store-dir', default=str(favicon.DEFAULT_STORE), help='内容ハッシュ名のローカル画像の置き場(既定 var/favicons/)')
    ap.add_argument('--jobs', type=int, default=DEFAULT_JOBS); ap.add_argument('--retries', type=int, default=DEFAULT_RETRIES)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('plan', help='移行の計画を表示')
    p = sub.add_parser('upload', help='サイト固定のキーで R2 に置く(--apply が無ければ計画だけ。本番は --apply --r2-remote)')
    p.add_argument('--apply', action='store_true'); p.add_argument('--r2-remote', action='store_true'); p.add_argument('--no-local-copy', action='store_true', help='手元の --store-dir に新キーの写しを作らない')
    p = sub.add_parser('sql', help='D1 切替の UPDATE を出す(R2 に置いた印のあるサイトだけ)')
    p.add_argument('--out'); p.add_argument('--store', choices=favicon.STORE_LABELS, default='r2-remote')
    p = sub.add_parser('list-old', help='削除対象の旧キーの一覧'); p.add_argument('--out')
    p = sub.add_parser('delete-old', help='旧キーを R2 から消す(D1 切替の確認後に 1 回だけ。--apply が無ければ計画だけ。本番は --apply --r2-remote --d1-done)')
    p.add_argument('--apply', action='store_true'); p.add_argument('--r2-remote', action='store_true'); p.add_argument('--d1-done', action='store_true', help='D1 の favicon_path/favicon_v を新キーに切り替え、配信を確認した')
    p.add_argument('--verify', action='store_true', help='新キーが R2 に実在することを 1 件ずつ確かめてから消す(遅い)')
    a = ap.parse_args(argv)
    items, dups, old = collect(a.db)
    if a.cmd == 'plan':
        miss = {'missing_source': 0, 'empty_source': 0, 'bad_source': 0}
        for it in items.values():
            _, why = read_source(a.store_dir, it)
            if why:
                miss[why] += 1
        print(f'ledgers={len(a.db)} sites={len(items)} duplicate_rows={dups} old_keys={len(old)} {miss}')
    elif a.cmd == 'upload':
        store = favicon.R2Store(remote=a.r2_remote, verify=True)   # verify は exists() 用。put は常に書く
        stats = upload(items, store, a.store_dir, None if a.no_local_copy else a.store_dir, a.apply, a.jobs, a.retries)
        print(stats)
        return 1 if stats['failed'] else 0
    elif a.cmd == 'sql':
        text = sql_text(items, a.store)
        if a.out:
            Path(a.out).write_text(text, encoding='utf-8'); print(f'wrote {a.out} ({text.count(chr(10))} 行)')
        else:
            sys.stdout.write(text)
    elif a.cmd == 'list-old':
        text = ''.join(f'icon/{n}\n' for n in sorted(old))
        if a.out:
            Path(a.out).write_text(text, encoding='utf-8'); print(f'wrote {a.out} ({len(old)} 件)')
        else:
            sys.stdout.write(text)
    else:
        if a.apply and not a.d1_done:
            print('旧キーを消す前に、`sql` の出力を D1 へ適用し、配信(/icon/<k>.webp?v=...)を確認する。済んでいたら --d1-done を付けて再実行する', file=sys.stderr)
            return 2
        store = favicon.R2Store(remote=a.r2_remote, verify=True)
        label = 'r2-remote' if a.r2_remote else 'r2-local'
        stats = delete_old(items, old, store, label, a.verify, a.apply, a.jobs, a.retries)
        print(stats)
        return 1 if stats['failed'] else 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
