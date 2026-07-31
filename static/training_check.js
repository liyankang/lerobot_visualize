const $ = id => document.getElementById(id);

const STATE = {
    report: null,
    filter: 'all',
};

function escHtml(value) {
    const div = document.createElement('div');
    div.textContent = value ?? '';
    return div.innerHTML;
}

function setStatus(message, type = 'info') {
    const el = $('status');
    el.textContent = message;
    el.className = `status ${type}`;
}

function fmt(value) {
    if (value === null || value === undefined || value === '') return '--';
    if (typeof value === 'number') return Number.isInteger(value) ? value.toLocaleString('zh-CN') : value.toFixed(4);
    return String(value);
}

function defaultFixedPath(path) {
    const text = String(path || '').trim().replace(/[\\/]+$/, '');
    return text ? `${text}_fixed` : '';
}

async function postJson(url, body) {
    const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
}

function summaryCard(label, value, cls = '') {
    return `
        <div class="summary-card">
            <div class="label">${escHtml(label)}</div>
            <div class="value ${escHtml(cls)}">${escHtml(fmt(value))}</div>
        </div>
    `;
}

function renderInspect(data) {
    const s = data.summary || {};
    const files = data.files || {};
    $('inspect-summary').innerHTML = [
        summaryCard('Version', s.codebase_version),
        summaryCard('FPS', s.fps),
        summaryCard('Robot', s.robot_type),
        summaryCard('Episodes', s.total_episodes),
        summaryCard('Frames', s.total_frames),
        summaryCard('Parquets', files.episode_parquets),
        summaryCard('Videos', files.videos),
        summaryCard('Features', s.feature_count),
    ].join('');

    $('feature-tags').innerHTML = (data.features || [])
        .map(name => `<span class="tag">${escHtml(name)}</span>`)
        .join('');
    $('inspect-panel').classList.remove('hidden');
}

function levelOrder(level) {
    return { error: 0, warn: 1, info: 2, pass: 3 }[level] ?? 4;
}

function renderReport(report) {
    STATE.report = report;
    const summary = report.summary || {};
    const fixableCount = (report.checks || []).filter(item => item.fixable && item.level !== 'pass').length;
    $('result-summary').innerHTML = [
        summaryCard('Status', String(report.status || '').toUpperCase(), report.status || ''),
        summaryCard('ERROR', summary.error || 0, 'error'),
        summaryCard('WARN', summary.warn || 0, 'warn'),
        summaryCard('PASS', summary.pass || 0, 'pass'),
        summaryCard('Fixable', fixableCount),
        summaryCard('Profile', report.profile || 'general'),
    ].join('');
    $('result-panel').classList.remove('hidden');
    updateFixPanel(fixableCount);
    renderChecks();
}

function updateFixPanel(fixableCount = 0) {
    const panel = $('fix-panel');
    const output = $('fix-output-path');
    const path = $('dataset-path').value.trim();
    if (!panel || !output) return;
    if (STATE.report && fixableCount > 0) {
        panel.classList.remove('hidden');
        if (!output.value.trim()) output.value = defaultFixedPath(path);
    } else {
        panel.classList.add('hidden');
    }
}

function renderChecks() {
    const report = STATE.report;
    if (!report) return;
    const checks = (report.checks || [])
        .filter(item => STATE.filter === 'all' || item.level === STATE.filter)
        .sort((a, b) => levelOrder(a.level) - levelOrder(b.level) || String(a.id).localeCompare(String(b.id)));

    $('check-list').innerHTML = checks.length
        ? checks.map(item => `
            <article class="check-item ${escHtml(item.level)}">
                <div class="check-head">
                    <span class="level ${escHtml(item.level)}">${escHtml(String(item.level).toUpperCase())}</span>
                    <span class="check-title">${escHtml(item.title)}${item.fixable && item.level !== 'pass' ? '<span class="fixable">可修复</span>' : ''}</span>
                    <span class="check-id">${escHtml(item.id)}</span>
                </div>
                <div class="check-detail">${escHtml(item.detail)}</div>
            </article>
        `).join('')
        : '<div class="check-item"><div class="check-detail">当前筛选下没有检查项。</div></div>';
}

async function inspectDataset() {
    const path = $('dataset-path').value.trim();
    if (!path) {
        setStatus('请先输入数据集路径。', 'error');
        return;
    }
    $('inspect-btn').disabled = true;
    setStatus('正在扫描数据集结构...', 'info');
    try {
        const data = await postJson('/api/training-check/inspect', { path });
        renderInspect(data);
        localStorage.setItem('lerobot-training-check-last-path', path);
        setStatus('扫描完成，可以开始训练可用性检查。', 'success');
    } catch (error) {
        setStatus(error.message || '扫描失败', 'error');
    } finally {
        $('inspect-btn').disabled = false;
    }
}

async function runCheck() {
    const path = $('dataset-path').value.trim();
    if (!path) {
        setStatus('请先输入数据集路径。', 'error');
        return;
    }
    $('run-btn').disabled = true;
    $('inspect-btn').disabled = true;
    setStatus('正在检查 parquet 字段、数据类型、task、timestamp 和 stats...', 'info');
    try {
        const report = await postJson('/api/training-check/run', {
            path,
            profile: $('profile').value,
            include_videos: $('include-videos').checked,
            max_issue_examples: Number($('max-examples').value) || 5,
        });
        renderReport(report);
        const statusType = report.status === 'error' ? 'error' : report.status === 'warn' ? 'warn' : 'success';
        const s = report.summary || {};
        setStatus(`检查完成: ERROR ${s.error || 0}, WARN ${s.warn || 0}, PASS ${s.pass || 0}`, statusType);
        localStorage.setItem('lerobot-training-check-last-path', path);
    } catch (error) {
        setStatus(error.message || '检查失败', 'error');
    } finally {
        $('run-btn').disabled = false;
        $('inspect-btn').disabled = false;
    }
}

async function fixDataset() {
    const path = $('dataset-path').value.trim();
    const outputPath = $('fix-output-path').value.trim();
    if (!path) {
        setStatus('请先输入数据集路径。', 'error');
        return;
    }
    if (!outputPath) {
        setStatus('请填写修复输出路径。', 'error');
        return;
    }

    $('fix-btn').disabled = true;
    $('run-btn').disabled = true;
    $('inspect-btn').disabled = true;
    setStatus('正在复制数据集并修复格式字段、task 索引和 stats...', 'info');
    try {
        const result = await postJson('/api/training-check/fix', {
            path,
            output_path: outputPath,
            overwrite: $('fix-overwrite').checked,
            profile: $('profile').value,
        });
        $('dataset-path').value = result.output_path;
        $('fix-output-path').value = defaultFixedPath(result.output_path);
        localStorage.setItem('lerobot-training-check-last-path', result.output_path);
        if (result.report) renderReport(result.report);
        const actionText = (result.actions || []).join('；');
        setStatus(`修复完成: ${result.output_path}${actionText ? `。${actionText}` : ''}`, 'success');
    } catch (error) {
        setStatus(error.message || '修复失败', 'error');
    } finally {
        $('fix-btn').disabled = false;
        $('run-btn').disabled = false;
        $('inspect-btn').disabled = false;
    }
}

function bindTabs() {
    document.querySelectorAll('.tab[data-filter]').forEach(button => {
        button.addEventListener('click', event => {
            STATE.filter = event.currentTarget.dataset.filter;
            document.querySelectorAll('.tab[data-filter]').forEach(tab => {
                tab.classList.toggle('active', tab === event.currentTarget);
            });
            renderChecks();
        });
    });
}

window.addEventListener('DOMContentLoaded', () => {
    const lastPath = localStorage.getItem('lerobot-training-check-last-path');
    if (lastPath) $('dataset-path').value = lastPath;
    $('inspect-btn').addEventListener('click', inspectDataset);
    $('run-btn').addEventListener('click', runCheck);
    $('fix-btn').addEventListener('click', fixDataset);
    $('dataset-path').addEventListener('keydown', event => {
        if (event.key === 'Enter') inspectDataset();
    });
    $('dataset-path').addEventListener('input', event => {
        const output = $('fix-output-path');
        if (output && !output.value.trim()) output.value = defaultFixedPath(event.target.value);
    });
    bindTabs();
});
