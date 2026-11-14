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

    /** 是否为视频文件（ugoira 动图合成后的 webm/mp4） */
    window.isVideoPath = function (p) {
        return /\.(webm|mp4|m4v|mkv)$/i.test(p || '');
    };

    /** 播放失败时的用户可见提示（只提示一次，避免刷屏） */
    function reportMediaError(videoEl, err) {
        var name = (err && err.name) || '';
        var msg;
        if (name === 'NotSupportedError') {
            msg = '当前浏览器不支持该视频编码（WebM/VP9），请换 Chrome/Edge 打开';
        } else if (name === 'NotAllowedError') {
            msg = '浏览器拦截了自动播放，点击画面中的播放按钮即可';
        } else {
            var code = videoEl && videoEl.error ? videoEl.error.code : '';
            msg = '视频播放失败' + (name ? '：' + name : '') + (code ? '（错误码 ' + code + '）' : '');
        }
        if (window.showToast) window.showToast(msg, 'error');
        try { console.warn('[setViewerMedia] 播放失败:', name, videoEl && videoEl.error); } catch (e) {}
    }

    /**
     * 在查看器中显示图片或动图。
     * 动图：隐藏 <img>，用 <video> 自动循环播放；普通图片则相反。
     */
    window.setViewerMedia = function (imgEl, videoEl, filePath, url) {
        if (!imgEl || !videoEl) return;
        if (window.isVideoPath(filePath)) {
            imgEl.style.display = 'none';
            imgEl.removeAttribute('src');
            // 必须显式用 block：CSS 里 video.viewer-media-video 默认 display:none，
            // 设成空字符串会回退到该样式，导致动图大图不可见
            videoEl.style.display = 'block';

            // muted/loop 用属性值设置：仅靠 HTML 属性在动态改 src 后不保证生效，
            // 而 muted=false 会让浏览器按自动播放策略拦截 play()
            videoEl.muted = true;
            videoEl.loop = true;
            videoEl.playsInline = true;
            videoEl.autoplay = true;
            videoEl.preload = 'auto';

            // 同时校验 src：关闭查看器时各页面会 removeAttribute('src')，
            // 只用 data-media-src 判断会导致重开后不再设置 src（黑屏无反应）
            var current = videoEl.getAttribute('data-media-src');
            if (current !== url || !videoEl.getAttribute('src')) {
                videoEl.setAttribute('data-media-src', url);
                videoEl.src = url;
                videoEl.load();
            }

            var playNow = function () {
                try { videoEl.currentTime = 0; } catch (e) {}
                var p = videoEl.play();
                if (p && p.catch) {
                    p.catch(function (err) { reportMediaError(videoEl, err); });
                }
            };
            // 元数据就绪后再播放，避免对空元素 play() 导致立刻中止（表现为"闪一下"）
            if (videoEl.readyState >= 2) {
                playNow();
            } else {
                videoEl.addEventListener('canplay', playNow, { once: true });
            }
        } else {
            try { videoEl.pause(); } catch (e) {}
            videoEl.removeAttribute('src');
            videoEl.removeAttribute('data-media-src');
            videoEl.style.display = 'none';
            imgEl.style.display = '';
            imgEl.src = url;
        }
    };

    /** 复制图片时，动图取封面（xxx.webm.jpg），普通图片取原路径 */
    window.mediaCopyPath = function (p) {
        return window.isVideoPath(p) ? (p + '.jpg') : p;
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
