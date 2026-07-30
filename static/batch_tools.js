const $ = (id) => document.getElementById(id);

function readNumber(id) {
    const v = $(id).value.trim();
    return v === '' ? null : Number(v);
}

function payload() {
    return {
        input_path: $('input-path').value.trim(),
        output_path: $('output-path').value.trim(),
        min_length: readNumber('min-length'),
        max_length: readNumber('max-length'),
        trim_static_edges: $('trim-static').checked,
        motion_threshold: readNumber('motion-threshold') ?? 0.0001,
        margin_frames: readNumber('margin-frames') ?? 0,
        min_static_frames: readNumber('min-static-frames') ?? 1,
        joint_indices: $('joint-indices').value.trim(),
        motion_metric: $('motion-metric').value,
        allow_empty: $('allow-empty').checked,
    };
}

function setBusy(busy) {
    $('preview-btn').disabled = busy;
    $('run-btn').disabled = busy;
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

function metric(num, label) {
    return `<div class="metric"><div class="num">${num}</div><div class="label">${label}</div></div>`;
}

function renderSummary(plan) {
    $('summary').innerHTML = [
        metric(plan.total_episodes, '原始 episodes'),
        metric(plan.total_frames, '原始 frames'),
        metric(plan.delete_episode_count, '按长度删除 episodes'),
        metric(plan.trim_frame_count, '静止段裁剪 frames'),
        metric(plan.keep_episodes, '保留 episodes'),
        metric(plan.keep_frames, '保留 frames'),
        metric(plan.delete_episode_frames, '按长度删除 frames'),
        metric(plan.static_trims.length, '裁剪静止段 episodes'),
    ].join('');
}

function table(headers, rows, rowFn) {
    if (!rows || !rows.length) return '<div class="empty">无</div>';
    const head = headers.map(h => `<th>${h}</th>`).join('');
    const body = rows.slice(0, 200).map(rowFn).join('');
    const more = rows.length > 200
        ? `<tr><td colspan="${headers.length}">... ${rows.length - 200} more</td></tr>`
        : '';
    return `<table><thead><tr>${head}</tr></thead><tbody>${body}${more}</tbody></table>`;
}

function renderPlan(plan) {
    renderSummary(plan);
    $('length-table').className = '';
    $('length-table').innerHTML = table(
        ['Episode', '长度', '原因'],
        plan.length_deletions,
        r => `<tr><td>${r.episode_index}</td><td>${r.length}</td><td>${r.reason}</td></tr>`
    );
    $('trim-table').className = '';
    $('trim-table').innerHTML = table(
        ['Episode', '长度', '开头', '结尾', '最大运动量'],
        plan.static_trims,
        r => `<tr><td>${r.episode_index}</td><td>${r.length}</td><td>${r.trim_start}</td><td>${r.trim_end}</td><td>${Number(r.max_motion || 0).toExponential(3)}</td></tr>`
    );
}

async function preview() {
    status('正在生成预览...');
    setBusy(true);
    try {
        const data = await post('/api/batch_tools/preview', payload());
        renderPlan(data.plan);
        status('预览完成', 'ok');
    } catch (e) {
        status(e.message, 'error');
    } finally {
        setBusy(false);
    }
}

async function run() {
    const body = payload();
    if (!body.output_path) {
        status('请指定输出数据集路径', 'error');
        return;
    }
    if (!confirm(`确认执行批量裁剪并保存到:\n${body.output_path}`)) return;
    status('正在执行批处理并保存，数据集较大时需要等待...');
    setBusy(true);
    try {
        const data = await post('/api/batch_tools/run', body);
        renderPlan(data.plan);
        status(`保存完成: ${data.path}`, 'ok');
    } catch (e) {
        status(e.message, 'error');
    } finally {
        setBusy(false);
    }
}

$('preview-btn').addEventListener('click', preview);
$('run-btn').addEventListener('click', run);
