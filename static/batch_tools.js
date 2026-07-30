const $ = (id) => document.getElementById(id);

function readNumber(id) {
    const v = $(id).value.trim();
    return v === '' ? null : Number(v);
}

function payload() {
    return {
        input_path: $('input-path').value.trim(),
        output_path: $('output-path').value.trim(),
        auto_length_iqr: $('auto-length-iqr').checked,
        iqr_multiplier: readNumber('iqr-multiplier') ?? 1.5,
        trim_static_edges: $('trim-static').checked,
        motion_threshold: readNumber('motion-threshold') ?? 0.0001,
        margin_frames: readNumber('margin-frames') ?? 0,
        min_static_frames: readNumber('min-static-frames') ?? 1,
        joint_indices: $('joint-indices').value.trim(),
        motion_metric: $('motion-metric').value,
        allow_empty: $('allow-empty').checked,
        skip_video_stats: $('skip-video-stats').checked,
        max_curve_episodes: 80,
        max_curve_points: 260,
        max_curve_dims: 8,
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
    const iqr = plan.length_iqr || {};
    const iqrText = iqr.enabled && iqr.lower !== null
        ? `${Number(iqr.lower).toFixed(1)}~${Number(iqr.upper).toFixed(1)}`
        : 'off';
    $('summary').innerHTML = [
        metric(plan.total_episodes, '原始 episodes'),
        metric(plan.total_frames, '原始 frames'),
        metric(plan.delete_episode_count, '按长度删除 episodes'),
        metric(plan.trim_frame_count, '静止段裁剪 frames'),
        metric(plan.keep_episodes, '保留 episodes'),
        metric(plan.keep_frames, '保留 frames'),
        metric(plan.delete_episode_frames, '按长度删除 frames'),
        metric(iqrText, 'IQR 保留区间'),
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
    renderCurves(plan.curve_previews || []);
}

const COLORS = ['#1f7aec', '#e11d48', '#059669', '#9333ea', '#d97706', '#0891b2', '#4f46e5', '#be123c'];

function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
}

function xScale(x, maxX, left, width) {
    if (maxX <= 0) return left;
    return left + (x / maxX) * width;
}

function yScale(y, minY, maxY, top, height) {
    if (maxY <= minY) return top + height / 2;
    return top + height - ((y - minY) / (maxY - minY)) * height;
}

function linePath(xs, ys, minY, maxY, maxX, left, top, width, height) {
    return ys.map((y, i) => {
        const cmd = i === 0 ? 'M' : 'L';
        return `${cmd}${xScale(xs[i], maxX, left, width).toFixed(1)},${yScale(y, minY, maxY, top, height).toFixed(1)}`;
    }).join(' ');
}

function curveSvg(item) {
    if (item.error || !item.series || !item.series.length) {
        return `<div class="empty">${esc(item.error || '无曲线数据')}</div>`;
    }
    const w = 760, h = 170, left = 34, top = 12, right = 10, bottom = 22;
    const pw = w - left - right, ph = h - top - bottom;
    const xs = item.x || [];
    const values = item.series.flat().filter(Number.isFinite);
    const minY = Math.min(...values);
    const maxY = Math.max(...values);
    const pad = Math.max((maxY - minY) * 0.08, 1e-9);
    const lo = minY - pad, hi = maxY + pad;
    const maxX = Math.max(1, item.length - 1);
    const startW = item.trim_start > 0 ? xScale(item.trim_start, maxX, left, pw) - left : 0;
    const endX = item.trim_end > 0 ? xScale(item.length - item.trim_end, maxX, left, pw) : left + pw;
    const endW = item.trim_end > 0 ? left + pw - endX : 0;
    const lines = item.series.map((ys, i) => {
        const d = linePath(xs, ys, lo, hi, maxX, left, top, pw, ph);
        return `<path d="${d}" fill="none" stroke="${COLORS[i % COLORS.length]}" stroke-width="1.4" opacity="0.92"/>`;
    }).join('');
    return `<svg class="curve-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
        <rect x="${left}" y="${top}" width="${pw}" height="${ph}" fill="#fff"/>
        ${startW > 0 ? `<rect x="${left}" y="${top}" width="${startW}" height="${ph}" fill="#fecaca" opacity="0.45"/>` : ''}
        ${endW > 0 ? `<rect x="${endX}" y="${top}" width="${endW}" height="${ph}" fill="#fecaca" opacity="0.45"/>` : ''}
        <line x1="${left}" y1="${top}" x2="${left}" y2="${top + ph}" stroke="#cfd8df"/>
        <line x1="${left}" y1="${top + ph}" x2="${left + pw}" y2="${top + ph}" stroke="#cfd8df"/>
        <text x="2" y="${top + 10}" font-size="10" fill="#60717f">${hi.toExponential(1)}</text>
        <text x="2" y="${top + ph}" font-size="10" fill="#60717f">${lo.toExponential(1)}</text>
        ${lines}
    </svg>`;
}

function renderCurves(items) {
    const el = $('curve-list');
    if (!items.length) {
        el.innerHTML = '<div class="empty">无受影响 episode 曲线</div>';
        return;
    }
    el.innerHTML = items.map(item => {
        const legend = (item.names || []).slice(0, item.dim_count || 0).map((name, i) =>
            `<span><span class="legend-dot" style="background:${COLORS[i % COLORS.length]}"></span>${esc(name)}</span>`
        ).join('');
        const note = item.reason === 'IQR length deletion'
            ? 'IQR 长度删除'
            : `静止段裁剪: 开头 ${item.trim_start || 0} / 结尾 ${item.trim_end || 0}`;
        return `<div class="curve-card">
            <div class="curve-head">
                <span><b>Episode ${item.episode_index}</b> · ${esc(item.source || 'n/a')} · ${item.length || 0} 帧</span>
                <span>${esc(note)}</span>
            </div>
            ${curveSvg(item)}
            <div class="curve-legend">${legend}</div>
        </div>`;
    }).join('');
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
