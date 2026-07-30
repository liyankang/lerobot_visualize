const $ = (id) => document.getElementById(id);
let currentAffected = [];
let currentCurves = new Map();
let progressTimer = null;
let browseTarget = null;

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
        } catch (_e) {
            // Keep polling; the final run request will surface errors.
        }
    }, 600);
}

function stopProgressPolling(keepVisible = true) {
    if (progressTimer) {
        clearInterval(progressTimer);
        progressTimer = null;
    }
    if (!keepVisible) $('progress-box').style.display = 'none';
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

function renderPlan(plan) {
    renderSummary(plan);
    renderEpisodeSelector(plan);
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

function buildAffectedItems(plan) {
    const byEp = new Map();
    for (const row of plan.length_deletions || []) {
        byEp.set(Number(row.episode_index), {
            episode_index: Number(row.episode_index),
            length: row.length,
            length_reason: row.reason,
            trim_start: 0,
            trim_end: 0,
            max_motion: null,
            type: 'IQR 长度删除',
        });
    }
    for (const row of plan.static_trims || []) {
        const ep = Number(row.episode_index);
        const item = byEp.get(ep) || {
            episode_index: ep,
            length: row.length,
            length_reason: '',
            type: '静止段裁剪',
        };
        item.trim_start = row.trim_start || 0;
        item.trim_end = row.trim_end || 0;
        item.max_motion = row.max_motion;
        item.keep_start = row.keep_start;
        item.keep_end = row.keep_end;
        if (item.length_reason) item.type = 'IQR 长度删除 + 静止段裁剪';
        byEp.set(ep, item);
    }
    return Array.from(byEp.values()).sort((a, b) => a.episode_index - b.episode_index);
}

function renderEpisodeSelector(plan) {
    currentAffected = buildAffectedItems(plan);
    currentCurves = new Map((plan.curve_previews || []).map(item => [Number(item.episode_index), item]));
    const select = $('episode-select');
    if (!currentAffected.length) {
        select.disabled = true;
        select.innerHTML = '<option value="">无受影响 episode</option>';
        $('affected-note').textContent = '没有 episode 会被删除或裁剪';
        $('episode-info').innerHTML = '';
        $('curve-panel').innerHTML = '<div class="empty">无受影响 episode 曲线</div>';
        return;
    }
    select.disabled = false;
    select.innerHTML = currentAffected.map(item => {
        const suffix = item.trim_start || item.trim_end
            ? ` · 裁 ${item.trim_start || 0}/${item.trim_end || 0}`
            : '';
        return `<option value="${item.episode_index}">Episode ${item.episode_index} · ${item.length} 帧 · ${esc(item.type)}${suffix}</option>`;
    }).join('');
    $('affected-note').textContent = `共 ${currentAffected.length} 个受影响 episode，选择一个查看动作曲线`;
    select.value = String(currentAffected[0].episode_index);
    renderSelectedEpisode();
}

function renderSelectedEpisode() {
    const ep = Number($('episode-select').value);
    const item = currentAffected.find(x => x.episode_index === ep);
    if (!item) return;
    $('episode-info').innerHTML = [
        infoChip(item.episode_index, 'Episode'),
        infoChip(item.length, '长度'),
        infoChip(item.type, '处理类型'),
        infoChip(item.trim_start || 0, '裁剪开头'),
        infoChip(item.trim_end || 0, '裁剪结尾'),
        infoChip(item.max_motion == null ? '-' : Number(item.max_motion).toExponential(3), '最大运动量'),
    ].join('');
    const curve = currentCurves.get(ep);
    if (!curve) {
        $('curve-panel').innerHTML = '<div class="empty">此 episode 没有曲线数据</div>';
        return;
    }
    const legend = (curve.names || []).slice(0, curve.dim_count || 0).map((name, i) =>
        `<span><span class="legend-dot" style="background:${COLORS[i % COLORS.length]}"></span>${esc(name)}</span>`
    ).join('');
    $('curve-panel').innerHTML = `<div class="curve-card">
        <div class="curve-head">
            <span><b>Episode ${curve.episode_index}</b> · ${esc(curve.source || 'n/a')} · ${curve.length || 0} 帧</span>
            <span>${esc(item.length_reason || '')}</span>
        </div>
        ${curveSvg(curve)}
        <div class="curve-legend">${legend}</div>
    </div>`;
}

function infoChip(value, label) {
    return `<div class="info-chip"><b>${esc(value)}</b>${esc(label)}</div>`;
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
    startProgressPolling();
    try {
        const data = await post('/api/batch_tools/run', body);
        renderPlan(data.plan);
        updateProgress({ title: '保存完成', detail: `数据集已保存到: ${data.path}`, percent: 100, current: 1, total: 1 });
        const keys = data.stats_keys || [];
        const hasState = keys.includes('observation.state');
        const numericKeys = keys.filter(k => k === 'observation.state' || k === 'action');
        const skipNote = data.skip_video_stats ? '已跳过视频统计；' : '';
        const stateNote = hasState
            ? `已计算数值统计: ${numericKeys.join(', ') || 'observation.state'}`
            : `未在 stats.json 中发现 observation.state，已有 keys: ${keys.join(', ') || 'none'}`;
        status(`保存完成: ${data.path}\n${skipNote}${stateNote}`, hasState ? 'ok' : 'error');
    } catch (e) {
        status(e.message, 'error');
        updateProgress({ title: '执行失败', detail: e.message, percent: 0 });
    } finally {
        stopProgressPolling(true);
        setBusy(false);
    }
}

$('preview-btn').addEventListener('click', preview);
$('run-btn').addEventListener('click', run);
$('episode-select').addEventListener('change', renderSelectedEpisode);

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

$('input-browse').addEventListener('click', () => openBrowse('input-path'));
$('output-browse').addEventListener('click', () => openBrowse('output-path'));
$('browse-input').addEventListener('keydown', ev => {
    if (ev.key === 'Enter') browseTo($('browse-input').value.trim());
});
$('browse-cancel').addEventListener('click', () => $('browse-modal').classList.remove('active'));
$('browse-ok').addEventListener('click', () => {
    if (browseTarget) {
        $(browseTarget).value = $('browse-input').value.trim() || $('browse-current').textContent.trim();
    }
    $('browse-modal').classList.remove('active');
});
