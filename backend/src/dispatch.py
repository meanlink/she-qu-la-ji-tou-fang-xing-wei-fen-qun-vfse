from fastapi import HTTPException

from src.clustering import fit
from src.fieldwork import get_point, required
from src.snapshot import collect_snapshot, reason_summary
from src.storage import encode, register

ENTRY_UPSERT = '''INSERT INTO dispatch_entries
    (run_id,point_id,point_revision,plan_version,state,cohort,goal,focus_slot,baseline,baseline_windows,
     fingerprint,reason_code,reason,task_id,updated_by)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON DUPLICATE KEY UPDATE point_revision=VALUES(point_revision),plan_version=VALUES(plan_version),
    state=VALUES(state),cohort=VALUES(cohort),goal=VALUES(goal),focus_slot=VALUES(focus_slot),
    baseline=VALUES(baseline),baseline_windows=VALUES(baseline_windows),fingerprint=VALUES(fingerprint),
    reason_code=VALUES(reason_code),reason=VALUES(reason),task_id=VALUES(task_id),updated_by=VALUES(updated_by)'''


def _members(result):
    """Yield (cohort number, member) for every point in a plan."""
    for group in result['groups']:
        for member in group['members']:
            yield group['number'], member


def _baseline_windows(snapshot, point_id, slot):
    point = next((item for item in snapshot['points'] if item['id'] == point_id), None)
    return [window['id'] for window in (point or {}).get('windows', []) if window['slot'] == slot]


def _active_task(ledger, point_id):
    return ledger.one('''SELECT id,state FROM outreach_tasks WHERE point_id=%s
        AND state IN ('assigned','retesting') ORDER BY id LIMIT 1''', (point_id,))


def _run_task(ledger, run_id, point_id):
    return ledger.one('SELECT id FROM outreach_tasks WHERE run_id=%s AND point_id=%s', (run_id, point_id))


def _entry(ledger, run_id, point_id):
    return ledger.one('SELECT * FROM dispatch_entries WHERE run_id=%s AND point_id=%s', (run_id, point_id))


def _write_entry(ledger, run, snapshot, member, state, actor, *, cohort=None, action=None,
                 reason_code=None, reason=None, task_id=None):
    """Freeze the plan version that governs this point's dispatch state."""
    ledger.execute(ENTRY_UPSERT, (
        run['id'], member['point_id'], member['revision'], run['version'], state, cohort,
        action['goal'] if action else None, action['focus_slot'] if action else None,
        encode(action) if action else None,
        encode(_baseline_windows(snapshot, member['point_id'], action['focus_slot'])) if action else None,
        run['fingerprint'], reason_code, reason, task_id, actor))


def _skipped(ledger, run, snapshot, member, actor, code, reason, action=None, cohort=None):
    _write_entry(ledger, run, snapshot, member, 'skipped', actor,
                 cohort=cohort, action=action, reason_code=code, reason=reason)


def _insert_task(ledger, run_id, cohort, member, action, actor):
    """Create the batch task; (run_id, point_id) is unique and callers check first."""
    task_id = ledger.execute('''INSERT INTO outreach_tasks
        (run_id,point_id,point_revision,cohort,goal,focus_slot,baseline) VALUES (%s,%s,%s,%s,%s,%s,%s)''',
        (run_id, member['point_id'], member['revision'], cohort, action['goal'], action['focus_slot'],
         encode(action)))
    ledger.audit(actor, 'outreach_assigned', task_id, {'run_id': run_id, 'point_id': member['point_id']})
    return task_id


def _save_plan(ledger, run_id, snapshot, checksum, result, version, actor):
    version = version or ledger.one(
        'SELECT COALESCE(MAX(version),0) AS v FROM dispatch_plans WHERE run_id=%s', (run_id,))['v'] + 1
    ledger.execute('''INSERT INTO dispatch_plans (run_id,version,fingerprint,computed_by,plan)
        VALUES (%s,%s,%s,%s,%s)''',
        (run_id, version, checksum, actor, encode({'snapshot': snapshot, 'result': result})))
    return {'id': run_id, 'version': version, 'fingerprint': checksum,
            'snapshot': snapshot, 'result': result}


def _latest_plan(ledger, run_id):
    row = ledger.one('''SELECT version,fingerprint,plan FROM dispatch_plans
        WHERE run_id=%s ORDER BY version DESC LIMIT 1''', (run_id,))
    if row is None:
        return None
    return {'id': run_id, 'version': row['version'], 'fingerprint': row['fingerprint'],
            'snapshot': row['plan']['snapshot'], 'result': row['plan']['result']}


def seed_plan(ledger, run, actor):
    """First plan: free points take tasks now; points with unfinished tasks wait in this batch."""
    plan = _save_plan(ledger, run['id'], run['input_snapshot'], run['fingerprint'], run['result'], 1, actor)
    created, waiting, skipped = [], [], []
    for cohort, member in _members(plan['result']):
        action = member['action']
        if not action:
            _skipped(ledger, plan, plan['snapshot'], member, actor, 'no_action', '本批无宣教建议')
            skipped.append(member['point_id'])
            continue
        active = _active_task(ledger, member['point_id'])
        if active:
            _write_entry(ledger, plan, plan['snapshot'], member, 'pending', actor,
                         cohort=cohort, action=action, reason_code='active_task',
                         reason=f"旧任务 #{active['id']} 进行中，留在本批待派")
            waiting.append(member['point_id'])
            continue
        existing = _run_task(ledger, run['id'], member['point_id'])
        task_id = existing['id'] if existing else _insert_task(ledger, run['id'], cohort, member, action, actor)
        _write_entry(ledger, plan, plan['snapshot'], member, 'dispatched', actor,
                     cohort=cohort, action=action, task_id=task_id)
        if not existing:
            created.append(task_id)
    return {'created': created, 'waiting': waiting, 'skipped': skipped}


def _dispatch(ledger, run, actor, note, only_point=None):
    """Dispatch pending entries from the latest plan. Caller holds the write lock."""
    plan = _latest_plan(ledger, run['id'])
    if plan is None:
        raise HTTPException(409, '尚未生成待派方案，请先重算')
    ledger.rows('SELECT * FROM dispatch_entries WHERE run_id=%s FOR UPDATE', (run['id'],))
    created, waiting, skipped = [], [], []
    for cohort, member in _members(plan['result']):
        point_id = member['point_id']
        if only_point is not None and point_id != only_point:
            continue
        entry = _entry(ledger, run['id'], point_id)
        if entry and entry['state'] == 'dispatched':
            continue
        action = member['action']
        if not action:
            _skipped(ledger, plan, plan['snapshot'], member, actor, 'no_longer_eligible',
                     '最新方案不再达到宣教条件', cohort=cohort)
            skipped.append(point_id)
            continue
        point = get_point(ledger, point_id)
        if point['pending_location']:
            _skipped(ledger, plan, plan['snapshot'], member, actor, 'migration_pending',
                     '迁移待核，暂不派发', action=action, cohort=cohort)
            skipped.append(point_id)
            continue
        if point['revision'] != member['revision']:
            _skipped(ledger, plan, plan['snapshot'], member, actor, 'point_moved',
                     f"地址已由 V{member['revision']} 更新为 V{point['revision']}", action=action, cohort=cohort)
            skipped.append(point_id)
            continue
        active = _active_task(ledger, point_id)
        if active:
            _write_entry(ledger, plan, plan['snapshot'], member, 'pending', actor, cohort=cohort,
                         action=action, reason_code='active_task',
                         reason=f"旧任务 #{active['id']} 进行中，留在本批待派")
            waiting.append(point_id)
            continue
        existing = _run_task(ledger, run['id'], point_id)
        task_id = existing['id'] if existing else _insert_task(ledger, run['id'], cohort, member, action, actor)
        _write_entry(ledger, plan, plan['snapshot'], member, 'dispatched', actor,
                     cohort=cohort, action=action, task_id=task_id)
        if not existing:
            created.append(task_id)
    if only_point is None:
        for entry in ledger.rows("SELECT * FROM dispatch_entries WHERE run_id=%s AND state='pending'",
                                 (run['id'],)):
            if entry['point_id'] in waiting:
                continue
            _skipped(ledger, plan, plan['snapshot'],
                     {'point_id': entry['point_id'], 'revision': entry['point_revision']}, actor,
                     'no_longer_eligible', '最新方案不再达到宣教条件')
            skipped.append(entry['point_id'])
    ledger.audit(actor, 'dispatch_topped_up', run['id'],
                 {'version': plan['version'], 'created': created, 'pending': waiting,
                  'skipped': skipped, 'note': note})
    return created, board_for(ledger, run, created=len(created))


def recompute_plan(run_id, form, actor):
    """Rebuild pending entries from whole-batch sampling; dispatched entries keep their basis."""
    with register(write=True) as ledger:
        run = required(ledger, 'SELECT * FROM behavior_runs WHERE id=%s', run_id, '分群运行')
        if run['state'] != 'confirmed':
            raise HTTPException(409, '只有已确认的分群可以重算待派方案')
        snapshot, checksum, reasons = collect_snapshot(ledger, run['from_day'], run['through_day'],
                                                       run['requested_clusters'])
        if reasons:
            raise HTTPException(409, reason_summary(reasons))
        try:
            result = fit(snapshot['points'], run['requested_clusters'])
        except (ArithmeticError, ValueError, KeyError) as error:
            raise HTTPException(409, f'重算失败：{type(error).__name__}')
        if result is None:
            raise HTTPException(409, '不同点位画像少于所选群数，无法重算待派方案')
        plan = _save_plan(ledger, run_id, snapshot, checksum, result, None, actor)
        entries = {row['point_id']: row for row in
                   ledger.rows('SELECT * FROM dispatch_entries WHERE run_id=%s', (run_id,))}
        planned, pending, skipped = set(), [], []
        for cohort, member in _members(result):
            planned.add(member['point_id'])
            entry = entries.get(member['point_id'])
            if entry and entry['state'] == 'dispatched':
                continue
            action = member['action']
            if not action:
                _skipped(ledger, plan, snapshot, member, actor, 'no_longer_eligible',
                         '最新方案不再达到宣教条件' if entry else '本批无宣教建议', cohort=cohort)
                skipped.append(member['point_id'])
                continue
            active = _active_task(ledger, member['point_id'])
            _write_entry(ledger, plan, snapshot, member, 'pending', actor, cohort=cohort, action=action,
                         reason_code='active_task' if active else 'awaiting_top_up',
                         reason=(f"旧任务 #{active['id']} 进行中，留在本批待派" if active else '可按最新方案补派'))
            pending.append(member['point_id'])
        for point_id, entry in entries.items():
            if point_id in planned or entry['state'] == 'dispatched':
                continue
            _skipped(ledger, plan, snapshot, {'point_id': point_id, 'revision': entry['point_revision']},
                     actor, 'no_longer_eligible', '最新方案不再达到宣教条件')
            skipped.append(point_id)
        ledger.audit(actor, 'dispatch_planned', run_id,
                     {'version': plan['version'], 'fingerprint': checksum, 'pending': pending,
                      'skipped': skipped, 'note': form.note})
        return board_for(ledger, run)


def dispatch_pending(run_id, form, actor):
    """Top up from the latest plan. Safe to retry and safe for two operators to race."""
    with register(write=True) as ledger:
        run = required(ledger, 'SELECT * FROM behavior_runs WHERE id=%s', run_id, '分群运行')
        if run['state'] != 'confirmed':
            raise HTTPException(409, '只有已确认的分群可以补派')
        _, result = _dispatch(ledger, run, actor, form.note)
        return result


def skip_pending_for_point(ledger, point_id, actor, code, reason):
    """Close pending entries when a point can no longer receive that batch's plan."""
    for row in ledger.rows('''SELECT e.run_id,e.point_revision FROM dispatch_entries e
            JOIN behavior_runs r ON r.id=e.run_id
            WHERE e.point_id=%s AND e.state='pending' AND r.state='confirmed' ORDER BY e.run_id''', (point_id,)):
        plan = _latest_plan(ledger, row['run_id'])
        _skipped(ledger, plan, plan['snapshot'],
                 {'point_id': point_id, 'revision': row['point_revision']}, actor, code, reason)
        ledger.audit(actor, 'dispatch_skipped', row['run_id'],
                     {'point_id': point_id, 'reason_code': code, 'reason': reason})


def top_up_after_task(ledger, point_id, actor):
    """When an old task ends, top the point up once from the newest plan still waiting on it."""
    if _active_task(ledger, point_id):
        return []
    waiting = ledger.rows('''SELECT DISTINCT r.id FROM dispatch_entries e JOIN behavior_runs r ON r.id=e.run_id
        WHERE e.point_id=%s AND e.state='pending' AND r.state='confirmed' ORDER BY r.id DESC''', (point_id,))
    if not waiting:
        return []
    newest = required(ledger, 'SELECT * FROM behavior_runs WHERE id=%s', waiting[0]['id'], '分群运行')
    task_ids, _ = _dispatch(ledger, newest, actor, f'旧任务结束，点位 #{point_id} 按最新方案补派',
                            only_point=point_id)
    # The newest plan has now resolved this point (dispatched or skipped); older waiters are stale.
    for older in waiting[1:]:
        entry = _entry(ledger, older['id'], point_id)
        if entry is None or entry['state'] != 'pending':
            continue
        plan = _latest_plan(ledger, older['id'])
        _skipped(ledger, plan, plan['snapshot'], {'point_id': point_id, 'revision': entry['point_revision']},
                 actor, 'superseded_by_later_run', f"已按分群 #{newest['id']} 的较新方案处理")
    return task_ids


def board_for(ledger, run, created=0):
    run_id = run['id']
    plans = ledger.rows('''SELECT version,fingerprint,computed_by,computed_at FROM dispatch_plans
        WHERE run_id=%s ORDER BY version DESC''', (run_id,))
    entries = ledger.rows('''SELECT e.*,p.code,p.name,p.revision AS current_revision,p.pending_location,
        t.state AS task_state FROM dispatch_entries e JOIN disposal_points p ON p.id=e.point_id
        LEFT JOIN outreach_tasks t ON t.id=e.task_id WHERE e.run_id=%s ORDER BY p.code''', (run_id,))
    counts = {state: sum(1 for entry in entries if entry['state'] == state)
              for state in ('pending', 'dispatched', 'skipped')}
    return {'run_id': run_id, 'state': run['state'], 'created': created,
            'latest_version': plans[0]['version'] if plans else None,
            'plans': plans, 'entries': entries, 'counts': counts}


def board(run_id):
    with register() as ledger:
        run = required(ledger, 'SELECT * FROM behavior_runs WHERE id=%s', run_id, '分群运行')
        return board_for(ledger, run)
