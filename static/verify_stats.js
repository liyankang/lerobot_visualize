// LeRobot Stats 校验 - 前端交互

const $ = (id) => document.getElementById(id);
const escHtml = (s) => String(s ?? '').replace(/[&<>"']/g, (m) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]
));

const S = {
    path: '',
    inspected: null,
    report: null,
    pollTimer: null,
};

function fmtBytes(n) {
    if (!n && n !== 0) return '—';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i === 0 ? 0 : 2)} ${u[i]}`;
}

function fmtDuration(s) {
    if (s === null || s === undefined) return '—';
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h) return `${h}h${m}m${sec}s`;
    if (m) return `${m}m${sec}s`;
    return `${sec}s`;
}

function notice(msg, kind = 'info') {
    const st = $('run-status');
    st.className = 'notice ' + kind;
    st.textContent = msg;
}

// ─── 1. 扫描数据集 ───

$('ds-inspect').addEventListener('click', async () => {
    const p = $('ds-path').value.trim();
    if (!p) { alert('请填写数据集路径'); return; }
    $('ds-inspect').disabled = true;
    $('ds-summary').style.display = 'none';
    try {
        const resp = await fetch('/api/verify-stats/inspect', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: p }),
        });
        const data = await resp.json();
        if (data.error) throw new Error(data.error);
        S.inspected = data;
        S.path = p;
        renderSummary(data);
    } catch (e) {
        alert('扫描失败: ' + e.message);
    } finally {
        $('ds-inspect').disabled = false;
    }
});

function renderSummary(d) {
    const s = d.summary || {};
    const eligible = d.eligible;
    const video_counts = d.video_episode_counts || {};
    const videoTags = (d.video_keys || []).map((k) =>
        `<span class="tag">${escHtml(k)} · ${video_counts[k] || 0} eps</span>`
    ).join('');

    const grid = `<div class="summary-grid" style="margin-bottom:12px">
        <div class="kv"><span>版本</span><span>${escHtml(s.codebase_version || '—')}</span></div>
        <div class="kv"><span>FPS</span><span>${escHtml(s.fps ?? '—')}</span></div>
        <div class="kv"><span>episodes</span><span>${s.total_episodes ?? '—'}</span></div>
        <div class="kv"><span>frames</span><span>${s.total_frames ?? '—'}</span></div>
        <div class="kv"><span>episode parquets</span><span>${d.num_episode_parquets}</span></div>
        <div class="kv"><span>meta/stats.json</span><span>${d.has_stats_json ? '✓ 存在' : '✗ 不存在'}</span></div>
        <div class="kv"><span>meta/episodes_stats.jsonl</span><span>${d.has_episodes_stats_jsonl ? '✓ 存在' : '✗ 不存在'}</span></div>
        <div class="kv"><span>robot_type</span><span>${escHtml(s.robot_type || '—')}</span></div>
    </div>`;

    const warn = eligible ? '' : `<div class="notice warn">当前版本为 <b>${escHtml(s.codebase_version)}</b>, 暂不支持直接校验 (目前只支持 v2.1)。</div>`;

    const videosHtml = videoTags
        ? `<div style="font-size:12px;margin-top:6px"><span style="color:#888">视频 feature:</span> ${videoTags}</div>`
        : '';

    $('ds-summary').innerHTML = grid + warn + videosHtml;
    $('ds-summary').style.display = '';

    if (eligible) {
        $('panel-params').style.display = '';
        // 根据数据集规模给出抽帧步长推荐
        const totalFrames = s.total_frames || 0;
        const nCams = (d.video_keys || []).length;
        if (totalFrames * Math.max(1, nCams) > 50000 && !$('opt-stride').dataset.touched) {
            $('opt-stride').value = 5;
        }
    } else {
        $('panel-params').style.display = 'none';
        $('panel-progress').style.display = 'none';
        $('panel-result').style.display = 'none';
    }
}

$('opt-stride').addEventListener('change', (ev) => { ev.target.dataset.touched = '1'; });

// ─── 2. 启动校验 ───

$('btn-start').addEventListener('click', async () => {
    if (!S.path) { alert('请先扫描数据集'); return; }
    const payload = {
        path: S.path,
        video_stride: Math.max(1, Number($('opt-stride').value) || 1),
        include_video_stats: $('opt-include-video').checked,
        max_abs_diff: Number($('opt-tol').value) || 1e-4,
    };
    $('btn-start').disabled = true;
    $('panel-progress').style.display = '';
    $('panel-result').style.display = 'none';
    notice('正在启动校验任务…', 'info');
    try {
        const resp = await fetch('/api/verify-stats/start', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (data.error) throw new Error(data.error);
        startPoll();
    } catch (e) {
        notice('启动失败: ' + e.message, 'error');
        $('btn-start').disabled = false;
    }
});

$('btn-cancel').addEventListener('click', async () => {
    try {
        await fetch('/api/verify-stats/cancel', { method: 'POST' });
    } catch (_) {}
    if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
    notice('已停止轮询(后台任务可能仍在运行)', 'warn');
    $('btn-cancel').style.display = 'none';
    $('btn-start').disabled = false;
});

function startPoll() {
    $('btn-cancel').style.display = 'inline-block';
    if (S.pollTimer) clearInterval(S.pollTimer);
    S.pollTimer = setInterval(async () => {
        try {
            const resp = await fetch('/api/verify-stats/progress');
            const p = await resp.json();
            updateProgressUI(p);
            if (!p.running) {
                clearInterval(S.pollTimer);
                S.pollTimer = null;
                $('btn-cancel').style.display = 'none';
                $('btn-start').disabled = false;
                if (p.error) {
                    notice('校验失败: ' + p.error, 'error');
                } else if (p.result) {
                    notice('校验完成 ✓', 'ok');
                    S.report = p.result;
                    renderResult(p.result);
                }
            }
        } catch (_) {}
    }, 600);
}

function updateProgressUI(p) {
    const bar = $('progress-bar');
    const det = $('progress-detail');
    const meta = $('progress-meta');

    bar.style.width = `${p.percent || 0}%`;
    bar.textContent = `${p.percent || 0}%`;
    det.textContent = `[${p.stage || ''}] ${p.title || ''} — ${p.detail || ''}`;
    meta.innerHTML = `
        <span>进度: ${p.current || 0} / ${p.total || 0}</span>
        <span>已耗时: ${fmtDuration(p.elapsed_sec)}</span>
        <span>预计剩余: ${fmtDuration(p.eta_sec)}</span>
    `;
    if (p.running) {
        notice(`执行中 · ${p.title || ''}`, 'info');
    }
}

// ─── 3. 结果渲染 ───

function renderResult(r) {
    $('panel-result').style.display = '';

    const overallStored = r.overall_recomputed_vs_stored;
    const overallAgg = r.overall_recomputed_vs_aggregated;

    const card = (label, summary) => {
        if (!summary) return `<div class="overall-card" style="background:#f5f5f5;color:#666;border-color:#ddd">
            <b>${escHtml(label)}:</b> 无对比源
        </div>`;
        const cls = summary.all_match ? 'ok' : 'bad';
        const head = summary.all_match ? '全部匹配 ✓' : `检测到 <b>${summary.mismatches.length}</b> 项不匹配`;
        const list = summary.mismatches.length
            ? `<div class="mismatches">${summary.mismatches.map(escHtml).join('<br>')}</div>`
            : '';
        return `<div class="overall-card ${cls}"><b>${escHtml(label)}:</b> ${head}${list}</div>`;
    };

    $('result-overall').innerHTML =
        card('重算 vs 数据集内的 meta/stats.json', overallStored) +
        card('重算 vs 聚合 meta/episodes_stats.jsonl', overallAgg);

    $('result-keylist').innerHTML =
        `重算 feature 数: <b>${(r.recomputed_keys || []).length}</b> · ` +
        `源 stats.json: <b>${(r.stored_stats_keys || []).length}</b> · ` +
        `源 episodes_stats 聚合: <b>${(r.aggregated_stats_keys || []).length || 0}</b>` +
        (r.recompute_warnings && r.recompute_warnings.length
            ? ` · <span style="color:#c62828">⚠ 重算警告 ${r.recompute_warnings.length} 条</span>`
            : '');

    // 填充对比目标选择
    const sel = $('result-target');
    sel.innerHTML = '';
    const options = [];
    if (r.diff_recomputed_vs_stored) options.push({ val: 'stored', label: '重算 vs meta/stats.json' });
    if (r.diff_recomputed_vs_aggregated) options.push({ val: 'aggregated', label: '重算 vs 聚合 episodes_stats' });
    if (!options.length) options.push({ val: 'none', label: '(无对比目标)' });
    options.forEach((o) => {
        const opt = document.createElement('option');
        opt.value = o.val; opt.textContent = o.label;
        sel.appendChild(opt);
    });
    sel.onchange = () => renderDiffTable(r);
    $('result-only-mismatch').onchange = () => renderDiffTable(r);

    renderDiffTable(r);
}

function renderDiffTable(r) {
    const sel = $('result-target').value;
    const tbody = $('diff-tbody');
    tbody.innerHTML = '';

    const diffs = sel === 'stored' ? (r.diff_recomputed_vs_stored || {})
               : sel === 'aggregated' ? (r.diff_recomputed_vs_aggregated || {})
               : {};

    const onlyMismatch = $('result-only-mismatch').checked;
    const entries = Object.entries(diffs);
    if (!entries.length) {
        tbody.innerHTML = '<tr><td colspan="7" style="color:#999;padding:12px;text-align:center">无对比数据</td></tr>';
        return;
    }

    for (const [k, m] of entries) {
        const isMissing = !!m.__missing_in__;
        const metrics = ['mean', 'std', 'min', 'max', 'count'];
        const hasMismatch = isMissing || metrics.some((mm) => {
            const d = m[mm];
            return d && (d.match === false || d.__shape_mismatch__ || d.__missing__);
        });
        if (onlyMismatch && !hasMismatch) continue;

        if (isMissing) {
            const tr = document.createElement('tr');
            tr.innerHTML = `<td style="font-family:monospace">${escHtml(k)}</td>
                <td colspan="5" style="color:#c62828">${m.__missing_in__ === 'a' ? '重算缺失' : '源 stats 缺失'}</td>
                <td class="num"><span class="tag bad">missing</span></td>`;
            tbody.appendChild(tr);
            continue;
        }

        const cells = metrics.map((metric) => {
            const d = m[metric];
            if (!d) return '<td class="num" style="color:#999">—</td>';
            if (d.__shape_mismatch__) return `<td class="num" style="color:#c62828" title="shape_a=${d.shape_a}, shape_b=${d.shape_b}">shape✗</td>`;
            if (d.__missing__) return '<td class="num" style="color:#c62828">缺失</td>';
            const abs = d.max_abs ?? 0;
            const color = d.match ? '#1b5e20' : '#c62828';
            return `<td class="num" style="color:${color}" title="shape=${(d.shape || []).join('×') || 'scalar'}, max_rel=${(d.max_rel || 0).toExponential(2)}">${abs.toExponential(2)}</td>`;
        }).join('');

        const status = hasMismatch
            ? '<span class="tag bad">不匹配</span>'
            : '<span class="tag ok">匹配</span>';

        const tr = document.createElement('tr');
        tr.innerHTML = `<td style="font-family:monospace;cursor:pointer">▸ ${escHtml(k)}</td>${cells}<td class="num">${status}</td>`;
        tr.querySelector('td:first-child').addEventListener('click', (ev) => {
            ev.stopPropagation();
            toggleDetail(tr, k, r);
        });
        tbody.appendChild(tr);
    }
}

function toggleDetail(row, key, r) {
    const existing = row.nextElementSibling;
    if (existing && existing.classList.contains('detail-row')) {
        existing.remove();
        row.firstElementChild.textContent = '▸ ' + key;
        return;
    }
    row.firstElementChild.textContent = '▾ ' + key;

    const stats = r.stats || {};
    const recomp = stats.recomputed?.[key];
    const stored = stats.stored?.[key];
    const agg = stats.aggregated?.[key];

    const fmt = (arr) => {
        if (arr === undefined || arr === null) return '<span style="color:#999">—</span>';
        try {
            const json = JSON.stringify(arr, null, 2);
            return `<pre style="margin:0;font-size:11px;white-space:pre-wrap;word-break:break-all">${escHtml(json)}</pre>`;
        } catch (_) { return escHtml(String(arr)); }
    };

    const metricRow = (m) => `
        <tr>
            <td style="font-family:monospace;color:#888">${m}</td>
            <td>${fmt(recomp?.[m])}</td>
            <td>${fmt(stored?.[m])}</td>
            <td>${fmt(agg?.[m])}</td>
        </tr>`;

    const tr = document.createElement('tr');
    tr.className = 'detail-row';
    tr.innerHTML = `<td colspan="7" style="background:#fafbfc;padding:10px">
        <div style="font-size:12px;color:#555;margin-bottom:6px">逐 metric 详细对比 (feature: <b>${escHtml(key)}</b>)</div>
        <table style="width:100%;border-collapse:collapse;font-size:11px">
            <thead style="background:#eee"><tr>
                <th style="padding:4px 8px;text-align:left">metric</th>
                <th style="padding:4px 8px;text-align:left">重算值</th>
                <th style="padding:4px 8px;text-align:left">源 stats.json</th>
                <th style="padding:4px 8px;text-align:left">聚合 episodes_stats</th>
            </tr></thead>
            <tbody>
                ${['mean','std','min','max','count'].map(metricRow).join('')}
            </tbody>
        </table>
    </td>`;
    row.parentNode.insertBefore(tr, row.nextSibling);
}

// ─── 4. 导出 ───

$('btn-export').addEventListener('click', () => {
    if (!S.report) return;
    const blob = new Blob([JSON.stringify(S.report, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `verify_stats_${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(url);
});

// ─── 目录选择 modal ───

let _browseCtx = null;

$('ds-browse').addEventListener('click', () => openBrowse('ds-path'));

async function openBrowse(inputId) {
    _browseCtx = { inputId };
    const initial = $(inputId).value.trim() || '';
    $('browse-modal').classList.add('active');
    $('browse-title').textContent = '选择数据集目录';
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
        if (data.parent !== undefined && data.parent !== '' && data.parent !== data.current) {
            const up = document.createElement('div');
            up.className = 'dir-item parent';
            up.textContent = '↑ 上级目录';
            up.addEventListener('click', () => browseTo(data.parent));
            list.appendChild(up);
        }
        for (const d of (data.dirs || [])) {
            const item = document.createElement('div');
            item.className = 'dir-item';
            item.textContent = '📁  ' + d.name;
            item.addEventListener('click', () => browseTo(d.path));
            list.appendChild(item);
        }
    } catch (e) {
        alert('浏览失败: ' + e.message);
    }
}

$('browse-input').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') browseTo($('browse-input').value.trim());
});

$('browse-cancel').addEventListener('click', () => $('browse-modal').classList.remove('active'));
$('browse-ok').addEventListener('click', () => {
    const p = $('browse-input').value.trim() || $('browse-current').textContent.trim();
    if (_browseCtx) $(_browseCtx.inputId).value = p;
    $('browse-modal').classList.remove('active');
});
