from collections import defaultdict
from hashlib import sha256

from src.storage import encode

MIN_ANONYMOUS = 30
MIN_WINDOWS = 2
MAX_POINTS = 80


def collect_snapshot(ledger, start, end, clusters):
    """Freeze the whole-batch baseline used by a run or a dispatch plan."""
    points = ledger.rows('''SELECT id,code,name,location,revision,pending_location
        FROM disposal_points ORDER BY id''')
    windows = defaultdict(list)
    reviews = []
    for row in ledger.rows('''SELECT w.id,w.point_id,w.sample_day,w.slot,w.total,w.correct,w.overflow,q.state AS quality_state,p.code
        FROM sampling_windows w JOIN disposal_points p ON p.id=w.point_id AND p.revision=w.point_revision
        LEFT JOIN sample_challenges q ON q.window_id=w.id
        WHERE w.task_id IS NULL AND w.sample_day BETWEEN %s AND %s ORDER BY w.id''', (start, end)):
        quality_state = row.pop('quality_state')
        point_code = row.pop('code')
        if quality_state in {'pending', 'excluded'}:
            if quality_state == 'pending':
                reviews.append({'code': 'quality_pending', 'point': point_code, 'window_id': row['id']})
            continue
        row['sample_day'] = row['sample_day'].isoformat()
        windows[row.pop('point_id')].append(row)
    reasons = reviews.copy()
    if len(points) <= clusters:
        reasons.append({'code': 'too_few_points', 'required': clusters + 1, 'actual': len(points)})
    if len(points) > MAX_POINTS:
        reasons.append({'code': 'point_limit', 'limit': MAX_POINTS})
    for point in points:
        point['windows'] = windows[point['id']]
        total = sum(window['total'] for window in point['windows'])
        if point['pending_location']:
            reasons.append({'code': 'migration_pending', 'point': point['code']})
        if total < MIN_ANONYMOUS or len(point['windows']) < MIN_WINDOWS:
            reasons.append({'code': 'sample_insufficient', 'point': point['code'], 'samples': total,
                            'windows': len(point['windows']), 'required_samples': MIN_ANONYMOUS,
                            'required_windows': MIN_WINDOWS})
    snapshot = {'from_day': start.isoformat(), 'through_day': end.isoformat(),
                'clusters': clusters, 'points': points}
    if reviews:
        snapshot['quality_pending'] = reviews
    return snapshot, sha256(encode(snapshot).encode()).hexdigest(), reasons


def reason_summary(reasons):
    """Render snapshot blockers for an operator-facing message."""
    labels = []
    for item in reasons:
        code = item['code']
        if code == 'migration_pending':
            labels.append(f"{item['point']} 迁移待核")
        elif code == 'sample_insufficient':
            labels.append(f"{item['point']} 样本不足（{item['samples']} / {item['required_samples']} 次）")
        elif code == 'quality_pending':
            labels.append(f"{item['point']} 窗口 #{item['window_id']} 待复核")
        elif code == 'too_few_points':
            labels.append(f"仅 {item['actual']} 个点位，至少需要 {item['required']} 个")
        elif code == 'point_limit':
            labels.append(f"超过 {item['limit']} 个点位上限")
        else:
            labels.append(str(code))
    return '源数据尚未满足全批重算：' + '；'.join(dict.fromkeys(labels))
