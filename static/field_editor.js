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
        // 切换 Tab 时隐藏旧预览面板
        const panel = $('dry-run-panel');
        if (panel) panel.style.display = 'none';
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

// ───────────────── Dry-run 预览 ─────────────────

function buildBodyForActiveTab() {
    const inputPath = $('input-path').value.trim();
    if (!inputPath) throw new Error('请先填写输入数据集路径');
    const common = { input_path: inputPath };
    switch (activeTab) {
        case 'rename': {
            const renames = collectRenames();
            if (!renames.length) throw new Error('请至少填写一组重命名规则');
            return { ...common, renames, rename_names: $('rename-names').checked };
        }
        case 'names': {
            const field = $('names-field').value.trim();
            if (!field) throw new Error('请填写要修改维度名的字段');
            const namesText = $('names-list').value.trim();
            if (!namesText) throw new Error('请填写新的维度名');
            const newNames = namesText.split(',').map((s) => s.trim()).filter(Boolean);
            if (newNames.length === 0) throw new Error('维度名不能为空');
            return { ...common, field_name: field, new_names: newNames };
        }
        case 'add': {
            const name = $('add-name').value.trim();
            if (!name) throw new Error('请填写字段名');
            const shapeN = parseInt($('add-shape').value, 10) || 1;
            return {
                ...common,
                field_name: name,
                dtype: $('add-dtype').value,
                shape: [shapeN],
                default: parseValueList($('add-default').value) ?? 0,
                names: ($('add-names').value.trim() || undefined)
                    ? $('add-names').value.trim().split(',').map((s) => s.trim())
                    : undefined,
            };
        }
        case 'delete': {
            const names = $('delete-names').value
                .split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
            if (!names.length) throw new Error('请填写至少一个要删除的字段名');
            return {
                ...common,
                field_names: names,
                allow_delete_protected: $('allow-delete-protected').checked,
            };
        }
        case 'assign': {
            const target = $('assign-target').value.trim();
            if (!target) throw new Error('请填写目标字段');
            const mode = $('assign-mode').value;
            const body = {
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
            return body;
        }
        default:
            throw new Error('未知操作');
    }
}

const DRY_RUN_URL = {
    rename: '/api/field_editor/preview_rename',
    names: '/api/field_editor/preview_rename_names',
    add: '/api/field_editor/preview_add',
    delete: '/api/field_editor/preview_delete',
    assign: '/api/field_editor/preview_assign',
};

function fmtSample(v) {
    if (v === null || v === undefined) return '';
    if (Array.isArray(v)) return '[' + v.map(esc).join(', ') + ']';
    return esc(v);
}

function renderFeaturesTable(features, extraRowClass = (() => (f) => '')) {
    if (!features || !features.length) {
        return '<div class="empty">无字段</div>';
    }
    const rows = features.map((f) => {
        const shapeStr = (f.shape || []).join('×') || '-';
        const dim = (f.shape || []).length ? f.shape[f.shape.length - 1] : 1;
        const badges = [];
        if (f.protected) badges.push('<span class="badge badge-protected">训练必需</span>');
        if (f.is_image) badges.push('<span class="badge badge-image">image/video</span>');
        if (dim > 1) badges.push(`<span class="badge badge-vector">vector(${dim})</span>`);
        else badges.push('<span class="badge badge-scalar">scalar</span>');
        const namesStr = (f.names && f.names.length) ? esc(f.names.join(', ')) : '';
        const sampleStr = fmtSample(f.sample);
        const rowCls = typeof extraRowClass === 'function' ? extraRowClass(f) : '';
        return `
            <tr class="${rowCls}">
                <td class="key-cell">${esc(f.key)}</td>
                <td>${badges.join(' ')}</td>
                <td>${esc(f.dtype || '-')}</td>
                <td>${shapeStr}</td>
                <td class="sample-cell">${namesStr || '-'}</td>
                <td class="sample-cell">${sampleStr}</td>
            </tr>
        `;
    }).join('');
    return `
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

function renderRenameDiff(result) {
    const applied = result.applied || [];
    const skipped = result.skipped || [];
    const beforeKeys = new Set((result.fields_before || []).map((f) => f.key));
    const afterKeys = new Set((result.fields_after || []).map((f) => f.key));
    const renameMap = new Map(applied.map((r) => [r.old, r.new]));
    const rowClass = (f) => {
        if (!beforeKeys.has(f.key) && afterKeys.has(f.key)) return 'diff-add';
        if (beforeKeys.has(f.key) && !afterKeys.has(f.key)) return 'diff-del';
        return 'diff-keep';
    };
    const afterTable = renderFeaturesTable(result.fields_after || [], rowClass);

    const lines = [];
    if (applied.length) {
        lines.push(`✓ 将重命名 ${applied.length} 个字段：`);
        applied.forEach((r) => {
            lines.push(`  ${r.old} → ${r.new}  (影响 ${r.episodes_affected} 个 episode)`);
        });
    }
    if (skipped.length) {
        lines.push(`⚠ 跳过 ${skipped.length} 项：`);
        skipped.forEach((s) => lines.push(`  ${s.old} → ${s.new}：${s.reason}`));
    }
    const summaryCls = skipped.length ? 'has-skip' : '';

    return `
        <div class="dry-run-summary ${summaryCls}">${esc(lines.join('\n'))}</div>
        <h4 style="font-size:12px;color:#52616d;margin:10px 0 6px;">重命名后的字段列表（绿色=新增/改名，红色=将被删除的旧名）</h4>
        ${afterTable}
    `;
}

function renderAddDiff(result) {
    const field = result.field || '?';
    const sampleRows = result.sample_rows || [];
    const isVector = !!result.is_vector;
    const lines = [];
    lines.push(`✓ 将添加字段 ${field}`);
    lines.push(`  dtype=${result.dtype || '-'}, shape=[${(result.shape || []).join(',') || '?'}], ` +
        `${isVector ? '向量' : '标量'}`);
    lines.push(`  将在 ${result.rows_added || 0} 行填充默认值`);

    const sampleHtml = sampleRows.length
        ? sampleRows.map(fmtSample).join('<br>')
        : '<span class="sample-empty">无样例</span>';

    return `
        <div class="dry-run-summary">${esc(lines.join('\n'))}</div>
        <div class="sample-compare">
            <div class="sample-block after">
                <h4>新字段 ${esc(field)} 样例</h4>
                <div class="sample-list">${sampleHtml}</div>
            </div>
        </div>
        <h4 style="font-size:12px;color:#52616d;margin:10px 0 6px;">添加后的字段列表（绿色=新增）</h4>
        ${renderFeaturesTable(result.fields_after || [],
            (f) => (f.key === field ? 'diff-add' : 'diff-keep'))}
    `;
}

function renderDeleteDiff(result) {
    const deleted = result.deleted || [];
    const skipped = result.skipped || [];
    const lines = [];
    if (deleted.length) {
        lines.push(`✓ 将删除 ${deleted.length} 个字段：`);
        deleted.forEach((d) => lines.push(`  ${d.field}  (影响 ${d.episodes_affected} 个 episode)`));
    }
    if (skipped.length) {
        lines.push(`⚠ 跳过 ${skipped.length} 项：`);
        skipped.forEach((s) => lines.push(`  ${s.field}：${s.reason}`));
    }
    const summaryCls = skipped.length ? 'has-skip' : (deleted.length ? '' : 'has-err');

    return `
        <div class="dry-run-summary ${summaryCls}">${esc(lines.join('\n'))}</div>
        <h4 style="font-size:12px;color:#52616d;margin:10px 0 6px;">删除后的字段列表（红色=已删除）</h4>
        ${renderFeaturesTable(result.fields_after || [])}
    `;
}

function renderNamesDiff(result) {
    const field = result.field || '?';
    const dim = result.dim || 0;
    const oldNames = result.old_names || [];
    const newNames = result.new_names || [];
    const lines = [];
    lines.push(`✓ 将修改字段 ${field} 的 ${dim} 个维度名`);
    if (!oldNames.length) {
        lines.push('  原数据没有维度名，将新增');
    } else if (oldNames.length !== newNames.length) {
        lines.push(`  ⚠ 原维度数(${oldNames.length})与新维度数(${newNames.length})不一致`);
    }
    const pairs = Math.max(oldNames.length, newNames.length);
    for (let i = 0; i < pairs; i++) {
        const o = oldNames[i] !== undefined ? oldNames[i] : '(空)';
        const n = newNames[i] !== undefined ? newNames[i] : '(空)';
        const mark = o === n ? '  ' : '→';
        lines.push(`  [${i}] ${o} ${mark} ${n}`);
    }

    return `
        <div class="dry-run-summary">${esc(lines.join('\n'))}</div>
        <div class="sample-compare">
            <div class="sample-block before">
                <h4>修改前 ${esc(field)} 维度名</h4>
                <div class="sample-list">${oldNames.length ? esc(oldNames.join(', ')) : '<span class="sample-empty">无</span>'}</div>
            </div>
            <div class="sample-block after">
                <h4>修改后 ${esc(field)} 维度名</h4>
                <div class="sample-list">${newNames.length ? esc(newNames.join(', ')) : '<span class="sample-empty">无</span>'}</div>
            </div>
        </div>
    `;
}

function renderAssignDiff(result) {
    const target = result.target || '?';
    const before = result.before_rows || [];
    const after = result.after_rows || [];
    const lines = [];
    lines.push(`✓ 将对字段 ${target} 批量赋值`);
    lines.push(`  模式: ${result.mode}`);
    lines.push(`  影响 ${result.episodes_changed || 0} 个 episode，共 ${result.rows_changed || 0} 行`);
    const summaryCls = (result.episodes_changed || 0) === 0 ? 'has-err' : '';

    const beforeHtml = before.length ? before.map(fmtSample).join('<br>') : '<span class="sample-empty">无数据</span>';
    const afterHtml = after.length ? after.map(fmtSample).join('<br>') : '<span class="sample-empty">无数据</span>';

    return `
        <div class="dry-run-summary ${summaryCls}">${esc(lines.join('\n'))}</div>
        <div class="sample-compare">
            <div class="sample-block before">
                <h4>修改前 ${esc(target)} (前3行)</h4>
                <div class="sample-list">${beforeHtml}</div>
            </div>
            <div class="sample-block after">
                <h4>修改后 ${esc(target)} (前3行)</h4>
                <div class="sample-list">${afterHtml}</div>
            </div>
        </div>
    `;
}

const DIFF_RENDERERS = {
    rename: renderRenameDiff,
    names: renderNamesDiff,
    add: renderAddDiff,
    delete: renderDeleteDiff,
    assign: renderAssignDiff,
};

async function dryRun() {
    const inputPath = $('input-path').value.trim();
    if (!inputPath) { status('请先填写输入数据集路径', 'error'); return; }
    let body, url;
    try {
        body = buildBodyForActiveTab();
        url = DRY_RUN_URL[activeTab];
    } catch (e) {
        status(e.message, 'error');
        return;
    }
    setBusy(true);
    status('正在生成预览...');
    try {
        const data = await post(url, body);
        const panel = $('dry-run-panel');
        const content = $('dry-run-content');
        const renderer = DIFF_RENDERERS[activeTab] || (() => `<pre>${esc(JSON.stringify(data.result, null, 2))}</pre>`);
        content.innerHTML = renderer(data.result || {});
        panel.style.display = 'block';
        status(`预览已生成（未写盘）`, 'ok');
    } catch (e) {
        status(`预览失败: ${e.message}`, 'error');
        $('dry-run-panel').style.display = 'none';
    } finally {
        setBusy(false);
    }
}

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

    const RUN_URL = {
        rename: '/api/field_editor/rename',
        names: '/api/field_editor/rename_names',
        add: '/api/field_editor/add',
        delete: '/api/field_editor/delete',
        assign: '/api/field_editor/assign',
    };

    let body, url;
    try {
        body = buildBodyForActiveTab();
        url = RUN_URL[activeTab];
        body.output_path = outputPath;
        body.skip_video_stats = $('skip-video-stats').checked;
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
$('dry-run-btn').addEventListener('click', dryRun);

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

$('names-load-current').addEventListener('click', () => {
    const field = $('names-field').value.trim();
    if (!field) { status('请先填写要修改的字段名', 'error'); return; }
    const f = (currentFeatures || []).find((x) => x.key === field);
    if (!f) { status(`字段 ${field} 不在当前列表，请先点"加载字段列表"`, 'error'); return; }
    const names = (f.names && f.names.length) ? f.names : [];
    const dim = (f.shape && f.shape.length) ? f.shape[f.shape.length - 1] : 1;
    if (names.length) {
        $('names-list').value = names.join(', ');
    } else if (dim > 1) {
        $('names-list').value = Array.from({ length: dim }, (_, i) => `dim_${i}`).join(', ');
    } else {
        $('names-list').value = '';
    }
    status(`已载入 ${names.length || dim} 个维度名，可直接修改`, 'ok');
});

updateAssignModeFields();
