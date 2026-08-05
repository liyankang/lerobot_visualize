const $ = (id) => document.getElementById(id);
let progressTimer = null;
let browseTarget = null;
let scanData = null;

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
    $('scan-btn').disabled = busy;
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
    if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
    if (!keepVisible) $('progress-box').style.display = 'none';
}

// ───────────────── ffmpeg 检测 ─────────────────

async function checkFFmpeg() {
    try {
        const res = await fetch('/api/video_transcode/ffmpeg_info');
        const data = await res.json();
        const el = $('ffmpeg-status');
        if (!data.ffmpeg || !data.ffprobe) {
            el.className = 'ffmpeg-status ffmpeg-err';
            el.innerHTML = '⚠ 未检测到 ' +
                (data.ffmpeg ? '' : '<b>ffmpeg</b> ') +
                (data.ffprobe ? '' : '<b>ffprobe</b> ') +
                '，请先安装并加入 PATH。';
            return false;
        }
        const encoders = (data.encoders || []).join(', ') || '未知';
        el.className = 'ffmpeg-status ffmpeg-ok';
        el.innerHTML = `✓ ffmpeg / ffprobe 可用。支持编码器: ${esc(encoders)}`;
        return true;
    } catch (_e) {
        $('ffmpeg-status').className = 'ffmpeg-status ffmpeg-err';
        $('ffmpeg-status').textContent = '⚠ 检测 ffmpeg 失败';
        return false;
    }
}

// ───────────────── 扫描 ─────────────────

function codecBadge(codec) {
    const c = String(codec || '').toLowerCase();
    let cls = 'codec-other';
    if (c === 'av1') cls = 'codec-av1';
    else if (c === 'h264') cls = 'codec-h264';
    else if (c === 'hevc' || c === 'h265') cls = 'codec-h265';
    return `<span class="codec-badge ${cls}">${esc(codec || '?')}</span>`;
}

function renderScan(data) {
    scanData = data;
    const items = data.items || [];
    const codecCounts = data.codec_summary || {};
    const targetCodec = $('target-codec').value;

    // 统计芯片
    const statRow = $('stat-row');
    const codecChips = Object.entries(codecCounts).map(([c, n]) =>
        `<div class="stat-chip"><b>${n}</b>${esc(c)}</div>`).join('');
    statRow.innerHTML = `
        <div class="stat-chip"><b>${data.video_count ?? 0}</b>视频</div>
        <div class="stat-chip"><b>${(data.total_size_mb ?? 0).toFixed(1)}</b>MB</div>
        ${codecChips}
    `;

    // 编码不一致警告
    const warn = $('warn-banner');
    if (data.is_mixed) {
        warn.className = 'warn-banner show';
        warn.innerHTML = `⚠ 检测到多种视频编码混合: ${esc(Object.keys(codecCounts).join(', '))}。` +
            `这会导致 ffmpeg concat 拼接时损坏后半段视频，<b>建议在 merge 前用本工具统一编码</b>。`;
    } else if (Object.keys(codecCounts).length === 1) {
        const only = Object.keys(codecCounts)[0];
        if (only !== targetCodec) {
            warn.className = 'warn-banner show';
            warn.innerHTML = `当前所有视频都是 <b>${esc(only)}</b>，将全部转码为 <b>${esc(targetCodec)}</b>。`;
        } else {
            warn.className = 'warn-banner';
        }
    } else {
        warn.className = 'warn-banner';
    }

    // 视频表格
    if (!items.length) {
        $('videos-panel').innerHTML = '<div class="empty">该数据集没有 mp4 视频文件</div>';
        return;
    }
    const rows = items.map((it) => {
        const codec = it.codec;
        const err = it.error;
        const codecCell = err
            ? `<span class="codec-badge codec-mismatch">错误</span>`
            : codecBadge(codec);
        const resStr = (it.width && it.height) ? `${it.width}×${it.height}` : '-';
        const framesStr = it.nb_frames != null ? String(it.nb_frames) : '-';
        const durStr = it.duration != null ? `${it.duration}s` : '-';
        return `
            <tr>
                <td class="path-cell">${esc(it.rel_path || it.path)}</td>
                <td>${codecCell}</td>
                <td>${resStr}</td>
                <td>${framesStr}</td>
                <td>${durStr}</td>
                <td>${esc(it.fps ?? '-')}</td>
                <td>${err ? '<span style="color:#b42318;font-size:11px">' + esc(err) + '</span>' : esc(it.pix_fmt || '-')}</td>
            </tr>
        `;
    }).join('');
    $('videos-panel').innerHTML = `
        <table class="videos">
            <thead>
                <tr>
                    <th>文件路径</th><th>编码</th><th>分辨率</th>
                    <th>帧数</th><th>时长</th><th>fps</th><th>pix_fmt / 错误</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>
    `;
}

async function scan() {
    const inputPath = $('input-path').value.trim();
    if (!inputPath) { status('请先填写输入数据集路径', 'error'); return; }
    setBusy(true);
    status('正在扫描视频...');
    try {
        const data = await post('/api/video_transcode/scan', { input_path: inputPath });
        renderScan(data);
        const summary = Object.entries(data.codec_summary || {})
            .map(([c, n]) => `${c}: ${n}`).join(', ') || '无';
        status(`扫描完成: ${data.video_count} 个视频 (${summary})`, 'ok');
    } catch (e) {
        status(`扫描失败: ${e.message}`, 'error');
    } finally {
        setBusy(false);
    }
}

// ───────────────── 转码 ─────────────────

async function run() {
    const inputPath = $('input-path').value.trim();
    const outputPath = $('output-path').value.trim();
    if (!inputPath) { status('请填写输入数据集路径', 'error'); return; }
    if (!outputPath) { status('请填写输出数据集路径', 'error'); return; }
    const ok = await checkFFmpeg();
    if (!ok) { status('ffmpeg/ffprobe 未就绪，无法转码', 'error'); return; }

    const target = $('target-codec').value;
    const onlyCodecRaw = $('only-codec').value.trim();
    const body = {
        input_path: inputPath,
        output_path: outputPath,
        target_codec: target,
        skip_verify: $('skip-verify').checked,
    };
    if (onlyCodecRaw) {
        body.only_codec = onlyCodecRaw.split(',').map((s) => s.trim()).filter(Boolean);
    }

    setBusy(true);
    status(`正在转码到 ${target} ...`);
    startProgressPolling();
    try {
        const data = await post('/api/video_transcode/run', body);
        const r = data.results || [];
        const failedItems = r.filter((x) => !x.ok);
        let msg = `完成：成功 ${data.transcoded}/${data.total}（跳过 ${data.skipped}）` +
            (data.info_updated ? '，info.json 已更新' : '') +
            `。\n输出: ${data.output}`;
        if (failedItems.length) {
            msg += `\n⚠ 失败 ${failedItems.length} 个：\n` +
                failedItems.slice(0, 5).map((x) => `  ${x.path}: ${x.error}`).join('\n');
            if (failedItems.length > 5) msg += `\n  ...等 ${failedItems.length} 个`;
            status(msg, 'error');
        } else {
            status(msg, 'ok');
        }
    } catch (e) {
        status(`转码失败: ${e.message}`, 'error');
        updateProgress({ title: '转码失败', detail: e.message, percent: 0 });
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

$('scan-btn').addEventListener('click', scan);
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

checkFFmpeg();
