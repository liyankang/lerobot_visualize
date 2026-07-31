// LeRobot 版本转换页面 - 前端交互

const $ = (id) => document.getElementById(id);
const escHtml = (s) => String(s ?? '').replace(/[&<>"']/g, (m) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]
));

const S = {
    currentStep: 1,
    src: {
        path: '',
        info: null,
    },
    lastResult: null,
    compare: {
        leftTree: null,
        rightTree: null,
    },
    progressTimer: null,
};

// ─── Step 切换 ───

function gotoStep(n) {
    S.currentStep = n;
    document.querySelectorAll('.step-panel').forEach((el) => {
        el.classList.toggle('active', Number(el.dataset.step) === n);
    });
    document.querySelectorAll('.step-indicator .step').forEach((el) => {
        const step = Number(el.dataset.step);
        el.classList.toggle('active', step === n);
        el.classList.toggle('done', step < n);
    });
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

document.querySelectorAll('[data-goto]').forEach((btn) => {
    btn.addEventListener('click', () => gotoStep(Number(btn.dataset.goto)));
});

// ─── 工具函数 ───

function fmtBytes(n) {
    if (n == null) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = n, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v < 10 ? 2 : 1)} ${units[i]}`;
}

function fmtDuration(sec) {
    if (sec == null) return '—';
    sec = Math.max(0, sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.round(sec % 60);
    if (h > 0) return `${h}h${m}m${s}s`;
    if (m > 0) return `${m}m${s}s`;
    return `${s}s`;
}

function notice(msg, kind = 'info') {
    const div = document.createElement('div');
    div.className = `notice ${kind}`;
    div.style.position = 'fixed';
    div.style.top = '20px';
    div.style.right = '20px';
    div.style.zIndex = '2000';
    div.style.minWidth = '220px';
    div.style.boxShadow = '0 4px 12px rgba(0,0,0,0.15)';
    div.textContent = msg;
    document.body.appendChild(div);
    setTimeout(() => div.remove(), 3500);
}

function verBadgeHtml(version) {
    const v = String(version || '').replace('.', '');
    return `<span class="ver-badge ${v === 'v20' ? 'v20' : v === 'v21' ? 'v21' : v === 'v30' ? 'v30' : ''}">${escHtml(version || '未知')}</span>`;
}

// ─── Step 1: 源数据集扫描 ───

$('src-scan').addEventListener('click', async () => {
    const path = $('src-path').value.trim();
    if (!path) { notice('请先输入数据集路径', 'warn'); return; }
    $('src-scan').disabled = true;
    try {
        const resp = await fetch('/api/convert/inspect', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path }),
        });
        const data = await resp.json();
        if (data.error) throw new Error(data.error);
        S.src.path = data.info.root;
        S.src.info = data.info;
        renderSourceSummary(data.info);
    } catch (e) {
        notice('扫描失败: ' + e.message, 'error');
    } finally {
        $('src-scan').disabled = false;
    }
});

function renderSourceSummary(info) {
    const el = $('src-summary');
    el.style.display = 'block';
    const dirKeys = Object.keys(info.dir_sizes_bytes || {});
    const dirSizes = dirKeys.map((k) => `<span class="kv"><span>${k}/ 大小</span><span>${fmtBytes(info.dir_sizes_bytes[k])}</span></span>`).join('');

    const features = (info.feature_keys || []).map((k) => `<span class="tag">${escHtml(k)}</span>`).join('');
    const cameras = (info.video_keys || []).map((k) => `<span class="tag">${escHtml(k)}</span>`).join('');

    el.innerHTML = `
        <h4>基本信息</h4>
        <div>${verBadgeHtml(info.codebase_version)}<span style="font-size:13px;color:#555">${escHtml(info.root)}</span></div>
        <div class="summary-grid" style="margin-top:10px">
            <span class="kv"><span>fps</span><span>${info.fps ?? '—'}</span></span>
            <span class="kv"><span>robot_type</span><span>${escHtml(info.robot_type ?? '—')}</span></span>
            <span class="kv"><span>total_episodes</span><span>${info.total_episodes ?? '—'}</span></span>
            <span class="kv"><span>total_frames</span><span>${info.total_frames ?? '—'}</span></span>
            <span class="kv"><span>total_tasks</span><span>${info.total_tasks ?? '—'}</span></span>
            <span class="kv"><span>chunks_size</span><span>${info.chunks_size ?? '—'}</span></span>
            ${dirSizes}
        </div>
        <h4>特征</h4>
        <div>${features || '<span style="color:#999;font-size:12px">无</span>'}</div>
        <h4>视频通道</h4>
        <div>${cameras || '<span style="color:#999;font-size:12px">无视频</span>'}</div>
        <div class="btn-row">
            <button class="btn primary" id="src-next">下一步 →</button>
        </div>
    `;
    $('src-next').addEventListener('click', () => {
        if (!(info.supported_targets || []).length) {
            notice(`当前版本 ${info.codebase_version} 没有支持的目标版本`, 'warn');
            return;
        }
        buildStep2(info);
        gotoStep(2);
    });
}

// ─── Step 2: 选择目标 ───

function buildStep2(info) {
    const sel = $('target-version');
    sel.innerHTML = '';
    const mapping = {
        'v2.1': [
            { val: 'v3.0', label: 'v2.1 → v3.0（合并成共享大文件 / 生成 stats.json）' },
            { val: 'v2.0', label: 'v2.1 → v2.0（聚合 per-episode stats 成全局 stats.json）' },
        ],
        'v3.0': [
            { val: 'v2.1', label: 'v3.0 → v2.1（拆分合并文件，还原逐 episode 布局）' },
        ],
        'v2.0': [],
    };
    const opts = mapping[info.codebase_version] || [];
    if (!opts.length) {
        sel.innerHTML = `<option>没有可用的转换方向</option>`;
        $('go-convert').disabled = true;
    } else {
        $('go-convert').disabled = false;
        opts.forEach((o) => {
            const opt = document.createElement('option');
            opt.value = o.val;
            opt.textContent = o.label;
            sel.appendChild(opt);
        });
    }
    refreshTargetHint();
    sel.onchange = refreshTargetHint;

    // 默认输出路径 = 源路径 + "_<目标版本>"
    const defaultOut = S.src.path.replace(/[\\/]$/, '') + '_' + (sel.value || 'converted');
    $('out-path').value = defaultOut;
}

function refreshTargetHint() {
    const tv = $('target-version').value;
    const hint = $('target-hint');
    const v3opts = $('v3-options');
    v3opts.style.display = tv === 'v3.0' ? 'block' : 'none';
    const texts = {
        'v3.0': '将把每个 episode 的 parquet/mp4 贪心合并到共享大文件，并写入 meta/episodes/*.parquet 与 meta/stats.json。需要 ffmpeg 支持 concat。',
        'v2.1': '将读取 meta/episodes/*.parquet，根据 dataset_from_index / to_index 反向切分出逐 episode 的 parquet；根据 from/to_timestamp 用 ffmpeg 切出每段 mp4。',
        'v2.0': '仅重写元数据：把 meta/episodes_stats.jsonl 按官方公式聚合成 meta/stats.json，并删除前者。parquet / mp4 原样拷贝。',
    };
    hint.textContent = texts[tv] || '';
}

// stats 模式切换
document.querySelectorAll('input[name="stats-mode"]').forEach((el) => {
    el.addEventListener('change', () => {
        const raw = document.querySelector('input[name="stats-mode"]:checked')?.value === 'raw';
        $('stats-raw-opts').style.display = raw ? '' : 'none';
    });
});

$('go-convert').addEventListener('click', async () => {
    const out = $('out-path').value.trim();
    const tv = $('target-version').value;
    if (!out) { notice('请填写输出目录', 'warn'); return; }
    const payload = {
        source: S.src.path,
        target: out,
        target_version: tv,
    };
    if (tv === 'v3.0') {
        payload.data_file_size_mb = Number($('opt-data-mb').value) || 100;
        payload.video_file_size_mb = Number($('opt-video-mb').value) || 500;
    }
    const statsMode = document.querySelector('input[name="stats-mode"]:checked')?.value || 'agg';
    if (statsMode === 'raw' && (tv === 'v2.0' || tv === 'v3.0')) {
        payload.recompute_stats = true;
        payload.video_stride = Math.max(1, Number($('opt-video-stride').value) || 1);
        payload.include_video_stats = !$('opt-skip-video-stats').checked;
    }
    $('go-convert').disabled = true;
    try {
        const resp = await fetch('/api/convert/start', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (data.error) throw new Error(data.error);
        S.lastResult = null;
        gotoStep(3);
        startProgressPoll();
    } catch (e) {
        notice('启动失败: ' + e.message, 'error');
    } finally {
        $('go-convert').disabled = false;
    }
});

// ─── Step 3: 进度轮询 ───

function startProgressPoll() {
    const bar = $('progress-bar');
    const detail = $('progress-detail');
    const meta = $('progress-meta');
    const status = $('convert-status');
    status.className = 'notice info';
    status.textContent = '正在执行转换…';
    $('back-to-step2').style.display = 'none';
    $('go-compare').style.display = 'none';

    if (S.progressTimer) { clearInterval(S.progressTimer); }
    S.progressTimer = setInterval(async () => {
        try {
            const resp = await fetch('/api/convert/progress');
            const p = await resp.json();
            bar.style.width = `${p.percent || 0}%`;
            bar.textContent = `${p.percent || 0}%`;
            detail.textContent = `[${p.stage || ''}] ${p.title || ''} — ${p.detail || ''}`;
            meta.innerHTML = `
                <span>进度: ${p.current || 0} / ${p.total || 0}</span>
                <span>已耗时: ${fmtDuration(p.elapsed_sec)}</span>
                <span>预计剩余: ${fmtDuration(p.eta_sec)}</span>
            `;

            if (!p.running) {
                clearInterval(S.progressTimer);
                S.progressTimer = null;

                if (p.error) {
                    status.className = 'notice error';
                    status.textContent = '转换失败: ' + p.error;
                    $('back-to-step2').style.display = 'inline-block';
                } else {
                    status.className = 'notice ok';
                    const src = p.result?.stats_source || '—';
                    const nWarn = (p.result?.stats_warnings || []).length;
                    status.innerHTML = `转换完成 ✓  输出: <code>${escHtml(p.result?.output || p.target)}</code>
                        <div style="margin-top:4px;font-size:12px">
                            stats 来源: <b>${escHtml(src)}</b>
                            · 覆盖 feature: <b>${(p.result?.stats_feature_keys || []).length}</b>
                            ${nWarn ? ` · <span style="color:#c00">⚠ ${nWarn} 条警告</span>` : ''}
                        </div>`;
                    bar.style.width = '100%';
                    bar.textContent = '100%';
                    S.lastResult = p.result;
                    $('go-compare').style.display = 'inline-block';
                    $('cmp-left').value = p.source;
                    $('cmp-right').value = p.result?.output || p.target;
                }
            }
        } catch (_) {}
    }, 700);
}

$('go-compare').addEventListener('click', () => {
    gotoStep(4);
    loadCompare();
    const vp = $('verify-path');
    if (vp && !vp.value.trim()) vp.value = $('cmp-left').value.trim();
});

// ─── Step 4: 对比视图 ───

$('cmp-refresh').addEventListener('click', loadCompare);

async function loadCompare() {
    const left = $('cmp-left').value.trim();
    const right = $('cmp-right').value.trim();
    if (!left && !right) {
        notice('请至少指定一边的路径', 'warn'); return;
    }
    $('tree-left').innerHTML = '<div style="color:#999;padding:8px">加载中…</div>';
    $('tree-right').innerHTML = '<div style="color:#999;padding:8px">加载中…</div>';
    try {
        const resp = await fetch('/api/convert/tree', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ left, right, include_diff: true }),
        });
        const data = await resp.json();
        if (data.error) throw new Error(data.error);

        S.compare.leftTree = data.left;
        S.compare.rightTree = data.right;
        S.compare.diff = data.diff || {};

        renderTree($('tree-left'), data.left, data.diff, 'left');
        renderTree($('tree-right'), data.right, data.diff, 'right');
        renderCompareMeta(data);
    } catch (e) {
        notice('加载失败: ' + e.message, 'error');
    }
}

function renderCompareMeta(data) {
    const leftMeta = data.left?.error
        ? `<span style="color:#c00">${escHtml(data.left.error)}</span>`
        : `<span>${verBadgeHtml(data.diff?.left?.info?.codebase_version)}</span>` +
          `<span>episodes: ${data.diff?.left?.info?.total_episodes ?? '—'}</span>` +
          `<span>文件: ${data.diff?.left?.file_count ?? 0}</span>` +
          `<span>总大小: ${fmtBytes(data.diff?.left?.total_size)}</span>`;
    const rightMeta = data.right?.error
        ? `<span style="color:#c00">${escHtml(data.right.error)}</span>`
        : `<span>${verBadgeHtml(data.diff?.right?.info?.codebase_version)}</span>` +
          `<span>episodes: ${data.diff?.right?.info?.total_episodes ?? '—'}</span>` +
          `<span>文件: ${data.diff?.right?.file_count ?? 0}</span>` +
          `<span>总大小: ${fmtBytes(data.diff?.right?.total_size)}</span>`;
    $('cmp-left-meta').innerHTML = leftMeta;
    $('cmp-right-meta').innerHTML = rightMeta;

    const d = data.diff || {};
    $('diff-summary').innerHTML =
        `共同文件: <b>${d.common_count ?? 0}</b> · ` +
        `其中大小不同: <b style="color:#e65100">${d.common_diff_count ?? 0}</b> · ` +
        `仅左: <b style="color:#c00">${d.only_left_count ?? 0}</b> · ` +
        `仅右: <b style="color:#070">${d.only_right_count ?? 0}</b>`;
}

function renderTree(container, treeData, diff, side) {
    container.innerHTML = '';
    container.classList.remove('only-diff');
    if (!treeData) {
        container.innerHTML = '<div style="color:#999;padding:8px">未指定路径</div>'; return;
    }
    if (treeData.error) {
        container.innerHTML = `<div style="color:#c00;padding:8px">${escHtml(treeData.error)}</div>`;
        return;
    }
    const onlySet = new Set((diff?.[`only_in_${side}`] || []));
    const commonSet = new Set((diff?.common || []));
    const diffSet = new Set((diff?.common_diff || [])); // files present on both but differ in size
    const rootPath = treeData.path;
    const root = document.createElement('ul');
    root.appendChild(buildNode(treeData, rootPath, { side, onlySet, commonSet, diffSet }));
    container.appendChild(root);

    S.compare.nodeMap = S.compare.nodeMap || {};
    S.compare.nodeMap[side] = indexByRel(container, rootPath);
}

function buildNode(node, rootPath, ctx) {
    const li = document.createElement('li');
    const rel = node.path.slice(rootPath.length).replace(/^[\\/]/, '');
    li.dataset.path = node.path;
    li.dataset.rel = rel;

    const row = document.createElement('div');
    row.className = 'row';
    const caret = document.createElement('span'); caret.className = 'caret';
    const icon = document.createElement('span'); icon.className = 'icon';
    const name = document.createElement('span'); name.className = 'name'; name.textContent = node.name;
    const sz = document.createElement('span'); sz.className = 'sz';
    row.append(caret, icon, name, sz);
    li.appendChild(row);

    if (node.is_dir) {
        li.className = 'dir open';
        icon.textContent = '📁';
        sz.textContent = `${node.count ?? 0} 项`;
        const ul = document.createElement('ul');
        (node.children || []).forEach((c) => ul.appendChild(buildNode(c, rootPath, ctx)));
        li.appendChild(ul);

        row.addEventListener('click', (ev) => {
            ev.stopPropagation();
            toggleDir(li);
        });
    } else {
        let cls = 'file';
        if (ctx.onlySet.has(rel)) cls += ctx.side === 'left' ? ' only-left' : ' only-right';
        else if (ctx.commonSet.has(rel)) {
            cls += ' common';
            if (ctx.diffSet.has(rel)) cls += ' common-diff';
        }
        li.className = cls;
        icon.textContent = fileIcon(node.name);
        sz.textContent = fmtBytes(node.size);

        row.addEventListener('click', (ev) => {
            ev.stopPropagation();
            selectFile(ctx.side, rel, node.path);
        });
    }
    return li;
}

function toggleDir(li) {
    const ul = li.querySelector(':scope > ul');
    const open = li.classList.toggle('open');
    if (ul) ul.style.display = open ? '' : 'none';
}

function fileIcon(name) {
    const lower = (name || '').toLowerCase();
    if (lower.endsWith('.parquet')) return '🧩';
    if (lower.endsWith('.mp4') || lower.endsWith('.mov') || lower.endsWith('.webm')) return '🎬';
    if (lower.endsWith('.json') || lower.endsWith('.jsonl')) return '🧾';
    if (lower.endsWith('.png') || lower.endsWith('.jpg') || lower.endsWith('.jpeg')) return '🖼';
    return '📄';
}

function indexByRel(container, rootPath) {
    const map = {};
    container.querySelectorAll('li.file').forEach((li) => {
        map[li.dataset.rel] = li;
    });
    return map;
}

function selectFile(side, rel, path) {
    const sideContainer = $(`tree-${side}`);
    sideContainer.querySelectorAll('li.file.selected, li.file.mirror')
        .forEach((x) => x.classList.remove('selected', 'mirror'));
    const li = sideContainer.querySelector(`li.file[data-rel="${cssEscape(rel)}"]`);
    if (li) {
        li.classList.add('selected');
        scrollIntoViewIfNeeded(li, sideContainer);
    }
    previewFile(path, side);

    const syncSel = $('cmp-sync-select');
    if (syncSel && syncSel.checked) {
        const other = side === 'left' ? 'right' : 'left';
        const otherMap = (S.compare.nodeMap || {})[other] || {};
        const otherLi = otherMap[rel];
        const otherContainer = $(`tree-${other}`);
        otherContainer.querySelectorAll('li.file.selected, li.file.mirror')
            .forEach((x) => x.classList.remove('selected', 'mirror'));
        if (otherLi) {
            otherLi.classList.add('mirror');
            scrollIntoViewIfNeeded(otherLi, otherContainer);
            previewFile(otherLi.dataset.path, other);
        } else {
            const box = $(`preview-${other}`);
            box.innerHTML = `<div class="hd" style="color:#ffcb6b">对侧不存在同名文件</div><div>${escHtml(rel)}</div>`;
        }
    }
}

function cssEscape(s) {
    return (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/"/g, '\\"');
}

function scrollIntoViewIfNeeded(el, container) {
    const erect = el.getBoundingClientRect();
    const crect = container.getBoundingClientRect();
    if (erect.top < crect.top || erect.bottom > crect.bottom) {
        el.scrollIntoView({ block: 'nearest' });
    }
}

async function previewFile(path, side) {
    const box = $(`preview-${side}`);
    const label = side === 'left' ? '左侧' : '右侧';
    box.innerHTML = `<div class="hd">${escHtml(label)} · 加载中…</div><div>${escHtml(path)}</div>`;
    try {
        const resp = await fetch(`/api/convert/file_preview?path=${encodeURIComponent(path)}`);
        const data = await resp.json();
        if (data.error) throw new Error(data.error);
        box.innerHTML = renderPreviewHtml(data.file);
    } catch (e) {
        box.innerHTML = `<div class="hd" style="color:#f88">预览失败</div><div>${escHtml(e.message)}</div>`;
    }
}

// ─── 工具栏: 展开/收起/只看差异/同步滚动 ───

document.querySelectorAll('[data-tool]').forEach((btn) => {
    btn.addEventListener('click', () => {
        const tool = btn.dataset.tool;
        const side = btn.dataset.side;
        const container = $(`tree-${side}`);
        if (tool === 'expand') {
            container.querySelectorAll('li.dir').forEach((d) => {
                d.classList.add('open');
                const u = d.querySelector(':scope > ul');
                if (u) u.style.display = '';
            });
        } else if (tool === 'collapse') {
            container.querySelectorAll('li.dir').forEach((d, i) => {
                if (i === 0) return; // keep root open
                d.classList.remove('open');
                const u = d.querySelector(':scope > ul');
                if (u) u.style.display = 'none';
            });
        } else if (tool === 'only-diff') {
            const on = container.classList.toggle('only-diff');
            btn.classList.toggle('active', on);
        }
    });
});

// 直接粘贴文件路径 → 预览
['left', 'right'].forEach((side) => {
    const inp = $(`cmp-${side}-file`);
    if (!inp) return;
    inp.addEventListener('keydown', (ev) => {
        if (ev.key !== 'Enter') return;
        const p = inp.value.trim();
        if (!p) return;
        previewFile(p, side);
        // 尝试高亮对应树节点
        const root = (S.compare[`${side}Tree`] || {}).path || '';
        if (root && p.startsWith(root)) {
            const rel = p.slice(root.length).replace(/^[\\/]/, '');
            selectFile(side, rel, p);
        }
    });
});

// 同步滚动
['left', 'right'].forEach((side) => {
    const el = $(`tree-${side}`);
    if (!el) return;
    el.addEventListener('scroll', () => {
        const sync = $('cmp-sync-scroll');
        if (!sync || !sync.checked) return;
        if (el.dataset.syncing === '1') return;
        const other = $(`tree-${side === 'left' ? 'right' : 'left'}`);
        if (!other) return;
        const ratio = el.scrollTop / Math.max(1, (el.scrollHeight - el.clientHeight));
        other.dataset.syncing = '1';
        other.scrollTop = ratio * Math.max(0, other.scrollHeight - other.clientHeight);
        requestAnimationFrame(() => { delete other.dataset.syncing; });
    });
});

function renderPreviewHtml(f) {
    const head = `<div class="hd">${escHtml(f.path)} · ${fmtBytes(f.size)} · ${escHtml(f.kind)}</div>`;
    if (f.kind === 'text') {
        const text = f.content?.text || '';
        let sample = '';
        if (f.content?.parsed_sample) {
            sample = `\n\n--- 结构化解析 (截断) ---\n` + JSON.stringify(f.content.parsed_sample, null, 2);
        }
        return head + `<div>${escHtml(text)}${f.content?.truncated ? '\n…(truncated)' : ''}${escHtml(sample)}</div>`;
    }
    if (f.kind === 'parquet') {
        const c = f.content;
        const cols = (c.schema || []).map((s) => `<tr><td>${escHtml(s.name)}</td><td>${escHtml(s.type)}</td></tr>`).join('');
        const schemaTable = `<table><thead><tr><th>列名</th><th>类型</th></tr></thead><tbody>${cols}</tbody></table>`;
        const sampleRowsJson = JSON.stringify(c.sample_rows || [], null, 2);
        return head +
            `<div>行数: <b>${c.num_rows}</b>, 列数: <b>${c.num_columns}</b></div>` +
            schemaTable +
            `<div style="margin-top:8px;color:#82aaff">— 前 20 行示例 —</div>` +
            `<div>${escHtml(sampleRowsJson)}</div>`;
    }
    if (f.kind === 'video') {
        return head + `<div>视频时长: ${fmtDuration(f.content?.duration)}</div>
            <video controls style="max-width:100%;margin-top:8px;border-radius:6px" src="/api/convert/video_file?path=${encodeURIComponent(f.path)}"></video>`;
    }
    if (f.kind === 'image') {
        return head + `<div>图像文件（二进制，不支持内嵌预览）</div>`;
    }
    return head + `<div>二进制文件，不支持文本预览</div>`;
}

// ─── 目录浏览器 ───

let _browseContext = null; // { inputId, wantDir }

$('src-browse').addEventListener('click', () => openBrowse('src-path'));
$('out-browse').addEventListener('click', () => openBrowse('out-path', { allowNonExistent: true }));
document.querySelectorAll('[data-cmp-browse]').forEach((btn) => {
    btn.addEventListener('click', () => {
        const k = btn.dataset.cmpBrowse;
        const id = k === 'left' ? 'cmp-left' : k === 'right' ? 'cmp-right' : 'verify-path';
        openBrowse(id);
    });
});

// ─── stats 校验 ───

$('verify-run').addEventListener('click', async () => {
    const p = $('verify-path').value.trim() || $('cmp-left').value.trim();
    if (!p) { notice('请填写数据集路径', 'warn'); return; }
    const stride = Math.max(1, Number($('verify-stride').value) || 1);
    const includeVideo = $('verify-include-video').checked;
    const tol = Number($('verify-tol').value) || 1e-4;
    const status = $('verify-status');
    const result = $('verify-result');
    status.innerHTML = '<span style="color:#1a73e8">正在重新计算 stats…（视频可能需要几分钟）</span>';
    result.innerHTML = '';
    $('verify-run').disabled = true;
    try {
        const resp = await fetch('/api/convert/verify_stats', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                path: p, video_stride: stride,
                include_video_stats: includeVideo,
                max_abs_diff: tol,
            }),
        });
        const data = await resp.json();
        if (data.error) throw new Error(data.error);
        renderVerifyResult(data);
        status.innerHTML = '<span style="color:#070">计算完成</span>';
    } catch (e) {
        status.innerHTML = `<span style="color:#c00">校验失败: ${escHtml(e.message)}</span>`;
    } finally {
        $('verify-run').disabled = false;
    }
});

function renderVerifyResult(r) {
    const box = $('verify-result');
    const overallCard = (title, summary) => {
        if (!summary) return `<div style="padding:6px 10px;background:#f0f0f0;border-radius:6px;font-size:12px;color:#999">${escHtml(title)}: 无对比源</div>`;
        const ok = summary.all_match;
        return `<div style="padding:8px 12px;border-radius:6px;font-size:12px;background:${ok ? '#e8f5e9' : '#fff3e0'};color:${ok ? '#1b5e20' : '#e65100'};border:1px solid ${ok ? '#81c784' : '#ffb74d'};margin-bottom:6px">
            <b>${escHtml(title)}:</b> ${ok ? '全部匹配 ✓' : `检测到 <b>${summary.mismatches.length}</b> 项不匹配`}
            ${summary.mismatches.length ? `<div style="margin-top:4px;font-family:monospace;font-size:11px">${summary.mismatches.map(escHtml).join('<br>')}</div>` : ''}
        </div>`;
    };

    const storedCard = overallCard('重算 vs 数据集内的 stats.json', r.overall_recomputed_vs_stored);
    const aggCard = overallCard('重算 vs 聚合 episodes_stats.jsonl', r.overall_recomputed_vs_aggregated);

    const keyListHtml = `<div style="font-size:12px;color:#444;margin-bottom:4px">
        重算 feature 数: <b>${(r.recomputed_keys || []).length}</b>
        · 源 stats.json 中: <b>${(r.stored_stats_keys || []).length}</b>
        · 源 episodes_stats 聚合: <b>${(r.aggregated_stats_keys || []).length}</b>
    </div>`;

    // 构造详细逐 feature 表
    const diffs = r.diff_recomputed_vs_stored || r.diff_recomputed_vs_aggregated || {};
    const rowsHtml = Object.entries(diffs).map(([k, m]) => {
        if (m.__missing_in__) {
            return `<tr><td>${escHtml(k)}</td><td colspan="5" style="color:#c00">缺失于 ${m.__missing_in__ === 'a' ? '重算结果' : '源 stats'}</td></tr>`;
        }
        const cells = ['mean', 'std', 'min', 'max', 'count'].map((metric) => {
            const d = m[metric];
            if (!d) return '<td style="color:#999">—</td>';
            if (d.__shape_mismatch__) return `<td style="color:#c00" title="shape_a=${d.shape_a} shape_b=${d.shape_b}">shape✗</td>`;
            if (d.__missing__) return '<td style="color:#c00">缺失</td>';
            const abs = d.max_abs ?? 0;
            const color = d.match ? '#1b5e20' : '#c62828';
            return `<td style="color:${color};font-family:monospace">${abs.toExponential(2)}</td>`;
        }).join('');
        return `<tr><td style="font-family:monospace">${escHtml(k)}</td>${cells}</tr>`;
    }).join('');

    const tableHtml = `<div style="max-height:320px;overflow:auto;border:1px solid #e0e0e0;border-radius:6px">
        <table style="width:100%;border-collapse:collapse;font-size:11px">
            <thead style="background:#f5f5f5;position:sticky;top:0">
                <tr>
                    <th style="text-align:left;padding:4px 8px">feature</th>
                    <th style="text-align:right;padding:4px 8px">mean</th>
                    <th style="text-align:right;padding:4px 8px">std</th>
                    <th style="text-align:right;padding:4px 8px">min</th>
                    <th style="text-align:right;padding:4px 8px">max</th>
                    <th style="text-align:right;padding:4px 8px">count</th>
                </tr>
            </thead>
            <tbody>${rowsHtml || '<tr><td colspan="6" style="color:#999;padding:10px;text-align:center">无对比数据</td></tr>'}</tbody>
        </table>
    </div>
    <div style="font-size:10px;color:#999;margin-top:4px">数字代表 max |重算 − 源| (绝对值)，在容忍阈值 ${r.tolerance} 之内显示为绿色。</div>`;

    box.innerHTML = storedCard + aggCard + keyListHtml + tableHtml;
}

async function openBrowse(inputId, opts = {}) {
    _browseContext = { inputId, ...opts };
    const initial = $(inputId).value.trim() || '';
    $('browse-modal').classList.add('active');
    $('browse-title').textContent = opts.allowNonExistent ? '选择/输入目录（允许不存在）' : '选择目录';
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
        notice('浏览失败: ' + e.message, 'error');
    }
}

$('browse-input').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
        browseTo($('browse-input').value.trim());
    }
});

$('browse-cancel').addEventListener('click', () => $('browse-modal').classList.remove('active'));
$('browse-ok').addEventListener('click', () => {
    const p = $('browse-input').value.trim() || $('browse-current').textContent.trim();
    if (_browseContext) {
        $(_browseContext.inputId).value = p;
    }
    $('browse-modal').classList.remove('active');
});

// ─── 文档链接 ───

$('link-doc').setAttribute('href', '/docs/lerobot_format_conversion.md');
$('link-doc').setAttribute('target', '_blank');
