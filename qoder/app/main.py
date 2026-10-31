"""FastAPI application for group-relay (组会接力)."""

import os
import fcntl
import asyncio
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.storage import (
    make_runtime_context, create_project_scope, list_project_scopes, project_exists,
    capture_event, get_all_events, rebuild_state, withdrawn_review_ids,
    validate_task, validate_review, parse_markdown_import, parse_csv_import,
    MAX_TASKS_PER_PROJECT, MAX_EVENTS_PER_PROJECT,
    RelayError, RevisionConflictError, ActionConflictError, EventLimitError,
    NotFoundError, ValidationError,
)


_runtime = None
_runtime_ctx = None
_data_dir = None
_lock_fh = None
_create_lock = asyncio.Lock()


def get_data_dir() -> Path:
    global _data_dir
    if _data_dir is None:
        _data_dir = Path(os.environ.get('DATA_DIR', Path(__file__).parent.parent / 'data'))
        _data_dir.mkdir(parents=True, exist_ok=True)
    return _data_dir


def get_runtime():
    if _runtime is None:
        raise RuntimeError("Runtime not initialized. Server may not be started correctly.")
    return _runtime


def require_action_and_revision(data: dict) -> tuple[str, int]:
    action_id = data.get('action_id')
    if not action_id or not isinstance(action_id, str) or len(action_id) > 200:
        raise HTTPException(status_code=400, detail="需要提供有效的 action_id")
    expected_revision = data.get('expected_revision')
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise HTTPException(status_code=400, detail="需要提供 expected_revision (非负整数)")
    return action_id, expected_revision


def form_controls(form) -> tuple[str, int]:
    action_id = form.get('action_id')
    if not action_id or not isinstance(action_id, str) or len(action_id) > 200:
        raise HTTPException(status_code=400, detail="需要提供有效的 action_id")
    raw_revision = form.get('expected_revision')
    try:
        expected_revision = int(raw_revision)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="需要提供 expected_revision (非负整数)")
    if expected_revision < 0:
        raise HTTPException(status_code=400, detail="需要提供 expected_revision (非负整数)")
    return action_id, expected_revision


async def require_project(runtime, scope_id: str):
    if not await project_exists(runtime, scope_id):
        raise HTTPException(status_code=404, detail="项目不存在")


def translate_relay_error(e: RelayError) -> HTTPException:
    if isinstance(e, (RevisionConflictError, ActionConflictError)):
        return HTTPException(status_code=409, detail=str(e))
    if isinstance(e, ValidationError):
        return HTTPException(status_code=400, detail={"errors": e.errors})
    if isinstance(e, NotFoundError):
        return HTTPException(status_code=404, detail=str(e))
    if isinstance(e, EventLimitError):
        return HTTPException(status_code=400, detail=str(e))
    return HTTPException(status_code=500, detail=str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _runtime, _runtime_ctx, _lock_fh
    data_dir = get_data_dir()

    _lock_fh = open(data_dir / '.app.lock', 'a+')
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _lock_fh.close()
        _lock_fh = None
        raise RuntimeError(f"数据目录已被另一个进程占用，拒绝启动: {data_dir}")
    _lock_fh.seek(0)
    _lock_fh.truncate()
    _lock_fh.write(str(os.getpid()))
    _lock_fh.flush()

    _runtime_ctx = make_runtime_context(data_dir)
    _runtime = await _runtime_ctx.__aenter__()
    try:
        yield
    finally:
        if _runtime_ctx:
            try:
                await _runtime_ctx.__aexit__(None, None, None)
            except Exception:
                pass
        _runtime = None
        _runtime_ctx = None
        if _lock_fh:
            try:
                fcntl.flock(_lock_fh, fcntl.LOCK_UN)
                _lock_fh.close()
            except OSError:
                pass
            _lock_fh = None


app = FastAPI(title="组会接力", lifespan=lifespan)

static_dir = Path(__file__).parent / 'static'
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def index():
    html_path = Path(__file__).parent / 'static' / 'index.html'
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding='utf-8'))
    return HTMLResponse(content="<h1>组会接力</h1><p>应用未正确部署，缺少 index.html</p>")


@app.get("/api/projects")
async def api_list_projects():
    runtime = get_runtime()
    return {"projects": await list_project_scopes(runtime)}


@app.post("/api/projects")
async def api_create_project(request: Request):
    data = await request.json()
    name = (data.get('name') or '').strip()
    summary = (data.get('summary') or '').strip()
    if not name:
        raise HTTPException(status_code=400, detail="项目名称不能为空")
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="项目名称过长（最大100字符）")

    runtime = get_runtime()
    async with _create_lock:
        try:
            scope_id = await create_project_scope(runtime, name, summary)
        except RelayError as e:
            raise translate_relay_error(e)
        await capture_event(
            runtime, scope_id,
            action_id=f"init-{scope_id}",
            payload={'action': 'set_preferences', 'preferences': {}},
            expected_revision=None,
        )
    return {"scope_id": scope_id, "title": name}


@app.get("/api/projects/{scope_id}/state")
async def api_get_project_state(scope_id: str):
    runtime = get_runtime()
    await require_project(runtime, scope_id)
    events = await get_all_events(runtime, scope_id)
    state = rebuild_state(events)
    return {
        **state,
        'revision': max((e.get('_position', 0) for e in events), default=0),
        'event_count': len(events),
        'event_limit': MAX_EVENTS_PER_PROJECT,
    }


@app.post("/api/projects/{scope_id}/tasks")
async def api_add_task(scope_id: str, request: Request):
    data = await request.json()
    action_id, expected_revision = require_action_and_revision(data)
    runtime = get_runtime()
    await require_project(runtime, scope_id)

    payload = {
        'action': 'create_task',
        'task_id': data.get('task_id', ''),
        'title': data.get('title', ''),
        'status': data.get('status', 'todo'),
        'owner': data.get('owner', ''),
        'due_date': data.get('due_date', ''),
    }

    def prepare(state, events):
        existing = {t['id']: t for t in state['tasks']}
        errors = validate_task(payload, existing)
        if errors:
            raise ValidationError(errors)
        if len(existing) >= MAX_TASKS_PER_PROJECT:
            raise ValidationError(f"任务数超过限制 ({MAX_TASKS_PER_PROJECT})")
        payload['task_id'] = payload['task_id'].strip()
        payload['title'] = payload['title'].strip()
        payload['due_date'] = payload['due_date'].strip()

    try:
        return await capture_event(runtime, scope_id, action_id, payload,
                                   expected_revision=expected_revision, prepare=prepare)
    except RelayError as e:
        raise translate_relay_error(e)


@app.put("/api/projects/{scope_id}/tasks/{task_id}")
async def api_update_task(scope_id: str, task_id: str, request: Request):
    data = await request.json()
    action_id, expected_revision = require_action_and_revision(data)
    runtime = get_runtime()
    await require_project(runtime, scope_id)

    payload = {'action': 'update_task', 'task_id': task_id}
    for field in ('title', 'status', 'owner', 'due_date'):
        if field in data:
            payload[field] = data[field]

    def prepare(state, events):
        if task_id not in {t['id'] for t in state['tasks']}:
            raise NotFoundError("任务不存在")
        errors = []
        if 'title' in payload:
            title = payload['title']
            if not isinstance(title, str):
                errors.append("任务标题必须是字符串")
            elif not title.strip():
                errors.append("任务标题不能为空")
            elif len(title) > 500:
                errors.append("任务标题过长（最大500字符）")
            else:
                payload['title'] = title.strip()
        if 'status' in payload and (
            not isinstance(payload['status'], str)
            or payload['status'] not in ('todo', 'doing', 'done')
        ):
            errors.append(f"未知状态: {payload['status']}（应为 todo/doing/done）")
        if 'due_date' in payload:
            due = payload['due_date']
            if not isinstance(due, str):
                errors.append("截止日期必须是字符串 (YYYY-MM-DD)")
            else:
                due = due.strip()
                if due:
                    try:
                        datetime.strptime(due, '%Y-%m-%d')
                    except ValueError:
                        errors.append(f"日期格式错误: {due} (应为 YYYY-MM-DD)")
                payload['due_date'] = due
        if 'owner' in payload:
            owner = payload['owner']
            if not isinstance(owner, str):
                errors.append("负责人必须是字符串")
            elif len(owner) > 100:
                errors.append("负责人名称过长（最大100字符）")
        if errors:
            raise ValidationError(errors)

    try:
        return await capture_event(runtime, scope_id, action_id, payload,
                                   expected_revision=expected_revision, prepare=prepare)
    except RelayError as e:
        raise translate_relay_error(e)


@app.post("/api/projects/{scope_id}/reviews")
async def api_add_review(scope_id: str, request: Request):
    data = await request.json()
    action_id, expected_revision = require_action_and_revision(data)
    runtime = get_runtime()
    await require_project(runtime, scope_id)

    payload = {
        'action': 'add_review',
        'record_id': data.get('record_id', ''),
        'date': data.get('date', ''),
        'completed': data.get('completed', ''),
        'blockers': data.get('blockers', ''),
        'decisions': data.get('decisions', ''),
        'next_steps': data.get('next_steps', ''),
    }

    def prepare(state, events):
        errors = validate_review(payload)
        if errors:
            raise ValidationError(errors)
        record_id = payload['record_id'].strip()
        if record_id in withdrawn_review_ids(events):
            raise ValidationError(
                f"记录编号 '{record_id}' 已撤回，不能复用；请使用新的记录编号")
        if record_id in {r['record_id'] for r in state['reviews']}:
            raise ValidationError(
                f"记录编号 '{record_id}' 已存在，如需修改请使用「修正记录」功能")
        payload['record_id'] = record_id

    try:
        return await capture_event(runtime, scope_id, action_id, payload,
                                   expected_revision=expected_revision, prepare=prepare)
    except RelayError as e:
        raise translate_relay_error(e)


@app.post("/api/projects/{scope_id}/reviews/{record_id}/correct")
async def api_correct_review(scope_id: str, record_id: str, request: Request):
    data = await request.json()
    action_id, expected_revision = require_action_and_revision(data)
    runtime = get_runtime()
    await require_project(runtime, scope_id)

    payload = {
        'action': 'correct_review',
        'record_id': record_id,
        'date': data.get('date', ''),
        'completed': data.get('completed', ''),
        'blockers': data.get('blockers', ''),
        'decisions': data.get('decisions', ''),
        'next_steps': data.get('next_steps', ''),
    }

    def prepare(state, events):
        review = next((r for r in state['reviews'] if r['record_id'] == record_id), None)
        if not review:
            raise NotFoundError("记录不存在或已撤回")
        errors = validate_review(payload)
        if errors:
            raise ValidationError(errors)
        return {'old_source_id': review['source_id']}

    try:
        return await capture_event(runtime, scope_id, action_id, payload,
                                   expected_revision=expected_revision, prepare=prepare)
    except RelayError as e:
        raise translate_relay_error(e)


@app.post("/api/projects/{scope_id}/reviews/{record_id}/withdraw")
async def api_withdraw_review(scope_id: str, record_id: str, request: Request):
    data = await request.json() if await request.body() else {}
    action_id, expected_revision = require_action_and_revision(data)
    runtime = get_runtime()
    await require_project(runtime, scope_id)

    payload = {'action': 'withdraw_review', 'record_id': record_id}

    def prepare(state, events):
        review = next((r for r in state['reviews'] if r['record_id'] == record_id), None)
        if not review:
            raise NotFoundError("记录不存在或已撤回")
        return {'source_id': review['source_id']}

    try:
        return await capture_event(runtime, scope_id, action_id, payload,
                                   expected_revision=expected_revision, prepare=prepare)
    except RelayError as e:
        raise translate_relay_error(e)


@app.post("/api/projects/{scope_id}/preferences")
async def api_set_preferences(scope_id: str, request: Request):
    data = await request.json()
    action_id, expected_revision = require_action_and_revision(data)
    runtime = get_runtime()
    await require_project(runtime, scope_id)

    prefs = data.get('preferences')
    payload = {'action': 'set_preferences', 'preferences': prefs}

    def prepare(state, events):
        if not isinstance(prefs, dict):
            raise ValidationError("preferences 必须是对象")
        for key, value in prefs.items():
            if not isinstance(key, str) or len(key) > 100:
                raise ValidationError("偏好键过长或非法")
            if not isinstance(value, str) or len(value) > 500:
                raise ValidationError(f"偏好 '{key}' 的值必须是不超过500字符的字符串")
        if 'focus' in prefs and prefs['focus'] not in ('', 'default', 'next_steps'):
            raise ValidationError("focus 仅支持 default 或 next_steps")

    try:
        return await capture_event(runtime, scope_id, action_id, payload,
                                   expected_revision=expected_revision, prepare=prepare)
    except RelayError as e:
        raise translate_relay_error(e)


@app.post("/api/projects/{scope_id}/import")
async def api_import(scope_id: str, request: Request):
    form = await request.form()
    action_id, expected_revision = form_controls(form)
    runtime = get_runtime()
    await require_project(runtime, scope_id)

    file = form.get('file')
    format_type = form.get('format', 'markdown')
    if format_type not in ('markdown', 'csv'):
        raise HTTPException(status_code=400, detail="format 仅支持 markdown 或 csv")

    if file is not None and hasattr(file, 'read'):
        content = await file.read()
        if len(content) > 100 * 1024:
            raise HTTPException(status_code=400, detail="文件过大（最大 100 KiB）")
        try:
            content_str = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=400,
                detail="文件编码不是 UTF-8，请将文件用 UTF-8 保存后重新上传（支持带 BOM 的 UTF-8）")
    else:
        content_str = form.get('content') or ''
        if len(content_str.encode('utf-8')) > 100 * 1024:
            raise HTTPException(status_code=400, detail="内容过大（最大 100 KiB）")

    if not content_str.strip():
        raise HTTPException(status_code=400, detail="导入内容为空")

    if format_type == 'csv':
        reviews, tasks, parse_errors = parse_csv_import(content_str)
    else:
        reviews, tasks, parse_errors = parse_markdown_import(content_str)
    if parse_errors:
        raise HTTPException(status_code=400, detail={"errors": parse_errors})
    if not reviews and not tasks:
        raise HTTPException(status_code=400, detail="没有可导入的记录（文件仅含表头或空段落），未写入任何事件")

    payload = {
        'action': 'bulk_import',
        'format': format_type,
        'reviews': reviews,
        'tasks': tasks,
        'raw_content': content_str,
    }

    def prepare(state, events):
        existing_tasks = {t['id']: t for t in state['tasks']}
        existing_reviews = {r['record_id']: r for r in state['reviews']}
        tombstoned = withdrawn_review_ids(events)
        new_task_ids = {t['task_id'] for t in tasks if t['task_id'] not in existing_tasks}
        if len(existing_tasks) + len(new_task_ids) > MAX_TASKS_PER_PROJECT:
            raise ValidationError(f"任务数将超过限制 ({MAX_TASKS_PER_PROJECT})")
        errors = []
        for review in reviews:
            if review['record_id'] in tombstoned:
                continue
            existing = existing_reviews.get(review['record_id'])
            if existing and any(
                existing.get(f) != review.get(f)
                for f in ('completed', 'blockers', 'decisions', 'next_steps')
            ):
                errors.append(
                    f"记录编号 '{review['record_id']}' 已存在且内容不同，请使用「修正记录」功能")
        if errors:
            raise ValidationError(errors)
        # Server-derived truthful counts, persisted inside the event so an
        # idempotent retry returns the original additions, not array lengths.
        return {
            'imported_tasks': len(new_task_ids),
            'imported_reviews': sum(
                1 for r in reviews
                if r['record_id'] not in tombstoned and r['record_id'] not in existing_reviews
            ),
        }

    try:
        receipt = await capture_event(runtime, scope_id, action_id, payload,
                                      expected_revision=expected_revision, prepare=prepare)
    except RelayError as e:
        raise translate_relay_error(e)

    stored = receipt.get('payload') or {}
    return {
        'imported_tasks': stored.get('imported_tasks', 0),
        'imported_reviews': stored.get('imported_reviews', 0),
        'source_id': receipt['source_id'],
        'idempotent': receipt.get('idempotent', False),
    }


def _source_ref(scope_id: str, source_id: str, created_at: str) -> str:
    """Cite the effective source with its original ISO time and raw entry link."""
    return (f"来源: [{source_id}](/api/projects/{scope_id}/sources/{source_id}) "
            f"时间: {created_at or '未知'}")


def _report_sections(state: dict, scope_id: str) -> dict[str, list[str]]:
    sections = {}

    done_tasks = [t for t in state['tasks'] if t['status'] == 'done']
    lines = ["## 已完成"]
    if done_tasks:
        for t in done_tasks:
            ref = _source_ref(scope_id, t['source_id'], t.get('updated_at') or t.get('created_at', ''))
            lines.append(f"- {t['title']} (负责人: {t['owner'] or '未指定'}, {ref})")
    else:
        lines.append("暂无记录")
    sections['done'] = lines + [""]

    pending_tasks = [t for t in state['tasks'] if t['status'] in ('todo', 'doing')]
    lines = ["## 待办事项"]
    if pending_tasks:
        for t in pending_tasks:
            status_label = '进行中' if t['status'] == 'doing' else '待办'
            due = f", 截止: {t['due_date']}" if t['due_date'] else ""
            ref = _source_ref(scope_id, t['source_id'], t.get('updated_at') or t.get('created_at', ''))
            lines.append(f"- [{status_label}] {t['title']} (负责人: {t['owner'] or '未指定'}{due}, {ref})")
    else:
        lines.append("暂无记录")
    sections['pending'] = lines + [""]

    lines = ["## 复盘记录"]
    if state['reviews']:
        for r in state['reviews']:
            lines.append(f"### {r['record_id']} ({r['date'] or '无日期'})")
            if r.get('completed'):
                lines.append(f"**已完成**: {r['completed']}")
            if r.get('blockers'):
                lines.append(f"**困难**: {r['blockers']}")
            if r.get('decisions'):
                lines.append(f"**决定**: {r['decisions']}")
            if r.get('next_steps'):
                lines.append(f"**下一步**: {r['next_steps']}")
            ref = _source_ref(scope_id, r['source_id'], r.get('updated_at') or r.get('created_at', ''))
            lines.append(f"*{ref}*")
            lines.append("")
    else:
        lines += ["暂无记录", ""]
    sections['reviews'] = lines

    lines = ["## 决定"]
    if state['decisions']:
        for d in state['decisions']:
            ref = _source_ref(scope_id, d['source_id'], d.get('created_at', ''))
            lines.append(f"- {d['content']} ({d['date'] or '无日期'}, {ref})")
    else:
        lines.append("暂无记录")
    sections['decisions'] = lines + [""]

    lines = ["## 下一步"]
    next_steps = [r for r in state['reviews'] if r.get('next_steps')]
    if next_steps:
        for r in next_steps:
            ref = _source_ref(scope_id, r['source_id'], r.get('updated_at') or r.get('created_at', ''))
            lines.append(f"- {r['next_steps']} ({ref})")
    else:
        lines.append("暂无记录")
    sections['next_steps'] = lines + [""]

    return sections


@app.get("/api/projects/{scope_id}/report")
async def api_generate_report(scope_id: str):
    runtime = get_runtime()
    await require_project(runtime, scope_id)
    events = await get_all_events(runtime, scope_id)
    state = rebuild_state(events)
    prefs = state.get('preferences', {})

    title = (prefs.get('report_title') or '').strip() or '组会接力报告'
    sections = _report_sections(state, scope_id)
    if prefs.get('focus') == 'next_steps':
        order = ['next_steps', 'done', 'pending', 'reviews', 'decisions']
    else:
        order = ['done', 'pending', 'reviews', 'decisions', 'next_steps']

    lines = [f"# {title}", ""]
    for key in order:
        lines.extend(sections[key])

    return {"report": '\n'.join(lines), "content_type": "text/markdown"}


@app.get("/api/projects/{scope_id}/sources/{source_id}")
async def api_get_source(scope_id: str, source_id: str):
    runtime = get_runtime()
    await require_project(runtime, scope_id)
    events = await get_all_events(runtime, scope_id)

    event = next((e for e in events if e.get('_source_id') == source_id), None)
    if not event:
        raise HTTPException(status_code=404, detail="来源不存在")

    return {
        'source_id': source_id,
        'position': event.get('_position'),
        'content': event.get('payload'),
        'created_at': event.get('created_at'),
    }