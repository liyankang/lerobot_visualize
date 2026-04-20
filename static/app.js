// ═══════════════════════ LeRobot 数据集编辑器 ═══════════════════════

let THREE = null;
let OrbitControls = null;
let STLLoader = null;
let ColladaLoader = null;
let OBJLoader = null;
let MTLLoader = null;
let GLTFLoader = null;
let URDFLoader = null;
let urdfRuntimePromise = null;

const S = {
    dataset: null, episodes: [], curEp: null, curEpData: null,
    selEpisodes: new Set(), selFrames: new Set(),
    jointNames: [], jointGroups: {}, activeJoints: new Set(),
    mergeDatasets: [],
    chart: null,            // 合并后的单一图表
    chartMode: 'both',      // 'both' | 'state' | 'action'
    videoPlaying: false, videoFrame: 0, videoTotalFrames: 0, videoTimer: null, videoRaf: null,
    integrity: null,        // 一致性检查结果: null=未执行, {}=结果
    integrityChecking: false,
    chartClickState: 0,     // 0=空闲, 1=等待结束帧
    bridgePreview: [],      // 桥接帧预览 (分析模态框打开时高亮)
    uiLocked: false,        // 保存期间锁住页面交互
    saveProgressTimer: null,
    urdf: {
        packageId: null,
        robotName: '',
        rootUrl: '',
        rootDir: '',
        assetRoot: '',
        jointNames: [],
        jointMap: [],
        jointInfo: {},
        mappingMode: 'none',
        mappingSummary: '',
        source: 'state',
        degreeMode: 'auto',
        _detectedDegree: null,
        scene: null,
        camera: null,
        renderer: null,
        controls: null,
        light: null,
        grid: null,
        robot: null,
        resizeObserver: null,
        animating: false,
        loadError: '',
    },
};

const COLORS = [
    '#e6194b','#3cb44b','#4363d8','#f58231','#911eb4',
    '#42d4f4','#f032e6','#bfef45','#fabed4','#469990',
    '#dcbeff','#9A6324','#fffac8','#800000','#aaffc3',
    '#808000','#ffd8b1','#000075','#a9a9a9','#ff6961',
    '#77dd77','#6b5b95','#feb236','#d64161','#ff7b25','#00bcd4',
];

// ═══════════════════════ API ═══════════════════════

async function api(u, o={}) {
    try { const r=await fetch(u,o); const d=await r.json(); if(!r.ok) throw new Error(d.error||`HTTP ${r.status}`); return d; }
    catch(e) { toast(e.message,'error'); throw e; }
}
const post = (u,b) => api(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});

async function ensureUrdfRuntime() {
    if (THREE && OrbitControls && STLLoader && ColladaLoader && OBJLoader && MTLLoader && GLTFLoader && URDFLoader) {
        return;
    }
    if (!urdfRuntimePromise) {
        const esmBase = 'https://esm.sh';
        const threeBase = `${esmBase}/three@0.160.1`;
        urdfRuntimePromise = Promise.all([
            import(threeBase),
            import(`${threeBase}/examples/jsm/controls/OrbitControls.js`),
            import(`${threeBase}/examples/jsm/loaders/STLLoader.js`),
            import(`${threeBase}/examples/jsm/loaders/ColladaLoader.js`),
            import(`${threeBase}/examples/jsm/loaders/OBJLoader.js`),
            import(`${threeBase}/examples/jsm/loaders/MTLLoader.js`),
            import(`${threeBase}/examples/jsm/loaders/GLTFLoader.js`),
            import(`${esmBase}/urdf-loader@0.12.4?deps=three@0.160.1`),
        ]).then(([
            threeMod,
            orbitMod,
            stlMod,
            colladaMod,
            objMod,
            mtlMod,
            gltfMod,
            urdfMod,
        ]) => {
            THREE = threeMod;
            OrbitControls = orbitMod.OrbitControls;
            STLLoader = stlMod.STLLoader;
            ColladaLoader = colladaMod.ColladaLoader;
            OBJLoader = objMod.OBJLoader;
            MTLLoader = mtlMod.MTLLoader;
            GLTFLoader = gltfMod.GLTFLoader;
            URDFLoader = urdfMod.default;
        }).catch((error) => {
            urdfRuntimePromise = null;
            throw error;
        });
    }
    return urdfRuntimePromise;
}

function normalizeJointName(name='') {
    return String(name).trim().toLowerCase().replace(/[\s\-]+/g, '_');
}

function tokenizeJointName(name='') {
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

function jointNameVariants(name='') {
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

function scoreJointNameMatch(datasetName, urdfName) {
    const datasetNorm = normalizeJointName(datasetName);
    const urdfNorm = normalizeJointName(urdfName);
    if (!datasetNorm || !urdfNorm) return 0;
    if (datasetNorm === urdfNorm) return 1000;

    const datasetVariants = jointNameVariants(datasetName);
    const urdfVariants = jointNameVariants(urdfName);
    const urdfSet = new Set(urdfVariants);
    let longestMatchLen = 0;
    for (const v of datasetVariants) {
        if (urdfSet.has(v) && v.length > longestMatchLen) longestMatchLen = v.length;
    }
    if (longestMatchLen > 0) {
        const maxLen = Math.max(...datasetVariants.map(v => v.length), 1);
        return Math.round(500 + 400 * (longestMatchLen / maxLen));
    }

    const dsTokens = tokenizeJointName(datasetName);
    const urTokens = tokenizeJointName(urdfName);
    if (!dsTokens.length || !urTokens.length) return 0;

    const dsSet = new Set(dsTokens);
    const urSet = new Set(urTokens);
    const overlap = dsTokens.filter(token => urSet.has(token));
    if (!overlap.length) {
        if (urdfNorm.endsWith(datasetNorm) || datasetNorm.endsWith(urdfNorm)) return 300;
        return 0;
    }

    let score = overlap.length * 120;
    if (dsTokens.at(-1) === urTokens.at(-1)) score += 220;
    if (dsTokens[0] === urTokens[0]) score += 80;
    if (urdfNorm.includes(datasetNorm) || datasetNorm.includes(urdfNorm)) score += 60;
    return score;
}

function buildUrdfJointMap(datasetJointNames, urdfJointNames) {
    const available = new Set(urdfJointNames);
    const map = [];

    for (const [datasetIndex, datasetName] of datasetJointNames.entries()) {
        let bestName = null;
        let bestScore = 0;
        for (const urdfName of available) {
            const score = scoreJointNameMatch(datasetName, urdfName);
            if (score > bestScore) {
                bestScore = score;
                bestName = urdfName;
            }
        }
        if (bestName && bestScore >= 300) {
            available.delete(bestName);
            map.push({
                datasetIndex,
                datasetName,
                urdfName: bestName,
                score: bestScore,
                mode: bestScore >= 900 ? 'name' : 'heuristic',
            });
        }
    }

    if (!map.length) {
        const count = Math.min(datasetJointNames.length, urdfJointNames.length);
        for (let i = 0; i < count; i += 1) {
            map.push({
                datasetIndex: i,
                datasetName: datasetJointNames[i],
                urdfName: urdfJointNames[i],
                score: 100,
                mode: 'index',
            });
        }
    } else if (map.length < Math.min(datasetJointNames.length, urdfJointNames.length)) {
        const remainingDataset = datasetJointNames
            .map((name, index) => ({ name, index }))
            .filter(item => !map.some(existing => existing.datasetIndex === item.index));
        const remainingUrdf = urdfJointNames.filter(name => !map.some(existing => existing.urdfName === name));
        if (remainingDataset.length && remainingUrdf.length) {
            const count = Math.min(remainingDataset.length, remainingUrdf.length);
            for (let i = 0; i < count; i += 1) {
                map.push({
                    datasetIndex: remainingDataset[i].index,
                    datasetName: remainingDataset[i].name,
                    urdfName: remainingUrdf[i],
                    score: 50,
                    mode: 'index-fallback',
                });
            }
        }
    }

    return map.sort((a, b) => a.datasetIndex - b.datasetIndex);
}

function summarizeJointMap(map, datasetJointNames, urdfJointNames) {
    if (!map.length) return { mode: 'none', text: '未建立关节映射' };
    const manual = map.filter(item => item.mode === 'manual').length;
    const pureName = map.filter(item => item.mode === 'name').length;
    const heuristic = map.filter(item => item.mode === 'heuristic').length;
    const index = map.filter(item => item.mode === 'index' || item.mode === 'index-fallback').length;
    const parts = [`已映射 ${map.length}/${datasetJointNames.length}`];
    if (manual) parts.push(`手动 ${manual}`);
    if (pureName) parts.push(`精确 ${pureName}`);
    if (heuristic) parts.push(`近似 ${heuristic}`);
    if (index) parts.push(`按序 ${index}`);
    let mode;
    if (manual) mode = 'manual';
    else if (index && !pureName && !heuristic) mode = 'index';
    else if (heuristic || index) mode = 'mixed';
    else mode = 'name';
    return {
        mode,
        text: `${parts.join(' · ')}，URDF 可动关节 ${urdfJointNames.length}`,
    };
}

async function uploadUrdfBundle(files, rootFile='') {
    const list = Array.from(files || []);
    if (!list.length) return;

    try {
        updateUrdfUploadStatus('正在加载 3D 组件...');
        await ensureUrdfRuntime();

        const form = new FormData();
        const manifest = list.map(file => ({
            path: file.webkitRelativePath || file.name,
            name: file.name,
        }));
        for (const file of list) form.append('files', file, file.name);
        form.append('manifest', JSON.stringify(manifest));
        form.append('root_file', rootFile || manifest.find(item => item.path.toLowerCase().endsWith('.urdf'))?.path || manifest[0].path);

        updateUrdfUploadStatus('正在上传 URDF 资源...');
        const resp = await fetch('/api/urdf/upload', { method: 'POST', body: form });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        await loadUrdfRobot(data);
        toast(`URDF 已加载: ${data.robot_name}`,'success');
    } catch (e) {
        updateUrdfUploadStatus(`URDF 加载失败: ${e.message}`);
        toast(e.message,'error');
        throw e;
    }
}

function guessRootUrdf(files) {
    const list = Array.from(files || []);
    const urdfs = list
        .map(file => file.webkitRelativePath || file.name)
        .filter(path => path.toLowerCase().endsWith('.urdf'))
        .sort();
    return urdfs[0] || '';
}

function resolveUrdfAssetUrl(url, baseUrl='') {
    if (!url) return '';
    if (url.startsWith('data:')) return url;
    if (/^(https?:)?\/\//.test(url) || url.startsWith('/api/urdf_asset/')) return url;
    if (url.startsWith('package://')) {
        return `${S.urdf.assetRoot}/${url.replace(/^package:\/\//, '')}`;
    }

    const fallbackBase = S.urdf.rootUrl || '/';
    try {
        return new URL(url, new URL(baseUrl || fallbackBase, window.location.origin)).toString();
    } catch (_e) {
        return url;
    }
}

function createDefaultMeshMaterial() {
    return new THREE.MeshStandardMaterial({
        color: 0xb8c7d9,
        metalness: 0.1,
        roughness: 0.78,
    });
}

function applyUrdfObjectStyle(root) {
    root.traverse(obj => {
        if (!obj.isMesh) return;
        if (!obj.material) obj.material = createDefaultMeshMaterial();
        if (Array.isArray(obj.material)) {
            obj.material = obj.material.map(mat => mat || createDefaultMeshMaterial());
        }
        obj.castShadow = true;
        obj.receiveShadow = true;
    });
    return root;
}

function loadObjWithOptionalMtl(url, manager, done) {
    const objLoader = new OBJLoader(manager);
    const resourceDir = url.replace(/[^/]*([?#].*)?$/, '');
    objLoader.setResourcePath(resourceDir);
    const finishObjLoad = () => {
        objLoader.load(
            url,
            object => done(applyUrdfObjectStyle(object)),
            undefined,
            () => done(new THREE.Group()),
        );
    };

    const mtlUrl = url.replace(/\.[^/.?#]+([?#].*)?$/i, '.mtl$1');
    const mtlLoader = new MTLLoader(manager);
    mtlLoader.setResourcePath(resourceDir);
    mtlLoader.load(
        mtlUrl,
        materials => {
            materials.preload();
            objLoader.setMaterials(materials);
            finishObjLoad();
        },
        undefined,
        () => finishObjLoad(),
    );
}

function createUrdfMeshLoader(manager) {
    return (path, loadManager, done) => {
        const effectiveManager = loadManager || manager;
        const resolved = resolveUrdfAssetUrl(path);
        const lower = resolved.toLowerCase();

        if (lower.endsWith('.stl')) {
            new STLLoader(effectiveManager).load(
                resolved,
                geometry => {
                    geometry.computeVertexNormals?.();
                    done(applyUrdfObjectStyle(new THREE.Mesh(geometry, createDefaultMeshMaterial())));
                },
                undefined,
                () => done(new THREE.Group()),
            );
            return;
        }

        if (lower.endsWith('.dae')) {
            new ColladaLoader(effectiveManager).load(
                resolved,
                collada => done(applyUrdfObjectStyle(collada.scene || new THREE.Group())),
                undefined,
                () => done(new THREE.Group()),
            );
            return;
        }

        if (lower.endsWith('.obj')) {
            loadObjWithOptionalMtl(resolved, effectiveManager, done);
            return;
        }

        if (lower.endsWith('.glb') || lower.endsWith('.gltf')) {
            new GLTFLoader(effectiveManager).load(
                resolved,
                gltf => done(applyUrdfObjectStyle(gltf.scene || new THREE.Group())),
                undefined,
                () => done(new THREE.Group()),
            );
            return;
        }

        console.warn(`Unsupported URDF mesh asset: ${path}`);
        done(new THREE.Group());
    };
}

// ═══════════════════════ 核心操作 ═══════════════════════

async function loadDataset() {
    if (S.uiLocked) return;
    const path = $('ds-path').value.trim();
    if (!path) return toast('请输入数据集路径','error');
    toast('正在加载...','info');
    const d = await post('/api/load',{path});
    S.dataset=d.summary; S.episodes=d.episodes;
    S.jointNames=d.joint_names; S.jointGroups=d.joint_groups;
    S.selEpisodes.clear(); S.selFrames.clear();
    S.curEp=null; S.curEpData=null;
    S.mergeDatasets = [];
    S.activeJoints = new Set(d.joint_names.slice(0,7));
    if (S.urdf.packageId) {
        S.urdf.jointMap = buildUrdfJointMap(S.jointNames, S.urdf.jointNames);
        S.urdf._detectedDegree = null;
        const mapping = summarizeJointMap(S.urdf.jointMap, S.jointNames, S.urdf.jointNames);
        S.urdf.mappingMode = mapping.mode;
        S.urdf.mappingSummary = mapping.text;
        updateUrdfUploadStatus(`已加载 ${S.urdf.robotName || 'URDF'}，${mapping.text}`);
        if (S.urdf.jointMap.length) showJointMappingModal();
    }
    S.integrity = null;
    renderSummary(); renderMergeQueue(); renderEpList(); renderJointSel(); hideDetail();
    $('save-path').value = path+'_edited';
    toast(`已加载: ${d.summary.total_episodes} ep, ${d.summary.total_frames} 帧`,'success');
    runIntegrityCheck();
}

async function runIntegrityCheck() {
    if (S.integrityChecking) return;
    S.integrityChecking = true;
    renderSummary();
    try {
        const r = await fetch('/api/integrity');
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        S.integrity = d;
        renderSummary();
        if (d.error_count > 0) {
            toast(`⚠ 一致性检查: ${d.error_count} 路视频与 parquet 不一致 (涉及 ${d.affected_episodes.length} 集)，点顶部红色角标查看`, 'error');
        } else if (d.warning_count > 0) {
            toast(`一致性检查: ${d.warning_count} 项警告 (如 ffprobe 探测失败)`, 'info');
        }
    } catch (e) {
        console.error('integrity check failed', e);
    } finally {
        S.integrityChecking = false;
        renderSummary();
    }
}

function showIntegrityModal() {
    if (!S.integrity) return;
    const d = S.integrity;
    const existing = document.getElementById('integ-modal');
    if (existing) existing.remove();

    let bodyHtml = `<div class="integ-hint">
        共检查 <b>${d.total_videos_checked}</b> 路视频 / <b>${d.total_episodes_checked}</b> 集，
        error <b style="color:#e74c3c">${d.error_count}</b>，
        warning <b style="color:#b7750a">${d.warning_count}</b>。
        info.json fps = <b>${d.info_fps}</b>。
        ${d.ffprobe_missing ? '<span style="color:#c0392b">⚠ 所有探测均失败，请确认 ffprobe 已安装。</span>' : ''}
        <br>命中一致性问题时, 视频与 parquet 曲线<b>不是按同一时间轴同步</b>的, 播放器会出现画面与曲线错位; 建议重新生成数据集或核对 meta/info.json 的 fps 字段。
    </div>`;

    if (!d.episodes || !d.episodes.length) {
        bodyHtml += `<div style="text-align:center;padding:20px;color:#27ae60;">✓ 全部通过</div>`;
    } else {
        let rows = '';
        for (const ep of d.episodes) {
            for (const cam of ep.cameras) {
                if (!cam.issues.length) continue;
                const lvl = cam.issues.some(i => i.level === 'error') ? 'error' : 'warning';
                const fpsCell = cam.video_fps == null ? '—' : cam.video_fps.toFixed(3);
                const framesCell = cam.video_nb_frames == null ? '—' : cam.video_nb_frames;
                const durCell = cam.video_duration == null ? '—' : cam.video_duration.toFixed(3) + 's';
                const issueHtml = cam.issues.map(i =>
                    `<span class="integ-issue ${i.level === 'warning' ? 'warn' : ''}">• ${i.message}</span>`
                ).join('');
                rows += `<tr class="integ-${lvl}">
                    <td class="integ-num">${ep.episode_index}</td>
                    <td>${cam.camera}</td>
                    <td class="integ-num">${fpsCell}</td>
                    <td class="integ-num">${framesCell}</td>
                    <td class="integ-num">${cam.parquet_rows}</td>
                    <td class="integ-num">${durCell}</td>
                    <td>${issueHtml}</td>
                </tr>`;
            }
        }
        bodyHtml += `<table class="integ-tbl">
            <thead><tr>
                <th>Ep</th><th>相机</th><th>视频 fps</th><th>视频帧数</th>
                <th>parquet 行数</th><th>视频时长</th><th>问题</th>
            </tr></thead>
            <tbody>${rows}</tbody>
        </table>`;
    }

    const html = `<div class="modal-overlay" id="integ-modal" onclick="if(event.target===this)closeIntegrityModal()">
        <div class="modal-card" style="width:820px;max-width:94vw;">
            <div class="modal-hdr"><h3>数据集一致性检查</h3><button class="modal-close" onclick="closeIntegrityModal()">×</button></div>
            <div class="modal-body">${bodyHtml}</div>
            <div class="modal-footer">
                <button class="bp" onclick="runIntegrityCheck();closeIntegrityModal();">重新检查</button>
                <button class="bg" onclick="closeIntegrityModal()">关闭</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML('beforeend', html);
}

function closeIntegrityModal() {
    const m = document.getElementById('integ-modal');
    if (m) m.remove();
}

async function addMergeDataset() {
    if (S.uiLocked) return;
    if (!S.dataset) return toast('请先加载主数据集','error');
    const path = $('merge-path').value.trim();
    if (!path) return toast('请输入待拼接数据集路径','error');
    if (path === S.dataset.path) return toast('不能把当前主数据集再次追加到自己后面','error');
    if (S.mergeDatasets.some(item => item.path === path)) return toast('该数据集已经在拼接队列中','info');

    toast('正在检查待拼接数据集...','info');
    const data = await post('/api/merge/inspect', { path });
    S.mergeDatasets.push({
        path,
        basename: data.basename || path.split('/').pop() || path,
        summary: data.summary,
    });
    $('merge-path').value = '';
    renderSummary();
    renderMergeQueue();
    toast(`已加入拼接队列: ${data.basename || path}`,'success');
}

function removeMergeDataset(path) {
    S.mergeDatasets = S.mergeDatasets.filter(item => item.path !== path);
    renderSummary();
    renderMergeQueue();
}

async function viewEpisode(idx) {
    if (S.uiLocked) return;
    videoPause();
    toast('加载 Episode...','info');
    const d = await api(`/api/episode/${idx}`);
    S.curEp=idx; S.curEpData=d; S.selFrames.clear(); S.chartClickState=0;
    S.urdf._detectedDegree = null;
    renderEpDetail(d); renderEpList();
    toast(`Ep ${idx}: ${d.frames.length} 帧`,'success');
}

async function doDeleteEp() {
    if (S.uiLocked) return;
    const idx = Array.from(S.selEpisodes);
    if (!idx.length) return toast('请先勾选要删除的 Episode','error');
    if (!confirm(`确定删除 ${idx.length} 个 Episode?\n\n${idx.join(', ')}`)) return;
    if (!confirm('⚠ 二次确认: 索引将重新编号。确认?')) return;
    const d = await post('/api/delete_episodes',{indices:idx});
    S.episodes=d.episodes; S.dataset=d.summary; S.selEpisodes.clear();
    S.curEp=null; S.curEpData=null;
    renderSummary(); renderEpList(); hideDetail();
    toast(`已删除 ${idx.length} ep, 剩余 ${d.remaining_episodes}`,'success');
}

async function doDeleteFrames() {
    if (S.uiLocked) return;
    const fr = Array.from(S.selFrames).sort((a,b)=>a-b);
    if (!fr.length) return toast('请先选择要删除的片段','error');
    if (S.curEp===null) return;

    toast('正在分析平滑性...','info');
    try {
        const activeIdx = Array.from(S.activeJoints)
            .map(j => S.jointNames.indexOf(j)).filter(i => i >= 0);
        const analysis = await post('/api/analyze_deletion', {
            episode_index: S.curEp, frame_indices: fr,
            active_joint_indices: activeIdx.length ? activeIdx : null
        });
        if (analysis.smooth) {
            await executeDelete(fr);
        } else {
            showAnalysisModal(analysis, fr);
        }
    } catch(e) {
        await executeDelete(fr);
    }
}

async function executeDelete(framesToDelete, keepFrames=[]) {
    if (S.uiLocked) return;
    const actual = keepFrames.length > 0
        ? framesToDelete.filter(f => !keepFrames.includes(f))
        : framesToDelete;
    if (!actual.length) { toast('没有需要删除的帧','info'); return; }

    const rng = compressRanges(actual.sort((a,b)=>a-b));
    const rs = rng.map(([s,e])=>s===e?`帧${s}`:`帧${s}-${e}`).join(', ');
    let msg = `从 Ep ${S.curEp} 删除 ${actual.length} 帧?\n\n${rs}`;
    if (keepFrames.length > 0) msg += `\n\n(保留桥接帧: ${keepFrames.join(', ')})`;
    if (!confirm(msg)) return;

    videoPause();
    const d = await post('/api/delete_frames',{episode_index:S.curEp,frame_indices:actual});
    S.episodes=d.episodes; S.dataset=d.summary; S.selFrames.clear(); S.chartClickState=0;
    if (d.episode_data) { S.curEpData=d.episode_data; renderEpDetail(d.episode_data); }
    else { S.curEp=null; S.curEpData=null; hideDetail(); }
    renderSummary(); renderEpList();
    toast(`已删除 ${actual.length} 帧, 剩余 ${d.remaining_frames}`,'success');
}

function showAnalysisModal(analysis, originalFrames) {
    const rec = analysis.recommendation || {};
    const alt = analysis.alternative;

    S.bridgePreview = rec.frames || [];
    refreshChart();

    let jHTML = '';
    for (const j of (analysis.junctions || [])) {
        const joints = (j.problematic_joints || [])
            .map(ji => S.jointNames[ji] || `关节${ji}`).slice(0, 5);
        jHTML += `<div class="aj-item">
            <span class="aj-range">帧 ${j.left_frame} ↔ 帧 ${j.right_frame}</span>
            <span class="aj-info">跨越 ${j.deleted_count} 帧 · 加速度超标 ${j.max_accel_ratio}×</span>
            ${joints.length ? `<span class="aj-joints">受影响: ${joints.join(', ')}</span>` : ''}
        </div>`;
    }

    let rHTML = '';
    if (rec.method === 'bridge' && rec.frames && rec.frames.length) {
        rHTML += `<div class="arec">
            <div class="arec-title">方案一: 保留桥接帧 <span class="arec-tag-pri">推荐</span></div>
            <p>通过递归二分法找到的最优桥接帧，保留这些真实帧使轨迹平滑过渡</p>
            <div class="arec-frames">${rec.frames.map(f=>`<span class="af-tag af-bridge">帧 ${f}</span>`).join('')}</div>
            <button class="bp" onclick="applyRecommendation(${JSON.stringify(rec.frames)},${JSON.stringify(originalFrames)})">采用此方案</button>
        </div>`;
        if (alt && alt.frames && alt.frames.length) {
            rHTML += `<div class="arec">
                <div class="arec-title">方案二: 滤波插值匹配</div>
                <p>通过滤波生成平滑参考轨迹，匹配最接近理想值的真实帧</p>
                <div class="arec-frames">${alt.frames.map(f=>`<span class="af-tag af-filter">帧 ${f}</span>`).join('')}</div>
                <button class="bp" onclick="applyRecommendation(${JSON.stringify(alt.frames)},${JSON.stringify(originalFrames)})">采用此方案</button>
            </div>`;
        }
    } else if (rec.method === 'filter' && rec.frames && rec.frames.length) {
        rHTML += `<div class="arec">
            <div class="arec-title">方案: 滤波插值匹配</div>
            <p>未找到理想桥接帧，通过滤波生成平滑参考轨迹，匹配最接近理想值的真实帧</p>
            <div class="arec-frames">${rec.frames.map(f=>`<span class="af-tag af-filter">帧 ${f}</span>`).join('')}</div>
            <button class="bp" onclick="applyRecommendation(${JSON.stringify(rec.frames)},${JSON.stringify(originalFrames)})">采用此方案</button>
        </div>`;
    } else {
        rHTML += `<div class="arec arec-warn"><p>未找到合适的过渡帧，建议减小删除范围或直接强制删除</p></div>`;
    }

    const html = `<div class="modal-overlay" id="analysis-modal" onclick="if(event.target===this)closeAnalysisModal()">
        <div class="modal-card">
            <div class="modal-hdr"><h3>平滑性分析结果</h3><button class="modal-close" onclick="closeAnalysisModal()">×</button></div>
            <div class="modal-body">
                <div class="aj-section"><h4>检测到不连续拼接点</h4>${jHTML}</div>
                <div class="arec-section"><h4>建议方案</h4>${rHTML}</div>
            </div>
            <div class="modal-footer">
                <button class="bd" onclick="forceDeleteAll(${JSON.stringify(originalFrames)})">强制全部删除</button>
                <button class="bg" onclick="closeAnalysisModal()">取消</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML('beforeend', html);
}

function closeAnalysisModal() {
    S.bridgePreview = []; refreshChart();
    const m = document.getElementById('analysis-modal');
    if (m) m.remove();
}

async function applyRecommendation(keepFrames, allFrames) {
    closeAnalysisModal();
    await executeDelete(allFrames, keepFrames);
}

async function forceDeleteAll(frames) {
    closeAnalysisModal();
    await executeDelete(frames);
}

async function doSave() {
    if (S.uiLocked) return;
    const p = $('save-path').value.trim();
    if (!p) return toast('请输入保存路径','error');
    const appendCount = S.mergeDatasets.length;
    const appendMsg = appendCount
        ? `\n\n将按顺序追加拼接 ${appendCount} 个数据集:\n${S.mergeDatasets.map(item => `- ${item.path}`).join('\n')}`
        : '';
    if (!confirm(`保存到:\n${p}\n\n已有路径将被覆盖!${appendMsg}`)) return;
    lockUI('正在保存数据集', '正在写入 Parquet、导出视频并重算统计信息，请稍候...');
    startSaveProgressPolling();
    toast('正在保存 (含统计重算)...','info');
    try {
        const d = await post('/api/save',{
            output_path:p,
            append_paths: S.mergeDatasets.map(item => item.path),
        });
        updateLoadingProgress({
            title: '保存完成',
            detail: `数据集已保存到: ${d.path}`,
            percent: 100,
        });
        toast(`保存成功: ${d.path}`,'success');
    } finally {
        stopSaveProgressPolling();
        unlockUI();
    }
}

// ═══════════════════════ URDF 预览 ═══════════════════════

function updateUrdfUploadStatus(text) {
    const el = $('urdf-upload-status');
    if (el) el.textContent = text;
}

function renderUrdfPanel() {
    const panel = $('urdf-panel');
    const info = $('urdf-info');
    const empty = $('urdf-viewer-empty');
    const viewer = $('urdf-viewer');
    if (!panel || !info || !empty || !viewer) return;

    const hasPackage = !!S.urdf.packageId;
    panel.classList.toggle('show', hasPackage);
    if (!hasPackage) return;

    const matched = S.urdf.jointMap.length;
    const total = S.jointNames.length || 0;
    const robotName = S.urdf.robotName || '未命名机器人';
    const src = S.urdf.source === 'action' ? 'Action' : 'State';
    const mappingText = S.urdf.mappingSummary || `已映射 ${matched}/${total}`;
    let degLabel = '';
    if (S.urdf.degreeMode === 'degree') degLabel = ' · 角度制→弧度';
    else if (S.urdf.degreeMode === 'radian') degLabel = ' · 弧度制';
    else if (S.urdf._detectedDegree === true) degLabel = ' · 自动:角度制→弧度';
    else if (S.urdf._detectedDegree === false) degLabel = ' · 自动:弧度制';
    info.textContent = S.urdf.loadError
        ? `${robotName} · 加载失败: ${S.urdf.loadError}`
        : `${robotName} · ${mappingText} · ${src}${degLabel}`;

    empty.style.display = S.urdf.robot ? 'none' : 'flex';
    if (!S.urdf.robot && S.urdf.loadError) {
        empty.textContent = `模型加载失败：${S.urdf.loadError}。如果 URDF 依赖 meshes/贴图，请优先使用“上传目录”并保持原始目录结构。`;
    } else {
        empty.textContent = '上传 URDF 后，这里会显示 3D 机器人预览，并跟随当前播放进度实时联动。';
    }
    viewer.style.display = S.urdf.robot ? 'block' : 'none';
    $('urdf-source').value = S.urdf.source;
    const degSel = $('urdf-deg-mode');
    if (degSel) degSel.value = S.urdf.degreeMode;
}

function ensureUrdfViewer() {
    if (S.urdf.scene) return;

    const container = $('urdf-viewer');
    if (!container) return;

    const scene = new THREE.Scene();
    scene.background = null;

    const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 100);
    camera.position.set(2.8, 2.1, 2.8);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(container.clientWidth || 640, container.clientHeight || 300);
    container.innerHTML = '';
    container.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.target.set(0, 0.7, 0);

    const hemi = new THREE.HemisphereLight(0xffffff, 0x8aa0b6, 1.25);
    scene.add(hemi);

    const dir = new THREE.DirectionalLight(0xffffff, 1.2);
    dir.position.set(4, 8, 5);
    scene.add(dir);

    const fill = new THREE.DirectionalLight(0xffffff, 0.45);
    fill.position.set(-3, 4, -5);
    scene.add(fill);

    const grid = new THREE.GridHelper(8, 20, 0x9db2c7, 0xd8e2ec);
    grid.position.y = 0;
    scene.add(grid);

    S.urdf.scene = scene;
    S.urdf.camera = camera;
    S.urdf.renderer = renderer;
    S.urdf.controls = controls;
    S.urdf.light = dir;
    S.urdf.grid = grid;

    if (!S.urdf.resizeObserver && window.ResizeObserver) {
        S.urdf.resizeObserver = new ResizeObserver(() => resizeUrdfViewer());
        S.urdf.resizeObserver.observe(container);
    }

    if (!S.urdf.animating) {
        S.urdf.animating = true;
        const loop = () => {
            if (!S.urdf.animating || !S.urdf.renderer || !S.urdf.scene || !S.urdf.camera) return;
            S.urdf.controls?.update();
            S.urdf.renderer.render(S.urdf.scene, S.urdf.camera);
            requestAnimationFrame(loop);
        };
        requestAnimationFrame(loop);
    }
}

function resizeUrdfViewer() {
    const container = $('urdf-viewer');
    if (!container || !S.urdf.renderer || !S.urdf.camera) return;
    const width = Math.max(container.clientWidth || 0, 1);
    const height = Math.max(container.clientHeight || 0, 1);
    S.urdf.camera.aspect = width / height;
    S.urdf.camera.updateProjectionMatrix();
    S.urdf.renderer.setSize(width, height);
}

function fitUrdfCamera() {
    const robot = S.urdf.robot;
    if (!robot || !S.urdf.camera || !S.urdf.controls) return;

    const box = new THREE.Box3().setFromObject(robot);
    if (box.isEmpty()) {
        S.urdf.camera.position.set(2.8, 2.1, 2.8);
        S.urdf.controls.target.set(0, 0.7, 0);
        S.urdf.controls.update();
        return;
    }

    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const radius = Math.max(size.x, size.y, size.z, 0.5);
    S.urdf.controls.target.copy(center);
    S.urdf.camera.position.set(
        center.x + radius * 1.8,
        center.y + radius * 1.4,
        center.z + radius * 1.8,
    );
    S.urdf.camera.near = Math.max(radius / 100, 0.01);
    S.urdf.camera.far = Math.max(radius * 20, 20);
    S.urdf.camera.updateProjectionMatrix();
    S.urdf.controls.update();
}

function clearUrdfRobot() {
    if (S.urdf.robot && S.urdf.scene) S.urdf.scene.remove(S.urdf.robot);
    S.urdf.robot = null;
    S.urdf.loadError = '';
    S.urdf.mappingMode = 'none';
    S.urdf.mappingSummary = '';
    renderUrdfPanel();
}

async function loadUrdfRobot(meta) {
    ensureUrdfViewer();
    clearUrdfRobot();

    S.urdf.packageId = meta.package_id;
    S.urdf.robotName = meta.robot_name || '';
    S.urdf.rootUrl = meta.root_url;
    S.urdf.rootDir = meta.root_url.replace(/[^/]*$/, '');
    S.urdf.assetRoot = meta.asset_root;
    S.urdf.jointInfo = meta.joint_info || {};
    S.urdf.jointNames = meta.movable_joint_names || meta.joint_names || [];
    S.urdf.jointMap = buildUrdfJointMap(S.jointNames, S.urdf.jointNames);
    S.urdf._detectedDegree = null;
    const mapping = summarizeJointMap(S.urdf.jointMap, S.jointNames, S.urdf.jointNames);
    S.urdf.mappingMode = mapping.mode;
    S.urdf.mappingSummary = mapping.text;
    S.urdf.loadError = '';
    updateUrdfUploadStatus(`已加载 ${S.urdf.robotName || 'URDF'}，${mapping.text}`);
    renderUrdfPanel();

    const manager = new THREE.LoadingManager();
    manager.setURLModifier((url) => resolveUrdfAssetUrl(url));

    const loader = new URDFLoader(manager);
    loader.packages = `${S.urdf.assetRoot}/`;
    loader.fetchOptions = { credentials: 'same-origin' };
    loader.loadMeshCb = createUrdfMeshLoader(manager);

    const robot = await new Promise((resolve, reject) => {
        loader.load(S.urdf.rootUrl, resolve, undefined, reject);
    }).catch((error) => {
        S.urdf.loadError = error?.message || '未知错误';
        renderUrdfPanel();
        throw error;
    });

    robot.rotation.x = -Math.PI / 2;

    const allRuntimeNames = Object.keys(robot.joints || {});
    const runtimeJointNames = allRuntimeNames.filter(name => {
        const j = robot.joints[name];
        return j && j.jointType && j.jointType !== 'fixed';
    });

    if (allRuntimeNames.length !== runtimeJointNames.length) {
        console.info(
            `[URDF] 已过滤 ${allRuntimeNames.length - runtimeJointNames.length} 个 fixed 关节，` +
            `保留 ${runtimeJointNames.length} 个可动关节`
        );
    }

    if (runtimeJointNames.length) {
        S.urdf.jointNames = runtimeJointNames;
        S.urdf.jointMap = buildUrdfJointMap(S.jointNames, runtimeJointNames);
        S.urdf._detectedDegree = null;
        const runtimeMapping = summarizeJointMap(S.urdf.jointMap, S.jointNames, runtimeJointNames);
        S.urdf.mappingMode = runtimeMapping.mode;
        S.urdf.mappingSummary = runtimeMapping.text;
        updateUrdfUploadStatus(`已加载 ${S.urdf.robotName || 'URDF'}，${runtimeMapping.text}`);
    }

    if (S.urdf.jointMap.length) {
        console.group('[URDF] 关节映射诊断');
        console.table(S.urdf.jointMap.map(m => ({
            '数据集索引': m.datasetIndex,
            '数据集关节': m.datasetName,
            'URDF关节': m.urdfName,
            '匹配方式': m.mode,
            '置信分数': m.score,
        })));
        const unmappedDataset = S.jointNames.filter(
            (_, i) => !S.urdf.jointMap.some(m => m.datasetIndex === i));
        const unmappedUrdf = runtimeJointNames.filter(
            name => !S.urdf.jointMap.some(m => m.urdfName === name));
        if (unmappedDataset.length) console.warn('[URDF] 未映射的数据集关节:', unmappedDataset);
        if (unmappedUrdf.length) console.warn('[URDF] 未映射的URDF关节:', unmappedUrdf);
        console.groupEnd();
    }

    S.urdf.robot = robot;
    S.urdf.scene.add(robot);
    fitUrdfCamera();
    resizeUrdfViewer();
    syncUrdfPose();
    renderUrdfPanel();

    if (S.jointNames.length && S.urdf.jointMap.length) {
        showJointMappingModal();
    }
}

function detectDegreeMode(source) {
    if (S.urdf.degreeMode === 'degree') return true;
    if (S.urdf.degreeMode === 'radian') return false;
    if (S.urdf._detectedDegree !== null) return S.urdf._detectedDegree;

    let maxAbsRevoluteVal = 0;
    for (const mapping of S.urdf.jointMap) {
        const val = source[mapping.datasetIndex];
        if (typeof val !== 'number' || Number.isNaN(val)) continue;
        const jType = (S.urdf.jointInfo[mapping.urdfName]?.type) || 'revolute';
        if (jType === 'revolute' || jType === 'continuous') {
            maxAbsRevoluteVal = Math.max(maxAbsRevoluteVal, Math.abs(val));
        }
    }

    S.urdf._detectedDegree = maxAbsRevoluteVal > 2 * Math.PI;
    if (S.urdf._detectedDegree) {
        console.info(
            `[URDF] 自动检测: 数据可能为角度制 (旋转关节最大绝对值=${maxAbsRevoluteVal.toFixed(2)}, ` +
            `超过 2π≈${(2 * Math.PI).toFixed(2)}), 将自动 deg→rad 转换`
        );
    } else {
        console.info(
            `[URDF] 自动检测: 数据为弧度制 (旋转关节最大绝对值=${maxAbsRevoluteVal.toFixed(2)})`
        );
    }
    return S.urdf._detectedDegree;
}

function syncUrdfPose() {
    if (!S.urdf.robot || !S.curEpData || !S.curEpData.frames?.length) return;
    const frame = S.curEpData.frames[Math.max(0, Math.min(S.videoFrame, S.curEpData.frames.length - 1))];
    if (!frame) return;

    const source = S.urdf.source === 'action' ? frame.action : frame.state;
    if (!Array.isArray(source)) return;

    const useDeg = detectDegreeMode(source);
    const DEG2RAD = Math.PI / 180;

    for (const mapping of S.urdf.jointMap) {
        const value = source[mapping.datasetIndex];
        if (typeof value !== 'number' || Number.isNaN(value)) continue;

        let finalValue = value;
        if (useDeg) {
            const jType = (S.urdf.jointInfo[mapping.urdfName]?.type) || 'revolute';
            if (jType === 'revolute' || jType === 'continuous') {
                finalValue = value * DEG2RAD;
            }
        }

        try {
            S.urdf.robot.setJointValue(mapping.urdfName, finalValue);
        } catch (_e) {}
    }
}

function resetUrdfCamera() {
    fitUrdfCamera();
}

function showJointMappingModal() {
    if (!S.jointNames.length || !S.urdf.jointNames.length) return;

    const existing = document.getElementById('jm-modal');
    if (existing) existing.remove();

    const autoMap = {};
    for (const m of S.urdf.jointMap) autoMap[m.datasetIndex] = m;

    const sortedUrdf = S.urdf.jointNames.slice().sort();
    const urdfOpts = sortedUrdf.map(n => `<option value="${n}">${n}</option>`).join('');

    function badgeFor(entry) {
        if (!entry) return { cls: 'jm-none', text: '未映射' };
        if (entry.mode === 'manual') return { cls: 'jm-manual', text: '手动' };
        if (entry.score >= 900) return { cls: 'jm-exact', text: '精确' };
        if (entry.score >= 600) return { cls: 'jm-heuristic', text: '近似' };
        if (entry.score >= 300) return { cls: 'jm-low', text: '低置信' };
        return { cls: 'jm-none', text: '未映射' };
    }

    function rowHTML(jName) {
        const idx = S.jointNames.indexOf(jName);
        if (idx < 0) return '';
        const entry = autoMap[idx];
        const b = badgeFor(entry);
        return `<tr>
            <td class="jm-ds">${jName}</td>
            <td style="color:#999;text-align:center;width:28px;">→</td>
            <td><select class="jm-select" data-ds-idx="${idx}" data-ds-name="${jName}">
                <option value="">(未映射)</option>${urdfOpts}
            </select></td>
            <td style="width:60px;"><span class="jm-badge ${b.cls}">${b.text}</span></td>
        </tr>`;
    }

    let tableHTML = '';
    const groupedSet = new Set();
    for (const [grp, joints] of Object.entries(S.jointGroups)) {
        tableHTML += `<tr class="jm-group"><td colspan="4">${grp}</td></tr>`;
        for (const jn of joints) { tableHTML += rowHTML(jn); groupedSet.add(jn); }
    }
    const ungrouped = S.jointNames.filter(j => !groupedSet.has(j));
    if (ungrouped.length) {
        tableHTML += `<tr class="jm-group"><td colspan="4">其他</td></tr>`;
        for (const jn of ungrouped) tableHTML += rowHTML(jn);
    }

    const html = `<div class="modal-overlay" id="jm-modal" onclick="if(event.target===this)closeJointMappingModal()">
        <div class="modal-card jm-card">
            <div class="modal-hdr"><h3>关节映射配置</h3><button class="modal-close" onclick="closeJointMappingModal()">×</button></div>
            <div class="jm-summary">
                数据集 ${S.jointNames.length} 个关节 · URDF ${S.urdf.jointNames.length} 个可动关节 · 自动匹配 ${S.urdf.jointMap.length} 个<br>
                <small style="color:#888;">请核对每行映射是否正确，有误可直接在下拉菜单中修改</small>
            </div>
            <div class="modal-body" style="max-height:55vh;overflow-y:auto;">
                <table class="jm-table">
                    <thead><tr><th>数据集关节</th><th></th><th>URDF 关节</th><th>置信度</th></tr></thead>
                    <tbody>${tableHTML}</tbody>
                </table>
            </div>
            <div class="modal-footer">
                <button class="bg" onclick="resetAutoMapping()">重新自动匹配</button>
                <button class="bp" onclick="confirmJointMapping()">确认映射</button>
            </div>
        </div>
    </div>`;

    document.body.insertAdjacentHTML('beforeend', html);

    for (const [idx, m] of Object.entries(autoMap)) {
        const sel = document.querySelector(`#jm-modal select[data-ds-idx="${idx}"]`);
        if (sel && m.urdfName) sel.value = m.urdfName;
    }

    document.querySelectorAll('#jm-modal .jm-select').forEach(sel => {
        sel.addEventListener('change', () => {
            const badge = sel.closest('tr').querySelector('.jm-badge');
            if (!badge) return;
            if (sel.value) {
                badge.className = 'jm-badge jm-manual';
                badge.textContent = '手动';
            } else {
                badge.className = 'jm-badge jm-none';
                badge.textContent = '未映射';
            }
        });
    });
}

function closeJointMappingModal() {
    const m = document.getElementById('jm-modal');
    if (m) m.remove();
}

function confirmJointMapping() {
    const selects = document.querySelectorAll('#jm-modal .jm-select');
    const newMap = [];
    const usedUrdf = new Map();
    const duplicates = [];

    for (const sel of selects) {
        const dsIdx = parseInt(sel.dataset.dsIdx);
        const dsName = sel.dataset.dsName;
        const urdfName = sel.value;
        if (!urdfName) continue;

        if (usedUrdf.has(urdfName)) {
            duplicates.push(`"${urdfName}" → ${usedUrdf.get(urdfName)} 和 ${dsName}`);
        }
        usedUrdf.set(urdfName, dsName);

        newMap.push({
            datasetIndex: dsIdx,
            datasetName: dsName,
            urdfName,
            score: 1000,
            mode: 'manual',
        });
    }

    if (duplicates.length) {
        if (!confirm(`以下 URDF 关节被重复映射:\n\n${duplicates.join('\n')}\n\n是否仍然继续?`)) return;
    }

    S.urdf.jointMap = newMap.sort((a, b) => a.datasetIndex - b.datasetIndex);
    S.urdf._detectedDegree = null;

    const mapping = summarizeJointMap(S.urdf.jointMap, S.jointNames, S.urdf.jointNames);
    S.urdf.mappingMode = mapping.mode;
    S.urdf.mappingSummary = mapping.text;
    updateUrdfUploadStatus(`已加载 ${S.urdf.robotName || 'URDF'}，${mapping.text}`);

    console.group('[URDF] 用户确认的关节映射');
    console.table(newMap.map(m => ({
        '数据集索引': m.datasetIndex,
        '数据集关节': m.datasetName,
        'URDF关节': m.urdfName,
    })));
    console.groupEnd();

    closeJointMappingModal();
    syncUrdfPose();
    renderUrdfPanel();
    toast(`关节映射已确认: ${newMap.length} 个映射`, 'success');
}

function resetAutoMapping() {
    S.urdf.jointMap = buildUrdfJointMap(S.jointNames, S.urdf.jointNames);
    S.urdf._detectedDegree = null;
    closeJointMappingModal();
    showJointMappingModal();
    toast('已重新执行自动匹配', 'info');
}

// ═══════════════════════ 视频同步 ═══════════════════════

const getVids = () => Array.from(document.querySelectorAll('#vw video'));
const getMasterVideo = () => getVids()[0] || null;

function cancelVideoRaf() {
    if (S.videoRaf) {
        cancelAnimationFrame(S.videoRaf);
        S.videoRaf = null;
    }
}

function syncAuxVideos(targetTime, toleranceSec = 0.05) {
    const vids = getVids();
    if (vids.length <= 1) return;
    for (const v of vids.slice(1)) {
        if (Math.abs(v.currentTime - targetTime) > toleranceSec) {
            v.currentTime = targetTime;
        }
    }
}

function renderPlaybackFrame() {
    const sl=$('vs'); if(sl) sl.value=S.videoFrame;
    const lb=$('vfl'); if(lb) lb.textContent=`${S.videoFrame} / ${Math.max(0,S.videoTotalFrames-1)}`;
    refreshChart();
    renderFrameValues(S.videoFrame);
    const prevDetected = S.urdf._detectedDegree;
    syncUrdfPose();
    if (prevDetected === null && S.urdf._detectedDegree !== null) renderUrdfPanel();
}

function startVideoLoop() {
    cancelVideoRaf();
    const fps = S.dataset ? S.dataset.fps : 30;
    const master = getMasterVideo();

    const tick = () => {
        if (!S.videoPlaying) {
            S.videoRaf = null;
            return;
        }

        let frameChanged = false;
        if (master && Number.isFinite(master.currentTime)) {
            syncAuxVideos(master.currentTime);
            const nextFrame = Math.max(0, Math.min(
                S.videoTotalFrames - 1,
                Math.round(master.currentTime * fps),
            ));
            frameChanged = nextFrame !== S.videoFrame;
            S.videoFrame = nextFrame;
        } else if (S.videoFrame >= S.videoTotalFrames - 1) {
            videoPause();
            return;
        }

        if (frameChanged || !master) renderPlaybackFrame();
        if (S.videoFrame >= S.videoTotalFrames - 1) {
            videoPause();
            return;
        }
        S.videoRaf = requestAnimationFrame(tick);
    };

    S.videoRaf = requestAnimationFrame(tick);
}

function videoToggle() { S.videoPlaying ? videoPause() : videoPlay(); }

function videoPlay() {
    if (S.videoPlaying || S.videoTotalFrames<=0) return;
    S.videoPlaying=true; $('btn-vp').textContent='⏸';
    const fps = S.dataset ? S.dataset.fps : 30;
    const targetTime = S.videoFrame / fps;
    const vids = getVids();
    if (vids.length) {
        for (const v of vids) {
            v.currentTime = targetTime;
            const playPromise = v.play();
            if (playPromise && typeof playPromise.catch === 'function') {
                playPromise.catch(() => {});
            }
        }
        startVideoLoop();
        return;
    }

    const iv = 1000 / fps;
    S.videoTimer = setInterval(()=>{
        if (S.videoFrame>=S.videoTotalFrames-1) { videoPause(); return; }
        S.videoFrame++;
        renderPlaybackFrame();
    }, iv);
}

function videoPause() {
    S.videoPlaying=false;
    if (S.videoTimer) { clearInterval(S.videoTimer); S.videoTimer=null; }
    cancelVideoRaf();
    for (const v of getVids()) v.pause();
    const b=$('btn-vp'); if(b) b.textContent='▶';
}

function videoStep(d) {
    videoPause();
    if (d===-Infinity) S.videoFrame=0;
    else if (d===Infinity) S.videoFrame=Math.max(0,S.videoTotalFrames-1);
    else S.videoFrame=Math.max(0,Math.min(S.videoTotalFrames-1,S.videoFrame+d));
    videoSync();
}

function videoSync() {
    const fps=S.dataset?S.dataset.fps:30;
    // 与恒定 FPS、按帧序编码的 MP4 对齐；仅在跳帧/拖动时 seek，避免播放时持续 currentTime 触发高成本解码。
    const t = S.videoFrame / fps;
    for (const v of getVids()) v.currentTime=t;
    renderPlaybackFrame();
}

function onSliderInput(v) { videoPause(); S.videoFrame=parseInt(v)||0; videoSync(); }

// ═══════════════════════ 渲染 ═══════════════════════

function renderSummary() {
    const s=S.dataset; if(!s) return;
    const mergeFrames = S.mergeDatasets.reduce((sum, item) => sum + (item.summary?.total_frames || 0), 0);
    let integHtml = '';
    if (S.integrityChecking) {
        integHtml = ' <span class="bw" style="background:#888;cursor:default;">一致性检查中…</span>';
    } else if (S.integrity) {
        const it = S.integrity;
        if (it.error_count > 0) {
            integHtml = ` <span class="be" title="点击查看详情" onclick="showIntegrityModal()">⚠ 一致性 ${it.error_count} 错误 / ${it.affected_episodes.length} 集</span>`;
        } else if (it.warning_count > 0) {
            integHtml = ` <span class="bw" style="cursor:pointer;" title="点击查看详情" onclick="showIntegrityModal()">一致性 ${it.warning_count} 警告</span>`;
        } else {
            integHtml = ` <span class="bo" title="点击查看详情" onclick="showIntegrityModal()">✓ 一致性 OK</span>`;
        }
    }
    $('si').innerHTML =
        `<span>FPS:<b>${s.fps}</b></span>`+
        `<span>机器人:<b>${s.robot_type}</b></span>`+
        `<span>Episodes:<b>${s.total_episodes}</b></span>`+
        `<span>帧:<b>${s.total_frames}</b></span>`+
        `<span>摄像头:${s.cameras.length?s.cameras.join(','):'无'}</span>`+
        (S.mergeDatasets.length ? ` <span class="bm">待拼接 ${S.mergeDatasets.length} 个 / ${mergeFrames} 帧</span>` : '')+
        (s.modified?' <span class="bw">已修改</span>':'')+
        integHtml;
}

function renderMergeQueue() {
    const el = $('merge-list');
    if (!el) return;
    if (!S.mergeDatasets.length) {
        el.innerHTML = '';
        return;
    }
    el.innerHTML = S.mergeDatasets.map(item => `
        <span class="merge-tag" title="${item.path}">
            <span>${item.basename} · ${item.summary?.total_episodes || 0} ep / ${item.summary?.total_frames || 0} 帧</span>
            <button type="button" onclick="removeMergeDataset(${JSON.stringify(item.path)})">×</button>
        </span>
    `).join('');
}

function renderEpList() {
    const el=$('ep-list');
    if (!S.episodes.length) { el.innerHTML='<div class="mt">暂无</div>'; return; }
    let h='';
    for (const ep of S.episodes) {
        const i=ep.episode_index, ck=S.selEpisodes.has(i)?'checked':'', act=S.curEp===i?'act':'';
        h+=`<div class="epi ${act}"><input type="checkbox" class="eck" ${ck} onclick="event.stopPropagation();toggleEpSel(${i},this.checked)"><span style="flex:1" onclick="viewEpisode(${i})">Ep ${i} <small>(${ep.length}帧)</small></span></div>`;
    }
    el.innerHTML=h; syncSelAll();
}

function renderJointSel() {
    const el=$('jb'); let h='';
    for (const [grp,joints] of Object.entries(S.jointGroups)) {
        const ac=joints.filter(j=>S.activeJoints.has(j)).length, all=ac===joints.length;
        h+='<div class="jg">';
        h+=`<label class="jgl"><input type="checkbox" ${all?'checked':''} data-grp="${grp}" onchange="toggleJtGrp('${grp}',this.checked)"> ${grp}</label>`;
        for (const nm of joints) {
            const gi=S.jointNames.indexOf(nm), ck=S.activeJoints.has(nm)?'checked':'', c=COLORS[gi%COLORS.length];
            h+=`<label class="jl" style="border-left:2px solid ${c};padding-left:3px;"><input type="checkbox" value="${nm}" ${ck} onchange="toggleJt('${nm}',this.checked)"> ${nm}</label>`;
        }
        h+='</div>';
    }
    el.innerHTML=h;
    for (const [grp,joints] of Object.entries(S.jointGroups)) {
        const ac=joints.filter(j=>S.activeJoints.has(j)).length;
        if (ac>0 && ac<joints.length) { const cb=el.querySelector(`input[data-grp="${grp}"]`); if(cb) cb.indeterminate=true; }
    }
}

function renderEpDetail(data) {
    $('no-sel').style.display='none';
    const det=$('ep-det'); det.classList.add('show');
    $('ep-t').textContent=`Episode ${data.episode_index} — ${data.frames.length} 帧`;
    S.videoFrame=0; S.videoTotalFrames=data.frames.length; S.videoPlaying=false;
    const sl=$('vs'); sl.max=Math.max(0,data.frames.length-1); sl.value=0;
    $('vfl').textContent=`0 / ${Math.max(0,data.frames.length-1)}`;
    $('btn-vp').textContent='▶';
    renderVideos(data.videos);
    updateChart(data.frames);
    renderFrameInfo(data.frames);
    renderFrameValues(0);
    renderUrdfPanel();
    syncUrdfPose();
}

function hideDetail() {
    $('no-sel').style.display='flex';
    $('ep-det').classList.remove('show');
    videoPause();
    renderUrdfPanel();
}

function renderVideos(videos) {
    const el=$('vw');
    if (!videos||!Object.keys(videos).length) { el.innerHTML='<div class="mt">无视频</div>'; return; }
    let h='';
    for (const [cam,url] of Object.entries(videos))
        h+=`<div class="vi"><h4>${cam}</h4><video preload="auto" muted width="240"><source src="${url}" type="video/mp4"></video></div>`;
    el.innerHTML=h;
}

function renderFrameInfo(frames) {
    $('fr-total').textContent=frames.length;
    $('fr-sel-cnt').textContent=S.selFrames.size;
    $('fr-from').value=''; $('fr-to').value='';
    $('fr-from').max=frames.length-1; $('fr-to').max=frames.length-1;
    renderSelRanges();
}

function renderSelRanges() {
    const el=$('sr');
    if (!S.selFrames.size) {
        el.innerHTML='<span class="ht">在图表上点两次选择片段，或手动输入范围</span>';
        $('fr-sel-cnt').textContent='0'; refreshChart(); return;
    }
    const sorted=Array.from(S.selFrames).sort((a,b)=>a-b), rng=compressRanges(sorted);
    let h='';
    for (const [s,e] of rng) {
        const l=s===e?`帧${s}`:`帧${s}-${e}(${e-s+1})`;
        h+=`<span class="rt">${l}<button onclick="rmRange(${s},${e})">×</button></span>`;
    }
    el.innerHTML=h; $('fr-sel-cnt').textContent=S.selFrames.size; refreshChart();
}

// ═══════════════════════ 当前帧关节数据 ═══════════════════════

function renderFrameValues(fi) {
    const lbl=$('cfl'), grid=$('fdg');
    if (!lbl||!grid) return;
    if (!S.curEpData||fi<0||fi>=S.curEpData.frames.length) {
        lbl.textContent='—'; grid.innerHTML='<div class="mt">无数据</div>'; return;
    }
    const f=S.curEpData.frames[fi], st=f.state||[], ac=f.action||[];
    const fps=S.dataset?S.dataset.fps:30;
    lbl.textContent=`帧 ${fi} | ${(fi/fps).toFixed(3)}s`;
    let h='';
    for (const [grp,joints] of Object.entries(S.jointGroups)) {
        h+='<div>';
        h+=`<div class="fgt">${grp}</div><table class="ft"><tr><th>关节</th><th>State</th><th>Action</th></tr>`;
        for (const nm of joints) {
            const ji=S.jointNames.indexOf(nm), c=COLORS[ji%COLORS.length];
            const sv=(ji>=0&&ji<st.length)?st[ji].toFixed(4):'—';
            const av=(ji>=0&&ji<ac.length)?ac[ji].toFixed(4):'—';
            h+=`<tr><td class="fjn" style="color:${c}">${nm}</td><td>${sv}</td><td>${av}</td></tr>`;
        }
        h+='</table></div>';
    }
    grid.innerHTML=h;
}

// ═══════════════════════ 合并图表 ═══════════════════════

const chartOverlay = {
    id:'chartOverlay',
    beforeDraw(chart) {
        const {ctx,chartArea,scales:{x}}=chart; if(!chartArea) return;
        const {top,bottom,left,right}=chartArea; ctx.save();
        // 选中片段高亮
        if (S.selFrames.size>0) {
            const rng=compressRanges(Array.from(S.selFrames).sort((a,b)=>a-b));
            ctx.fillStyle='rgba(231,76,60,0.12)'; ctx.strokeStyle='rgba(231,76,60,0.35)'; ctx.lineWidth=1;
            for (const [s,e] of rng) {
                const p1=x.getPixelForValue(s-0.4), p2=x.getPixelForValue(e+0.4);
                ctx.fillRect(p1,top,p2-p1,bottom-top); ctx.strokeRect(p1,top,p2-p1,bottom-top);
            }
        }
        // 桥接帧预览 (绿色虚线)
        if (S.bridgePreview && S.bridgePreview.length>0) {
            ctx.strokeStyle='rgba(39,174,96,0.85)'; ctx.lineWidth=2.5; ctx.setLineDash([4,3]);
            for (const f of S.bridgePreview) {
                const px=x.getPixelForValue(f);
                if (px>=left&&px<=right) {
                    ctx.beginPath(); ctx.moveTo(px,top); ctx.lineTo(px,bottom); ctx.stroke();
                }
            }
            ctx.setLineDash([]);
        }
        // 播放位置指示线
        if (S.videoTotalFrames>0&&S.curEpData) {
            const px=x.getPixelForValue(S.videoFrame);
            if (px>=left&&px<=right) {
                ctx.strokeStyle='#e74c3c'; ctx.lineWidth=2; ctx.setLineDash([5,3]);
                ctx.beginPath(); ctx.moveTo(px,top); ctx.lineTo(px,bottom); ctx.stroke(); ctx.setLineDash([]);
            }
        }
        ctx.restore();
    }
};
Chart.register(chartOverlay);

function updateChart(frames) {
    if (!frames||!frames.length) return;
    const labels=frames.map(f=>f.frame_index);
    const joints=Array.from(S.activeJoints);
    buildChart(labels,frames,joints);
}

function buildChart(labels,frames,joints) {
    if (S.chart) { S.chart.destroy(); S.chart=null; }
    const ds=[], mode=S.chartMode, showS=(mode==='both'||mode==='state'), showA=(mode==='both'||mode==='action');

    for (const jn of joints) {
        const ji=S.jointNames.indexOf(jn); if(ji<0) continue;
        const c=COLORS[ji%COLORS.length];
        if (showS) ds.push({
            label: mode==='both'?`${jn} (S)`:jn,
            data: frames.map(f=>{const a=f.state;return(a&&ji<a.length)?a[ji]:null;}),
            borderColor:c, borderWidth:1.5, borderDash:[], pointRadius:0, pointHitRadius:6, tension:0, fill:false,
        });
        if (showA) ds.push({
            label: mode==='both'?`${jn} (A)`:jn,
            data: frames.map(f=>{const a=f.action;return(a&&ji<a.length)?a[ji]:null;}),
            borderColor:c, borderWidth:1, borderDash:[5,3], pointRadius:0, pointHitRadius:6, tension:0, fill:false,
        });
    }

    S.chart = new Chart($('main-chart'), {
        type:'line', data:{labels,datasets:ds},
        options:{
            responsive:true, maintainAspectRatio:false, animation:false,
            interaction:{mode:'index',intersect:false},
            plugins:{
                title:{display:false},
                legend:{display:true,position:'bottom',labels:{font:{size:9},boxWidth:10,padding:6}},
                zoom:{pan:{enabled:true,mode:'x'},zoom:{wheel:{enabled:true},pinch:{enabled:true},mode:'x'}},
            },
            scales:{
                x:{title:{display:true,text:'帧',font:{size:10}},ticks:{maxTicksLimit:20,font:{size:9}}},
                y:{title:{display:true,text:'值',font:{size:10}},ticks:{font:{size:9}}},
            },
            onClick:(_ev,_el,chart)=>{
                const rect=chart.canvas.getBoundingClientRect();
                const mx=_ev.native.clientX-rect.left, fi=Math.round(chart.scales.x.getValueForPixel(mx));
                if (fi<0||fi>=S.videoTotalFrames) return;
                // 同步视频
                videoPause(); S.videoFrame=fi; videoSync();
                // 选段
                if (S.chartClickState===0) {
                    $('fr-from').value=fi; $('fr-to').value=''; S.chartClickState=1;
                    toast(`起始帧: ${fi} — 再点一次选结束帧`,'info');
                } else {
                    const from=parseInt($('fr-from').value), lo=Math.min(from,fi), hi=Math.max(from,fi);
                    $('fr-from').value=lo; $('fr-to').value=hi;
                    for(let i=lo;i<=hi;i++) S.selFrames.add(i);
                    S.chartClickState=0; renderSelRanges();
                    toast(`已添加: 帧${lo}-${hi} (${hi-lo+1}帧)`,'success');
                }
            },
        },
    });
}

function setChartMode(m) {
    S.chartMode=m;
    document.querySelectorAll('.mode-b').forEach(b=>b.classList.toggle('on',b.dataset.mode===m));
    if (S.curEpData) updateChart(S.curEpData.frames);
}

function resetZoom() { if(S.chart) S.chart.resetZoom(); }
function refreshChart() { if(S.chart) S.chart.update('none'); }

// ═══════════════════════ 选择逻辑 ═══════════════════════

function toggleEpSel(i,ck) { ck?S.selEpisodes.add(i):S.selEpisodes.delete(i); syncSelAll(); }
function toggleSelAll(ck) { ck?S.episodes.forEach(e=>S.selEpisodes.add(e.episode_index)):S.selEpisodes.clear(); renderEpList(); }
function syncSelAll() { const cb=$('sel-all'),t=S.episodes.length,s=S.selEpisodes.size; cb.checked=s===t&&t>0; cb.indeterminate=s>0&&s<t; }

function toggleJt(nm,ck) { ck?S.activeJoints.add(nm):S.activeJoints.delete(nm); renderJointSel(); if(S.curEpData)updateChart(S.curEpData.frames); }
function toggleJtGrp(g,ck) { for(const j of(S.jointGroups[g]||[])){ck?S.activeJoints.add(j):S.activeJoints.delete(j);} renderJointSel(); if(S.curEpData)updateChart(S.curEpData.frames); }

function addFrameRange() {
    if (S.uiLocked) return;
    const f=parseInt($('fr-from').value), t=parseInt($('fr-to').value);
    if (isNaN(f)||isNaN(t)||f<0||t<f) return toast('请输入有效范围','error');
    for(let i=f;i<=t;i++) S.selFrames.add(i); S.chartClickState=0; renderSelRanges();
    toast(`已添加: 帧${f}-${t} (${t-f+1}帧)`,'success');
}
function rmRange(s,e) { if (S.uiLocked) return; for(let i=s;i<=e;i++) S.selFrames.delete(i); renderSelRanges(); }
function clearFrameSel() { if (S.uiLocked) return; S.selFrames.clear(); S.chartClickState=0; $('fr-from').value=''; $('fr-to').value=''; renderSelRanges(); }

// ═══════════════════════ 工具 ═══════════════════════

function $(id) { return document.getElementById(id); }
function lockUI(title='处理中', detail='请稍候...') {
    S.uiLocked = true;
    document.body.classList.add('ui-locked');
    const overlay = $('loading-overlay');
    if (!overlay) return;
    $('loading-title').textContent = title;
    $('loading-detail').textContent = detail;
    $('loading-meta').textContent = '';
    overlay.classList.add('show');
    overlay.setAttribute('aria-hidden', 'false');
}
function unlockUI() {
    S.uiLocked = false;
    document.body.classList.remove('ui-locked');
    const overlay = $('loading-overlay');
    if (!overlay) return;
    overlay.classList.remove('show');
    overlay.setAttribute('aria-hidden', 'true');
}
function updateLoadingProgress(progress) {
    if (!progress) return;
    const title = progress.title || '正在处理中';
    const detail = progress.detail || '请稍候...';
    $('loading-title').textContent = title;
    $('loading-detail').textContent = detail;
    if (typeof progress.percent === 'number') {
        $('loading-meta').textContent = `进度 ${progress.percent}%`;
    } else if (progress.current > 0 || progress.total > 0) {
        $('loading-meta').textContent = `进度 ${progress.current || 0}/${progress.total || 0}`;
    } else {
        $('loading-meta').textContent = '';
    }
}
async function pollSaveProgressOnce() {
    try {
        const r = await fetch('/api/save_progress');
        if (!r.ok) return;
        const d = await r.json();
        updateLoadingProgress(d);
    } catch(_e) {}
}
function startSaveProgressPolling() {
    stopSaveProgressPolling();
    pollSaveProgressOnce();
    S.saveProgressTimer = setInterval(pollSaveProgressOnce, 500);
}
function stopSaveProgressPolling() {
    if (S.saveProgressTimer) {
        clearInterval(S.saveProgressTimer);
        S.saveProgressTimer = null;
    }
}
function compressRanges(sorted) {
    if(!sorted.length)return[]; const r=[]; let rs=sorted[0],re=sorted[0];
    for(let i=1;i<sorted.length;i++){sorted[i]===re+1?re=sorted[i]:(r.push([rs,re]),rs=re=sorted[i]);}
    r.push([rs,re]); return r;
}
function toast(m,t='info') { const b=$('tb'),e=document.createElement('div'); e.className=`tt t${t[0]}`; e.textContent=m; b.appendChild(e); setTimeout(()=>e.remove(),3500); }

// ═══════════════════════ 目录浏览器 ═══════════════════════

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

    function escHtml(s) {
        const d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    }

    // Build modal
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';

    const card = document.createElement('div');
    card.className = 'modal-card';
    card.style.width = '600px';

    // Header
    const hdr = document.createElement('div');
    hdr.className = 'modal-hdr';
    hdr.innerHTML = '<h3 style="color:#333;">选择目录</h3>';
    const closeBtn = document.createElement('button');
    closeBtn.className = 'modal-close';
    closeBtn.textContent = '\u00d7';
    closeBtn.addEventListener('click', () => overlay.remove());
    hdr.appendChild(closeBtn);

    // Body
    const body = document.createElement('div');
    body.className = 'modal-body';

    const pathRow = document.createElement('div');
    pathRow.className = 'dir-browser-path';

    const upBtn = document.createElement('button');
    upBtn.textContent = '\u2191 上级';
    upBtn.addEventListener('click', async () => {
        const data = await fetchDirs(currentPath);
        if (data.parent !== undefined && data.parent !== currentPath) {
            render(data.parent);
        }
    });

    const pathInput = document.createElement('input');
    pathInput.value = currentPath;
    pathInput.placeholder = '输入路径后按回车跳转...';
    pathInput.addEventListener('keydown', e => {
        if (e.key === 'Enter') render(pathInput.value.trim());
    });

    pathRow.appendChild(upBtn);
    pathRow.appendChild(pathInput);

    const listEl = document.createElement('div');
    listEl.className = 'dir-list';

    body.appendChild(pathRow);
    body.appendChild(listEl);

    // Footer
    const footer = document.createElement('div');
    footer.className = 'modal-footer';

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'bp';
    cancelBtn.style.cssText = 'background:#999;';
    cancelBtn.textContent = '取消';
    cancelBtn.addEventListener('click', () => overlay.remove());

    const confirmBtn = document.createElement('button');
    confirmBtn.className = 'bp';
    confirmBtn.textContent = '选择此目录';
    confirmBtn.addEventListener('click', () => {
        inputEl.value = currentPath;
        overlay.remove();
    });

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

document.addEventListener('DOMContentLoaded',()=>{
    renderUrdfPanel();
    renderMergeQueue();
    $('btn-load').addEventListener('click',loadDataset);
    $('btn-merge-add').addEventListener('click',addMergeDataset);
    $('btn-save').addEventListener('click',doSave);
    $('btn-browse-ds').addEventListener('click',()=>openDirBrowser('ds-path'));
    $('btn-browse-merge').addEventListener('click',()=>openDirBrowser('merge-path'));
    $('btn-browse-save').addEventListener('click',()=>openDirBrowser('save-path'));
    $('btn-del-ep').addEventListener('click',doDeleteEp);
    $('btn-del-fr').addEventListener('click',doDeleteFrames);
    $('btn-add-rng').addEventListener('click',addFrameRange);
    $('btn-clr-fr').addEventListener('click',clearFrameSel);
    $('btn-urdf-file').addEventListener('click',()=>$('urdf-file-input').click());
    $('btn-urdf-dir').addEventListener('click',()=>$('urdf-dir-input').click());
    $('urdf-source').addEventListener('change',e=>{
        S.urdf.source = e.target.value === 'action' ? 'action' : 'state';
        S.urdf._detectedDegree = null;
        renderUrdfPanel();
        syncUrdfPose();
    });
    $('urdf-deg-mode').addEventListener('change',e=>{
        S.urdf.degreeMode = e.target.value;
        S.urdf._detectedDegree = null;
        syncUrdfPose();
    });
    $('btn-urdf-mapping').addEventListener('click',showJointMappingModal);
    $('btn-urdf-reset').addEventListener('click',resetUrdfCamera);
    $('urdf-file-input').addEventListener('change',async e=>{
        const files = e.target.files;
        if (files?.length) {
            try { await uploadUrdfBundle(files, guessRootUrdf(files)); }
            catch (_e) {}
        }
        e.target.value = '';
    });
    $('urdf-dir-input').addEventListener('change',async e=>{
        const files = e.target.files;
        if (files?.length) {
            try { await uploadUrdfBundle(files, guessRootUrdf(files)); }
            catch (_e) {}
        }
        e.target.value = '';
    });
    $('sel-all').addEventListener('change',e=>toggleSelAll(e.target.checked));
    $('ds-path').addEventListener('keydown',e=>{if(e.key==='Enter')loadDataset();});
    $('merge-path').addEventListener('keydown',e=>{if(e.key==='Enter')addMergeDataset();});
    $('fr-to').addEventListener('keydown',e=>{if(e.key==='Enter')addFrameRange();});
    document.addEventListener('keydown',e=>{
        if (S.uiLocked) {
            e.preventDefault();
            return;
        }
        if(e.target.tagName==='INPUT'||e.target.tagName==='TEXTAREA')return;
        if(!S.curEpData)return;
        switch(e.code){
            case'Space':e.preventDefault();videoToggle();break;
            case'ArrowLeft':e.preventDefault();videoStep(e.shiftKey?-10:-1);break;
            case'ArrowRight':e.preventDefault();videoStep(e.shiftKey?10:1);break;
            case'Home':e.preventDefault();videoStep(-Infinity);break;
            case'End':e.preventDefault();videoStep(Infinity);break;
        }
    });
});

Object.assign(window, {
    viewEpisode,
    videoStep,
    videoToggle,
    onSliderInput,
    toggleEpSel,
    toggleJt,
    toggleJtGrp,
    applyRecommendation,
    forceDeleteAll,
    closeAnalysisModal,
    closeJointMappingModal,
    confirmJointMapping,
    resetAutoMapping,
    showJointMappingModal,
    rmRange,
    setChartMode,
    resetZoom,
});
