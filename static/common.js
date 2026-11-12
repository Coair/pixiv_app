/**
 * 公共脚本：
 * 1. isLocalHost() —— 判断是否本机访问（各页面共用，避免重复定义）
 * 2. 自动填充侧边栏底部统计 #navFooter（各页面引入即可，无需各自实现）
 */
(function () {
    window.isLocalHost = function () {
        var h = location.hostname;
        return h === 'localhost' || h === '127.0.0.1' || h === '::1' || h === '[::1]';
    };

    /**
     * 轻量非阻塞提示（各页面通用）
     * 页面若自己定义了 showToast 会覆盖本实现（不影响既有页面）
     */
    window.showToast = function (message, type) {
        try {
            var el = document.createElement('div');
            el.textContent = message;
            var bg = type === 'error' ? 'rgba(240,67,81,0.95)' : 'rgba(0,181,120,0.95)';
            el.style.cssText = 'position:fixed;left:50%;bottom:80px;transform:translateX(-50%);'
                + 'background:' + bg + ';color:#fff;padding:10px 18px;border-radius:10px;'
                + 'font-size:13px;z-index:9999;box-shadow:0 6px 20px rgba(0,0,0,0.35);'
                + 'opacity:0;transition:opacity .25s;max-width:80vw;text-align:center;';
            document.body.appendChild(el);
            requestAnimationFrame(function () { el.style.opacity = '1'; });
            setTimeout(function () {
                el.style.opacity = '0';
                setTimeout(function () { el.remove(); }, 300);
            }, 2200);
        } catch (e) { /* 忽略提示失败 */ }
    };

    function fillFooter() {
        var el = document.getElementById('navFooter');
        if (!el) return;
        fetch('/api/stats')
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d && d.success && d.data) {
                    el.textContent = '📦 ' + d.data.artists + ' 作者 · ' + d.data.works + ' 作品';
                }
            })
            .catch(function () {});
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', fillFooter);
    } else {
        fillFooter();
    }
})();
