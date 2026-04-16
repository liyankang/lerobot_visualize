/* ═══════════════════════ ROS2 Bag 转换工具 前端 ═══════════════════════ */

const $ = id => document.getElementById(id);
const ARM_TOKENS = new Set(['shoulder', 'elbow', 'wrist']);
const HAND_TOKENS = new Set([
    'hand', 'gripper', 'grip', 'grasp', 'claw',
    'thumb', 'index', 'middle', 'ring', 'pinky',
    'finger', 'prox', 'meta', 'distal',
]);
const HEAD_TOKENS = new Set(['head', 'neck', 'jaw']);
const TORSO_TOKENS = new Set(['torso', 'spine', 'waist', 'hip', 'pelvis', 'chest']);
const LEG_TOKENS = new Set(['knee', 'ankle', 'foot', 'toe', 'thigh', 'shin']);

// ── 全局状态 ──
const R = {
    scanPath: '',
    bags: [],
    topics: [],
    rosVersion: {},
    config: {
        base_topic: '',
        tolerance_sec: 0.01,
        fps: 30,
        rebuild_timestamps: false,
        task: '',
        robot_type: 'unknown',
        selected_topics: [],
        joint_target_names: [],
        convert_workers: 1,
        output_dir: '',
    },
    jointMapState: {
        targetNames: [],
        assignments: {},
    },
    projectDir: '',   // 进度存储目录
    currentStep: 1,
    progressTimer: null,
};

// ── 工具 ──
async function api(url, body) {
    const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
}

function toast(msg, type = 'info') {
    const tb = $('r2-toast');
    const t = document.createElement('div');
    t.className = `r2-tt r2-t${type[0]}`;
    t.textContent = msg;
    tb.appendChild(t);
    setTimeout(() => t.remove(), 4000);
}

function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

function formatDuration(sec) {
    if (sec === null || sec === undefined || !Number.isFinite(sec)) return '--';
    const total = Math.max(0, Math.round(sec));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    if (h > 0) return `${h}h ${m}m ${s}s`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

function formatRate(value, unit = '项') {
    if (!Number.isFinite(value) || value <= 0) return '--';
    return `${value.toFixed(value >= 10 ? 1 : 2)} ${unit}/s`;
}

function localizeUnit(unit) {
    if (unit === 'bag') return '个 bag';
    if (unit === 'step') return '个步骤';
    if (!unit) return '项';
    return unit;
}

function countSelectedCameras() {
    const camBox = $('cam-mapping');
    if (!camBox) return 0;
    return camBox.querySelectorAll('input[type=checkbox]:checked').length;
}

function estimateConvertMemoryGiB({ workers, cameraCount }) {
    const safeWorkers = Math.max(1, parseInt(workers, 10) || 1);
    const cams = Math.max(0, parseInt(cameraCount, 10) || 0);

    // 粗略经验值：每个 worker 至少要承载 1 个 episode 的对齐数据、解码帧和编码缓冲。
    const baseGiB = 0.8;
    const perWorkerLow = 0.7 + cams * 0.9;
    const perWorkerHigh = 1.2 + cams * 1.6;
    return {
        low: +(baseGiB + safeWorkers * perWorkerLow).toFixed(1),
        high: +(baseGiB + safeWorkers * perWorkerHigh).toFixed(1),
    };
}

function updateMemoryEstimate() {
    const el = $('cfg-mem-estimate');
    if (!el) return;
    const workers = parseInt($('cfg-convert-workers')?.value, 10) || 1;
    const cameraCount = countSelectedCameras();
    const estimate = estimateConvertMemoryGiB({ workers, cameraCount });
    el.textContent = `粗略内存预估: 约 ${estimate.low} - ${estimate.high} GiB（${workers} 线程，${cameraCount} 路相机；实际取决于分辨率、帧数和图像编码）`;
    el.style.color = estimate.high >= 12 ? '#c0392b' : estimate.high >= 8 ? '#b26a00' : '#666';
}

function normalizeJointName(name = '') {
    return String(name).trim().toLowerCase().replace(/[\s\-]+/g, '_');
}

function rawJointTokens(name = '', context = '') {
    return normalizeJointName(`${name}_${context}`)
        .replace(/([a-z])([0-9])/g, '$1_$2')
        .replace(/([0-9])([a-z])/g, '$1_$2')
        .split(/[^a-z0-9]+/)
        .filter(Boolean);
}

function inferJointGroup(name = '', context = '') {
    const tokens = new Set(rawJointTokens(name, context));
    let side = '';
    if (tokens.has('left')) side = 'left';
    else if (tokens.has('right')) side = 'right';

    let part = '';
    if ([...tokens].some(token => ARM_TOKENS.has(token))) part = 'arm';
    else if ([...tokens].some(token => HAND_TOKENS.has(token))) part = 'hand';
    else if ([...tokens].some(token => HEAD_TOKENS.has(token))) part = 'head';
    else if ([...tokens].some(token => TORSO_TOKENS.has(token))) part = 'torso';
    else if ([...tokens].some(token => LEG_TOKENS.has(token))) part = 'leg';

    let key = 'other';
    if (side && part) key = `${side}_${part}`;
    else if (part) key = part;
    else if (side) key = side;

    return { side, part, key };
}

function formatJointGroupLabel(groupKey = 'other') {
    return groupKey
        .split('_')
        .filter(Boolean)
        .map(part => part.charAt(0).toUpperCase() + part.slice(1))
        .join(' ') || 'Other';
}

function buildGroupedJointEntries(names = [], context = '') {
    const groups = [];
    const seen = new Map();
    names.forEach((name, index) => {
        const info = inferJointGroup(name, context);
        if (!seen.has(info.key)) {
            const group = {
                key: info.key,
                label: formatJointGroupLabel(info.key),
                items: [],
            };
            seen.set(info.key, group);
            groups.push(group);
        }
        seen.get(info.key).items.push({ name, index, group: info });
    });
    return groups;
}

function tokenizeJointName(name = '') {
    const text = normalizeJointName(name)
        .replace(/([a-z])([0-9])/g, '$1_$2')
        .replace(/([0-9])([a-z])/g, '$1_$2');
    return text
        .split(/[^a-z0-9]+/)
        .filter(Boolean)
        .filter(token => !new Set([
            'joint', 'joints', 'link', 'base', 'world', 'fixed', 'motor', 'actuator',
            'axis', 'frame', 'root', 'arm', 'hand'
        ]).has(token));
}

function jointNameVariants(name = '') {
    const normalized = normalizeJointName(name);
    const tokens = tokenizeJointName(name);
    const compact = normalized.replace(/_/g, '');
    const variants = new Set([normalized, compact, tokens.join('_'), tokens.join('')]);
    if (tokens.length > 1) {
        variants.add(tokens.slice(-2).join('_'));
        variants.add(tokens.slice(-2).join(''));
        variants.add(tokens.at(-1));
    }
    return Array.from(variants).filter(Boolean);
}

function groupsCompatible(sourceGroup, targetGroup) {
    if (sourceGroup.side && targetGroup.side && sourceGroup.side !== targetGroup.side) return false;
    if (sourceGroup.part && targetGroup.part && sourceGroup.part !== targetGroup.part) return false;
    return true;
}

function scoreJointNameMatch(sourceName, targetName, { sourceContext = '', targetContext = '' } = {}) {
    const sourceNorm = normalizeJointName(sourceName);
    const targetNorm = normalizeJointName(targetName);
    if (!sourceNorm || !targetNorm) return 0;

    const sourceGroup = inferJointGroup(sourceName, sourceContext);
    const targetGroup = inferJointGroup(targetName, targetContext);
    if (!groupsCompatible(sourceGroup, targetGroup)) return 0;
    if (sourceNorm === targetNorm) return 1000;

    const sourceVariants = jointNameVariants(sourceName);
    const targetVariants = jointNameVariants(targetName);
    const targetSet = new Set(targetVariants);
    let longestMatchLen = 0;
    for (const variant of sourceVariants) {
        if (targetSet.has(variant) && variant.length > longestMatchLen) {
            longestMatchLen = variant.length;
        }
    }
    if (longestMatchLen > 0) {
        const maxLen = Math.max(...sourceVariants.map(v => v.length), 1);
        return Math.round(500 + 400 * (longestMatchLen / maxLen));
    }

    const sourceTokens = tokenizeJointName(sourceName);
    const targetTokens = tokenizeJointName(targetName);
    if (!sourceTokens.length || !targetTokens.length) return 0;

    const targetTokenSet = new Set(targetTokens);
    const overlap = sourceTokens.filter(token => targetTokenSet.has(token));
    if (!overlap.length) {
        if (targetNorm.endsWith(sourceNorm) || sourceNorm.endsWith(targetNorm)) return 300;
        return 0;
    }

    let score = overlap.length * 120;
    if (sourceTokens.at(-1) === targetTokens.at(-1)) score += 220;
    if (sourceTokens[0] === targetTokens[0]) score += 80;
    if (targetNorm.includes(sourceNorm) || sourceNorm.includes(targetNorm)) score += 60;
    if (sourceGroup.side && targetGroup.side && sourceGroup.side === targetGroup.side) score += 140;
    if (sourceGroup.part && targetGroup.part && sourceGroup.part === targetGroup.part) score += 220;
    return score;
}

function pickFallbackCandidates(sourceEntry, targetEntries) {
    const sourceGroup = sourceEntry.group;
    let candidates = targetEntries.filter(entry => groupsCompatible(sourceGroup, entry.group));
    if (sourceGroup.part) {
        candidates = candidates.filter(entry => entry.group.part === sourceGroup.part);
    }
    if (sourceGroup.side) {
        candidates = candidates.filter(entry => !entry.group.side || entry.group.side === sourceGroup.side);
    }
    if (!candidates.length) return [];

    if (!sourceGroup.part) {
        const distinctParts = new Set(candidates.map(entry => entry.group.part).filter(Boolean));
        if (distinctParts.size > 1) return [];
    }
    return candidates;
}

function buildApproxJointMap(sourceJointNames, targetJointNames, { sourceContext = '', targetContext = '' } = {}) {
    const available = new Set(targetJointNames);
    const mappings = [];
    const sourceEntries = sourceJointNames.map((name, index) => ({
        name,
        index,
        group: inferJointGroup(name, sourceContext),
    }));
    const targetEntries = targetJointNames.map((name, index) => ({
        name,
        index,
        group: inferJointGroup(name, targetContext),
    }));

    for (const { index: sourceIndex, name: sourceName } of sourceEntries) {
        let bestName = null;
        let bestScore = 0;
        for (const targetName of available) {
            const score = scoreJointNameMatch(sourceName, targetName, { sourceContext, targetContext });
            if (score > bestScore) {
                bestScore = score;
                bestName = targetName;
            }
        }
        if (bestName && bestScore >= 300) {
            available.delete(bestName);
            mappings.push({
                sourceIndex,
                sourceName,
                targetName: bestName,
                score: bestScore,
                mode: bestScore >= 900 ? 'name' : 'heuristic',
            });
        }
    }

    const remainingSource = sourceEntries.filter(
        item => !mappings.some(existing => existing.sourceIndex === item.index)
    );
    const remainingTarget = targetEntries.filter(
        item => !mappings.some(existing => existing.targetName === item.name)
    );

    if (!mappings.length) {
        for (const sourceEntry of remainingSource) {
            const candidates = pickFallbackCandidates(sourceEntry, remainingTarget);
            if (!candidates.length) continue;
            const picked = candidates[0];
            mappings.push({
                sourceIndex: sourceEntry.index,
                sourceName: sourceEntry.name,
                targetName: picked.name,
                score: 100,
                mode: 'index',
            });
            const idx = remainingTarget.findIndex(item => item.name === picked.name);
            if (idx >= 0) remainingTarget.splice(idx, 1);
        }
    } else if (remainingSource.length && remainingTarget.length) {
        for (const sourceEntry of remainingSource) {
            const candidates = pickFallbackCandidates(sourceEntry, remainingTarget);
            if (!candidates.length) continue;
            const picked = candidates[0];
            mappings.push({
                sourceIndex: sourceEntry.index,
                sourceName: sourceEntry.name,
                targetName: picked.name,
                score: 50,
                mode: 'index-fallback',
            });
            const idx = remainingTarget.findIndex(item => item.name === picked.name);
            if (idx >= 0) remainingTarget.splice(idx, 1);
        }
    }

    return mappings.sort((a, b) => a.sourceIndex - b.sourceIndex);
}

function getTopicRole(topic) {
    const row = Array.from($('js-mapping')?.querySelectorAll('.map-row') || []).find(item => {
        const topicEl = item.querySelector('code.map-topic');
        return topicEl && topicEl.textContent === topic;
    });
    if (!row) return '';
    const stateChecked = row.querySelector('[data-role="state"]')?.checked;
    const actionChecked = row.querySelector('[data-role="action"]')?.checked;
    if (stateChecked && actionChecked) return 'state+action';
    if (stateChecked) return 'state';
    if (actionChecked) return 'action';
    return '';
}

function getJointSignature(topicInfo) {
    return getTopicJointNames(topicInfo)
        .map(name => {
            const info = inferJointGroup(name, topicInfo.topic);
            return `${normalizeJointName(name)}@${info.key}`;
        })
        .join('|');
}

function rolesOverlap(roleA, roleB) {
    if (!roleA || !roleB) return false;
    const hasState = role => role.includes('state');
    const hasAction = role => role.includes('action');
    return (hasState(roleA) && hasState(roleB)) || (hasAction(roleA) && hasAction(roleB));
}

function findLinkedTopicInfos(topicInfo) {
    const role = getTopicRole(topicInfo.topic);
    const signature = getJointSignature(topicInfo);
    if (!role || !signature) return [];

    return getJointTopics().filter(other => {
        if (other.topic === topicInfo.topic) return false;
        if (getJointSignature(other) !== signature) return false;
        const otherRole = getTopicRole(other.topic);
        if (!otherRole) return false;
        return !rolesOverlap(role, otherRole);
    });
}

function setTopicAssignment(topicInfo, sourceIndex, targetName, { propagate = true } = {}) {
    ensureTopicJointAssignments(topicInfo)[sourceIndex] = targetName;
    if (!propagate) return;

    for (const linkedTopic of findLinkedTopicInfos(topicInfo)) {
        ensureTopicJointAssignments(linkedTopic)[sourceIndex] = targetName;
    }
}

function parseJointTargetNames(text) {
    const seen = new Set();
    const names = [];
    for (const raw of (text || '').split(/[\n,]+/)) {
        const name = raw.trim();
        if (!name || seen.has(name)) continue;
        seen.add(name);
        names.push(name);
    }
    return names;
}

function getJointTopics() {
    return R.topics.filter(t => t.category === 'joint_state');
}

function getTopicJointNames(topicInfo) {
    const names = Array.isArray(topicInfo.joint_names) ? topicInfo.joint_names : [];
    if (names.length) return names;
    const count = Number.isFinite(topicInfo.joint_count) ? topicInfo.joint_count : 0;
    return Array.from({ length: Math.max(0, count) }, (_, idx) => `joint_${idx}`);
}

function ensureTopicJointAssignments(topicInfo) {
    const topic = topicInfo.topic;
    if (!R.jointMapState.assignments[topic]) {
        R.jointMapState.assignments[topic] = {};
    }
    const assignments = R.jointMapState.assignments[topic];
    const jointNames = getTopicJointNames(topicInfo);
    for (let idx = 0; idx < jointNames.length; idx += 1) {
        if (!(idx in assignments)) {
            assignments[idx] = '';
        }
    }
    return assignments;
}

function fillSameNameJointMappings(targetNames, { showToast = false, overwrite = true } = {}) {
    R.jointMapState.targetNames = targetNames;

    if (!targetNames.length) {
        if (showToast) toast('请先填写目标 LeRobot 关节名列表', 'error');
        return false;
    }

    for (const topicInfo of getJointTopics()) {
        const assignments = ensureTopicJointAssignments(topicInfo);
        const jointNames = getTopicJointNames(topicInfo);
        const autoMap = buildApproxJointMap(jointNames, targetNames, {
            sourceContext: topicInfo.topic,
        });
        const autoMapByIndex = new Map(autoMap.map(item => [item.sourceIndex, item.targetName]));
        for (let idx = 0; idx < jointNames.length; idx += 1) {
            if (!overwrite && assignments[idx]) continue;
            setTopicAssignment(topicInfo, idx, autoMapByIndex.get(idx) || '', { propagate: true });
        }
    }

    if (showToast) toast('已按同名/近似名称规则自动填充映射', 'success');
    return true;
}

function autoMatchJointMappings() {
    const targetNames = parseJointTargetNames($('cfg-joint-targets').value);
    if (!fillSameNameJointMappings(targetNames, { showToast: true, overwrite: true })) {
        return;
    }
    renderJointMappingEditor();
}

function renderJointMappingEditor() {
    const panel = $('joint-map-panel');
    const summaryEl = $('joint-map-summary');
    const jointTopics = getJointTopics();
    const targetNames = parseJointTargetNames($('cfg-joint-targets').value);
    R.jointMapState.targetNames = targetNames;
    const hasAnyExplicitAssignment = Object.values(R.jointMapState.assignments).some(topicAssignments =>
        Object.values(topicAssignments || {}).some(Boolean)
    );
    if (targetNames.length && !hasAnyExplicitAssignment) {
        fillSameNameJointMappings(targetNames, { overwrite: true });
    }

    if (!jointTopics.length) {
        summaryEl.textContent = '当前没有 JointState topic，无需配置关节映射。';
        panel.innerHTML = '<div class="r2-empty">未发现可映射的 JointState topic</div>';
        return;
    }

    let mappedCount = 0;
    let totalCount = 0;
    const targetGroups = buildGroupedJointEntries(targetNames);

    panel.innerHTML = jointTopics.map(topicInfo => {
        const assignments = ensureTopicJointAssignments(topicInfo);
        const jointNames = getTopicJointNames(topicInfo);
        const linkedTopics = findLinkedTopicInfos(topicInfo);
        const sourceGroups = buildGroupedJointEntries(jointNames, topicInfo.topic);
        totalCount += jointNames.length;
        jointNames.forEach((_, idx) => {
            if (assignments[idx]) mappedCount += 1;
        });

        const groupedTables = sourceGroups.map(group => {
            const rows = group.items.map(({ name: sourceName, index: idx }) => {
                const currentTarget = assignments[idx] || '';
                if (!targetNames.length) {
                    return `
                    <tr>
                        <td class="num">${idx}</td>
                        <td><code>${escHtml(sourceName)}</code></td>
                        <td><span class="joint-map-hint">未填写目标列表时，默认沿用源名称与顺序</span></td>
                    </tr>`;
                }

                const sourceGroup = inferJointGroup(sourceName, topicInfo.topic);
                return `
                <tr>
                    <td class="num">${idx}</td>
                    <td><code>${escHtml(sourceName)}</code></td>
                    <td>
                        <select class="joint-map-select" data-topic="${escHtml(topicInfo.topic)}" data-source-index="${idx}">
                            <option value="">跳过此关节</option>
                            ${targetGroups.map(targetGroup => `
                                <optgroup label="${escHtml(targetGroup.label)}">
                                    ${targetGroup.items.map(({ name: targetName }) => {
                                        const targetInfo = inferJointGroup(targetName);
                                        const sameSide = !sourceGroup.side || !targetInfo.side || sourceGroup.side === targetInfo.side;
                                        const samePart = !sourceGroup.part || !targetInfo.part || sourceGroup.part === targetInfo.part;
                                        const marker = sameSide && samePart ? '' : ' [跨组]';
                                        return `
                                            <option value="${escHtml(targetName)}" ${currentTarget === targetName ? 'selected' : ''}>
                                                ${escHtml(targetName + marker)}
                                            </option>`;
                                    }).join('')}
                                </optgroup>
                            `).join('')}
                        </select>
                    </td>
                </tr>`;
            }).join('');

            return `
            <div class="joint-map-subgroup">
                <div class="joint-map-subgroup-hd">${escHtml(group.label)} · ${group.items.length}</div>
                <table class="joint-map-table">
                    <thead>
                        <tr><th class="num">源序号</th><th>ROS2 关节名</th><th>LeRobot 目标关节名</th></tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>`;
        }).join('');

        const mappedTopicCount = jointNames.filter((_, idx) => assignments[idx]).length;
        return `
        <div class="joint-map-card">
            <div class="joint-map-card-hd">
                <code>${escHtml(topicInfo.topic)}</code>
                <span class="joint-map-badge">已映射 ${mappedTopicCount}/${jointNames.length}${linkedTopics.length ? ` · 联动 ${linkedTopics.length}` : ''}</span>
            </div>
            ${jointNames.length ? `
                ${groupedTables}
            ` : '<div class="joint-map-empty">此 topic 未读取到 JointState.name，将按原顺序直接输出。</div>'}
        </div>`;
    }).join('');

    if (!targetNames.length) {
        summaryEl.textContent = `已发现 ${jointTopics.length} 个 JointState topic。未填写目标关节名时，转换会直接沿用 ROS2 原始关节名和顺序。`;
    } else {
        const groupedTargetCount = buildGroupedJointEntries(targetNames).length;
        summaryEl.textContent = `目标关节名 ${targetNames.length} 个，已自动分成 ${groupedTargetCount} 组；当前已显式映射 ${mappedCount}/${totalCount} 个源关节。系统会结合关节名和 topic 上下文推断组别，优先在同组内做同名/近似匹配；拿不准时会留空，不再跨组硬配。`;
    }

    panel.querySelectorAll('.joint-map-select').forEach(select => {
        select.addEventListener('change', e => {
            const topic = e.target.dataset.topic;
            const sourceIndex = parseInt(e.target.dataset.sourceIndex, 10);
            const topicInfo = getJointTopics().find(item => item.topic === topic) || { topic, joint_names: [] };
            setTopicAssignment(topicInfo, sourceIndex, e.target.value, { propagate: true });
            renderJointMappingEditor();
        });
    });
}

function collectJointMappings(topicInfo, targetNames) {
    const jointNames = getTopicJointNames(topicInfo);
    const assignments = ensureTopicJointAssignments(topicInfo);

    if (!targetNames.length) {
        return jointNames.map((sourceName, idx) => ({
            source_index: idx,
            source_name: sourceName,
            target_index: null,
            target_name: sourceName,
        }));
    }

    const mappings = [];
    for (let idx = 0; idx < jointNames.length; idx += 1) {
        const targetName = (assignments[idx] || '').trim();
        if (!targetName) continue;
        const targetIndex = targetNames.indexOf(targetName);
        if (targetIndex < 0) continue;
        mappings.push({
            source_index: idx,
            source_name: jointNames[idx],
            target_index: targetIndex,
            target_name: targetName,
        });
    }
    return mappings;
}

function validateJointMappings(config) {
    const targetNames = config.joint_target_names || [];

    for (const role of ['state', 'action']) {
        const usedTargets = new Map();
        let mappedForRole = 0;

        for (const topic of config.selected_topics) {
            if (topic.category !== 'joint_state' || !topic.role.includes(role)) continue;
            const mappings = Array.isArray(topic.joint_mapping) ? topic.joint_mapping : [];
            mappedForRole += mappings.length;

            if (!targetNames.length) continue;
            for (const item of mappings) {
                const key = Number.isInteger(item.target_index) ? item.target_index : -1;
                if (key < 0) continue;
                if (usedTargets.has(key)) {
                    return `${role.toUpperCase()} 映射中存在重复目标位: ${item.target_name}（${topic.topic} 与 ${usedTargets.get(key)}）`;
                }
                usedTargets.set(key, topic.topic);
            }
        }

        const hasRole = config.selected_topics.some(
            topic => topic.category === 'joint_state' && topic.role.includes(role)
        );
        if (hasRole && mappedForRole === 0) {
            return `${role.toUpperCase()} 已勾选 JointState topic，但没有任何关节被映射`;
        }
    }

    return '';
}

// ═══════════════════════ Step Navigation ═══════════════════════

function goToStep(step) {
    R.currentStep = step;
    document.querySelectorAll('.step-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.step-indicator .step').forEach(s => {
        const sn = parseInt(s.dataset.step);
        s.classList.toggle('active', sn === step);
        s.classList.toggle('done', sn < step);
    });
    const panel = $(`step-${step}`);
    if (panel) panel.classList.add('active');
}

// ═══════════════════════ Step 1: 扫描 ═══════════════════════

async function doScan() {
    const path = $('r2-scan-path').value.trim();
    if (!path) return toast('请输入或选择 bag 目录', 'error');

    R.scanPath = path;
    // 项目目录 = 临时目录 / hash
    R.projectDir = path + '/.ros2_convert_project';

    toast('正在扫描...', 'info');
    try {
        const data = await api('/api/ros2/scan', { path });
        R.bags = data.bags;
        renderBagList();
        toast(`发现 ${data.count} 个 bag`, 'success');
    } catch (e) {
        toast(e.message, 'error');
    }
}

function renderBagList() {
    const el = $('bag-list');
    if (!R.bags.length) {
        el.innerHTML = '<div class="r2-empty">未发现 ROS2 bag 文件</div>';
        return;
    }
    el.innerHTML = R.bags.map((b, i) => `
        <div class="bag-item">
            <input type="checkbox" checked data-idx="${i}">
            <span class="bag-name">${escHtml(b.name)}</span>
            <span class="bag-info">${b.storage_format} · ${b.size_mb} MB</span>
        </div>
    `).join('');

    $('btn-step1-next').style.display = 'inline-block';
}

async function step1Next() {
    // 选中的 bags
    const checks = $('bag-list').querySelectorAll('input[type=checkbox]:checked');
    if (!checks.length) return toast('请至少选择一个 bag', 'error');

    const selectedBags = Array.from(checks).map(c => R.bags[parseInt(c.dataset.idx)]);

    // 用第一个 bag 发现 topics
    toast('正在分析 topic...', 'info');
    try {
        const data = await api('/api/ros2/topics', { bag_path: selectedBags[0].path });
        R.topics = data.topics;
        R.rosVersion = data.ros_version;
        R.jointMapState = { targetNames: [], assignments: {} };
        R.config.base_topic = data.recommended_base;

        // 推断默认 FPS
        const baseTopic = R.topics.find(t => t.topic === data.recommended_base);
        if (baseTopic) {
            R.config.fps = Math.round(baseTopic.frequency_hz) || 30;
        }

        renderTopicList();
        renderVersionInfo();
        goToStep(2);
        toast(`发现 ${data.topics.length} 个 topic`, 'success');
    } catch (e) {
        toast(e.message, 'error');
    }
}

// ═══════════════════════ Step 2: Topic 查看 ═══════════════════════

function renderVersionInfo() {
    const el = $('ros-version-info');
    const v = R.rosVersion;
    el.innerHTML = `
        <span class="ver-badge">${escHtml(v.ros_distro || 'unknown')}</span>
        <span>storage: ${escHtml(v.storage_id || '?')}</span>
        <span>version: ${v.version || '?'}</span>
    `;
}

function renderTopicList() {
    const el = $('topic-list');
    el.innerHTML = R.topics.map((t, i) => {
        const catClass = `cat-${t.category}`;
        const catLabel = { camera: '相机', joint_state: '关节', other: '其他' }[t.category] || '?';
        return `
        <tr class="${catClass}" data-idx="${i}">
            <td><code>${escHtml(t.topic)}</code></td>
            <td><code class="msg-type">${escHtml(t.msg_type)}</code></td>
            <td class="num">${t.frequency_hz} Hz</td>
            <td><span class="cat-tag ${catClass}">${catLabel}</span></td>
            <td class="num">${t.msg_count}</td>
        </tr>`;
    }).join('');
}

function step2Next() {
    renderMappingConfig();
    goToStep(3);
}

// ═══════════════════════ Step 3: 配置映射 ═══════════════════════

function renderMappingConfig() {
    // Camera topics
    const camEl = $('cam-mapping');
    const camTopics = R.topics.filter(t => t.category === 'camera');
    camEl.innerHTML = camTopics.length ? camTopics.map(t => `
        <div class="map-row">
            <label class="map-check">
                <input type="checkbox" checked data-topic="${escHtml(t.topic)}" data-cat="camera">
                <code>${escHtml(t.topic)}</code>
            </label>
            <span class="map-arrow">&rarr;</span>
            <input class="map-name" type="text" value="${escHtml(t.suggested_name)}"
                   data-topic="${escHtml(t.topic)}">
            <span class="map-freq">${t.frequency_hz} Hz</span>
        </div>
    `).join('') : '<div class="r2-empty">未发现相机 topic</div>';

    // JointState topics
    const jsEl = $('js-mapping');
    const jsTopics = R.topics.filter(t => t.category === 'joint_state');
    jsEl.innerHTML = jsTopics.length ? jsTopics.map(t => {
        const isState = t.default_role.includes('state');
        const isAction = t.default_role.includes('action');
        const jointNames = getTopicJointNames(t);
        return `
        <div class="map-row">
            <code class="map-topic">${escHtml(t.topic)}</code>
            <label class="map-cb"><input type="checkbox" ${isState ? 'checked' : ''}
                data-topic="${escHtml(t.topic)}" data-role="state"> State</label>
            <label class="map-cb"><input type="checkbox" ${isAction ? 'checked' : ''}
                data-topic="${escHtml(t.topic)}" data-role="action"> Action</label>
            <span class="map-meta">${jointNames.length} joints</span>
            <span class="map-freq">${t.frequency_hz} Hz</span>
        </div>`;
    }).join('') : '<div class="r2-empty">未发现 JointState topic</div>';

    // Other topics
    const otherEl = $('other-mapping');
    const otherTopics = R.topics.filter(t => t.category === 'other');
    otherEl.innerHTML = otherTopics.length ? otherTopics.map(t => `
        <div class="map-row">
            <label class="map-check">
                <input type="checkbox" data-topic="${escHtml(t.topic)}" data-cat="other">
                <code>${escHtml(t.topic)}</code>
            </label>
            <span class="map-freq">${t.frequency_hz} Hz · ${escHtml(t.msg_type)}</span>
        </div>
    `).join('') : '<div class="r2-empty">无其他 topic</div>';

    // Base topic 下拉
    const baseEl = $('base-topic-select');
    baseEl.innerHTML = camTopics.map(t =>
        `<option value="${escHtml(t.topic)}" ${t.topic === R.config.base_topic ? 'selected' : ''}>
            ${escHtml(t.topic)} (${t.frequency_hz} Hz)
        </option>`
    ).join('');
    baseEl.addEventListener('change', () => {
        R.config.base_topic = baseEl.value;
        const bt = R.topics.find(t => t.topic === baseEl.value);
        if (bt && bt.frequency_hz < 30) {
            $('base-warning').textContent = `注意: 选择的 base topic 频率仅 ${bt.frequency_hz} Hz，低于 30 Hz`;
            $('base-warning').style.display = 'block';
        } else {
            $('base-warning').style.display = 'none';
        }
    });

    // FPS / tolerance
    $('cfg-fps').value = R.config.fps;
    $('cfg-rebuild-ts').checked = !!R.config.rebuild_timestamps;
    $('cfg-tolerance').value = R.config.tolerance_sec;
    $('cfg-convert-workers').value = R.config.convert_workers || 1;
    $('cfg-task').value = R.config.task;
    $('cfg-joint-targets').value = (R.config.joint_target_names || []).join('\n');
    renderJointMappingEditor();
    updateMemoryEstimate();

    $('cam-mapping').querySelectorAll('input[type=checkbox]').forEach(cb => {
        cb.addEventListener('change', updateMemoryEstimate);
    });
    $('cfg-convert-workers').addEventListener('input', updateMemoryEstimate);
}

function collectConfig() {
    const selected = [];
    const targetNames = parseJointTargetNames($('cfg-joint-targets').value);

    // Cameras
    $('cam-mapping').querySelectorAll('input[type=checkbox]').forEach(cb => {
        if (!cb.checked) return;
        const topic = cb.dataset.topic;
        const nameInput = $('cam-mapping').querySelector(`input.map-name[data-topic="${topic}"]`);
        const t = R.topics.find(x => x.topic === topic);
        selected.push({
            topic,
            name: nameInput ? nameInput.value.trim() : t.suggested_name,
            role: 'camera',
            category: 'camera',
            msg_type: t.msg_type,
        });
    });

    // JointState
    $('js-mapping').querySelectorAll('.map-row').forEach(row => {
        const topicEl = row.querySelector('code.map-topic');
        if (!topicEl) return;
        const topic = topicEl.textContent;
        const stateChecked = row.querySelector('[data-role="state"]')?.checked;
        const actionChecked = row.querySelector('[data-role="action"]')?.checked;
        if (!stateChecked && !actionChecked) return;

        let role = '';
        if (stateChecked && actionChecked) role = 'state+action';
        else if (stateChecked) role = 'state';
        else role = 'action';

        const t = R.topics.find(x => x.topic === topic);
        selected.push({
            topic,
            name: t ? t.suggested_name : topic,
            role,
            category: 'joint_state',
            msg_type: t ? t.msg_type : '',
            joint_names: t ? getTopicJointNames(t) : [],
            joint_count: t ? getTopicJointNames(t).length : 0,
            joint_mapping: t ? collectJointMappings(t, targetNames) : [],
        });
    });

    R.config.selected_topics = selected;
    R.config.joint_target_names = targetNames;
    R.config.base_topic = $('base-topic-select').value;
    R.config.fps = parseInt($('cfg-fps').value) || 30;
    R.config.rebuild_timestamps = $('cfg-rebuild-ts').checked;
    R.config.tolerance_sec = parseFloat($('cfg-tolerance').value) || 0.01;
    R.config.convert_workers = Math.max(1, parseInt($('cfg-convert-workers').value, 10) || 1);
    R.config.task = $('cfg-task').value.trim();
    R.config.output_dir = $('cfg-output').value.trim();

    return R.config;
}

async function step3Next() {
    const config = collectConfig();

    if (!config.selected_topics.length) return toast('请至少选择一个 topic', 'error');
    if (!config.base_topic) return toast('请选择 base topic', 'error');
    if (!config.fps || config.fps <= 0) return toast('目标 FPS 必须大于 0', 'error');
    if (!config.convert_workers || config.convert_workers <= 0) return toast('转换线程数必须大于 0', 'error');
    if (!config.output_dir) return toast('请指定输出目录', 'error');
    const jointMapError = validateJointMappings(config);
    if (jointMapError) return toast(jointMapError, 'error');

    // 保存配置
    try {
        await api('/api/ros2/save_config', {
            project_dir: R.projectDir,
            config,
        });
        toast('配置已保存', 'success');
    } catch (e) {
        toast(e.message, 'error');
    }

    goToStep(4);
    renderAlignPanel();
}

// ═══════════════════════ Step 4: 对齐 ═══════════════════════

function renderAlignPanel() {
    const el = $('align-status');
    const checks = $('bag-list').querySelectorAll('input[type=checkbox]:checked');
    const count = checks.length;
    el.innerHTML = `
        <p>将对 <strong>${count}</strong> 个 bag 执行时间戳对齐</p>
        <p>Base topic: <code>${escHtml(R.config.base_topic)}</code></p>
        <p>目标输出频率: <strong>${R.config.fps} Hz</strong></p>
        <p>时间轴模式: ${R.config.rebuild_timestamps ? '按目标 FPS 重建统一时间轴' : '沿用 Base topic 原始时间戳'}</p>
        <p>容差: ${R.config.tolerance_sec * 1000} ms</p>
    `;
}

async function startAlign() {
    const checks = $('bag-list').querySelectorAll('input[type=checkbox]:checked');
    const bags = Array.from(checks).map(c => R.bags[parseInt(c.dataset.idx)]);

    $('btn-start-align').disabled = true;
    toast('对齐任务已启动', 'info');

    try {
        await api('/api/ros2/align', {
            project_dir: R.projectDir,
            bags,
            config: R.config,
        });
        startProgressPolling('align', () => {
            $('btn-start-align').disabled = false;
            renderAlignResults();
            $('btn-step4-next').style.display = 'inline-block';
        });
    } catch (e) {
        toast(e.message, 'error');
        $('btn-start-align').disabled = false;
    }
}

function renderAlignResults() {
    const el = $('align-results');
    const res = R._lastProgress;
    if (!res) return;

    const results = res.results || [];
    const errors = res.errors || [];

    let html = `<h4>对齐完成: ${results.length} 个 episode</h4>`;

    if (results.length) {
        const maxDelta = Math.max(...results.map(r => r.max_delta_ms));
        const mode = results[0].timeline_mode === 'uniform_fps' ? `按 ${results[0].target_fps} Hz 重建时间轴` : '沿用 Base topic 时间轴';
        html += `<p>时间轴模式: <strong>${mode}</strong></p>`;
        html += `<p>全局最大时间差: <strong>${maxDelta.toFixed(3)} ms</strong></p>`;
        html += `<table class="r2-table"><tr><th>Episode</th><th>帧数</th><th>最大 delta (ms)</th><th>警告</th></tr>`;
        for (const r of results) {
            const warn = r.warnings?.length ? `<span class="warn-count">${r.warnings.length}</span>` : '-';
            const deltaClass = r.max_delta_ms > R.config.tolerance_sec * 1000 ? ' class="over-tol"' : '';
            html += `<tr><td>${r.episode_idx}</td><td>${r.frame_count}</td><td${deltaClass}>${r.max_delta_ms.toFixed(3)}</td><td>${warn}</td></tr>`;
        }
        html += '</table>';
    }
    if (errors.length) {
        html += `<h4 class="err-hdr">错误 (${errors.length})</h4>`;
        for (const e of errors) {
            html += `<div class="err-item">Episode ${e.episode}: ${escHtml(e.error)}</div>`;
        }
    }
    el.innerHTML = html;
}

function step4Next() {
    goToStep(5);
    renderConvertPanel();
}

// ═══════════════════════ Step 5: 转换 ═══════════════════════

function renderConvertPanel() {
    const el = $('convert-status');
    const estimate = estimateConvertMemoryGiB({
        workers: R.config.convert_workers || 1,
        cameraCount: R.config.selected_topics.filter(t => t.category === 'camera').length,
    });
    el.innerHTML = `
        <p>输出目录: <code>${escHtml(R.config.output_dir)}</code></p>
        <p>目标输出频率: ${R.config.fps} Hz</p>
        <p>转换线程数: ${R.config.convert_workers || 1}</p>
        <p>时间轴模式: ${R.config.rebuild_timestamps ? '按目标 FPS 重建' : '沿用原始时间戳'}</p>
        <p>粗略内存预估: ${estimate.low} - ${estimate.high} GiB</p>
        <p>任务描述: ${escHtml(R.config.task || '(无)')}</p>
    `;
}

async function startConvert() {
    $('btn-start-convert').disabled = true;
    $('convert-done').style.display = 'none';
    $('convert-results').innerHTML = '';
    toast('转换任务已启动', 'info');

    try {
        await api('/api/ros2/convert', {
            project_dir: R.projectDir,
            output_dir: R.config.output_dir,
            config: R.config,
        });
        startProgressPolling('convert', () => {
            $('btn-start-convert').disabled = false;
            renderConvertResults();
            const hasErrors = (R._lastProgress?.errors || []).length > 0;
            if (!hasErrors) {
                $('convert-done').style.display = 'block';
                toast('转换完成!', 'success');
            } else {
                toast('转换结束，但存在错误', 'error');
            }
        });
    } catch (e) {
        toast(e.message, 'error');
        $('btn-start-convert').disabled = false;
    }
}

function renderConvertResults() {
    const el = $('convert-results');
    const res = R._lastProgress;
    if (!res) return;

    const errors = res.errors || [];
    let html = '';
    if (errors.length) {
        html += `<h4 class="err-hdr">转换错误 (${errors.length})</h4>`;
        for (const e of errors) {
            const label = e.episode !== undefined ? `Episode ${e.episode}` : (e.step || '步骤');
            html += `<div class="err-item">${escHtml(label)}: ${escHtml(e.error || '未知错误')}</div>`;
        }
    }
    el.innerHTML = html;
}

// ═══════════════════════ 进度轮询 ═══════════════════════

function startProgressPolling(step, onDone) {
    if (R.progressTimer) clearInterval(R.progressTimer);
    const bar = $('progress-bar');
    const detail = $('progress-detail');
    const count = $('progress-count');
    const elapsed = $('progress-elapsed');
    const eta = $('progress-eta');
    const rate = $('progress-rate');
    $('progress-panel').style.display = 'block';

    bar.style.width = '0%';
    bar.textContent = '0%';
    detail.textContent = step === 'align' ? '正在等待对齐任务开始...' : '正在等待转换任务开始...';
    count.textContent = '进度: 0 / 0';
    elapsed.textContent = '已耗时: --';
    eta.textContent = '预计剩余: --';
    rate.textContent = '速度: --';

    R.progressTimer = setInterval(async () => {
        try {
            const resp = await fetch('/api/ros2/progress');
            const p = await resp.json();
            R._lastProgress = p;
            const unitLabel = localizeUnit(p.unit);

            bar.style.width = `${p.percent || 0}%`;
            bar.textContent = `${p.percent || 0}%`;
            detail.textContent = p.detail || '';
            count.textContent = `进度: ${p.current || 0} / ${p.total || 0} ${unitLabel}`;
            elapsed.textContent = `已耗时: ${formatDuration(p.elapsed_sec)}`;
            eta.textContent = `预计剩余: ${formatDuration(p.eta_sec)}`;
            rate.textContent = `速度: ${formatRate(p.rate_per_sec, unitLabel)}`;

            if (!p.running) {
                clearInterval(R.progressTimer);
                R.progressTimer = null;
                if (p.elapsed_sec !== null && p.elapsed_sec !== undefined) {
                    eta.textContent = '预计剩余: 0s';
                }
                if (onDone) onDone();
            }
        } catch (_) {}
    }, 800);
}

// ═══════════════════════ 目录浏览器 (复用) ═══════════════════════

async function openDirBrowser(targetInputId) {
    const inputEl = $(targetInputId);
    let currentPath = inputEl.value.trim() || '';

    async function fetchDirs(path) {
        const params = path ? `?path=${encodeURIComponent(path)}` : '';
        const resp = await fetch(`/api/browse${params}`);
        return resp.json();
    }

    async function render(path) {
        const data = await fetchDirs(path);
        if (data.error) {
            toast(data.error, 'error');
            if (path) return render('');
            return;
        }
        currentPath = data.current || '';
        pathInput.value = currentPath;

        listEl.innerHTML = '';
        if (!data.dirs.length) {
            listEl.innerHTML = '<div class="dir-empty">此目录下无子目录</div>';
            return;
        }
        for (const d of data.dirs) {
            const item = document.createElement('div');
            item.className = 'dir-item';
            item.innerHTML = `<span class="dir-item-icon">&#128194;</span><span class="dir-item-name">${escHtml(d.name)}</span>`;
            item.addEventListener('click', () => render(d.path));
            listEl.appendChild(item);
        }
    }

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';

    const card = document.createElement('div');
    card.className = 'modal-card';
    card.style.width = '600px';

    const hdr = document.createElement('div');
    hdr.className = 'modal-hdr';
    hdr.innerHTML = '<h3 style="color:#333;">选择目录</h3>';
    const closeBtn = document.createElement('button');
    closeBtn.className = 'modal-close';
    closeBtn.textContent = '\u00d7';
    closeBtn.addEventListener('click', () => overlay.remove());
    hdr.appendChild(closeBtn);

    const body = document.createElement('div');
    body.className = 'modal-body';

    const pathRow = document.createElement('div');
    pathRow.className = 'dir-browser-path';

    const upBtn = document.createElement('button');
    upBtn.textContent = '\u2191 上级';
    upBtn.addEventListener('click', async () => {
        const data = await fetchDirs(currentPath);
        if (data.parent !== undefined && data.parent !== currentPath) render(data.parent);
    });

    const pathInput = document.createElement('input');
    pathInput.value = currentPath;
    pathInput.placeholder = '输入路径后按回车跳转...';
    pathInput.addEventListener('keydown', e => {
        if (e.key === 'Enter') render(pathInput.value.trim());
    });

    pathRow.appendChild(upBtn);
    pathRow.appendChild(pathInput);
    body.appendChild(pathRow);

    const listEl = document.createElement('div');
    listEl.className = 'dir-list';
    body.appendChild(listEl);

    const footer = document.createElement('div');
    footer.className = 'modal-footer';

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'r2-btn secondary';
    cancelBtn.textContent = '取消';
    cancelBtn.addEventListener('click', () => overlay.remove());

    const confirmBtn = document.createElement('button');
    confirmBtn.className = 'r2-btn primary';
    confirmBtn.textContent = '选择此目录';
    confirmBtn.addEventListener('click', () => { inputEl.value = currentPath; overlay.remove(); });

    footer.appendChild(cancelBtn);
    footer.appendChild(confirmBtn);

    card.appendChild(hdr);
    card.appendChild(body);
    card.appendChild(footer);
    overlay.appendChild(card);
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
    document.body.appendChild(overlay);
    render(currentPath);
}

// ═══════════════════════ 初始化 ═══════════════════════

document.addEventListener('DOMContentLoaded', () => {
    $('btn-scan').addEventListener('click', doScan);
    $('btn-browse-scan').addEventListener('click', () => openDirBrowser('r2-scan-path'));
    $('btn-step1-next').addEventListener('click', step1Next);
    $('btn-step2-next').addEventListener('click', step2Next);
    $('btn-auto-joint-map').addEventListener('click', autoMatchJointMappings);
    $('btn-refresh-joint-map').addEventListener('click', renderJointMappingEditor);
    $('cfg-joint-targets').addEventListener('input', () => {
        R.jointMapState.targetNames = parseJointTargetNames($('cfg-joint-targets').value);
        renderJointMappingEditor();
    });
    $('btn-step3-next').addEventListener('click', step3Next);
    $('btn-start-align').addEventListener('click', startAlign);
    $('btn-step4-next').addEventListener('click', step4Next);
    $('btn-start-convert').addEventListener('click', startConvert);
    $('btn-browse-output').addEventListener('click', () => openDirBrowser('cfg-output'));

    $('r2-scan-path').addEventListener('keydown', e => { if (e.key === 'Enter') doScan(); });

    goToStep(1);
});
