/**
 * Group Relay (组会接力) - Frontend Application Logic
 *
 * Every business write carries a client-generated action_id and the current
 * project revision (expected_revision). A brand-new user action gets a fresh
 * action_id; a network-level retry of that same action reuses the original
 * action_id and revision so the server can answer idempotently.
 */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let currentProject = null;
let projects = [];
let projectState = null;
let currentReport = '';
let reportContext = null; // { scopeId, revision } captured when the report was generated
let correctingRecordId = null;

const elements = {
    projectList: document.getElementById('project-list'),
    noProjectView: document.getElementById('no-project-view'),
    projectView: document.getElementById('project-view'),
    continuationContent: document.getElementById('continuation-content'),
    tasksList: document.getElementById('tasks-list'),
    reviewsList: document.getElementById('reviews-list'),
    reportPreview: document.getElementById('report-preview'),
    loadingOverlay: document.getElementById('loading-overlay'),
    toast: document.getElementById('toast'),
    sourceModal: document.getElementById('source-modal'),
    sourceDetailContent: document.getElementById('source-detail-content'),
    newProjectModal: document.getElementById('new-project-modal'),
    revisionBadge: document.getElementById('revision-badge'),
};

// ---------------------------------------------------------------------------
// Small utilities
// ---------------------------------------------------------------------------
function newActionId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return window.crypto.randomUUID();
    }
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
        const r = Math.random() * 16 | 0;
        const v = c === 'x' ? r : (r & 0x3 | 0x8);
        return v.toString(16);
    });
}

function showLoading() { elements.loadingOverlay.classList.remove('hidden'); }
function hideLoading() { elements.loadingOverlay.classList.add('hidden'); }

let toastTimer = null;
function showToast(message, isError = false) {
    elements.toast.textContent = message;
    elements.toast.className = 'toast' + (isError ? ' error' : '');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => elements.toast.classList.add('hidden'), 4000);
}
function showError(message) { showToast(message, true); }

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
}

function formatTime(iso) {
    if (!iso) return '未知时间';
    const d = new Date(iso);
    return isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN');
}

// Read-only JSON request (GET).
async function apiGet(url) {
    const response = await fetch(url, { headers: { 'Accept': 'application/json' } });
    if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: '请求失败' }));
        const message = typeof error.detail === 'object' && error.detail.errors
            ? error.detail.errors.join('; ')
            : (error.detail || '请求失败');
        const e = new Error(message);
        e.status = response.status;
        throw e;
    }
    return response.json();
}

// ---------------------------------------------------------------------------
// Unified write path
// ---------------------------------------------------------------------------
// buildBody(actionId, expectedRevision) must return a plain object (JSON) or a
// FormData. The helper injects action_id / expected_revision for JSON bodies;
// FormData callers append the two fields themselves inside buildBody.
async function submitWrite({ method, path, buildBody, button, onSuccess, successMessage }) {
    const actionId = newActionId();
    const expectedRevision = projectState ? projectState.revision : 0;

    if (button) button.disabled = true;
    showLoading();
    try {
        let attempt = 0;
        while (true) {
            const body = buildBody(actionId, expectedRevision);
            const isForm = body instanceof FormData;
            let response;
            try {
                response = await fetch(path, {
                    method,
                    body: isForm ? body : JSON.stringify(body),
                    headers: isForm ? {} : { 'Content-Type': 'application/json' },
                });
            } catch (networkErr) {
                // Network-level failure: retry once with the SAME action_id and
                // revision. If the first attempt actually landed, the server
                // returns the original receipt idempotently (no new event).
                if (attempt === 0) { attempt += 1; continue; }
                throw new Error('网络错误，请稍后重试: ' + networkErr.message);
            }

            if (response.status === 409) {
                const err = await response.json().catch(() => ({}));
                await refreshState();
                throw new Error((err.detail || '保存冲突') + '。已刷新到最新状态，请重新提交。');
            }

            if (!response.ok) {
                const err = await response.json().catch(() => ({ detail: '请求失败' }));
                const message = typeof err.detail === 'object' && err.detail.errors
                    ? err.detail.errors.join('; ')
                    : (err.detail || '请求失败');
                throw new Error(message);
            }

            const data = await response.json();
            await refreshState();
            if (onSuccess) onSuccess(data);
            if (successMessage) showToast(successMessage);
            return data;
        }
    } catch (e) {
        showError(e.message);
        throw e;
    } finally {
        if (button) button.disabled = false;
        hideLoading();
    }
}

function withControls(payload, actionId, expectedRevision) {
    return { ...payload, action_id: actionId, expected_revision: expectedRevision };
}

// ---------------------------------------------------------------------------
// Project management
// ---------------------------------------------------------------------------
async function loadProjects() {
    try {
        const data = await apiGet('/api/projects');
        projects = data.projects;
        renderProjectList();
    } catch (error) {
        showError('加载项目列表失败: ' + error.message);
    }
}

function renderProjectList() {
    if (projects.length === 0) {
        elements.projectList.innerHTML = '<div class="empty-state">暂无项目，点击「新建项目」开始</div>';
        return;
    }
    elements.projectList.innerHTML = projects.map(p => `
        <div class="project-item ${currentProject === p.scope_id ? 'active' : ''}"
             data-scope-id="${escapeHtml(p.scope_id)}">
            <div class="project-name">${escapeHtml(p.title)}</div>
        </div>
    `).join('');
    elements.projectList.querySelectorAll('.project-item').forEach(item => {
        item.addEventListener('click', () => selectProject(item.dataset.scopeId));
    });
}

async function selectProject(scopeId) {
    currentProject = scopeId;
    correctingRecordId = null;
    invalidateReport();
    renderProjectList();
    localStorage.setItem('lastProject', scopeId);
    try {
        showLoading();
        projectState = await apiGet(`/api/projects/${scopeId}/state`);
        if (currentProject !== scopeId) return;
        renderProjectView();
    } catch (error) {
        showError('加载项目状态失败: ' + error.message);
    } finally {
        hideLoading();
    }
}

function renderProjectView() {
    elements.noProjectView.classList.add('hidden');
    elements.projectView.classList.remove('hidden');
    updateRevisionBadge();
    renderContinuationCard();
    renderTasks();
    renderReviews();
    loadPreferences();
    resetReviewForm();
}

function updateRevisionBadge() {
    if (!elements.revisionBadge || !projectState) return;
    elements.revisionBadge.textContent =
        `修订 r${projectState.revision} · 事件 ${projectState.event_count}/${projectState.event_limit}`;
}

function renderContinuationCard() {
    const pendingTasks = projectState.tasks.filter(t => t.status !== 'done');
    const nextStepReviews = projectState.reviews.filter(r => r.next_steps);

    const sourceLine = (sourceId, iso) => `
        <div class="continuation-source">
            来源: <code>${escapeHtml(sourceId || '无')}</code>
            · <time datetime="${escapeHtml(iso || '')}">${escapeHtml(iso || '未知时间')}</time>
            <button class="btn btn-small btn-secondary" data-source-id="${escapeHtml(sourceId || '')}">查看来源</button>
        </div>`;

    let html = '';
    if (pendingTasks.length > 0) {
        html += '<h4>未完成任务</h4>';
        pendingTasks.forEach(task => {
            const statusLabel = task.status === 'doing' ? '进行中' : '待办';
            const due = task.due_date ? `，截止: ${escapeHtml(task.due_date)}` : '';
            html += `<div class="task-item">[${statusLabel}] ${escapeHtml(task.title)}（${escapeHtml(task.owner || '未指定')}${due}）`
                + sourceLine(task.source_id, task.updated_at || task.created_at)
                + '</div>';
        });
    }
    if (nextStepReviews.length > 0) {
        html += '<h4 style="margin-top: 1rem;">下一步</h4>';
        nextStepReviews.forEach(review => {
            html += `<div class="task-item">${escapeHtml(review.next_steps)}`
                + sourceLine(review.source_id, review.updated_at || review.created_at)
                + '</div>';
        });
    }
    if (!html) html = '<p class="empty-text">暂无未完成任务和下一步计划</p>';
    elements.continuationContent.innerHTML = html;

    elements.continuationContent.querySelectorAll('button[data-source-id]').forEach(btn => {
        btn.addEventListener('click', () => viewSource(btn.dataset.sourceId));
    });
}

// ---------------------------------------------------------------------------
// Tasks
// ---------------------------------------------------------------------------
function renderTasks() {
    if (projectState.tasks.length === 0) {
        elements.tasksList.innerHTML = '<div class="empty-state">暂无任务，可在上方添加或导入</div>';
        return;
    }
    elements.tasksList.innerHTML = projectState.tasks.map(task => {
        const statusClass = `status-${task.status}`;
        const statusLabel = task.status === 'todo' ? '待办' : task.status === 'doing' ? '进行中' : '已完成';
        const due = task.due_date ? `, ${escapeHtml(task.due_date)}` : '';
        return `
            <div class="task-row" data-task-id="${escapeHtml(task.id)}">
                <div class="task-title">${escapeHtml(task.title)}</div>
                <div><span class="status-badge ${statusClass}">${statusLabel}</span></div>
                <div class="task-meta">${escapeHtml(task.owner || '未指定')}${due}</div>
                <div class="task-meta">ID: ${escapeHtml(task.id)}</div>
                <div class="task-actions">
                    ${task.status !== 'done' ? `<button class="btn btn-small" data-act="complete">完成</button>` : ''}
                    <button class="btn btn-small btn-secondary" data-act="edit">编辑</button>
                    <button class="btn btn-small btn-secondary" data-act="source">来源</button>
                </div>
            </div>`;
    }).join('');

    elements.tasksList.querySelectorAll('.task-row').forEach(row => {
        const taskId = row.dataset.taskId;
        const task = projectState.tasks.find(t => t.id === taskId);
        row.querySelectorAll('button[data-act]').forEach(btn => {
            btn.addEventListener('click', () => {
                const act = btn.dataset.act;
                if (act === 'complete') completeTask(taskId, btn);
                else if (act === 'edit') editTask(taskId, btn);
                else if (act === 'source') viewSource(task.source_id);
            });
        });
    });
}

async function completeTask(taskId, button) {
    try {
        await submitWrite({
            method: 'PUT',
            path: `/api/projects/${currentProject}/tasks/${encodeURIComponent(taskId)}`,
            button,
            buildBody: (aid, rev) => withControls({ status: 'done' }, aid, rev),
            successMessage: '任务已标记为完成',
        });
    } catch (e) { /* already surfaced */ }
}

async function editTask(taskId, button) {
    const task = projectState.tasks.find(t => t.id === taskId);
    if (!task) return;
    const newTitle = prompt('修改标题:', task.title);
    if (newTitle === null) return;
    const newOwner = prompt('修改负责人:', task.owner || '');
    if (newOwner === null) return;
    const newDue = prompt('修改截止日期 (YYYY-MM-DD):', task.due_date || '');
    if (newDue === null) return;
    try {
        await submitWrite({
            method: 'PUT',
            path: `/api/projects/${currentProject}/tasks/${encodeURIComponent(taskId)}`,
            button,
            buildBody: (aid, rev) => withControls({
                title: newTitle, owner: newOwner, due_date: newDue,
            }, aid, rev),
            successMessage: '任务已更新',
        });
    } catch (e) { /* already surfaced */ }
}

// ---------------------------------------------------------------------------
// Reviews
// ---------------------------------------------------------------------------
function renderReviews() {
    if (projectState.reviews.length === 0) {
        elements.reviewsList.innerHTML = '<div class="empty-state">暂无复盘记录，可在上方手填或导入</div>';
        return;
    }
    elements.reviewsList.innerHTML = projectState.reviews.map(review => `
        <div class="review-item" data-record-id="${escapeHtml(review.record_id)}">
            <div class="review-header">
                <span class="review-id">${escapeHtml(review.record_id)}${review.is_corrected ? '（已修正）' : ''}</span>
                <span class="review-date">${escapeHtml(review.date || '无日期')}</span>
            </div>
            ${review.completed ? `<div class="review-field"><div class="review-label">已完成</div><div class="review-value">${escapeHtml(review.completed)}</div></div>` : ''}
            ${review.blockers ? `<div class="review-field"><div class="review-label">困难</div><div class="review-value">${escapeHtml(review.blockers)}</div></div>` : ''}
            ${review.decisions ? `<div class="review-field"><div class="review-label">决定</div><div class="review-value">${escapeHtml(review.decisions)}</div></div>` : ''}
            ${review.next_steps ? `<div class="review-field"><div class="review-label">下一步</div><div class="review-value">${escapeHtml(review.next_steps)}</div></div>` : ''}
            <div class="review-source">
                来源: <code>${escapeHtml(review.source_id)}</code> · ${escapeHtml(formatTime(review.updated_at || review.created_at))}
                <button class="btn btn-small btn-secondary" data-act="source">查看来源</button>
            </div>
            <div class="review-actions">
                <button class="btn btn-small btn-secondary" data-act="correct">修正记录</button>
                <button class="btn btn-small btn-danger" data-act="withdraw">撤回</button>
            </div>
        </div>
    `).join('');

    elements.reviewsList.querySelectorAll('.review-item').forEach(item => {
        const recordId = item.dataset.recordId;
        const review = projectState.reviews.find(r => r.record_id === recordId);
        item.querySelectorAll('button[data-act]').forEach(btn => {
            btn.addEventListener('click', () => {
                const act = btn.dataset.act;
                if (act === 'source') viewSource(review.source_id);
                else if (act === 'correct') startCorrection(recordId);
                else if (act === 'withdraw') withdrawReview(recordId, btn);
            });
        });
    });
}

async function viewSource(sourceId) {
    if (!sourceId) { showError('该记录没有来源信息'); return; }
    try {
        showLoading();
        const data = await apiGet(`/api/projects/${currentProject}/sources/${encodeURIComponent(sourceId)}`);
        elements.sourceDetailContent.innerHTML = `
            <div><strong>来源 ID:</strong> <code>${escapeHtml(data.source_id)}</code></div>
            <div><strong>事件位置:</strong> ${escapeHtml(data.position)}</div>
            <div><strong>记录时间:</strong> ${escapeHtml(formatTime(data.created_at))}</div>
            <hr style="margin: 1rem 0;">
            <div><strong>来源原文:</strong></div>
            <pre style="white-space: pre-wrap; background: var(--paper-white); padding: 1rem; border-radius: 4px;">${escapeHtml(JSON.stringify(data.content, null, 2))}</pre>
        `;
        elements.sourceModal.classList.remove('hidden');
    } catch (error) {
        showError(error.message);
    } finally {
        hideLoading();
    }
}

function startCorrection(recordId) {
    const review = projectState.reviews.find(r => r.record_id === recordId);
    if (!review) return;
    correctingRecordId = recordId;
    document.getElementById('review-record-id').value = recordId;
    document.getElementById('review-record-id').readOnly = true;
    document.getElementById('review-date').value = review.date || '';
    document.getElementById('review-completed').value = review.completed || '';
    document.getElementById('review-blockers').value = review.blockers || '';
    document.getElementById('review-decisions').value = review.decisions || '';
    document.getElementById('review-next-steps').value = review.next_steps || '';
    document.getElementById('review-correction-banner').classList.remove('hidden');
    document.getElementById('review-correction-target').textContent = recordId;
    document.getElementById('review-submit-btn').textContent = '保存修正';
    document.querySelector('[data-tab="review"]').click();
    showToast('正在修正记录，修改后点击「保存修正」');
}

function resetReviewForm() {
    correctingRecordId = null;
    const form = document.getElementById('review-form');
    if (form) form.reset();
    const idInput = document.getElementById('review-record-id');
    if (idInput) idInput.readOnly = false;
    const banner = document.getElementById('review-correction-banner');
    if (banner) banner.classList.add('hidden');
    const btn = document.getElementById('review-submit-btn');
    if (btn) btn.textContent = '保存复盘';
}

async function withdrawReview(recordId, button) {
    if (!confirm(`确定要撤回记录 "${recordId}" 吗？撤回后不再生效。`)) return;
    try {
        await submitWrite({
            method: 'POST',
            path: `/api/projects/${currentProject}/reviews/${encodeURIComponent(recordId)}/withdraw`,
            button,
            buildBody: (aid, rev) => withControls({}, aid, rev),
            successMessage: '记录已撤回',
        });
    } catch (e) { /* already surfaced */ }
}

// ---------------------------------------------------------------------------
// Import
// ---------------------------------------------------------------------------
async function handleImport(button) {
    const fileInput = document.getElementById('import-file');
    const contentInput = document.getElementById('import-content');
    const formatType = document.getElementById('import-format').value;
    const resultDiv = document.getElementById('import-result');

    const hasFile = fileInput.files.length > 0;
    const hasText = contentInput.value.trim().length > 0;
    if (!hasFile && !hasText) {
        showError('请选择文件或粘贴内容');
        return;
    }

    try {
        const data = await submitWrite({
            method: 'POST',
            path: `/api/projects/${currentProject}/import`,
            button,
            buildBody: (aid, rev) => {
                const fd = new FormData();
                fd.append('format', formatType);
                if (hasFile) fd.append('file', fileInput.files[0]);
                else fd.append('content', contentInput.value);
                fd.append('action_id', aid);
                fd.append('expected_revision', String(rev));
                return fd;
            },
            onSuccess: (d) => {
                resultDiv.className = 'result-message success';
                resultDiv.textContent = `导入成功: ${d.imported_tasks} 个任务, ${d.imported_reviews} 条复盘`;
                resultDiv.style.display = 'block';
                contentInput.value = '';
                fileInput.value = '';
            },
            successMessage: '导入成功',
        });
        return data;
    } catch (e) {
        resultDiv.className = 'result-message error';
        resultDiv.textContent = e.message;
        resultDiv.style.display = 'block';
    }
}

// ---------------------------------------------------------------------------
// Report
// ---------------------------------------------------------------------------
const REPORT_PLACEHOLDER = '点击「生成报告」查看当前项目的报告';

// Single invalidation path: any project switch or revision change drops the
// cached report so copy/download can never export another project's or an
// outdated project's facts.
function invalidateReport(message) {
    currentReport = '';
    reportContext = null;
    elements.reportPreview.textContent = message || REPORT_PLACEHOLDER;
    document.getElementById('btn-copy-report').disabled = true;
    document.getElementById('btn-download-report').disabled = true;
}

function reportIsCurrent() {
    return !!(currentReport && reportContext && projectState
        && reportContext.scopeId === currentProject
        && reportContext.revision === projectState.revision);
}

async function generateReport(button) {
    const scopeId = currentProject;
    if (!scopeId) { showError('请先选择一个项目'); return; }
    try {
        if (button) button.disabled = true;
        showLoading();
        const data = await apiGet(`/api/projects/${scopeId}/report`);
        // The user may have switched projects while the request was in flight.
        if (scopeId !== currentProject) return;
        currentReport = data.report;
        reportContext = { scopeId, revision: projectState ? projectState.revision : null };
        elements.reportPreview.textContent = currentReport;
        document.getElementById('btn-copy-report').disabled = false;
        document.getElementById('btn-download-report').disabled = false;
    } catch (error) {
        showError(error.message);
    } finally {
        if (button) button.disabled = false;
        hideLoading();
    }
}

function copyReport() {
    if (!reportIsCurrent()) {
        invalidateReport('项目或数据已变化，请重新生成当前项目的报告');
        showError('报告已失效，请重新生成后再复制');
        return;
    }
    navigator.clipboard.writeText(currentReport)
        .then(() => showToast('报告已复制到剪贴板'))
        .catch(() => showError('复制失败，请手动选择复制'));
}

function downloadReport() {
    if (!reportIsCurrent()) {
        invalidateReport('项目或数据已变化，请重新生成当前项目的报告');
        showError('报告已失效，请重新生成后再下载');
        return;
    }
    const blob = new Blob([currentReport], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `组会接力报告-${new Date().toISOString().split('T')[0]}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast('报告已下载');
}

// ---------------------------------------------------------------------------
// Preferences (report_title / focus)
// ---------------------------------------------------------------------------
function loadPreferences() {
    const prefs = projectState.preferences || {};
    document.getElementById('pref-report-title').value = prefs.report_title || '';
    document.getElementById('pref-focus').value = prefs.focus === 'next_steps' ? 'next_steps' : 'default';
}

async function savePreferences(button) {
    const preferences = {
        report_title: document.getElementById('pref-report-title').value.trim(),
        focus: document.getElementById('pref-focus').value,
    };
    const resultDiv = document.getElementById('prefs-result');
    try {
        await submitWrite({
            method: 'POST',
            path: `/api/projects/${currentProject}/preferences`,
            button,
            buildBody: (aid, rev) => withControls({ preferences }, aid, rev),
            onSuccess: () => {
                resultDiv.className = 'result-message success';
                resultDiv.textContent = '偏好已保存';
                resultDiv.style.display = 'block';
            },
            successMessage: '偏好已保存',
        });
    } catch (e) {
        resultDiv.className = 'result-message error';
        resultDiv.textContent = e.message;
        resultDiv.style.display = 'block';
    }
}

// ---------------------------------------------------------------------------
// Sample data (atomic markdown import)
// ---------------------------------------------------------------------------
const sampleAlgae = `# 复盘：第一周
记录编号: algae-week-1
日期: 2026-10-26
已完成: 配制三组培养基，设置光照周期
困难: 蓝光灯需要重新标定，盐度波动较大
决定: 先固定盐度再调整光照强度
下一步: 记录七天的生长曲线，采购新的盐度计

| ID | 标题 | 状态 | 负责人 | 截止日期 |
| --- | --- | --- | --- | --- |
| algae-light | 标定蓝光灯 | doing | 小林 | 2026-11-02 |
| algae-curve | 记录生长曲线 | todo | 小周 | 2026-11-06 |
| algae-salt | 采购盐度计 | todo | 小王 | 2026-11-03 |
`;

const sampleCourse = `# 复盘：第三周
记录编号: course-week-3
日期: 2026-10-28
已完成: 完成用户调研，确定核心功能清单
困难: 团队成员时间协调困难，技术选型争议
决定: 采用 React + FastAPI 技术栈，每周二四晚固定会议
下一步: 搭建基础框架，分配模块开发任务

| ID | 标题 | 状态 | 负责人 | 截止日期 |
| --- | --- | --- | --- | --- |
| course-frame | 搭建基础框架 | doing | 张同学 | 2026-11-05 |
| course-auth | 实现用户认证 | todo | 李同学 | 2026-11-08 |
| course-db | 设计数据库模型 | todo | 王同学 | 2026-11-06 |
`;

async function loadSampleData(sampleContent, button) {
    if (!currentProject) {
        showError('请先创建或选择一个项目');
        return;
    }
    try {
        await submitWrite({
            method: 'POST',
            path: `/api/projects/${currentProject}/import`,
            button,
            buildBody: (aid, rev) => {
                const fd = new FormData();
                fd.append('format', 'markdown');
                fd.append('content', sampleContent);
                fd.append('action_id', aid);
                fd.append('expected_revision', String(rev));
                return fd;
            },
            onSuccess: (d) => {
                showToast(d.idempotent || (d.imported_tasks === 0 && d.imported_reviews === 0)
                    ? '样例已在项目中，未产生重复记录'
                    : `样例数据已导入: ${d.imported_tasks} 个任务, ${d.imported_reviews} 条复盘`);
            },
        });
    } catch (e) { /* already surfaced */ }
}

// ---------------------------------------------------------------------------
// Refresh
// ---------------------------------------------------------------------------
async function refreshState() {
    if (!currentProject) return;
    const scopeId = currentProject;
    const state = await apiGet(`/api/projects/${scopeId}/state`);
    if (scopeId !== currentProject) return;
    projectState = state;
    if (reportContext && (reportContext.scopeId !== scopeId
        || reportContext.revision !== projectState.revision)) {
        invalidateReport('数据已更新（修订版变化），旧报告已失效，请重新生成当前项目的报告');
    }
    updateRevisionBadge();
    renderContinuationCard();
    renderTasks();
    renderReviews();
    loadPreferences();
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    loadProjects();
    const lastProject = localStorage.getItem('lastProject');
    if (lastProject) selectProject(lastProject);

    document.getElementById('btn-new-project').addEventListener('click', () => {
        elements.newProjectModal.classList.remove('hidden');
    });

    document.getElementById('new-project-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const name = document.getElementById('new-project-name').value.trim();
        const summary = document.getElementById('new-project-summary').value.trim();
        if (!name) { showError('项目名称不能为空'); return; }
        const button = e.target.querySelector('button[type="submit"]');
        try {
            if (button) button.disabled = true;
            showLoading();
            const response = await fetch('/api/projects', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, summary }),
            });
            if (!response.ok) {
                const err = await response.json().catch(() => ({ detail: '创建失败' }));
                throw new Error(err.detail || '创建失败');
            }
            const data = await response.json();
            elements.newProjectModal.classList.add('hidden');
            document.getElementById('new-project-form').reset();
            await loadProjects();
            await selectProject(data.scope_id);
            showToast('项目创建成功');
        } catch (error) {
            showError(error.message);
        } finally {
            if (button) button.disabled = false;
            hideLoading();
        }
    });

    document.querySelectorAll('.modal-close').forEach(btn => {
        btn.addEventListener('click', () => btn.closest('.modal').classList.add('hidden'));
    });

    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const tabName = btn.dataset.tab;
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            document.getElementById(`tab-${tabName}`).classList.add('active');
        });
    });

    // Task create form
    document.getElementById('task-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const button = e.target.querySelector('button[type="submit"]');
        const taskData = {
            task_id: document.getElementById('task-id').value.trim(),
            title: document.getElementById('task-title').value.trim(),
            status: document.getElementById('task-status').value,
            owner: document.getElementById('task-owner').value.trim(),
            due_date: document.getElementById('task-due').value,
        };
        try {
            await submitWrite({
                method: 'POST',
                path: `/api/projects/${currentProject}/tasks`,
                button,
                buildBody: (aid, rev) => withControls(taskData, aid, rev),
                onSuccess: () => document.getElementById('task-form').reset(),
                successMessage: '任务已添加',
            });
        } catch (err) { /* already surfaced */ }
    });

    // Review create / correction form
    document.getElementById('review-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const button = document.getElementById('review-submit-btn');
        const reviewData = {
            record_id: document.getElementById('review-record-id').value.trim(),
            date: document.getElementById('review-date').value,
            completed: document.getElementById('review-completed').value.trim(),
            blockers: document.getElementById('review-blockers').value.trim(),
            decisions: document.getElementById('review-decisions').value.trim(),
            next_steps: document.getElementById('review-next-steps').value.trim(),
        };
        const isCorrection = correctingRecordId !== null;
        const path = isCorrection
            ? `/api/projects/${currentProject}/reviews/${encodeURIComponent(correctingRecordId)}/correct`
            : `/api/projects/${currentProject}/reviews`;
        try {
            await submitWrite({
                method: 'POST',
                path,
                button,
                buildBody: (aid, rev) => withControls(reviewData, aid, rev),
                onSuccess: () => resetReviewForm(),
                successMessage: isCorrection ? '记录已修正' : '复盘已保存',
            });
        } catch (err) { /* already surfaced */ }
    });

    document.getElementById('btn-cancel-correction').addEventListener('click', resetReviewForm);

    document.getElementById('btn-import').addEventListener('click', (e) => handleImport(e.currentTarget));

    document.getElementById('btn-generate-report').addEventListener('click', (e) => generateReport(e.currentTarget));
    document.getElementById('btn-copy-report').addEventListener('click', copyReport);
    document.getElementById('btn-download-report').addEventListener('click', downloadReport);

    document.getElementById('preferences-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        await savePreferences(e.target.querySelector('button[type="submit"]'));
    });

    document.getElementById('btn-load-algae').addEventListener('click', (e) => loadSampleData(sampleAlgae, e.currentTarget));
    document.getElementById('btn-load-course').addEventListener('click', (e) => loadSampleData(sampleCourse, e.currentTarget));

    document.getElementById('btn-refresh').addEventListener('click', async () => {
        try { showLoading(); await refreshState(); showToast('已刷新到最新状态'); }
        catch (error) { showError(error.message); }
        finally { hideLoading(); }
    });

    elements.sourceModal.addEventListener('click', (e) => {
        if (e.target === elements.sourceModal) elements.sourceModal.classList.add('hidden');
    });
    elements.newProjectModal.addEventListener('click', (e) => {
        if (e.target === elements.newProjectModal) elements.newProjectModal.classList.add('hidden');
    });
});