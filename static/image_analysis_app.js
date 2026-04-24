const $ = id => document.getElementById(id);

const STATE = {
    loading: false,
    analyzing: false,
    datasetInfo: null,
    report: null,
    charts: {},
    pollTimer: null,
    camera: null,
    currentEpisode: null,
    currentProblems: [],
    galleryShown: 0,
    lightboxIndex: 0,
};

const GALLERY_PAGE_SIZE = 20;

const REASON_LABELS = {
    blurry: '模糊',
    dark: '过暗',
    bright: '过亮',
    overexposed: '过曝',
    underexposed: '曝光不足',
    low_info: '信息量低',
    static: '静止帧',
    scene_change: '场景突变',
};

const GRADE_LABELS = {
    excellent: '优秀',
    good: '良好',
    acceptable: '可接受',
    poor: '较差',
    bad: '很差',
};

const METRIC_TIPS = {
    quality_score: [
        '公式: Q = 0.35×模糊度评分 + 0.20×信息熵评分 + 0.15×亮度评分 + 0.15×对比度评分 + 0.15×曝光评分',
        '综合衡量一帧图像对 VLA 模型学习的可用程度，各子项先映射到 0-100 分再加权。',
        '≥90 优秀: 图像清晰、光照合理、信息丰富，模型可高效学习。',
        '75-90 良好: 可正常用于训练。',
        '60-75 可接受: 存在轻微质量问题，可能降低学习效率。',
        '<60 较差/很差: 图像存在严重问题（模糊/过暗/过曝/无信息），建议排查或剔除。',
    ].join('\n'),

    blur: [
        '公式: Var(Laplacian(Gray))',
        '对灰度图做 Laplacian 二阶微分，取方差。',
        '值越高 → 图像越清晰（边缘细节丰富、高频信息多）。',
        '值越低 → 图像越模糊（运动模糊或失焦）。',
        '≥200 清晰 | 50-200 可接受 | <50 模糊。',
        '影响: 模糊帧会导致模型无法识别物体边界和抓取点位置，是最关键的图像质量指标。',
    ].join('\n'),

    brightness: [
        '公式: mean(Gray[Gray ≥ 15]) / 255',
        '排除纯黑像素(< 15)后，对可见内容区域的灰度均值归一化到 [0, 1]。',
        '这样大面积黑色背景不会拉低数值，更能反映实际可见内容的光照情况。',
        '值偏高(>0.85) → 内容区域过亮，细节丢失。',
        '值偏低(<0.15) → 内容区域确实过暗。',
        '影响: 过暗或过亮都会让 VLA 模型难以从视觉中提取有效的物体位置和状态信息。',
    ].join('\n'),

    brightness_overall: [
        '公式: mean(Gray) / 255',
        '灰度图所有像素（含纯黑区域）的均值，归一化到 [0, 1]。',
        '如果画面中有大面积黑色背景/边框，此值会被显著拉低。',
        '与"内容亮度"对比可以判断亮度低是因为整体光照不足，还是只是背景黑。',
    ].join('\n'),

    dark_ratio: [
        '公式: count(Gray < 15) / total_pixels',
        '像素值低于 15 的比例，代表画面中"几乎纯黑"的区域占比。',
        '值高(>50%) → 画面大面积是黑色（背景/遮挡/镜头边缘），实际可视内容只占小部分。',
        '值低(<10%) → 画面绝大部分有可见内容。',
        '影响: 暗区占比高时，"整体亮度"会被拉低，但不一定说明光照不足——需要结合"内容亮度"判断。',
    ].join('\n'),

    entropy: [
        '公式: H = -Σ p(x) × log₂(p(x))',
        '对灰度直方图(256 bins)计算 Shannon 信息熵，范围 0-8 bits。',
        '值越高 → 图像包含的信息越丰富（纹理、颜色层次多）。',
        '值越低 → 图像越单调（纯色、遮挡、黑屏、白屏）。',
        '≥6.5 丰富 | 4-6.5 可接受 | <4 信息量过低。',
        '影响: 低信息熵意味着该帧几乎没有可学习的视觉内容，相当于无效数据。',
    ].join('\n'),

    contrast: [
        '公式: std(Gray) / 255',
        '灰度图标准差归一化到 [0, 1]，即 RMS 对比度。',
        '值越高 → 图像明暗层次分明，物体与背景区分清晰。',
        '值越低 → 图像灰蒙蒙一片，前景与背景难以分离。',
        '≥0.15 正常 | 0.05-0.15 偏低 | <0.05 非常低。',
        '影响: 低对比度让模型难以分辨操作目标和环境边界，降低空间推理能力。',
    ].join('\n'),

    overexposed: [
        '公式: count(Gray > 250) / total_pixels',
        '像素值接近饱和(>250)的比例。',
        '>10% 判定为过曝帧。',
        '影响: 过曝区域完全丢失细节信息，如果抓取目标恰好在过曝区域，模型将无法定位。',
    ].join('\n'),

    underexposed: [
        '公式: count(Gray < 5) / total_pixels',
        '像素值接近纯黑(<5)的比例。',
        '>10% 判定为曝光不足帧。',
        '影响: 曝光不足的暗部区域无法提供有效视觉信息，尤其在暗色物体/阴影区域操作时致命。',
    ].join('\n'),

    exposure: [
        '公式: max(过曝率, 曝光不足率)',
        '过曝率 = count(Gray > 250) / total | 曝光不足率 = count(Gray < 5) / total',
        '<1% 正常 | 1-10% 有轻微曝光问题 | >10% 严重曝光问题。',
        '影响: 极端曝光区域的像素信息被截断(clipped)，无法恢复。在这些区域内的操作目标对模型来说是"不可见"的。',
    ].join('\n'),

    frame_diff: [
        '公式: mean(|Gray_t - Gray_{t-1}|) / 255',
        '相邻两帧灰度图逐像素绝对差的均值，归一化到 [0, 1]。',
        '值接近0(<0.002) → 静止帧，连续多帧几乎无变化，是冗余数据。',
        '值正常(0.002-0.3) → 正常的场景运动。',
        '值很大(>0.3) → 场景突变（视角跳变、闪烁、相机掉落等）。',
        '影响: 静止帧是低价值冗余数据，增加训练成本但不贡献新信息；场景突变帧可能引入噪声，导致策略学习不稳定。',
    ].join('\n'),

    problem_ratio: [
        '判定规则: 满足以下任一条件即标记为问题帧:',
        '• 模糊: Laplacian方差 < 50',
        '• 过暗: 亮度 < 0.15',
        '• 过亮: 亮度 > 0.85',
        '• 过曝: 过曝像素 > 10%',
        '• 曝光不足: 欠曝像素 > 10%',
        '• 信息量低: 信息熵 < 4 bits',
        '• 静止帧: 帧间差异 < 0.002',
        '• 场景突变: 帧间差异 > 0.30',
        '影响: 问题帧占比越高，数据集中可有效用于策略学习的帧越少。建议排查原因（相机参数、光照条件、采集速度等）。',
    ].join('\n'),

    velocity_blur: [
        '公式: r = Σ(vᵢ - v̄)(bᵢ - b̄) / √[Σ(vᵢ - v̄)² × Σ(bᵢ - b̄)²]',
        'Pearson 相关系数，衡量关节最大绝对速度与 Laplacian 模糊度的线性关联。',
        '负值(如 -0.3) → 速度越高图像越模糊，说明相机曝光时间偏长，高速运动时产生运动模糊。',
        '接近0 → 速度与模糊度无明显关联，运动模糊不是主要问题。',
        '正值 → 异常情况，可能是分析噪声。',
        '影响: 如果存在显著负相关，说明恰恰在机器人做关键动作（高速运动）时图像质量最差——模型在最需要视觉信息的时刻获得了最差的输入。建议降低相机曝光时间或增加补光。',
    ].join('\n'),

    best_episode: [
        '所有 episode 中综合质量评分最高的 episode。',
        '影响: 可作为数据质量基准线。如果最佳 episode 的评分都较低，说明整体采集环境或相机配置存在系统性问题。',
    ].join('\n'),

    worst_episode: [
        '所有 episode 中综合质量评分最低的 episode。',
        '影响: 优先排查该 episode 的问题原因。如果该 episode 远低于平均水平，可考虑剔除或重新采集。',
    ].join('\n'),
};

function helpIcon(tipKey) {
    const tip = METRIC_TIPS[tipKey];
    if (!tip) return '';
    return `<span class="term-help" tabindex="0" data-tip="${escHtml(tip)}">?</span>`;
}

function escHtml(text) {
    const div = document.createElement('div');
    div.textContent = text ?? '';
    return div.innerHTML;
}

function fmt(v, d = 2) {
    if (v == null || !Number.isFinite(Number(v))) return '--';
    return Number(v).toFixed(d);
}

function fmtInt(v) {
    if (v == null) return '--';
    return Number(v).toLocaleString('zh-CN');
}

function fmtPct(v) {
    if (v == null || !Number.isFinite(Number(v))) return '--';
    return (Number(v) * 100).toFixed(1) + '%';
}

function setStatus(id, msg, type = 'info') {
    const el = $(id);
    if (!el) return;
    el.textContent = msg;
    el.className = `status-line ${type}`;
}

async function postJson(url, body) {
    const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
}

async function getJson(url) {
    const resp = await fetch(url);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
}

function ensureChartPlugins() {
    if (typeof Chart === 'undefined') return;
    if (STATE._pluginsRegistered) return;
    STATE._pluginsRegistered = true;
    const plugin = window.ChartZoom || window['chartjs-plugin-zoom'] || window.zoomPlugin;
    if (plugin && typeof Chart.register === 'function') {
        try { Chart.register(plugin); } catch (_) { /* already registered */ }
    }
}

function destroyChart(key) {
    if (STATE.charts[key]) {
        STATE.charts[key].destroy();
        delete STATE.charts[key];
    }
}

function destroyAllCharts() {
    Object.keys(STATE.charts).forEach(destroyChart);
}

// ══════════════════════ 加载数据集 ══════════════════════

async function loadDataset() {
    const path = $('dataset-path').value.trim();
    if (!path) { setStatus('load-status', '请输入数据集路径', 'error'); return; }
    if (STATE.loading) return;

    STATE.loading = true;
    $('load-btn').disabled = true;
    setStatus('load-status', '正在加载...', 'loading');
    destroyAllCharts();

    try {
        const data = await postJson('/api/image-analysis/load', { path });
        STATE.datasetInfo = data;

        const grid = $('summary-grid');
        grid.innerHTML = [
            { label: 'Episodes', value: fmtInt(data.total_episodes) },
            { label: '相机数', value: fmtInt(data.cameras?.length) },
            { label: 'FPS', value: fmt(data.fps, 0) },
            { label: 'Robot', value: escHtml(data.robot_type || 'unknown') },
        ].map(i => `<article class="summary-chip"><span class="label">${i.label}</span><span class="value">${i.value}</span></article>`).join('');
        $('dataset-summary').classList.remove('hidden');

        const camSelect = $('camera-select');
        camSelect.innerHTML = '<option value="">-- 选择相机 --</option>';
        (data.cameras || []).forEach(cam => {
            camSelect.innerHTML += `<option value="${escHtml(cam)}">${escHtml(cam)}</option>`;
        });
        if (data.cameras?.length === 1) camSelect.value = data.cameras[0];

        $('analyze-control').classList.remove('hidden');
        $('results-section').classList.add('hidden');
        $('progress-section').classList.add('hidden');

        setStatus('load-status', `加载成功: ${data.total_episodes} 个 episode, ${data.cameras?.length} 个相机`, 'success');
    } catch (e) {
        setStatus('load-status', `加载失败: ${e.message}`, 'error');
    } finally {
        STATE.loading = false;
        $('load-btn').disabled = false;
    }
}

// ══════════════════════ 开始分析 ══════════════════════

async function startAnalysis() {
    const camera = $('camera-select').value;
    if (!camera) { alert('请选择一个相机'); return; }
    if (STATE.analyzing) return;

    STATE.analyzing = true;
    $('analyze-btn').disabled = true;
    $('results-section').classList.add('hidden');
    $('progress-section').classList.remove('hidden');
    destroyAllCharts();

    try {
        await postJson('/api/image-analysis/start', { camera });
        pollProgress();
    } catch (e) {
        setStatus('load-status', `启动分析失败: ${e.message}`, 'error');
        STATE.analyzing = false;
        $('analyze-btn').disabled = false;
        $('progress-section').classList.add('hidden');
    }
}

function pollProgress() {
    if (STATE.pollTimer) clearTimeout(STATE.pollTimer);

    async function tick() {
        try {
            const data = await getJson('/api/image-analysis/progress');
            $('progress-title').textContent = data.title || '分析中...';
            $('progress-percent').textContent = (data.percent ?? 0) + '%';
            $('progress-fill').style.width = (data.percent ?? 0) + '%';
            $('progress-detail').textContent = data.detail || '';

            if (data.error) {
                setStatus('load-status', `分析失败: ${data.error}`, 'error');
                $('progress-section').classList.add('hidden');
                STATE.analyzing = false;
                $('analyze-btn').disabled = false;
                return;
            }

            if (data.stage === 'done' && !data.running) {
                STATE.report = data.result;
                $('progress-section').classList.add('hidden');
                STATE.analyzing = false;
                $('analyze-btn').disabled = false;
                renderResults(data.result);
                return;
            }

            STATE.pollTimer = setTimeout(tick, 800);
        } catch (e) {
            STATE.pollTimer = setTimeout(tick, 2000);
        }
    }
    tick();
}

// ══════════════════════ 渲染结果 ══════════════════════

function renderResults(report) {
    if (!report) return;
    ensureChartPlugins();
    STATE.camera = report.camera;
    $('results-section').classList.remove('hidden');

    renderQualitySummary(report);
    renderEpisodeQualityChart(report);
    renderMetricDistributions(report);
    renderEpisodeDetailSection(report);
    renderVelocityBlurSection(report);

    setStatus('load-status',
        `分析完成: ${report.episodes_analyzed} 个 episode, ${fmtInt(report.total_frames)} 帧`,
        'success');
}

function renderQualitySummary(report) {
    const s = report.summary || {};
    const grade = s.quality_grade || 'unknown';
    const gradeLabel = GRADE_LABELS[grade] || grade;

    const items = [
        { label: '综合评分', value: fmt(s.quality_score, 1), sub: gradeLabel, cls: `grade-${grade}`, tip: 'quality_score' },
        { label: '平均模糊度', value: fmt(s.avg_blur, 1), sub: 'Laplacian方差', tip: 'blur' },
        { label: '内容亮度', value: fmt(s.avg_brightness_content ?? s.avg_brightness, 3), sub: `整体 ${fmt(s.avg_brightness, 3)}`, tip: 'brightness' },
        { label: '暗区占比', value: fmtPct(s.avg_dark_ratio ?? 0), sub: '像素<15', tip: 'dark_ratio' },
        { label: '平均信息熵', value: fmt(s.avg_entropy, 2), sub: '0~8 bits', tip: 'entropy' },
        { label: '平均对比度', value: fmt(s.avg_contrast, 3), sub: 'RMS归一化', tip: 'contrast' },
        { label: '问题帧占比', value: fmtPct(s.problem_frame_ratio), sub: `${fmtInt(s.problem_frame_count)} 帧`, tip: 'problem_ratio' },
        { label: '最佳Episode', value: `#${s.best_episode ?? '--'}`, sub: `评分 ${fmt(s.best_quality, 1)}`, tip: 'best_episode' },
        { label: '最差Episode', value: `#${s.worst_episode ?? '--'}`, sub: `评分 ${fmt(s.worst_quality, 1)}`, tip: 'worst_episode' },
    ];

    $('quality-summary').innerHTML = items.map(i => `
        <article class="summary-chip">
            <span class="label-row"><span class="label">${i.label}</span>${helpIcon(i.tip)}</span>
            <span class="value ${i.cls || ''}">${i.value}</span>
            ${i.sub ? `<span class="sub">${i.sub}</span>` : ''}
        </article>
    `).join('');
}

function renderEpisodeQualityChart(report) {
    destroyChart('episodeQuality');
    const episodes = report.episodes || [];
    if (!episodes.length) return;

    const labels = episodes.map(e => `Ep ${e.episode_index}`);
    const scores = episodes.map(e => e.quality_score);
    const colors = scores.map(s => {
        if (s >= 90) return 'rgba(21, 128, 61, 0.7)';
        if (s >= 75) return 'rgba(8, 145, 178, 0.7)';
        if (s >= 60) return 'rgba(217, 119, 6, 0.7)';
        if (s >= 40) return 'rgba(234, 88, 12, 0.7)';
        return 'rgba(180, 35, 24, 0.7)';
    });

    const ctx = $('chart-episode-quality');
    STATE.charts.episodeQuality = new Chart(ctx, {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: '质量评分',
                data: scores,
                backgroundColor: colors,
                borderRadius: 4,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        afterLabel(ctx) {
                            const ep = episodes[ctx.dataIndex];
                            return [
                                `模糊度: ${fmt(ep.avg_blur, 1)}`,
                                `内容亮度: ${fmt(ep.avg_brightness_content ?? ep.avg_brightness, 3)}`,
                                `暗区占比: ${fmtPct(ep.avg_dark_ratio ?? 0)}`,
                                `信息熵: ${fmt(ep.avg_entropy, 2)}`,
                                `问题帧: ${ep.problem_frame_count}`,
                            ].join('\n');
                        },
                    },
                },
            },
            scales: {
                y: { beginAtZero: true, max: 100, title: { display: true, text: '质量评分' } },
                x: { ticks: { maxRotation: 45 } },
            },
            onClick(_, elements) {
                if (elements.length) {
                    const idx = elements[0].index;
                    const epIdx = episodes[idx].episode_index;
                    $('detail-episode-select').value = epIdx;
                    loadEpisodeDetail(epIdx);
                }
            },
        },
    });
}

function renderMetricDistributions(report) {
    const container = $('metric-distribution-charts');
    container.innerHTML = '';
    const episodes = report.episodes || [];
    if (!episodes.length) return;

    const metrics = [
        { key: 'avg_blur', label: '模糊度 (Laplacian方差)', color: '#2563eb', tip: 'blur' },
        { key: 'avg_brightness_content', fallback: 'avg_brightness', label: '内容亮度', color: '#0891b2', tip: 'brightness' },
        { key: 'avg_dark_ratio', label: '暗区占比', color: '#64748b', tip: 'dark_ratio' },
        { key: 'avg_entropy', label: '信息熵', color: '#7c3aed', tip: 'entropy' },
        { key: 'avg_contrast', label: '对比度', color: '#ea580c', tip: 'contrast' },
    ];

    metrics.forEach(m => {
        const card = document.createElement('div');
        card.className = 'chart-card';
        card.innerHTML = `
            <div class="chart-title-row"><h4>${m.label}</h4>${helpIcon(m.tip)}</div>
            <div class="chart-canvas-wrap"><canvas id="chart-dist-${m.key}"></canvas></div>
        `;
        container.appendChild(card);

        const labels = episodes.map(e => `Ep ${e.episode_index}`);
        const values = episodes.map(e => e[m.key] ?? (m.fallback ? e[m.fallback] : 0));

        destroyChart(`dist-${m.key}`);
        STATE.charts[`dist-${m.key}`] = new Chart($(`chart-dist-${m.key}`), {
            type: 'bar',
            data: {
                labels,
                datasets: [{
                    label: m.label,
                    data: values,
                    backgroundColor: m.color + '88',
                    borderColor: m.color,
                    borderWidth: 1,
                    borderRadius: 3,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    y: { beginAtZero: true, title: { display: true, text: m.label } },
                    x: { ticks: { maxRotation: 45 } },
                },
            },
        });
    });
}

function renderEpisodeDetailSection(report) {
    const select = $('detail-episode-select');
    const episodes = report.episodes || [];
    select.innerHTML = '';
    episodes.forEach(ep => {
        const opt = document.createElement('option');
        opt.value = ep.episode_index;
        opt.textContent = `Episode ${ep.episode_index} (评分: ${fmt(ep.quality_score, 1)}, ${ep.frame_count} 帧)`;
        select.appendChild(opt);
    });

    const rep = report.representative_detail;
    if (rep) {
        select.value = rep.episode_index;
        STATE.currentEpisode = rep.episode_index;
        renderTimelineCharts(rep);
        renderProblemSection(rep.episode_index, rep.problems || [], rep.timeline);
    }
}

async function loadEpisodeDetail(episodeIndex) {
    try {
        const data = await getJson(`/api/image-analysis/episode-detail?episode=${episodeIndex}`);
        STATE.currentEpisode = episodeIndex;
        renderTimelineCharts({
            episode_index: episodeIndex,
            timeline: data.timeline,
            problems: data.problems,
            velocity_blur: data.velocity_blur,
        });
        renderProblemSection(episodeIndex, data.problems || [], data.timeline);
    } catch (e) {
        console.warn('加载 episode 详情失败:', e);
    }
}

function renderProblemSection(episodeIndex, problems, timeline) {
    STATE.currentProblems = (problems || []).map(p => {
        const entry = { ...p, episode: episodeIndex };
        if (timeline) {
            const i = p.frame;
            if (timeline.blur) entry.blur = timeline.blur[i];
            if (timeline.brightness_content) entry.brightness = timeline.brightness_content[i];
            else if (timeline.brightness) entry.brightness = timeline.brightness[i];
            if (timeline.entropy) entry.entropy = timeline.entropy[i];
            if (timeline.quality) entry.quality = timeline.quality[i];
        }
        return entry;
    });
    STATE.galleryShown = 0;
    renderProblemGallery();
    renderProblemTable(problems);
}

function renderTimelineCharts(detail) {
    const container = $('timeline-charts');
    container.innerHTML = '';
    const tl = detail?.timeline;
    if (!tl) return;

    const ts = tl.timestamps || [];

    const defs = [
        { key: 'brightness_content', fallback: 'brightness', label: '内容亮度', color: '#0891b2', unit: '', tip: 'brightness' },
        { key: 'dark_ratio', label: '暗区占比', color: '#64748b', unit: '', tip: 'dark_ratio' },
        { key: 'quality', label: '综合质量', color: '#15803d', unit: '分', tip: 'quality_score' },
        { key: 'blur', label: '模糊度 (Laplacian方差)', color: '#2563eb', unit: '', tip: 'blur' },
        { key: 'entropy', label: '信息熵', color: '#7c3aed', unit: 'bits', tip: 'entropy' },
        { key: 'contrast', label: '对比度', color: '#ea580c', unit: '', tip: 'contrast' },
        { key: 'frame_diff', label: '帧间差异', color: '#be123c', unit: '', tip: 'frame_diff' },
    ];

    defs.forEach(d => {
        const values = tl[d.key] || (d.fallback ? tl[d.fallback] : null);
        if (!values || !values.length) return;

        const card = document.createElement('div');
        card.className = 'chart-card';
        card.innerHTML = `
            <div class="chart-title-row"><h4>${d.label}</h4>${helpIcon(d.tip)}</div>
            <div class="subtitle">Episode ${detail.episode_index}</div>
            <div class="chart-canvas-wrap"><canvas id="chart-tl-${d.key}"></canvas></div>
        `;
        container.appendChild(card);

        const chartData = ts.map((t, i) => ({ x: t, y: values[i] }));

        destroyChart(`tl-${d.key}`);
        STATE.charts[`tl-${d.key}`] = new Chart($(`chart-tl-${d.key}`), {
            type: 'line',
            data: {
                datasets: [{
                    label: d.label,
                    data: chartData,
                    borderColor: d.color,
                    backgroundColor: d.color + '18',
                    borderWidth: 1.5,
                    pointRadius: 0,
                    fill: true,
                    tension: 0,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                parsing: false,
                plugins: {
                    legend: { display: false },
                    zoom: {
                        pan: { enabled: true, mode: 'x' },
                        zoom: { wheel: { enabled: true }, mode: 'x' },
                    },
                },
                scales: {
                    x: {
                        type: 'linear',
                        title: { display: true, text: '时间 (s)' },
                    },
                    y: {
                        title: { display: true, text: d.label },
                        beginAtZero: d.key === 'frame_diff',
                    },
                },
                interaction: { mode: 'nearest', axis: 'x', intersect: false },
            },
        });
    });
}

const REASON_COLORS = {
    blurry:       { bg: 'rgba(234, 179, 8, 0.65)',   border: '#b45309' },
    dark:         { bg: 'rgba(99, 102, 241, 0.65)',   border: '#4338ca' },
    bright:       { bg: 'rgba(250, 204, 21, 0.65)',   border: '#a16207' },
    overexposed:  { bg: 'rgba(239, 68, 68, 0.65)',    border: '#b91c1c' },
    underexposed: { bg: 'rgba(59, 130, 246, 0.65)',   border: '#1d4ed8' },
    low_info:     { bg: 'rgba(168, 85, 247, 0.65)',   border: '#7e22ce' },
    static:       { bg: 'rgba(156, 163, 175, 0.65)',  border: '#4b5563' },
    scene_change: { bg: 'rgba(236, 72, 153, 0.65)',   border: '#be185d' },
};

function renderProblemTable(problems) {
    const container = $('problem-table-container');

    renderProblemDistChart(problems);
    renderProblemTimelineChart(problems);

    if (!problems || !problems.length) {
        container.innerHTML = '<div style="padding:16px;text-align:center;color:var(--muted);font-size:13px;">该 episode 没有检测到问题帧。</div>';
        return;
    }

    const rows = problems.slice(0, 200).map(p => `
        <tr>
            <td>${p.frame}</td>
            <td>${p.reasons.map(r => `<span class="tag tag-${r}">${REASON_LABELS[r] || r}</span>`).join('')}</td>
        </tr>
    `).join('');

    container.innerHTML = `
        <div class="problem-table-wrap">
            <table class="problem-table">
                <thead><tr><th>帧号</th><th>问题类型</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
        ${problems.length > 200 ? `<div style="padding:8px 12px;font-size:12px;color:var(--muted);">仅显示前 200 个问题帧，共 ${problems.length} 个。</div>` : ''}
    `;
}

function renderProblemDistChart(problems) {
    destroyChart('problemDist');
    const canvas = $('chart-problem-dist');
    if (!canvas) return;

    if (!problems || !problems.length) {
        STATE.charts.problemDist = new Chart(canvas, {
            type: 'bar',
            data: { labels: ['无问题帧'], datasets: [{ data: [0] }] },
            options: {
                responsive: true, maintainAspectRatio: false, indexAxis: 'y',
                plugins: { legend: { display: false } },
            },
        });
        return;
    }

    const counts = {};
    problems.forEach(p => {
        (p.reasons || []).forEach(r => { counts[r] = (counts[r] || 0) + 1; });
    });

    const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    const labels = sorted.map(([r]) => REASON_LABELS[r] || r);
    const values = sorted.map(([, c]) => c);
    const bgColors = sorted.map(([r]) => (REASON_COLORS[r] || { bg: 'rgba(156,163,175,0.5)' }).bg);
    const borderColors = sorted.map(([r]) => (REASON_COLORS[r] || { border: '#6b7280' }).border);

    STATE.charts.problemDist = new Chart(canvas, {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: '问题帧数',
                data: values,
                backgroundColor: bgColors,
                borderColor: borderColors,
                borderWidth: 1,
                borderRadius: 4,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            indexAxis: 'y',
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        afterLabel(ctx) {
                            const total = problems.length;
                            const pct = (ctx.raw / total * 100).toFixed(1);
                            return `占问题帧总数 ${pct}% (${ctx.raw}/${total})`;
                        },
                    },
                },
            },
            scales: {
                x: {
                    beginAtZero: true,
                    title: { display: true, text: '帧数' },
                    ticks: { precision: 0 },
                },
            },
        },
    });
}

function renderProblemTimelineChart(problems) {
    destroyChart('problemTimeline');
    const canvas = $('chart-problem-timeline');
    if (!canvas) return;

    if (!problems || !problems.length) {
        STATE.charts.problemTimeline = new Chart(canvas, {
            type: 'scatter',
            data: { datasets: [] },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } },
        });
        return;
    }

    const byReason = {};
    problems.forEach(p => {
        (p.reasons || []).forEach(r => {
            if (!byReason[r]) byReason[r] = [];
            byReason[r].push(p.frame);
        });
    });

    const reasonOrder = Object.keys(byReason).sort((a, b) => byReason[b].length - byReason[a].length);
    const reasonToY = {};
    reasonOrder.forEach((r, i) => { reasonToY[r] = i; });

    const datasets = reasonOrder.map(r => ({
        label: REASON_LABELS[r] || r,
        data: byReason[r].map(f => ({ x: f, y: reasonToY[r] })),
        backgroundColor: (REASON_COLORS[r] || { bg: 'rgba(156,163,175,0.6)' }).bg,
        borderColor: (REASON_COLORS[r] || { border: '#6b7280' }).border,
        borderWidth: 1,
        pointRadius: 3,
        pointHoverRadius: 5,
    }));

    STATE.charts.problemTimeline = new Chart(canvas, {
        type: 'scatter',
        data: { datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: true,
                    position: 'top',
                    labels: { boxWidth: 10, font: { size: 11 } },
                },
                zoom: {
                    pan: { enabled: true, mode: 'x' },
                    zoom: { wheel: { enabled: true }, mode: 'x' },
                },
                tooltip: {
                    callbacks: {
                        label(ctx) {
                            return `${ctx.dataset.label}: 帧 ${ctx.parsed.x}`;
                        },
                    },
                },
            },
            scales: {
                x: {
                    title: { display: true, text: '帧号' },
                    beginAtZero: true,
                    ticks: { precision: 0 },
                },
                y: {
                    title: { display: false },
                    ticks: {
                        callback(value) {
                            const r = reasonOrder[value];
                            return r ? (REASON_LABELS[r] || r) : '';
                        },
                        stepSize: 1,
                    },
                    min: -0.5,
                    max: reasonOrder.length - 0.5,
                    reverse: false,
                },
            },
        },
    });
}

// ══════════════════════ 问题帧画廊与大图 ══════════════════════

function frameImgUrl(episode, frame) {
    return `/api/image-analysis/frame?camera=${encodeURIComponent(STATE.camera)}&episode=${episode}&frame=${frame}`;
}

function renderProblemGallery() {
    const gallery = $('problem-gallery');
    const moreWrap = $('gallery-load-more');
    const problems = STATE.currentProblems;

    if (!gallery) return;

    if (!problems.length) {
        gallery.innerHTML = '<div style="padding:16px;text-align:center;color:var(--muted);font-size:13px;">该 episode 没有检测到问题帧。</div>';
        moreWrap.classList.add('hidden');
        return;
    }

    const end = Math.min(STATE.galleryShown + GALLERY_PAGE_SIZE, problems.length);

    if (STATE.galleryShown === 0) gallery.innerHTML = '';

    for (let i = STATE.galleryShown; i < end; i++) {
        const p = problems[i];
        const thumb = document.createElement('div');
        thumb.className = 'frame-thumb';
        thumb.setAttribute('data-gallery-idx', i);
        thumb.onclick = () => openLightbox(i);

        const tags = (p.reasons || []).map(r =>
            `<span class="tag tag-${r}" style="font-size:10px;padding:1px 6px;">${REASON_LABELS[r] || r}</span>`
        ).join('');

        const metricLines = [];
        if (p.quality != null) metricLines.push(`质量: ${fmt(p.quality, 1)}`);
        if (p.blur != null) metricLines.push(`模糊度: ${fmt(p.blur, 1)}`);
        if (p.brightness != null) metricLines.push(`内容亮度: ${fmt(p.brightness, 3)}`);
        if (p.entropy != null) metricLines.push(`信息熵: ${fmt(p.entropy, 2)}`);

        thumb.innerHTML = `
            <img src="${frameImgUrl(p.episode, p.frame)}" alt="Frame ${p.frame}" loading="lazy">
            <div class="frame-thumb-info">
                <div class="frame-num">帧 #${p.frame}</div>
                <div class="frame-tags">${tags}</div>
                ${metricLines.length ? `<div class="frame-metrics">${metricLines.join(' | ')}</div>` : ''}
            </div>
        `;
        gallery.appendChild(thumb);
    }

    STATE.galleryShown = end;

    if (end < problems.length) {
        moreWrap.classList.remove('hidden');
        $('gallery-counter').textContent = `已加载 ${end} / ${problems.length}`;
    } else {
        moreWrap.classList.add('hidden');
    }
}

function openLightbox(idx) {
    const problems = STATE.currentProblems;
    if (idx < 0 || idx >= problems.length) return;
    STATE.lightboxIndex = idx;

    const p = problems[idx];
    const lb = $('frame-lightbox');
    const img = $('lightbox-img');
    const info = $('lightbox-info');

    img.src = frameImgUrl(p.episode, p.frame);

    const tags = (p.reasons || []).map(r => REASON_LABELS[r] || r).join(', ');
    const metrics = [];
    if (p.quality != null) metrics.push(`质量 ${fmt(p.quality, 1)}`);
    if (p.blur != null) metrics.push(`模糊度 ${fmt(p.blur, 1)}`);
    if (p.brightness != null) metrics.push(`内容亮度 ${fmt(p.brightness, 3)}`);
    if (p.entropy != null) metrics.push(`信息熵 ${fmt(p.entropy, 2)}`);

    info.innerHTML = `Episode ${p.episode} · 帧 #${p.frame} · ${tags}` +
        (metrics.length ? `<br>${metrics.join(' | ')}` : '') +
        `<br><span style="font-size:11px;opacity:0.6;">${idx + 1} / ${problems.length}</span>`;

    lb.classList.remove('hidden');
    $('lightbox-prev').style.visibility = idx > 0 ? 'visible' : 'hidden';
    $('lightbox-next').style.visibility = idx < problems.length - 1 ? 'visible' : 'hidden';
}

function closeLightbox(e) {
    if (e && e.target !== $('frame-lightbox')) return;
    $('frame-lightbox').classList.add('hidden');
}

function navLightbox(dir) {
    const next = STATE.lightboxIndex + dir;
    if (next >= 0 && next < STATE.currentProblems.length) {
        openLightbox(next);
    }
}

function renderVelocityBlurSection(report) {
    const section = $('velocity-blur-section');
    const vb = report.velocity_blur_correlation;
    if (!vb || !vb.velocity || !vb.blur) {
        section.classList.add('hidden');
        return;
    }
    section.classList.remove('hidden');

    const corr = vb.correlation;
    const corrStr = corr != null ? fmt(corr, 4) : 'N/A';
    let corrClass = '';
    if (corr != null) {
        if (corr < -0.3) corrClass = 'grade-bad';
        else if (corr < -0.1) corrClass = 'grade-poor';
        else if (corr < 0.1) corrClass = 'grade-good';
        else corrClass = 'grade-acceptable';
    }

    $('velocity-blur-info').innerHTML = `
        <div>
            <div class="corr-value ${corrClass}">${corrStr}</div>
            <div class="corr-label">Pearson 相关系数 ${helpIcon('velocity_blur')}</div>
        </div>
        <div>
            <div style="font-size:13px;color:var(--muted);line-height:1.6;">
                负值表示关节速度越高，图像越模糊（Laplacian方差越低）。<br>
                接近0表示无明显关联。样本数: ${fmtInt(vb.sample_count)}。
            </div>
        </div>
    `;

    destroyChart('velocityBlur');
    const scatterData = vb.velocity.map((v, i) => ({ x: v, y: vb.blur[i] }));

    STATE.charts.velocityBlur = new Chart($('chart-velocity-blur'), {
        type: 'scatter',
        data: {
            datasets: [{
                label: '关节速度 vs 模糊度',
                data: scatterData,
                backgroundColor: 'rgba(124, 58, 237, 0.25)',
                borderColor: 'rgba(124, 58, 237, 0.6)',
                borderWidth: 1,
                pointRadius: 2,
                pointHoverRadius: 4,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                zoom: {
                    pan: { enabled: true, mode: 'xy' },
                    zoom: { wheel: { enabled: true }, mode: 'xy' },
                },
            },
            scales: {
                x: {
                    title: { display: true, text: '关节最大绝对速度 (rad/s)' },
                    beginAtZero: true,
                },
                y: {
                    title: { display: true, text: '模糊度 (Laplacian方差)' },
                    beginAtZero: true,
                },
            },
        },
    });
}

// ══════════════════════ 事件绑定 ══════════════════════

$('load-btn').addEventListener('click', loadDataset);
$('dataset-path').addEventListener('keydown', e => { if (e.key === 'Enter') loadDataset(); });
$('analyze-btn').addEventListener('click', startAnalysis);
$('detail-episode-select').addEventListener('change', e => {
    const idx = parseInt(e.target.value);
    if (Number.isFinite(idx)) loadEpisodeDetail(idx);
});
$('gallery-more-btn').addEventListener('click', renderProblemGallery);

document.addEventListener('keydown', e => {
    const lb = $('frame-lightbox');
    if (lb.classList.contains('hidden')) return;
    if (e.key === 'Escape') closeLightbox();
    else if (e.key === 'ArrowLeft') navLightbox(-1);
    else if (e.key === 'ArrowRight') navLightbox(1);
});
