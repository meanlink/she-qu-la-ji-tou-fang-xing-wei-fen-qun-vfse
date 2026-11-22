from fastapi import HTTPException

from src.clustering import fit
from src.contracts import RunState
from src.dispatch import seed_plan
from src.fieldwork import required
from src.snapshot import collect_snapshot
from src.storage import encode, register


def get_run(ledger, run_id):
    return required(ledger, 'SELECT * FROM behavior_runs WHERE id=%s', run_id, '分群运行')


def queue_run(form, actor):
    with register(write=True) as ledger:
        snapshot, checksum, reasons = collect_snapshot(ledger, form.from_day, form.through_day, form.clusters)
        old = ledger.one('''SELECT id,reasons FROM behavior_runs WHERE fingerprint=%s AND state<>'failed'
            ORDER BY id DESC LIMIT 1''', (checksum,))
        # A stale snapshot can recur if an unconfirmed relocation is cleared.
        if old and all(reason['code'] != 'source_changed' for reason in old['reasons']):
            return get_run(ledger, old['id'])
        state = RunState.PAUSED if reasons else RunState.QUEUED
        run_id = ledger.execute('''INSERT INTO behavior_runs
            (from_day,through_day,requested_clusters,state,reasons,input_snapshot,fingerprint,requested_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''', (form.from_day, form.through_day, form.clusters, state,
                                                  encode(reasons), encode(snapshot), checksum, actor))
        ledger.audit(actor, f'run_{state}', run_id, {'fingerprint': checksum, 'reasons': reasons})
        return get_run(ledger, run_id)


def verify_source(ledger, run, actor):
    _, checksum, reasons = collect_snapshot(ledger, run['from_day'], run['through_day'], run['requested_clusters'])
    if checksum == run['fingerprint'] and not reasons:
        return True
    reasons.insert(0, {'code': 'source_changed'})
    ledger.execute("UPDATE behavior_runs SET state='paused',reasons=%s WHERE id=%s", (encode(reasons), run['id']))
    ledger.audit(actor, 'run_paused', run['id'], {'reasons': reasons})
    return False


def compute_next():
    with register(write=True) as ledger:
        run = ledger.one("SELECT * FROM behavior_runs WHERE state='queued' ORDER BY id LIMIT 1")
        if run is None or not verify_source(ledger, run, 'worker'):
            return
        try:
            result = fit(run['input_snapshot']['points'], run['requested_clusters'])
        except (ArithmeticError, ValueError, KeyError) as error:
            reasons = [{'code': 'calculation_failed'}]
            ledger.execute("UPDATE behavior_runs SET state='failed',reasons=%s WHERE id=%s", (encode(reasons), run['id']))
            ledger.audit('worker', 'run_failed', run['id'], {'error_type': type(error).__name__})
            return
        if result is None:
            reasons = [{'code': 'identical_profiles'}]
            ledger.execute("UPDATE behavior_runs SET state='paused',reasons=%s WHERE id=%s", (encode(reasons), run['id']))
            ledger.audit('worker', 'run_paused', run['id'], {'reasons': reasons})
            return
        ledger.execute("UPDATE behavior_runs SET state='review',result=%s WHERE id=%s", (encode(result), run['id']))
        ledger.audit('worker', 'run_computed', run['id'], {'groups': len(result['groups'])})


def confirm_run(run_id, form, actor):
    """Confirm the batch and dispatch every point that has no unfinished outreach task."""
    with register(write=True) as ledger:
        run = get_run(ledger, run_id)
        if run['state'] != RunState.REVIEW:
            raise HTTPException(409, '只有待运营确认的分群可发布')
        if not verify_source(ledger, run, actor):
            return {'id': run_id, 'state': RunState.PAUSED, 'created_tasks': 0, 'waiting_tasks': 0}
        outcome = seed_plan(ledger, run, actor)
        ledger.execute("UPDATE behavior_runs SET state='confirmed',reviewed_by=%s,review_note=%s WHERE id=%s",
                       (actor, form.note, run_id))
        ledger.audit(actor, 'run_confirmed', run_id,
                     {'task_ids': outcome['created'], 'waiting': outcome['waiting'], 'note': form.note})
        return {'id': run_id, 'state': RunState.CONFIRMED, 'created_tasks': len(outcome['created']),
                'waiting_tasks': len(outcome['waiting'])}
