#!/usr/bin/env python3
"""継続実行: nightly.py の既存の判定・予算・本番ガードを使い、台帳の待ちを消化する。

Windows のタスクスケジューラ等で 1 プロセスを起動する。中断時は次回同じサイクルを再開する。
実運用は --apply-prod の明示が必要。ログや状態ファイルに環境変数を出力しない。
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts' / 'llm'))
import filelock  # noqa: E402
sys.path.insert(0, str(ROOT / 'scripts'))
import nightly  # noqa: E402


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def next_cycle(directory, day):
    """枝なしは cron に予約。mkdir の原子性で複数実行の名前衝突を避ける。"""
    for n in range(2, 1_000_000):
        name = f'{day}-{n}'
        try:
            (directory / name).mkdir()
            return name
        except FileExistsError:
            continue
    raise RuntimeError('同日の枝番を確保できない')


def outcome(status, rc):
    """夜間ジョブが exit 0 でも、ガード等による中断を成功扱いしない。"""
    if not isinstance(status, dict):
        return 'status.json が無いか読めない'
    stages = {s['name']: s for s in status.get('stages', []) if isinstance(s, dict) and 'name' in s}
    if rc or status.get('ok') is not True or not status.get('finished_at'):
        return 'サイクルが失敗または未完了'
    if any(s.get('status') in ('failed', 'partial', 'warning') for s in stages.values()):
        return '失敗・未解決・画像段の警告あり'
    llm = stages.get('llm', {})
    if llm.get('status') == 'skipped' and (status.get('candidates_tonight', 0) or '予算ガード' in llm.get('note', '')):
        return 'LLM 判定がスキップされた（予算等を確認）'
    prod = stages.get('prod', {})
    if status.get('added', 0) and not status.get('prod_applied'):
        return '追加予定が本番に反映されなかった: ' + str(prod.get('note', ''))
    return None


def unsafe_to_resume(status):
    """投入中のプロセス停止は成否不明。再投入する前に手動で本番と bookmark を確認。"""
    if not isinstance(status, dict) or status.get('finished_at'):
        return False
    stages = {s.get('name'): s for s in status.get('stages', []) if isinstance(s, dict)}
    return ('gate' in stages and 'prod' not in stages and stages['gate'].get('status') == 'ok')


def nightly_running(directory):
    """再起動後に旧子プロセスがまだ動いていれば、状態を評価せず待つ。"""
    with (directory / '.lock').open('a+') as lock:
        try:
            filelock.lock(lock, blocking=False)
        except OSError:
            return True
        filelock.unlock(lock)
        return False


def work_ready(db):
    """対象が 0 件の回でも、取得待ちが残れば余計な待機を入れない。"""
    con = nightly.ro_connect(str(db))
    if con is None:
        raise RuntimeError('frontier 台帳が無い')
    try:
        due = now().isoformat(timespec='seconds')
        ready = con.execute("SELECT 1 FROM frontier f WHERE f.state IN ('discovered','fetched') OR "
                            "((f.state='queued' OR (f.state='failed' AND f.fetch_attempts<3)) "
                            "AND (f.next_fetch_at IS NULL OR f.next_fetch_at<=?)) LIMIT 1", (due,)).fetchone()
    finally:
        con.close()
    # nightly は正規化した cand_id で判定済みを照合する（raw key ではない）。
    return ready is not None or bool(nightly.unjudged_candidates(str(db)))


def discovery_succeeded(status):
    stages = {s.get('name'): s for s in status.get('stages', []) if isinstance(s, dict)}
    if stages.get('discover', {}).get('status') != 'ok':
        return False
    return not any('discover-' in str(g).lower() or 'wikidata' in str(g).lower()
                   for g in status.get('guards', []))


def budget_paused(status):
    if not isinstance(status, dict) or status.get('ok') is not True or not status.get('finished_at'):
        return False
    stages = {s.get('name'): s for s in status.get('stages', []) if isinstance(s, dict)}
    llm = stages.get('llm', {})
    return (llm.get('status') == 'skipped' and '予算ガード' in llm.get('note', '')
            and not any(s.get('status') in ('failed', 'partial', 'warning') for s in stages.values())
            and (not status.get('added', 0) or status.get('prod_applied') is True))


def discovery_due(state, stamp, hours):
    try:
        last = datetime.datetime.fromisoformat(state['last_discovery'])
        return (stamp - last).total_seconds() >= hours * 3600
    except (KeyError, ValueError, TypeError):
        return True


def run(args):
    nightly_dir = Path(args.var_dir).resolve() / 'nightly'
    nightly_dir.mkdir(parents=True, exist_ok=True)
    state_file = nightly_dir / 'continuous.json'
    # 外側のロックは .lock と別物。nightly.py は従来の .lock だけを取得する。
    with (nightly_dir / '.continuous.lock').open('a+') as lock:
        try:
            filelock.lock(lock, blocking=False)
        except OSError:
            print('継続処理は既に実行中', file=sys.stderr)
            return 75
        state = nightly.read_json(str(state_file)) if state_file.exists() else {}
        if not isinstance(state, dict):
            raise RuntimeError('continuous.json を読めない')
        if state.get('stopped'):
            print('停止中: ' + state['stopped'], file=sys.stderr)
            return 1
        while True:
            budget_month = state.get('budget_pause_month')
            only_discover = state.get('active_discovery_only', False) if state.get('active') else False
            if budget_month:
                if now().strftime('%Y-%m') == budget_month:
                    # 予算待ちの間も定期発見は続ける（Claude API と本番反映は使わない）。
                    if not state.get('active') and discovery_due(state, now(), args.discovery_hours):
                        only_discover = True
                    elif not only_discover:
                        print('月予算ガード中。次の発見または翌 UTC 月まで待機', flush=True)
                        if args.once:
                            return 0
                        time.sleep(args.idle_seconds)
                        continue
                else:
                    state.pop('budget_pause_month', None)
                    nightly.write_json(str(state_file), state)
            if nightly_running(nightly_dir):
                print('別の nightly が実行中。待って再確認', flush=True)
                time.sleep(args.wait_seconds)
                continue
            name = state.get('active')
            stamp = now()
            if not name:
                name = next_cycle(nightly_dir, stamp.strftime('%Y-%m-%d'))
                # 既存の発見源も周期実行。その他のサイクルは台帳の処理待ちに集中する。
                discover = only_discover or discovery_due(state, stamp, args.discovery_hours)
                state.update(active=name, active_discovery=discover, active_discovery_only=only_discover, started_at=stamp.isoformat())
                nightly.write_json(str(state_file), state)
            else:
                discover = state.get('active_discovery', False)
            status_path = nightly_dir / name / 'status.json'
            old = nightly.read_json(str(status_path))
            if unsafe_to_resume(old):
                state['stopped'] = f'{name}: 本番投入の成否を確認してから再開'
                nightly.write_json(str(state_file), state)
                print(state['stopped'], file=sys.stderr)
                return 1
            if isinstance(old, dict) and old.get('finished_at'):
                rc = 0 if old.get('ok') else 1
            else:
                cmd = [sys.executable, str(ROOT / 'scripts' / 'nightly.py'), '--date', name,
                       '--var-dir', str(Path(args.var_dir).resolve()), '--db', str(Path(args.db).resolve()),
                       '--fetch-limit', str(args.fetch_limit), '--judge-limit', str(args.judge_limit),
                       '--budget-month', str(args.budget_month), '--discover-limit', str(args.discover_limit),
                       '--cc-sample-every', str(args.cc_sample_every), '--wikidata-leaves', str(args.wikidata_leaves),
                       '--anomaly-factor', str(args.anomaly_factor), '--media-favicon-limit', str(args.media_favicon_limit)]
                if only_discover:
                    cmd.append('--discover-only')
                elif args.apply_prod:
                    cmd.append('--apply-prod')
                if not discover:
                    cmd.append('--skip-discovery')
                print('サイクル開始:', name, '発見:', discover, flush=True)
                rc = subprocess.run(cmd, cwd=ROOT).returncode
                if rc == 75:
                    print('別の nightly が実行中。待って再試行', flush=True)
                    time.sleep(args.wait_seconds)
                    continue
            status = nightly.read_json(str(status_path))
            if rc == 0 and budget_paused(status):
                state.update(active=None, last_completed=name, budget_pause_month=name[:7])
                nightly.write_json(str(state_file), state)
                print('月予算ガード:', name, flush=True)
                if args.once:
                    return 0
                continue
            reason = outcome(status, rc)
            if reason:
                state['stopped'] = f'{name}: {reason}'
                nightly.write_json(str(state_file), state)
                print(state['stopped'], file=sys.stderr)
                return 1
            if discover:
                # 失敗や 429 の際も次の試行まで間隔を置く。成功日時は別に記録する。
                state['last_discovery'] = stamp.isoformat()
                if discovery_succeeded(status):
                    state['last_discovery_success'] = stamp.isoformat()
                    state.pop('discovery_error_cycle', None)
                else:
                    state['discovery_error_cycle'] = name
            state.update(active=None, active_discovery_only=False, last_completed=name, last_completed_at=now().isoformat())
            nightly.write_json(str(state_file), state)
            print('サイクル完了:', name, flush=True)
            if args.once:
                return 0
            # 候補が尽きた時も間隔を置いて次の発見・期限到来分を確認する。
            if only_discover or (status.get('candidates_tonight', 0) == 0 and status.get('added', 0) == 0 and not work_ready(args.db)):
                time.sleep(args.idle_seconds)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--var-dir', default=str(ROOT / 'var'))
    ap.add_argument('--db', default=str(ROOT / 'var' / 'frontier.sqlite'))
    ap.add_argument('--apply-prod', action='store_true')
    ap.add_argument('--fetch-limit', type=int, default=1500)
    ap.add_argument('--judge-limit', type=int, default=1500)
    ap.add_argument('--budget-month', type=float, default=100.0)
    ap.add_argument('--discover-limit', type=int, default=3000)
    ap.add_argument('--cc-sample-every', type=int, default=100)
    ap.add_argument('--wikidata-leaves', type=int, default=20)
    ap.add_argument('--anomaly-factor', type=float, default=nightly.ANOMALY_FACTOR)
    ap.add_argument('--media-favicon-limit', type=int, default=0)
    ap.add_argument('--discovery-hours', type=float, default=6)
    ap.add_argument('--idle-seconds', type=int, default=1800)
    ap.add_argument('--wait-seconds', type=int, default=60)
    ap.add_argument('--once', action='store_true')
    a = ap.parse_args(argv)
    if not a.apply_prod:
        ap.error('継続運転には --apply-prod を明示する')
    if min(a.fetch_limit, a.judge_limit, a.discovery_hours, a.idle_seconds, a.wait_seconds, a.budget_month, a.anomaly_factor) <= 0:
        ap.error('件数・予算・待ち時間は正の値が必要')
    if a.discover_limit < 0 or a.wikidata_leaves < 0 or a.cc_sample_every < 1 or not 0 <= a.media_favicon_limit <= 100:
        ap.error('発見・画像取得の設定が不正')
    try:
        return run(a)
    except (OSError, ValueError, RuntimeError) as e:
        print(f'継続処理エラー: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
