"""PowerContext storage layer for group-relay (event-sourced, SDK-backed)."""

import json
import hashlib
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from powercontext.builtin.runtime import BuiltinConfig, open_builtin_runtime, CaptureWorkingNote
from powercontext.builtin.scope.models import ScopeDraft
from powercontext.errors import SourceConflictError


APP_SCHEMA_VERSION = "group-relay-v1"
MAX_PROJECTS = 20
MAX_TASKS_PER_PROJECT = 200
MAX_EVENTS_PER_PROJECT = 500
PAGE_SIZE = 20


class RelayError(ValueError):
    """Base class for business/storage errors mapped to HTTP codes."""


class RevisionConflictError(RelayError):
    """expected_revision does not match current revision (HTTP 409)."""


class ActionConflictError(RelayError):
    """Same action_id reused with a different payload (HTTP 409)."""


class EventLimitError(RelayError):
    """Project reached the event cap; writes refused, reads still work."""


class NotFoundError(RelayError):
    """Referenced record does not exist (HTTP 404)."""


class ValidationError(RelayError):
    """Business validation failed (HTTP 400)."""

    def __init__(self, errors):
        self.errors = errors if isinstance(errors, list) else [str(errors)]
        super().__init__('; '.join(self.errors))


def _make_idempotency_key(project_name: str) -> str:
    return f"group-relay-project-{hashlib.sha256(project_name.encode()).hexdigest()[:16]}"


def make_runtime_context(data_dir: Path):
    config = BuiltinConfig.model_validate({
        'database': {
            'kind': 'sqlite',
            'url': f'sqlite+aiosqlite:///{data_dir}/powercontext.db'
        },
        'runtime': {
            'profile_schedule_enabled': False
        }
    })
    return open_builtin_runtime(config, scheduler_path=data_dir / 'scheduler.db')


async def create_project_scope(runtime, project_name: str, summary: str = "") -> str:
    scopes = await runtime.scopes.list()
    app_scopes = [s for s in scopes if s.title and APP_SCHEMA_VERSION in (s.summary or "")]
    if len(app_scopes) >= MAX_PROJECTS:
        raise ValidationError(f"最多支持 {MAX_PROJECTS} 个项目")

    idempotency_key = _make_idempotency_key(project_name)
    actual_summary = summary.strip() if summary else f"{APP_SCHEMA_VERSION} project"
    descriptor = await runtime.scopes.create(ScopeDraft(
        title=project_name,
        summary=f"{APP_SCHEMA_VERSION}: {actual_summary}",
        idempotency_key=idempotency_key
    ))
    return descriptor.scope_id


async def list_project_scopes(runtime) -> list[dict]:
    scopes = await runtime.scopes.list()
    result = []
    for s in scopes:
        if s.title and APP_SCHEMA_VERSION in (s.summary or ""):
            result.append({
                'scope_id': s.scope_id,
                'title': s.title,
                'summary': s.summary,
            })
    return result


async def project_exists(runtime, scope_id: str) -> bool:
    scopes = await runtime.scopes.list()
    return any(
        s.scope_id == scope_id and APP_SCHEMA_VERSION in (s.summary or "")
        for s in scopes
    )


_project_locks: dict[str, asyncio.Lock] = {}


def get_project_lock(scope_id: str) -> asyncio.Lock:
    if scope_id not in _project_locks:
        _project_locks[scope_id] = asyncio.Lock()
    return _project_locks[scope_id]


async def capture_event(runtime, scope_id: str, action_id: str, payload: dict,
                        session_id: str = "default", expected_revision: int | None = None,
                        prepare=None) -> dict:
    """Append one immutable event atomically under the project lock.

    Order inside the lock: read all events -> same-action idempotency ->
    revision check -> event cap -> business validation (prepare) -> SDK write.
    Idempotent retries therefore succeed even with a stale expected_revision
    and never append a second event. `prepare(state, events)` runs business
    checks and may return extra server-derived payload fields; the idempotency
    hash always covers only the client payload so retries stay stable.
    """
    client_str = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload_hash = hashlib.sha256(client_str.encode('utf-8')).hexdigest()[:16]

    lock = get_project_lock(scope_id)
    async with lock:
        all_events = await get_all_events(runtime, scope_id)

        for evt in all_events:
            if evt.get('action_id') == action_id:
                if evt.get('payload_hash') == payload_hash:
                    return {
                        'source_id': evt['_source_id'],
                        'sequence': evt['_position'],
                        'idempotent': True,
                        'payload': evt.get('payload'),
                    }
                raise ActionConflictError(f"保存冲突: 动作 {action_id} 已存在且内容不同")

        if expected_revision is not None:
            current_rev = max((e.get('_position', 0) for e in all_events), default=0)
            if current_rev != expected_revision:
                raise RevisionConflictError(
                    f"版本冲突: 期望修订版 {expected_revision}, 当前为 {current_rev}，请刷新后重试")

        if len(all_events) >= MAX_EVENTS_PER_PROJECT:
            raise EventLimitError(
                f"项目事件已达上限 ({MAX_EVENTS_PER_PROJECT})，拒绝写入；仍可读取状态与生成报告")

        stored_payload = payload
        if prepare is not None:
            extra = prepare(rebuild_state(all_events), all_events)
            if isinstance(extra, dict):
                stored_payload = {**payload, **extra}

        content = json.dumps({
            'app': 'group-relay',
            'schema': APP_SCHEMA_VERSION,
            'action_id': action_id,
            'payload_hash': payload_hash,
            'payload': stored_payload,
            'created_at': datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False)

        try:
            receipt = await runtime.sources.for_scope(scope_id).capture_working_note(
                CaptureWorkingNote(
                    source_id=action_id,
                    content=content,
                    session_id=session_id,
                    note_kind='validation',
                    metadata={'app': 'group-relay'}
                )
            )
        except SourceConflictError:
            raise ActionConflictError(f"保存冲突: {action_id} - 系统内部冲突")

        return {
            'source_id': receipt.source_ref.source_id,
            'sequence': receipt.sequence,
            'payload': stored_payload,
        }


async def get_all_events(runtime, scope_id: str) -> list[dict]:
    """Read every event of a scope through SDK pages of 20, sorted by position.

    Reads never raise on volume so existing reports stay available after the
    write cap is reached.
    """
    events = []
    cursor = None

    while True:
        page = await runtime.records.for_scope(scope_id).list_sources(
            limit=PAGE_SIZE, cursor=cursor
        )
        for item in page.items:
            try:
                content = json.loads(item.content)
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
            if isinstance(content, dict) and content.get('app') == 'group-relay':
                content['_source_id'] = item.source_id
                content['_position'] = item.position
                content['_content_digest'] = item.content_digest
                events.append(content)
        if not page.next_cursor:
            break
        cursor = page.next_cursor

    events.sort(key=lambda e: e.get('_position', 0))
    return events


def withdrawn_review_ids(events: list[dict]) -> set[str]:
    """Record IDs tombstoned by a withdrawal; they must never become effective again."""
    return {
        e.get('payload', {}).get('record_id')
        for e in events
        if e.get('payload', {}).get('action') == 'withdraw_review'
        and e.get('payload', {}).get('record_id')
    }


def rebuild_state(events: list[dict]) -> dict:
    """Replay events into the current effective state.

    Task source_id/updated_at always cite the latest event that produced the
    current values. Corrections replace a review; withdrawals remove it and
    block later re-imports of the same record. Bulk imports are additive:
    already-effective records keep their original source.
    """
    tasks = {}
    reviews_by_id = {}
    preferences = {}
    withdrawn_record_ids = set()

    for event in events:
        payload = event.get('payload', {})
        action = payload.get('action')
        source_id = event.get('_source_id')
        position = event.get('_position', 0)
        created_at = event.get('created_at', '')

        if action == 'create_task':
            task_id = payload.get('task_id')
            if task_id:
                tasks[task_id] = {
                    'id': task_id,
                    'title': payload.get('title', ''),
                    'status': payload.get('status', 'todo'),
                    'owner': payload.get('owner', ''),
                    'due_date': payload.get('due_date', ''),
                    'source_id': source_id,
                    'created_at': created_at,
                    'updated_at': created_at,
                    'position': position,
                }

        elif action == 'update_task':
            task_id = payload.get('task_id')
            if task_id and task_id in tasks:
                for field in ('title', 'owner', 'due_date'):
                    if field in payload:
                        tasks[task_id][field] = payload[field]
                if payload.get('status') in ('todo', 'doing', 'done'):
                    tasks[task_id]['status'] = payload['status']
                tasks[task_id]['source_id'] = source_id
                tasks[task_id]['updated_at'] = created_at
                tasks[task_id]['position'] = position

        elif action == 'add_review':
            record_id = payload.get('record_id')
            if record_id and record_id not in withdrawn_record_ids:
                reviews_by_id[record_id] = {
                    'record_id': record_id,
                    'date': payload.get('date', ''),
                    'completed': payload.get('completed', ''),
                    'blockers': payload.get('blockers', ''),
                    'decisions': payload.get('decisions', ''),
                    'next_steps': payload.get('next_steps', ''),
                    'source_id': source_id,
                    'created_at': created_at,
                    'updated_at': created_at,
                    'position': position,
                    'is_corrected': False,
                }

        elif action == 'correct_review':
            record_id = payload.get('record_id')
            if record_id and record_id not in withdrawn_record_ids:
                reviews_by_id[record_id] = {
                    'record_id': record_id,
                    'date': payload.get('date', ''),
                    'completed': payload.get('completed', ''),
                    'blockers': payload.get('blockers', ''),
                    'decisions': payload.get('decisions', ''),
                    'next_steps': payload.get('next_steps', ''),
                    'source_id': source_id,
                    'created_at': created_at,
                    'updated_at': created_at,
                    'position': position,
                    'is_corrected': True,
                    'old_source_id': payload.get('old_source_id'),
                }

        elif action == 'withdraw_review':
            record_id = payload.get('record_id')
            if record_id:
                withdrawn_record_ids.add(record_id)
                reviews_by_id.pop(record_id, None)

        elif action == 'bulk_import':
            for review in payload.get('reviews', []):
                record_id = review.get('record_id')
                if record_id and record_id not in withdrawn_record_ids and record_id not in reviews_by_id:
                    reviews_by_id[record_id] = {
                        'record_id': record_id,
                        'date': review.get('date', ''),
                        'completed': review.get('completed', ''),
                        'blockers': review.get('blockers', ''),
                        'decisions': review.get('decisions', ''),
                        'next_steps': review.get('next_steps', ''),
                        'source_id': source_id,
                        'created_at': created_at,
                        'updated_at': created_at,
                        'position': position,
                        'is_corrected': False,
                    }
            for task_data in payload.get('tasks', []):
                task_id = task_data.get('task_id')
                if task_id and task_id not in tasks:
                    tasks[task_id] = {
                        'id': task_id,
                        'title': task_data.get('title', ''),
                        'status': task_data.get('status', 'todo'),
                        'owner': task_data.get('owner', ''),
                        'due_date': task_data.get('due_date', ''),
                        'source_id': source_id,
                        'created_at': created_at,
                        'updated_at': created_at,
                        'position': position,
                    }

        elif action == 'set_preferences':
            prefs = payload.get('preferences', {})
            if isinstance(prefs, dict):
                preferences.update(prefs)

    reviews = list(reviews_by_id.values())
    decisions = [
        {
            'content': r['decisions'],
            'date': r.get('date', ''),
            'source_id': r['source_id'],
            'created_at': r.get('updated_at') or r.get('created_at', ''),
            'record_id': r['record_id'],
        }
        for r in reviews if r.get('decisions')
    ]

    return {
        'tasks': list(tasks.values()),
        'reviews': reviews,
        'decisions': decisions,
        'preferences': preferences,
    }


def validate_task(task_data: dict, existing_tasks: dict | None = None) -> list[str]:
    errors = []

    task_id = task_data.get('task_id', '')
    if not isinstance(task_id, str):
        errors.append("任务 ID 必须是字符串")
        task_id = ''
    else:
        task_id = task_id.strip()
    if task_id == '':
        if isinstance(task_data.get('task_id', ''), str):
            errors.append("任务 ID 不能为空")
    elif len(task_id) > 100:
        errors.append("任务 ID 过长（最大100字符）")
    elif existing_tasks and task_id in existing_tasks:
        errors.append(f"任务 ID 已存在: {task_id}")

    title = task_data.get('title', '')
    if not isinstance(title, str):
        errors.append("任务标题必须是字符串")
        title = ''
    else:
        title = title.strip()
    if title == '':
        if isinstance(task_data.get('title', ''), str):
            errors.append("任务标题不能为空")
    elif len(title) > 500:
        errors.append("任务标题过长（最大500字符）")

    status = task_data.get('status', '')
    if not isinstance(status, str) or status not in ('todo', 'doing', 'done'):
        errors.append(f"未知状态: {status}（应为 todo/doing/done）")

    due_date = task_data.get('due_date', '')
    if not isinstance(due_date, str):
        errors.append("截止日期必须是字符串 (YYYY-MM-DD)")
    else:
        due_date = due_date.strip()
        if due_date:
            try:
                datetime.strptime(due_date, '%Y-%m-%d')
            except ValueError:
                errors.append(f"日期格式错误: {due_date} (应为 YYYY-MM-DD)")

    owner = task_data.get('owner', '')
    if not isinstance(owner, str):
        errors.append("负责人必须是字符串")
    elif len(owner) > 100:
        errors.append("负责人名称过长（最大100字符）")

    return errors


def validate_review(review_data: dict) -> list[str]:
    errors = []

    record_id = review_data.get('record_id', '')
    if not isinstance(record_id, str):
        errors.append("记录编号必须是字符串")
        record_id = ''
    else:
        record_id = record_id.strip()
    if record_id == '':
        if isinstance(review_data.get('record_id', ''), str):
            errors.append("记录编号不能为空")
    elif len(record_id) > 100:
        errors.append("记录编号过长（最大100字符）")

    date = review_data.get('date', '')
    if not isinstance(date, str):
        errors.append("日期必须是字符串 (YYYY-MM-DD)")
    else:
        date = date.strip()
        if date:
            try:
                datetime.strptime(date, '%Y-%m-%d')
            except ValueError:
                errors.append(f"日期格式错误: {date} (应为 YYYY-MM-DD)")

    field_labels = {'completed': '已完成', 'blockers': '困难',
                    'decisions': '决定', 'next_steps': '下一步'}
    for field, label in field_labels.items():
        value = review_data.get(field, '')
        if not isinstance(value, str):
            errors.append(f"{label} ({field}) 必须是字符串")
        elif len(value) > 5000:
            errors.append(f"{field} 字段过长（最大5000字符）")

    return errors


def _parse_md_section(lines: list[str]) -> tuple[dict, list[dict], list[str]]:
    review = {}
    tasks = []

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith('# '):
            i += 1
            continue

        if ':' in line and not line.startswith('|'):
            key, _, value = line.partition(':')
            key = key.strip()
            value = value.strip()
            if key == '记录编号':
                review['record_id'] = value
            elif key == '日期':
                review['date'] = value
            elif key == '已完成':
                review['completed'] = value
            elif key == '困难':
                review['blockers'] = value
            elif key == '决定':
                review['decisions'] = value
            elif key == '下一步':
                review['next_steps'] = value

        if line.startswith('|'):
            separator_line = lines[i + 1] if i + 1 < len(lines) else ''
            if '---' in separator_line:
                j = i + 2
                while j < len(lines) and lines[j].strip().startswith('|'):
                    cells = [c.strip() for c in lines[j].split('|')[1:-1]]
                    if len(cells) >= 5:
                        tasks.append({
                            'task_id': cells[0],
                            'title': cells[1],
                            'status': cells[2],
                            'owner': cells[3],
                            'due_date': cells[4],
                        })
                    j += 1
                break
        i += 1

    errors = validate_review(review)
    if not tasks:
        errors.append("未找到任务表格")
    for idx, task in enumerate(tasks):
        task_errors = validate_task(task, {})
        if task_errors:
            errors.append(f"任务 {idx + 1}: {'; '.join(task_errors)}")

    if errors:
        return {}, [], errors
    return review, tasks, []


def parse_markdown_import(content: str) -> tuple[list[dict], list[dict], list[str]]:
    """Parse the fixed Markdown layout; `---` lines separate weekly sections.

    Each section: 标题行 + 记录编号/日期/已完成/困难/决定/下一步 字段 + 任务表格。
    Returns (reviews, tasks, errors); any error aborts the whole import.
    """
    sections = []
    current = []
    for line in content.split('\n'):
        if line.strip() == '---':
            sections.append(current)
            current = []
        else:
            current.append(line)
    sections.append(current)

    reviews = []
    tasks = []
    errors = []
    seen_records = set()
    seen_tasks = set()
    index = 0
    for section in sections:
        if not any(l.strip() for l in section):
            continue
        index += 1
        review, section_tasks, section_errors = _parse_md_section(section)
        if section_errors:
            errors.extend(f"第 {index} 段: {e}" for e in section_errors)
            continue
        if review['record_id'] in seen_records:
            errors.append(f"第 {index} 段: 记录编号重复: {review['record_id']}")
            continue
        for task in section_tasks:
            if task['task_id'] in seen_tasks:
                errors.append(f"第 {index} 段: 任务 ID 重复: {task['task_id']}")
        seen_records.add(review['record_id'])
        seen_tasks.update(t['task_id'] for t in section_tasks)
        reviews.append(review)
        tasks.extend(section_tasks)

    if index == 0:
        errors.append("导入内容为空")
    if errors:
        return [], [], errors
    return reviews, tasks, []


def parse_csv_import(content: str) -> tuple[list[dict], list[dict], list[str]]:
    """Parse the fixed CSV layout; any invalid row aborts the whole import."""
    import csv
    import io

    errors = []
    reviews_by_id = {}
    tasks = []

    if content.startswith('\ufeff'):
        content = content[1:]

    reader = csv.DictReader(io.StringIO(content))

    required_fields = ['record_id', 'task_id', 'title', 'status']
    if reader.fieldnames:
        for field in required_fields:
            if field not in reader.fieldnames:
                errors.append(f"缺少必需列: {field}")
    else:
        errors.append("缺少表头行")

    if errors:
        return [], [], errors

    seen_tasks = set()
    for row_num, row in enumerate(reader, start=2):
        record_id = (row.get('record_id') or '').strip()
        task_id = (row.get('task_id') or '').strip()

        if not record_id:
            errors.append(f"第 {row_num} 行: 记录编号为空")
            continue
        if not task_id:
            errors.append(f"第 {row_num} 行: 任务 ID 为空")
            continue
        if task_id in seen_tasks:
            errors.append(f"第 {row_num} 行: 任务 ID 重复: {task_id}")
            continue

        if record_id not in reviews_by_id:
            review = {
                'record_id': record_id,
                'date': (row.get('date') or '').strip(),
                'completed': (row.get('completed') or '').strip(),
                'blockers': (row.get('blockers') or '').strip(),
                'decisions': (row.get('decisions') or '').strip(),
                'next_steps': (row.get('next_steps') or '').strip(),
            }
            review_errors = validate_review(review)
            if review_errors:
                errors.append(f"第 {row_num} 行: {'; '.join(review_errors)}")
            else:
                reviews_by_id[record_id] = review

        task = {
            'task_id': task_id,
            'title': (row.get('title') or '').strip(),
            'status': (row.get('status') or '').strip(),
            'owner': (row.get('owner') or '').strip(),
            'due_date': (row.get('due_date') or '').strip(),
        }
        task_errors = validate_task(task, {})
        if task_errors:
            errors.append(f"第 {row_num} 行: {'; '.join(task_errors)}")
        else:
            tasks.append(task)
            seen_tasks.add(task_id)

    if errors:
        return [], [], errors
    return list(reviews_by_id.values()), tasks, []