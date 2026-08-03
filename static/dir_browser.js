/**
 * 通用目录浏览器模块
 *
 * 用法:
 *   1. HTML 中引入: <script src="/static/dir_browser.js"></script>
 *   2. 给路径输入框旁边加一个浏览按钮:
 *      <button onclick="DirBrowser.open('datasetPath')">📁</button>
 *   或者用 data 属性自动绑定:
 *      <input id="datasetPath" ...>
 *      <button class="dir-browse-btn" data-target="datasetPath">浏览</button>
 *      然后调用 DirBrowser.autoBind() 自动绑定所有 .dir-browse-btn
 */
window.DirBrowser = (function () {

    function _escHtml(s) {
        const d = document.createElement('div');
        d.textContent = s == null ? '' : String(s);
        return d.innerHTML;
    }

    function _injectStyles() {
        if (document.getElementById('dir-browser-styles')) return;
        const style = document.createElement('style');
        style.id = 'dir-browser-styles';
        style.textContent = `
.dir-browser-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.45);
    z-index: 10000; display: flex; align-items: center; justify-content: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
.dir-browser-card {
    background: #fff; border-radius: 10px; width: 600px; max-width: 90vw;
    max-height: 80vh; display: flex; flex-direction: column;
    box-shadow: 0 8px 32px rgba(0,0,0,0.2); overflow: hidden;
}
.dir-browser-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 18px; border-bottom: 1px solid #e5e7eb; background: #f9fafb;
}
.dir-browser-header h3 { font-size: 15px; font-weight: 600; color: #1f2937; margin: 0; }
.dir-browser-close {
    background: none; border: none; font-size: 22px; cursor: pointer;
    color: #6b7280; padding: 0 4px; line-height: 1;
}
.dir-browser-close:hover { color: #1f2937; }
.dir-browser-body { flex: 1; overflow-y: auto; padding: 12px 18px; }
.dir-browser-path-row {
    display: flex; gap: 8px; margin-bottom: 12px; align-items: center;
}
.dir-browser-up-btn {
    padding: 6px 14px; border: 1px solid #d1d5db; border-radius: 6px;
    background: #fff; cursor: pointer; font-size: 13px; white-space: nowrap; color: #374151;
}
.dir-browser-up-btn:hover { background: #f3f4f6; }
.dir-browser-path-input {
    flex: 1; padding: 7px 10px; border: 1px solid #d1d5db;
    border-radius: 6px; font-size: 13px;
}
.dir-browser-list { max-height: 360px; overflow-y: auto; }
.dir-browser-item {
    display: flex; align-items: center; gap: 8px; padding: 8px 10px;
    border-radius: 6px; cursor: pointer; font-size: 13px; color: #374151;
}
.dir-browser-item:hover { background: #eff6ff; color: #1d4ed8; }
.dir-browser-item-icon { font-size: 16px; }
.dir-browser-empty {
    text-align: center; padding: 32px; color: #9ca3af; font-size: 13px;
}
.dir-browser-footer {
    display: flex; gap: 10px; justify-content: flex-end;
    padding: 12px 18px; border-top: 1px solid #e5e7eb; background: #f9fafb;
}
.dir-browser-btn {
    padding: 8px 20px; border: none; border-radius: 6px;
    font-size: 13px; cursor: pointer; font-weight: 500;
}
.dir-browser-btn-cancel { background: #e5e7eb; color: #374151; }
.dir-browser-btn-cancel:hover { background: #d1d5db; }
.dir-browser-btn-confirm { background: #2563eb; color: #fff; }
.dir-browser-btn-confirm:hover { background: #1d4ed8; }
        `;
        document.head.appendChild(style);
    }

    async function _fetchDirs(path) {
        const params = path ? `?path=${encodeURIComponent(path)}` : '';
        const resp = await fetch(`/api/browse${params}`);
        return resp.json();
    }

    /**
     * 打开目录浏览器弹窗。
     * @param {string} targetInputId - 目标 input 元素的 id
     */
    async function open(targetInputId) {
        const inputEl = document.getElementById(targetInputId);
        if (!inputEl) {
            console.error(`DirBrowser: 找不到目标 input #${targetInputId}`);
            return;
        }

        _injectStyles();

        let currentPath = (inputEl.value || '').trim();
        const overlay = document.createElement('div');
        overlay.className = 'dir-browser-overlay';

        const card = document.createElement('div');
        card.className = 'dir-browser-card';

        // Header
        const header = document.createElement('div');
        header.className = 'dir-browser-header';
        header.innerHTML = '<h3>\ud83d\udcc1 选择目录</h3>';
        const closeBtn = document.createElement('button');
        closeBtn.className = 'dir-browser-close';
        closeBtn.innerHTML = '&times;';
        closeBtn.addEventListener('click', () => overlay.remove());
        header.appendChild(closeBtn);

        // Body
        const body = document.createElement('div');
        body.className = 'dir-browser-body';

        const pathRow = document.createElement('div');
        pathRow.className = 'dir-browser-path-row';

        const upBtn = document.createElement('button');
        upBtn.className = 'dir-browser-up-btn';
        upBtn.textContent = '\u2191 \u4e0a\u7ea7';
        upBtn.addEventListener('click', async () => {
            const data = await _fetchDirs(currentPath);
            if (data.parent !== undefined && data.parent !== currentPath) {
                await render(data.parent || '');
            }
        });

        const pathInput = document.createElement('input');
        pathInput.className = 'dir-browser-path-input';
        pathInput.value = currentPath;
        pathInput.placeholder = '\u8f93\u5165\u8def\u5f84\u540e\u6309\u56de\u8f66\u8df3\u8f6c...';
        pathInput.addEventListener('keydown', e => {
            if (e.key === 'Enter') render(pathInput.value.trim());
        });

        pathRow.appendChild(upBtn);
        pathRow.appendChild(pathInput);

        const listEl = document.createElement('div');
        listEl.className = 'dir-browser-list';

        body.appendChild(pathRow);
        body.appendChild(listEl);

        // Footer
        const footer = document.createElement('div');
        footer.className = 'dir-browser-footer';

        const cancelBtn = document.createElement('button');
        cancelBtn.className = 'dir-browser-btn dir-browser-btn-cancel';
        cancelBtn.textContent = '\u53d6\u6d88';
        cancelBtn.addEventListener('click', () => overlay.remove());

        const confirmBtn = document.createElement('button');
        confirmBtn.className = 'dir-browser-btn dir-browser-btn-confirm';
        confirmBtn.textContent = '\u9009\u62e9\u6b64\u76ee\u5f55';
        confirmBtn.addEventListener('click', () => {
            inputEl.value = currentPath;
            overlay.remove();
        });

        footer.appendChild(cancelBtn);
        footer.appendChild(confirmBtn);

        card.appendChild(header);
        card.appendChild(body);
        card.appendChild(footer);
        overlay.appendChild(card);

        overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
        document.body.appendChild(overlay);

        async function render(path) {
            const data = await _fetchDirs(path);
            if (data.error) {
                listEl.innerHTML = `<div class="dir-browser-empty">\u26a0\ufe0f ${_escHtml(data.error)}</div>`;
                if (path) {
                    // 尝试回退到根
                    const rootData = await _fetchDirs('');
                    if (!rootData.error) return render('');
                }
                return;
            }
            currentPath = data.current || '';
            pathInput.value = currentPath;

            listEl.innerHTML = '';
            if (!data.dirs || !data.dirs.length) {
                listEl.innerHTML = '<div class="dir-browser-empty">\u6b64\u76ee\u5f55\u4e0b\u65e0\u5b50\u76ee\u5f55</div>';
                return;
            }
            for (const d of data.dirs) {
                const item = document.createElement('div');
                item.className = 'dir-browser-item';
                item.innerHTML = `<span class="dir-browser-item-icon">\ud83d\udcc1</span><span>${_escHtml(d.name)}</span>`;
                item.addEventListener('click', () => render(d.path));
                listEl.appendChild(item);
            }
        }

        await render(currentPath);
    }

    /**
     * 自动绑定所有带 class="dir-browse-btn" 且有 data-target 属性的按钮。
     * 在 DOMContentLoaded 后调用一次即可。
     */
    function autoBind() {
        document.querySelectorAll('.dir-browse-btn').forEach(btn => {
            const target = btn.getAttribute('data-target');
            if (target && !btn._dirBrowserBound) {
                btn.addEventListener('click', () => open(target));
                btn._dirBrowserBound = true;
            }
        });
    }

    // 自动在 DOMContentLoaded 时绑定
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', autoBind);
    } else {
        autoBind();
    }

    return { open, autoBind };
})();
