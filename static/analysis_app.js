const $ = id => document.getElementById(id);

const ANALYSIS_STATE = {
    loading: false,
    report: null,
    nextChartGroupId: 1,
    chartGroups: new Map(),
    chartContexts: new Map(),
    chartInstances: new Map(),
    jointSelections: new Map(),
    interactionsBound: false,
    zoomRegistered: false,
    helperRegistered: false,
};

const TERM_TIPS = {
    position: '位值: 直接展示代表性 episode 中该关节在时间轴上的位置变化。',
    velocity: '速度: v_t = (theta_t - theta_{t-1}) / (t_t - t_{t-1})。图里展示的是代表性 episode 的速度曲线。',
    anomaly: '突变阈值: 对当前曲线先计算 |x| 的中位数 median 和 MAD，再取 threshold = max(median(|x|) + 6*MAD(|x|), mean(|x|) + 3*std(|x|))。超过阈值的点会用红色标出。',
    smoothing: '动作平滑: 复用速度突变点，把突变转移到对应 action 位值帧，并只在这些帧附近用 5 帧局部均值生成预览曲线。',
};

const METRIC_DEFS = {
    position: {
        key: 'position',
        label: '位值 theta',
        short: '位值',
        color: '#2563eb',
        emptyText: '该来源没有可用的位值曲线。',
    },
    velocity: {
        key: 'velocity',
        label: '速度 v',
        short: '速度',
        color: '#0891b2',
        emptyText: '该来源没有可用的速度曲线。',
    },
};

function escHtml(text) {
    const div = document.createElement('div');
    div.textContent = text ?? '';
    return div.innerHTML;
}

function formatNumber(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '--';
    const num = Number(value);
    const abs = Math.abs(num);
    if (abs >= 1000) return num.toFixed(2);
    if (abs >= 10) return num.toFixed(3);
    if (abs >= 1) return num.toFixed(4);
    return num.toFixed(6).replace(/0+$/, '').replace(/\.$/, '');
}

function formatInteger(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '--';
    return Number(value).toLocaleString('zh-CN');
}

function setStatus(message, type = 'info') {
    const el = $('analysis-status');
    el.textContent = message;
    el.className = `status-line ${type}`;
}

function helpIcon(termKey) {
    const tip = TERM_TIPS[termKey];
    if (!tip) return '';
    return `<span class="term-help" tabindex="0" data-tip="${escHtml(tip)}">?</span>`;
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

function buildSummaryStrip(summary) {
    const items = [
        { label: 'Episodes', value: formatInteger(summary.total_episodes) },
        { label: 'Frames', value: formatInteger(summary.total_frames) },
        { label: 'Joint Pairs', value: formatInteger(summary.joint_pair_count) },
        { label: 'FPS', value: formatNumber(summary.fps) },
        { label: 'Robot', value: escHtml(summary.robot_type || 'unknown') },
    ];

    return items.map(item => `
        <article class="summary-chip">
            <span class="label">${item.label}</span>
            <span class="value">${item.value}</span>
        </article>
    `).join('');
}

function hasSeries(preview, metricKey) {
    const series = preview?.[metricKey];
    return Array.isArray(series?.x) && Array.isArray(series?.y) && series.x.length && series.y.length;
}

function normalizeSeries(series) {
    if (!series || !Array.isArray(series.x) || !Array.isArray(series.y)) return null;

    const length = Math.min(series.x.length, series.y.length);
    if (!length) return null;

    const anomalyRaw = Array.isArray(series.anomaly) ? series.anomaly : [];
    const points = [];
    const anomalyPoints = [];

    for (let index = 0; index < length; index += 1) {
        const x = Number(series.x[index]);
        const y = Number(series.y[index]);
        if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
        const point = { x, y };
        points.push(point);
        if (Boolean(anomalyRaw[index])) anomalyPoints.push(point);
    }

    if (!points.length) return null;

    return {
        points,
        anomalyPoints,
        threshold: Number.isFinite(Number(series.threshold)) ? Number(series.threshold) : null,
        anomalyCount: Number(series.anomaly_count) || anomalyPoints.length,
        minX: Math.min(...points.map(point => point.x)),
        maxX: Math.max(...points.map(point => point.x)),
        minY: Math.min(...points.map(point => point.y)),
        maxY: Math.max(...points.map(point => point.y)),
    };
}

function destroyCharts() {
    ANALYSIS_STATE.chartInstances.forEach(chart => chart.destroy());
    ANALYSIS_STATE.chartInstances.clear();
    ANALYSIS_STATE.chartGroups.clear();
    ANALYSIS_STATE.chartContexts.clear();
    ANALYSIS_STATE.nextChartGroupId = 1;
}

function allocChartGroupId() {
    const groupId = `chart-group-${ANALYSIS_STATE.nextChartGroupId}`;
    ANALYSIS_STATE.nextChartGroupId += 1;
    return groupId;
}

function buildChartId(groupId, metricKey) {
    return `${groupId}-${metricKey}`;
}

function getChartCanvasWrap(chart) {
    return chart?.canvas?.closest('.chart-canvas-wrap') || null;
}

function setChartDraggingState(chart, dragging) {
    const wrap = getChartCanvasWrap(chart);
    if (wrap) wrap.classList.toggle('is-dragging', Boolean(dragging));
}

function chartLibraryReady() {
    return typeof window.Chart === 'function';
}

function ensureChartPlugins() {
    if (!chartLibraryReady()) return;

    if (!ANALYSIS_STATE.helperRegistered && typeof window.Chart.register === 'function') {
        const syncMarkerPlugin = {
            id: 'syncMarker',
            afterDatasetsDraw(chart) {
                const selectionX = chart.$syncSelectionX;
                const xScale = chart.scales?.x;
                const area = chart.chartArea;
                if (!Number.isFinite(selectionX) || !xScale || !area) return;

                const pixel = xScale.getPixelForValue(selectionX);
                if (!Number.isFinite(pixel) || pixel < area.left || pixel > area.right) return;

                const ctx = chart.ctx;
                ctx.save();
                ctx.beginPath();
                ctx.moveTo(pixel, area.top);
                ctx.lineTo(pixel, area.bottom);
                ctx.lineWidth = 1;
                ctx.strokeStyle = 'rgba(15, 23, 42, 0.36)';
                ctx.setLineDash([4, 4]);
                ctx.stroke();
                ctx.restore();
            },
        };

        try {
            window.Chart.register(syncMarkerPlugin);
        } catch (error) {
            // Ignore duplicate plugin registration.
        }
        ANALYSIS_STATE.helperRegistered = true;
    }

    if (!ANALYSIS_STATE.zoomRegistered && typeof window.Chart.register === 'function') {
        const plugin = window.ChartZoom || window['chartjs-plugin-zoom'] || window.zoomPlugin;
        if (plugin) {
            try {
                window.Chart.register(plugin);
            } catch (error) {
                // Ignore duplicate plugin registration.
            }
        }
        ANALYSIS_STATE.zoomRegistered = true;
    }
}

function registerChartGroup(preview, options = {}) {
    const availableMetrics = Object.keys(METRIC_DEFS).filter(metricKey => hasSeries(preview, metricKey));
    if (!availableMetrics.length) return null;

    const groupId = allocChartGroupId();
    const chartIds = {};

    Object.keys(METRIC_DEFS).forEach(metricKey => {
        const chartId = buildChartId(groupId, metricKey);
        chartIds[metricKey] = chartId;
        ANALYSIS_STATE.chartContexts.set(chartId, { groupId, metricKey });
    });

    ANALYSIS_STATE.chartGroups.set(groupId, {
        preview,
        sourceType: options.sourceType || 'unknown',
        availableMetrics,
        chartIds,
        range: null,
        hoverSelectionX: null,
        lockedSelectionX: null,
        syncingRange: false,
        syncingSelection: false,
    });
    return groupId;
}

function renderMetricPanel(groupId, metricKey) {
    const group = ANALYSIS_STATE.chartGroups.get(groupId);
    const chartId = group?.chartIds?.[metricKey];
    const def = METRIC_DEFS[metricKey];
    if (!group || !chartId) return '';

    if (!group.availableMetrics.includes(metricKey)) {
        return `
            <section class="metric-panel metric-panel-empty">
                <div class="metric-head">
                    <div class="metric-title">${escHtml(def.short)}</div>
                    <div class="metric-caption">${helpIcon(metricKey)}</div>
                </div>
                <div class="series-empty">${escHtml(def.emptyText)}</div>
            </section>
        `;
    }

    return `
        <section class="metric-panel" data-chart-panel="${chartId}">
            <div class="metric-head">
                <div class="metric-title">${escHtml(def.short)}</div>
                <div class="metric-caption">${helpIcon(metricKey)}</div>
            </div>
            <div id="chart-meta-${chartId}" class="chart-meta"></div>
            <div class="chart-canvas-wrap">
                <canvas id="canvas-${chartId}" class="series-canvas" aria-label="${escHtml(def.label)} 交互图"></canvas>
            </div>
        </section>
    `;
}

function renderSourcePanel(label, axisName, metrics) {
    const preview = metrics?.temporal_preview;
    const sourceType = label === 'Action' ? 'action' : 'state';
    const groupId = registerChartGroup(preview, { sourceType });

    if (!groupId) {
        return `
            <div class="source-panel">
                <div class="source-head">
                    <div class="source-title">${escHtml(label)}</div>
                    <div class="source-name">${axisName ? escHtml(axisName) : '未命名轴'}</div>
                </div>
                <div class="series-empty">该来源没有可用的位值 / 速度联动曲线。</div>
            </div>
        `;
    }

    const group = ANALYSIS_STATE.chartGroups.get(groupId);
    const episodeIndex = Number(preview?.episode_index);
    const frameCount = Number(preview?.frame_count);
    const previewMeta = [
        Number.isFinite(episodeIndex) ? `episode #${formatInteger(episodeIndex)}` : null,
        Number.isFinite(frameCount) ? `${formatInteger(frameCount)} 帧` : null,
        '双图联动',
    ].filter(Boolean).join(' · ');

    return `
        <div class="source-panel">
            <div class="source-head">
                <div>
                    <div class="source-title">${escHtml(label)}</div>
                    <div class="source-subtitle">${escHtml(previewMeta)}</div>
                </div>
                <div class="source-name">${axisName ? escHtml(axisName) : '未命名轴'}</div>
            </div>
            <div class="chart-shell" data-chart-group="${groupId}">
                <div class="chart-toolbar">
                    <div class="sync-note">缩放、拖动、悬停、点击选时会在位值 / 速度两图间同步</div>
                    <button type="button" class="ghost-btn reset-zoom-btn" data-chart-group-id="${groupId}">重置视图</button>
                </div>
                <div class="paired-chart-grid">
                    ${renderMetricPanel(groupId, 'position')}
                    ${renderMetricPanel(groupId, 'velocity')}
                </div>
            </div>
        </div>
    `;
}

function renderJointCard(joint) {
    return `
        <article class="joint-card">
            <div class="joint-card-head">
                <div>
                    <h5>${escHtml(joint.joint_name)}</h5>
                    <p>
                        state: ${escHtml(joint.state_name || '--')}<br>
                        action: ${escHtml(joint.action_name || '--')}
                    </p>
                </div>
                <span class="joint-index">#${formatInteger(joint.joint_index)}</span>
            </div>
            <div class="source-grid">
                ${renderSourcePanel('Observation State', joint.state_name, joint.state)}
                ${renderSourcePanel('Action', joint.action_name, joint.action)}
            </div>
        </article>
    `;
}

function getSelectedJointIndex(group) {
    const saved = ANALYSIS_STATE.jointSelections.get(group.key);
    if (Number.isInteger(saved) && saved >= 0 && saved < (group.joints || []).length) return saved;
    return 0;
}

function renderJointSelector(group, selectedIndex) {
    return `
        <label class="joint-select-wrap">
            <span class="joint-select-label">当前 joint</span>
            <select class="joint-select" data-group-key="${escHtml(group.key)}">
                ${(group.joints || []).map((joint, index) => `
                    <option value="${index}" ${index === selectedIndex ? 'selected' : ''}>${escHtml(joint.joint_name)}</option>
                `).join('')}
            </select>
        </label>
    `;
}

function renderJointGroupBody(group) {
    const selectedIndex = getSelectedJointIndex(group);
    const selectedJoint = (group.joints || [])[selectedIndex];
    if (!selectedJoint) {
        return '<div class="empty-state">当前分组没有可用关节。</div>';
    }

    return `
        <div class="joint-group-controls">
            <div class="joint-group-caption">当前分组只展示一个 joint，切换后保留联动位值 / 速度双图。</div>
            ${renderJointSelector(group, selectedIndex)}
        </div>
        <div class="joint-grid">
            ${renderJointCard(selectedJoint)}
        </div>
    `;
}

function buildJointGroupList(jointGroups) {
    if (!jointGroups.length) {
        return '<div class="empty-state">当前数据集没有可用于生成关节图的向量字段。</div>';
    }

    return jointGroups.map((group, groupIndex) => `
        <details class="joint-group" data-joint-group-key="${escHtml(group.key)}" ${groupIndex < 2 ? 'open' : ''}>
            <summary>
                <div class="group-title">
                    <h4>${escHtml(group.label)}</h4>
                    <p>${escHtml(group.key)} · ${formatInteger(group.joint_count)} 个关节</p>
                </div>
                <span class="group-badge">展开 / 隐藏</span>
            </summary>
            <div class="joint-group-body">${renderJointGroupBody(group)}</div>
        </details>
    `).join('');
}

function destroyChartsIn(root = document) {
    root.querySelectorAll?.('[data-chart-panel]').forEach(panelEl => {
        const chartId = panelEl.dataset.chartPanel;
        const chart = chartId ? ANALYSIS_STATE.chartInstances.get(chartId) : null;
        if (chart) chart.destroy();
        if (chartId) {
            ANALYSIS_STATE.chartInstances.delete(chartId);
            ANALYSIS_STATE.chartContexts.delete(chartId);
        }
    });

    root.querySelectorAll?.('[data-chart-group]').forEach(groupEl => {
        const chartGroupId = groupEl.dataset.chartGroup;
        if (chartGroupId) ANALYSIS_STATE.chartGroups.delete(chartGroupId);
    });
}

function rerenderJointGroup(groupKey, detailsEl) {
    const reportGroup = ANALYSIS_STATE.report?.joint_groups?.find(group => group.key === groupKey);
    const bodyEl = detailsEl?.querySelector('.joint-group-body');
    if (!reportGroup || !bodyEl) return;

    destroyChartsIn(bodyEl);
    bodyEl.innerHTML = renderJointGroupBody(reportGroup);
    requestAnimationFrame(() => initChartsIn(bodyEl));
}

function buildThresholdDataset(points, threshold) {
    if (!Number.isFinite(threshold) || points.length < 2) return [];
    const minX = points[0].x;
    const maxX = points[points.length - 1].x;
    return [
        {
            label: '阈值 +',
            data: [{ x: minX, y: threshold }, { x: maxX, y: threshold }],
            borderColor: 'rgba(217,45,32,0.55)',
            borderDash: [6, 6],
            borderWidth: 1.4,
            pointRadius: 0,
            fill: false,
        },
        {
            label: '阈值 -',
            data: [{ x: minX, y: -threshold }, { x: maxX, y: -threshold }],
            borderColor: 'rgba(217,45,32,0.32)',
            borderDash: [6, 6],
            borderWidth: 1.2,
            pointRadius: 0,
            fill: false,
        },
    ];
}

function buildChartDatasets(metricDef, seriesData, group = null) {
    const datasets = [
        {
            label: metricDef.label,
            data: seriesData.points,
            borderColor: metricDef.color,
            backgroundColor: `${metricDef.color}20`,
            borderWidth: 2.2,
            pointRadius: 0,
            pointHoverRadius: 3,
            tension: 0.14,
            fill: false,
        },
    ];

    const smoothingData = (
        group?.sourceType === 'action' && metricDef.key === 'position'
            ? normalizeSeries(group.preview?.smoothing?.position)
            : null
    );
    if (smoothingData?.points?.length) {
        datasets.push({
            label: '动作平滑建议',
            data: smoothingData.points,
            borderColor: '#16a34a',
            backgroundColor: 'rgba(22,163,74,0.12)',
            borderDash: [5, 4],
            borderWidth: 2,
            pointRadius: 0,
            pointHoverRadius: 3,
            tension: 0.16,
            fill: false,
        });
    }

    if (metricDef.key === 'velocity') {
        datasets.push(...buildThresholdDataset(seriesData.points, seriesData.threshold));
        if (seriesData.anomalyPoints.length) {
            datasets.push({
                type: 'scatter',
                label: '突变点',
                data: seriesData.anomalyPoints,
                borderColor: '#d92d20',
                backgroundColor: '#d92d20',
                pointRadius: 4,
                pointHoverRadius: 5,
                pointBorderColor: '#ffffff',
                pointBorderWidth: 1.4,
                showLine: false,
            });
        }
    }

    return datasets;
}

function getChartContext(chartId) {
    return ANALYSIS_STATE.chartContexts.get(chartId) || null;
}

function getChartGroup(groupId) {
    return ANALYSIS_STATE.chartGroups.get(groupId) || null;
}

function getChartById(chartId) {
    return ANALYSIS_STATE.chartInstances.get(chartId) || null;
}

function snapSelectionX(chart, targetX) {
    const points = chart?.data?.datasets?.[0]?.data;
    if (!Array.isArray(points) || !points.length || !Number.isFinite(targetX)) return null;

    let bestX = null;
    let bestDist = Number.POSITIVE_INFINITY;
    for (const point of points) {
        const dist = Math.abs(Number(point.x) - targetX);
        if (dist < bestDist) {
            bestDist = dist;
            bestX = Number(point.x);
        }
    }
    return Number.isFinite(bestX) ? bestX : null;
}

function getActiveSelectionX(chart, event) {
    const xScale = chart?.scales?.x;
    const nativeEvent = event?.native || event;
    const area = chart?.chartArea;
    if (!xScale || !nativeEvent || !area) return null;

    const offsetX = Number(nativeEvent.offsetX);
    if (!Number.isFinite(offsetX) || offsetX < area.left || offsetX > area.right) return null;

    return snapSelectionX(chart, xScale.getValueForPixel(offsetX));
}

function setActiveSelectionForChart(chart, selectionX) {
    if (!chart?.setActiveElements || !chart?.tooltip?.setActiveElements) return;

    chart.$syncSelectionX = Number.isFinite(selectionX) ? selectionX : null;
    if (!Number.isFinite(selectionX)) {
        chart.setActiveElements([]);
        chart.tooltip.setActiveElements([], { x: 0, y: 0 });
        chart.update('none');
        return;
    }

    const points = chart.data?.datasets?.[0]?.data || [];
    let nearestIndex = -1;
    let nearestPoint = null;
    let bestDist = Number.POSITIVE_INFINITY;

    points.forEach((point, index) => {
        const dist = Math.abs(Number(point.x) - selectionX);
        if (dist < bestDist) {
            bestDist = dist;
            nearestIndex = index;
            nearestPoint = point;
        }
    });

    if (nearestIndex < 0 || !nearestPoint) {
        chart.setActiveElements([]);
        chart.tooltip.setActiveElements([], { x: 0, y: 0 });
        chart.update('none');
        return;
    }

    const position = {
        x: chart.scales.x.getPixelForValue(nearestPoint.x),
        y: chart.scales.y.getPixelForValue(nearestPoint.y),
    };

    chart.setActiveElements([{ datasetIndex: 0, index: nearestIndex }]);
    chart.tooltip.setActiveElements([{ datasetIndex: 0, index: nearestIndex }], position);
    chart.update('none');
}

function applyGroupSelection(groupId) {
    const group = getChartGroup(groupId);
    if (!group || group.syncingSelection) return;

    group.syncingSelection = true;
    const selectionX = Number.isFinite(group.lockedSelectionX)
        ? group.lockedSelectionX
        : group.hoverSelectionX;

    Object.values(group.chartIds).forEach(chartId => {
        const chart = getChartById(chartId);
        if (chart) setActiveSelectionForChart(chart, selectionX);
    });
    group.syncingSelection = false;
}

function setGroupHoverSelection(groupId, selectionX) {
    const group = getChartGroup(groupId);
    if (!group || Number.isFinite(group.lockedSelectionX)) return;
    group.hoverSelectionX = Number.isFinite(selectionX) ? selectionX : null;
    applyGroupSelection(groupId);
}

function toggleGroupLockedSelection(groupId, selectionX) {
    const group = getChartGroup(groupId);
    if (!group || !Number.isFinite(selectionX)) return;

    if (Number.isFinite(group.lockedSelectionX) && Math.abs(group.lockedSelectionX - selectionX) < 1e-9) {
        group.lockedSelectionX = null;
        group.hoverSelectionX = selectionX;
    } else {
        group.lockedSelectionX = selectionX;
        group.hoverSelectionX = selectionX;
    }
    applyGroupSelection(groupId);
}

function clearGroupHoverSelection(groupId) {
    const group = getChartGroup(groupId);
    if (!group || Number.isFinite(group.lockedSelectionX)) return;
    group.hoverSelectionX = null;
    applyGroupSelection(groupId);
}

function syncGroupRangeFromChart(chart) {
    const chartId = chart?.$chartId;
    const chartContext = chartId ? getChartContext(chartId) : null;
    const group = chartContext ? getChartGroup(chartContext.groupId) : null;
    const xScale = chart?.scales?.x;

    if (!group || !xScale || group.syncingRange) return;

    const range = {
        min: Number.isFinite(xScale.min) ? xScale.min : null,
        max: Number.isFinite(xScale.max) ? xScale.max : null,
    };

    group.range = range;
    group.syncingRange = true;
    Object.values(group.chartIds).forEach(peerChartId => {
        if (peerChartId === chartId) return;
        const peerChart = getChartById(peerChartId);
        if (!peerChart) return;

        if (range.min === null) {
            delete peerChart.options.scales.x.min;
        } else {
            peerChart.options.scales.x.min = range.min;
        }
        if (range.max === null) {
            delete peerChart.options.scales.x.max;
        } else {
            peerChart.options.scales.x.max = range.max;
        }
        peerChart.update('none');
    });
    group.syncingRange = false;
}

function resetChartGroup(groupId) {
    const group = getChartGroup(groupId);
    if (!group) return;

    group.range = null;
    group.hoverSelectionX = null;
    group.lockedSelectionX = null;

    Object.values(group.chartIds).forEach(chartId => {
        const chart = getChartById(chartId);
        if (!chart) return;
        if (chart.resetZoom) {
            chart.resetZoom('none');
        } else {
            delete chart.options.scales.x.min;
            delete chart.options.scales.x.max;
            chart.update('none');
        }
        setChartDraggingState(chart, false);
        chart.$syncSelectionX = null;
    });
    applyGroupSelection(groupId);
}

function updateChartMeta(chartId) {
    const chartContext = getChartContext(chartId);
    const metaEl = $(`chart-meta-${chartId}`);
    if (!chartContext || !metaEl) return;

    const group = getChartGroup(chartContext.groupId);
    const metricDef = METRIC_DEFS[chartContext.metricKey];
    const seriesData = normalizeSeries(group?.preview?.[chartContext.metricKey]);
    if (!metricDef || !seriesData) {
        metaEl.innerHTML = '<span class="chart-pill">暂无可用数据</span>';
        return;
    }

    const pills = [
        `<span class="chart-pill">${escHtml(metricDef.short)} ${helpIcon(chartContext.metricKey)}</span>`,
        `<span class="chart-pill">范围 ${formatNumber(seriesData.minY)} → ${formatNumber(seriesData.maxY)}</span>`,
        `<span class="chart-pill">时间 ${formatNumber(seriesData.minX)} → ${formatNumber(seriesData.maxX)}</span>`,
        `<span class="chart-pill">采样点 ${formatInteger(seriesData.points.length)}</span>`,
    ];

    if (chartContext.metricKey === 'velocity') {
        pills.push(`<span class="chart-pill">阈值 ${formatNumber(seriesData.threshold)} ${helpIcon('anomaly')}</span>`);
        pills.push(`<span class="chart-pill">突变点 ${formatInteger(seriesData.anomalyCount)}</span>`);
    }
    if (chartContext.metricKey === 'position' && group?.sourceType === 'action' && group.preview?.smoothing) {
        const smoothing = group.preview.smoothing;
        pills.push(`<span class="chart-pill">动作平滑建议 ${helpIcon('smoothing')}</span>`);
        pills.push(`<span class="chart-pill">突变点 ${formatInteger(smoothing.anomaly_count)}</span>`);
        pills.push(`<span class="chart-pill">窗口 ${formatInteger(smoothing.window)} 帧</span>`);
        pills.push(`<span class="chart-pill">最大修正 ${formatNumber(smoothing.max_abs_delta)}</span>`);
    }

    metaEl.innerHTML = pills.join('');
}

function buildChartOptions(chartId, metricDef) {
    return {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        normalized: true,
        parsing: false,
        interaction: {
            mode: 'nearest',
            intersect: false,
        },
        onHover(event, activeElements, chart) {
            if (!activeElements.length) return;
            const chartContext = getChartContext(chartId);
            if (!chartContext) return;
            const selectionX = getActiveSelectionX(chart, event);
            if (Number.isFinite(selectionX)) setGroupHoverSelection(chartContext.groupId, selectionX);
        },
        scales: {
            x: {
                type: 'linear',
                title: {
                    display: true,
                    text: '时间 t (s)',
                },
                grid: {
                    color: 'rgba(148,163,184,0.16)',
                },
                ticks: {
                    callback(value) {
                        return formatNumber(value);
                    },
                },
            },
            y: {
                title: {
                    display: true,
                    text: metricDef.short,
                },
                grid: {
                    color: 'rgba(148,163,184,0.16)',
                },
                ticks: {
                    callback(value) {
                        return formatNumber(value);
                    },
                },
            },
        },
        plugins: {
            legend: {
                display: false,
            },
            tooltip: {
                callbacks: {
                    title(items) {
                        const item = items?.[0];
                        return item ? `t = ${formatNumber(item.parsed.x)} s` : '';
                    },
                    label(context) {
                        if (context.dataset.type === 'scatter') {
                            return `突变点: ${formatNumber(context.parsed.y)}`;
                        }
                        return `${context.dataset.label}: ${formatNumber(context.parsed.y)}`;
                    },
                },
            },
            zoom: {
                pan: {
                    enabled: true,
                    mode: 'x',
                    threshold: 6,
                    onPanStart({ chart }) {
                        setChartDraggingState(chart, true);
                    },
                    onPan({ chart }) {
                        syncGroupRangeFromChart(chart);
                    },
                    onPanComplete({ chart }) {
                        setChartDraggingState(chart, false);
                        syncGroupRangeFromChart(chart);
                    },
                    onPanRejected({ chart }) {
                        setChartDraggingState(chart, false);
                    },
                },
                zoom: {
                    wheel: {
                        enabled: true,
                    },
                    pinch: {
                        enabled: true,
                    },
                    mode: 'x',
                    onZoom({ chart }) {
                        syncGroupRangeFromChart(chart);
                    },
                    onZoomComplete({ chart }) {
                        setChartDraggingState(chart, false);
                        syncGroupRangeFromChart(chart);
                    },
                    onZoomRejected({ chart }) {
                        setChartDraggingState(chart, false);
                    },
                },
            },
        },
    };
}

function bindChartCanvasInteractions(chartId, chart) {
    const canvas = chart?.canvas;
    if (!canvas) return;

    canvas.dataset.chartId = chartId;
    if (canvas.dataset.syncBound === '1') return;

    canvas.dataset.syncBound = '1';
    canvas.addEventListener('mouseleave', event => {
        const currentChartId = event.currentTarget?.dataset?.chartId;
        const chartContext = currentChartId ? getChartContext(currentChartId) : null;
        if (chartContext) clearGroupHoverSelection(chartContext.groupId);
    });
    canvas.addEventListener('click', event => {
        const currentChartId = event.currentTarget?.dataset?.chartId;
        const chartContext = currentChartId ? getChartContext(currentChartId) : null;
        const currentChart = currentChartId ? getChartById(currentChartId) : null;
        if (!chartContext || !currentChart) return;
        const selectionX = getActiveSelectionX(currentChart, event);
        if (Number.isFinite(selectionX)) toggleGroupLockedSelection(chartContext.groupId, selectionX);
    });
}

function createOrUpdateChart(chartId) {
    const chartContext = getChartContext(chartId);
    const group = chartContext ? getChartGroup(chartContext.groupId) : null;
    const metricDef = chartContext ? METRIC_DEFS[chartContext.metricKey] : null;
    if (!chartContext || !group || !metricDef) return;
    if (!group.availableMetrics.includes(chartContext.metricKey)) return;

    if (!chartLibraryReady()) {
        const metaEl = $(`chart-meta-${chartId}`);
        if (metaEl) {
            metaEl.innerHTML = '<span class="chart-pill">图表库未加载，当前浏览器没有成功初始化交互图。</span>';
        }
        return;
    }
    ensureChartPlugins();

    const canvas = $(`canvas-${chartId}`);
    if (!canvas) return;
    const host = canvas.parentElement;
    if (!host || host.clientWidth < 24 || host.clientHeight < 24) {
        requestAnimationFrame(() => createOrUpdateChart(chartId));
        return;
    }

    const seriesData = normalizeSeries(group.preview?.[chartContext.metricKey]);
    if (!seriesData) return;

    const existing = getChartById(chartId);
    if (existing) existing.destroy();

    const options = buildChartOptions(chartId, metricDef);
    if (group.range?.min !== null && group.range?.min !== undefined) {
        options.scales.x.min = group.range.min;
    }
    if (group.range?.max !== null && group.range?.max !== undefined) {
        options.scales.x.max = group.range.max;
    }

    const chart = new Chart(canvas, {
        type: 'line',
        data: {
            datasets: buildChartDatasets(metricDef, seriesData, group),
        },
        options,
    });

    chart.$chartId = chartId;
    ANALYSIS_STATE.chartInstances.set(chartId, chart);
    updateChartMeta(chartId);
    bindChartCanvasInteractions(chartId, chart);
    applyGroupSelection(chartContext.groupId);
}

function initChartsIn(root = document) {
    root.querySelectorAll?.('[data-chart-panel]').forEach(panelEl => {
        const chartId = panelEl.dataset.chartPanel;
        if (!chartId) return;
        createOrUpdateChart(chartId);
    });
}

function bindInteractions() {
    if (ANALYSIS_STATE.interactionsBound) return;
    ANALYSIS_STATE.interactionsBound = true;

    $('joint-group-list').addEventListener('click', event => {
        const resetButton = event.target.closest('.reset-zoom-btn[data-chart-group-id]');
        if (resetButton) {
            resetChartGroup(resetButton.dataset.chartGroupId);
        }
    });

    $('joint-group-list').addEventListener('change', event => {
        const jointPicker = event.target.closest('select.joint-select[data-group-key]');
        if (!jointPicker) return;

        const groupKey = jointPicker.dataset.groupKey;
        const nextIndex = Number(jointPicker.value);
        if (!groupKey || !Number.isInteger(nextIndex)) return;

        ANALYSIS_STATE.jointSelections.set(groupKey, nextIndex);
        rerenderJointGroup(groupKey, jointPicker.closest('.joint-group'));
    });
}

function bindGroupToggles() {
    document.querySelectorAll('.joint-group').forEach(group => {
        if (group.dataset.toggleBound === '1') return;
        group.dataset.toggleBound = '1';
        group.addEventListener('toggle', () => {
            if (!group.open) return;
            requestAnimationFrame(() => initChartsIn(group));
        });
    });
}

function renderReport(report) {
    destroyCharts();
    $('summary-grid').innerHTML = buildSummaryStrip(report.summary || {});
    $('joint-group-list').innerHTML = buildJointGroupList(report.joint_groups || []);

    $('summary-section').classList.remove('hidden');
    $('joint-chart-section').classList.remove('hidden');

    bindInteractions();
    bindGroupToggles();
    requestAnimationFrame(() => {
        requestAnimationFrame(() => {
            document.querySelectorAll('.joint-group[open]').forEach(group => initChartsIn(group));
        });
    });
}

async function loadAnalysis() {
    const input = $('dataset-path');
    const path = input.value.trim();
    if (!path || ANALYSIS_STATE.loading) {
        if (!path) setStatus('请先输入数据集路径。', 'error');
        return;
    }

    ANALYSIS_STATE.loading = true;
    $('analyze-btn').disabled = true;
    setStatus('正在加载数据集，并生成每个关节的位值 / 速度联动报告...', 'loading');

    try {
        const report = await postJson('/api/analysis/load', { path });
        ANALYSIS_STATE.report = report;
        renderReport(report);
        localStorage.setItem('lerobot-analysis-last-path', path);
        const groupCount = (report.joint_groups || []).length;
        if (chartLibraryReady()) {
            setStatus(`分析完成，已按 ${formatInteger(groupCount)} 个 joint group 生成标准位值 / 速度联动图，可同步缩放、拖动和选时查看。`, 'success');
        } else {
            setStatus('数据已分析完成，但浏览器没有成功加载图表库，所以当前只显示控制栏，未显示图形。', 'error');
        }
    } catch (error) {
        setStatus(error.message || '分析失败', 'error');
    } finally {
        ANALYSIS_STATE.loading = false;
        $('analyze-btn').disabled = false;
    }
}

function restoreLastPath() {
    const lastPath = localStorage.getItem('lerobot-analysis-last-path');
    if (lastPath && !$('dataset-path').value.trim()) {
        $('dataset-path').value = lastPath;
    }
}

window.addEventListener('DOMContentLoaded', () => {
    restoreLastPath();
    $('analyze-btn').addEventListener('click', loadAnalysis);
    $('dataset-path').addEventListener('keydown', event => {
        if (event.key === 'Enter') loadAnalysis();
    });
});
