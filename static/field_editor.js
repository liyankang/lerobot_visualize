const $ = (id) => document.getElementById(id);
let progressTimer = null;
let browseTarget = null;
let currentFeatures = [];
let activeTab = 'rename';

function readNumber(id) {
    const v = $(id).value.trim();
    return v === '' ? null : Number(v);
}

function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function status(text, kind = '') {
    const el = $('status');
    el.textContent = text || '';
    el.className = `status ${kind}`;
    el.style.display = text ? 'block' : 'none';
}

async function post(url, body) {
    const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
}

function setBusy(busy) {
    $('preview-btn').disabled = busy;
    $('run-btn').disabled = busy;
}

// ───────────────── 进度轮询 ─────────────────

function updateProgress(data) {
    $('progress-box').style.display = 'block';
    $('progress-title').textContent = data.title || '处理中';
    $('progress-detail').textContent = data.detail || '';
    const pct = data.percent == null ? 0 : Math.max(0, Math.min(100, Number(data.percent)));
    $('progress-fill').style.width = `${pct}%`;
    const count = data.total ? ` · ${data.current || 0}/${data.total}` : '';
    $('progress-meta').textContent = `${pct}%${count}`;
}

function startProgressPolling() {
    stopProgressPolling(false);
    $('progress-box').style.display = 'block';
    updateProgress({ title: '正在启动', detail: '等待服务端开始处理...', percent: 0 });
    progressTimer = setInterval(async () => {
        try {
            const res = await fetch('/api/save_progress');
            const data = await res.json();
            updateProgress(data);
            if (data.stage === 'done' || data.stage === 'error') {
                stopProgressPolling(true);
            }
        } catch (_e) { /* keep polling */ }
    }, 600);
}

function stopProgressPolling(keepVisible = true) {
    if (progressTimer) {
        clearInterval(progressTimer);
        progressTimer = null;
    }
    if (!keepVisible) $('progress-box').style.display = 'none';
}

// ───────────────── Tab 切换 ─────────────────

document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
        activeTab = tab.dataset.tab;
        document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t === tab));
        document.querySelectorAll('.pane').forEach((p) => p.classList.remove('active'));
        $(`pane-${activeTab}`).classList.add('active');
        // 赋值模式下根据 mode 显隐字段
        if (activeTab === 'assign') updateAssignModeFields();
    });
});

// ───────────────── 重命名行管理 ─────────────────

function addRenameRow(oldVal = '', newVal = '') {
    const row = document.createElement('div');
    row.className = 'rename-row';
    row.innerHTML = `
        <input type="text" class="rn-old" placeholder="原字段名" value="${esc(oldVal)}">
        <span class="arrow">→</span>
        <input type="text" class="rn-new" placeholder="新字段名" value="${esc(newVal)}">
        <button class="del-btn" type="button" title="删除此行">×</button>
    `;
    row.querySelector('.del-btn').addEventListener('click', () => {
        if (document.querySelectorAll('.rename-row').length > 1) {
            row.remove();
        } else {
            row.querySelectorAll('input').forEach((i) => (i.value = ''));
        }
    });
    $('rename-list').appendChild(row);
}

$('add-rename-row').addEventListener('click', () => addRenameRow());

function collectRenames() {
    const rows = document.querySelectorAll('.rename-row');
    const result = [];
    rows.forEach((row) => {
        const oldV = row.querySelector('.rn-old').value.trim();
        const newV = row.querySelector('.rn-new').value.trim();
        if (oldV && newV) result.push({ from: oldV, to: newV });
    });
    return result;
}

// ───────────────── 赋值模式切换 ─────────────────

function updateAssignModeFields() {
    const mode = $('assign-mode').value;
    $('assign-constant-field').style.display = mode === 'constant' ? '' : 'none';
    $('assign-source-field').style.display = mode === 'copy' ? '' : 'none';
    $('assign-expr-field').style.display = mode === 'expr' ? '' : 'none';
}

$('assign-mode').addEventListener('change', updateAssignModeFields);

// ───────────────── 预览 ─────────────────

async function preview() {
    const inputPath = $('input-path').value.trim();
    if (!inputPath) { status('请先填写输入数据集路径', 'error'); return; }
    setBusy(true);
    status('正在加载字段列表...');
    try {
        const data = await post('/api/field_editor/preview', { input_path: inputPath });
        currentFeatures = data.features || [];
        renderFeatures(data);
        status(`已加载 ${currentFeatures.length} 个字段`, 'ok');
    } catch (e) {
        status(`加载失败: ${e.message}`, 'error');
    } finally {
        setBusy(false);
    }
}

function renderFeatures(data) {
    const statRow = $('stat-row');
    statRow.innerHTML = `
        <div class="stat-chip"><b>${data.episode_count ?? 0}</b>episodes</div>
        <div class="stat-chip"><b>${data.total_frames ?? 0}</b>frames</div>
        <div class="stat-chip"><b>${(data.features || []).length}</b>字段</div>
    `;
    const feats = data.features || [];
    if (!feats.length) {
        $('fields-panel').innerHTML = '<div class="empty">数据集中没有字段</div>';
        return;
    }
    const rows = feats.map((f) => {
        const shapeStr = (f.shape || []).join('×') || '-';
        const dim = (f.shape || []).length ? f.shape[f.shape.length - 1] : 1;
        const badges = [];
        if (f.protected) badges.push('<span class="badge badge-protected">训练必需</span>');
        if (f.is_image) badges.push('<span class="badge badge-image">image/video</span>');
        if (dim > 1) badges.push(`<span class="badge badge-vector">vector(${dim})</span>`);
        else badges.push('<span class="badge badge-scalar">scalar</span>');
        const namesStr = (f.names && f.names.length) ? esc(f.names.join(', ')) : '';
        const sampleStr = f.sample === null || f.sample === undefined
            ? '' : (Array.isArray(f.sample) ? '[' + f.sample.map(esc).join(', ') + ']' : esc(f.sample));
        return `
            <tr>
                <td class="key-cell">${esc(f.key)}</td>
                <td>${badges.join(' ')}</td>
                <td>${esc(f.dtype || '-')}</td>
                <td>${shapeStr}</td>
                <td class="sample-cell">${namesStr || '-'}</td>
                <td class="sample-cell">${sampleStr}</td>
            </tr>
        `;
    }).join('');
    $('fields-panel').innerHTML = `
        <table class="fields">
            <thead>
                <tr>
                    <th>字段名</th><th>类型</th><th>dtype</th><th>shape</th>
                    <th>维度名</th><th>样例值</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>
    `;
}

// ───────────────── 执行 ─────────────────

function parseValueList(text) {
    const t = (text || '').trim();
    if (!t) return null;
    if (t.includes(',')) {
        return t.split(',').map((s) => Number(s.trim())).filter((n) => !Number.isNaN(n));
    }
    const n = Number(t);
    return Number.isNaN(n) ? null : n;
}

function parseEpisodeIndices(text) {
    const t = (text || '').trim();
    if (!t) return null;
    return t.split(/[,\s;]+/).map((s) => parseInt(s.trim(), 10)).filter((n) => !Number.isNaN(n));
}

async function run() {
    const inputPath = $('input-path').value.trim();
    const outputPath = $('output-path').value.trim();
    if (!inputPath) { status('请填写输入数据集路径', 'error'); return; }
    if (!outputPath) { status('请填写输出数据集路径', 'error'); return; }

    const common = {
        input_path: inputPath,
        output_path: outputPath,
        skip_video_stats: $('skip-video-stats').checked,
    };

    let url = '';
    let body = {};
    try {
        switch (activeTab) {
            case 'rename': {
                const renames = collectRenames();
                if (!renames.length) throw new Error('请至少填写一组重命名规则');
                url = '/api/field_editor/rename';
                body = { ...common, renames, rename_names: $('rename-names').checked };
                break;
            }
            case 'add': {
                const name = $('add-name').value.trim();
                if (!name) throw new Error('请填写字段名');
                const shapeN = parseInt($('add-shape').value, 10) || 1;
                url = '/api/field_editor/add';
                body = {
                    ...common,
                    field_name: name,
                    dtype: $('add-dtype').value,
                    shape: [shapeN],
                    default: parseValueList($('add-default').value) ?? 0,
                    names: ($('add-names').value.trim() || undefined)
                        ? $('add-names').value.trim().split(',').map((s) => s.trim())
                        : undefined,
                };
                break;
            }
            case 'delete': {
                const names = $('delete-names').value
                    .split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
                if (!names.length) throw new Error('请填写至少一个要删除的字段名');
                url = '/api/field_editor/delete';
                body = {
                    ...common,
                    field_names: names,
                    allow_delete_protected: $('allow-delete-protected').checked,
                };
                break;
            }
            case 'assign': {
                const target = $('assign-target').value.trim();
                if (!target) throw new Error('请填写目标字段');
                const mode = $('assign-mode').value;
                url = '/api/field_editor/assign';
                body = {
                    ...common,
                    target,
                    mode,
                    episode_indices: parseEpisodeIndices($('assign-episodes').value),
                };
                if (mode === 'constant') body.value = parseValueList($('assign-value').value) ?? 0;
                if (mode === 'copy') body.source = $('assign-source').value.trim();
                if (mode === 'expr') {
                    body.expression = $('assign-expr').value.trim();
                    body.source = $('assign-source').value.trim() || undefined;
                }
                break;
            }
            default:
                throw new Error('未知操作');
        }
    } catch (e) {
        status(e.message, 'error');
        return;
    }

    setBusy(true);
    status(`正在执行 ${activeTab} ...`);
    startProgressPolling();
    try {
        const data = await post(url, body);
        const detail = data.result
            ? ' ' + Object.entries(data.result)
                .map(([k, v]) => `${k}: ${JSON.stringify(v)}`).join(' | ')
            : '';
        status(`完成。已保存到: ${data.path}${detail}`, 'ok');
    } catch (e) {
        status(`执行失败: ${e.message}`, 'error');
        updateProgress({ title: '执行失败', detail: e.message, percent: 0 });
    } finally {
        stopProgressPolling(true);
        setBusy(false);
    }
}

// ───────────────── 目录浏览 ─────────────────

async function openBrowse(targetInputId) {
    browseTarget = targetInputId;
    const initial = $(targetInputId).value.trim();
    $('browse-title').textContent = targetInputId === 'output-path'
        ? '选择/输入输出目录'
        : '选择输入数据集目录';
    $('browse-modal').classList.add('active');
    await browseTo(initial);
}

async function browseTo(path) {
    try {
        const resp = await fetch(`/api/browse?path=${encodeURIComponent(path || '')}`);
        const data = await resp.json();
        if (data.error) {
            $('browse-current').textContent = data.error;
            $('browse-list').innerHTML = '';
            return;
        }
        $('browse-current').textContent = data.current || '/';
        $('browse-input').value = data.current || '';
        const list = $('browse-list');
        list.innerHTML = '';
        if (data.parent !== undefined && data.parent !== data.current) {
            const up = document.createElement('div');
            up.className = 'dir-item parent';
            up.textContent = '↑ 上级目录';
            up.addEventListener('click', () => browseTo(data.parent));
            list.appendChild(up);
        }
        for (const d of (data.dirs || [])) {
            const item = document.createElement('div');
            item.className = 'dir-item';
            item.textContent = '[DIR] ' + d.name;
            item.addEventListener('click', () => browseTo(d.path));
            list.appendChild(item);
        }
    } catch (e) {
        status(`浏览失败: ${e.message}`, 'error');
    }
}

// ───────────────── 事件绑定 ─────────────────

$('preview-btn').addEventListener('click', preview);
$('run-btn').addEventListener('click', run);

$('input-browse').addEventListener('click', () => openBrowse('input-path'));
$('output-browse').addEventListener('click', () => openBrowse('output-path'));
$('browse-input').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') browseTo($('browse-input').value.trim());
});
$('browse-cancel').addEventListener('click', () => $('browse-modal').classList.remove('active'));
$('browse-ok').addEventListener('click', () => {
    if (browseTarget) {
        $(browseTarget).value = $('browse-input').value.trim() || $('browse-current').textContent.trim();
    }
    $('browse-modal').classList.remove('active');
});

updateAssignModeFields();
