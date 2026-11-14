# -*- coding: utf-8 -*-
"""
ffmpeg 工具：检测可执行文件 + ugoira 动图合成。

设计原则：
- 未检测到 ffmpeg 时，ugoira 功能整体不可用（不下载、不转换），不影响普通图片。
- 支持在 config.json 中通过 ffmpeg_path 指定路径（留空则自动查找 PATH 与常见安装位置）。
"""
import logging
import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional, Tuple

from config import get_config

logger = logging.getLogger(__name__)

# 检测结果缓存：避免每次下载都跑一次 ffmpeg -version
_cache: Dict[str, Optional[object]] = {
    'checked': False,
    'path': None,
    'version': None,
}

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _candidate_paths() -> List[str]:
    """按优先级返回候选 ffmpeg 路径"""
    cands: List[str] = []

    configured = (getattr(get_config(), 'ffmpeg_path', '') or '').strip().strip('"')
    if configured:
        cands.append(configured)

    which = shutil.which('ffmpeg')
    if which:
        cands.append(which)

    # 常见安装位置（Windows）
    cands.extend([
        r'C:\ffmpeg\bin\ffmpeg.exe',
        os.path.expanduser(r'~\scoop\shims\ffmpeg.exe'),
        os.path.expanduser(r'~\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe'),
    ])
    return cands


def detect(force: bool = False) -> Dict:
    """
    检测 ffmpeg 是否可用。

    Returns:
        {'available': bool, 'path': str | None, 'version': str | None}
    """
    if _cache['checked'] and not force:
        return {
            'available': bool(_cache['path']),
            'path': _cache['path'],
            'version': _cache['version'],
        }

    path: Optional[str] = None
    version: Optional[str] = None
    seen = set()

    for cand in _candidate_paths():
        if not cand:
            continue
        exe = cand
        if os.path.isdir(exe):
            exe = os.path.join(exe, 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
        if exe in seen:
            continue
        seen.add(exe)
        if not os.path.isfile(exe):
            continue
        try:
            proc = subprocess.run(
                [exe, '-version'],
                capture_output=True, text=True, timeout=10,
                creationflags=_NO_WINDOW,
            )
            if proc.returncode == 0:
                first_line = (proc.stdout or '').splitlines()[0] if proc.stdout else ''
                m = re.search(r'ffmpeg version (\S+)', first_line)
                path = exe
                version = m.group(1) if m else first_line[:100]
                break
        except Exception:
            continue

    _cache.update({'checked': True, 'path': path, 'version': version})
    if path:
        logger.info(f"检测到 ffmpeg: {path} ({version})")
    else:
        logger.warning("未检测到 ffmpeg，ugoira 动图功能不可用")
    return {'available': bool(path), 'path': path, 'version': version}


def is_available(force: bool = False) -> bool:
    return bool(detect(force).get('available'))


def reset_cache():
    """配置变更后强制重新检测"""
    _cache.update({'checked': False, 'path': None, 'version': None})


def safe_extract(zip_file, target_dir: str) -> None:
    """解压 zip，阻止路径穿越（zip slip）"""
    base = os.path.abspath(target_dir)
    for member in zip_file.namelist():
        dest = os.path.abspath(os.path.join(base, member))
        if not dest.startswith(base + os.sep) and dest != base:
            continue
        zip_file.extract(member, base)


def make_poster(frame_path: str, poster_path: str) -> bool:
    """把首帧图片转成 JPEG 封面（供列表缩略图/视频封面使用）"""
    try:
        from PIL import Image
        with Image.open(frame_path) as im:
            rgb = im.convert('RGB')
            rgb.save(poster_path, 'JPEG', quality=88)
        return True
    except Exception as e:
        logger.warning(f"生成 ugoira 封面失败 {poster_path}: {e}")
        return False


def extract_poster(video_path: str, poster_path: str, timeout: int = 120) -> bool:
    """
    用 ffmpeg 抽取视频首帧生成 JPEG 封面（供列表缩略图/视频封面使用）。
    适用于非本工具下载、缺少 xxx.webm.jpg 封面的视频文件。

    Returns:
        成功返回 True；ffmpeg 不可用或抽取失败返回 False。
    """
    info = detect()
    exe = info.get('path')
    if not exe:
        logger.warning(f"未检测到 ffmpeg，无法生成视频封面 {video_path}")
        return False

    # 先写临时文件再原子替换，避免并发请求读到半截封面
    tmp_path = poster_path + '.tmp.jpg'
    try:
        os.makedirs(os.path.dirname(poster_path) or '.', exist_ok=True)
        proc = subprocess.run(
            [exe, '-y', '-loglevel', 'error',
             '-i', video_path,
             '-frames:v', '1', '-q:v', '2', tmp_path],
            capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        if proc.returncode != 0 or not os.path.isfile(tmp_path) \
                or os.path.getsize(tmp_path) == 0:
            logger.warning(f"ffmpeg 抽取视频封面失败 {video_path}: "
                           f"{(proc.stderr or '').strip()[-200:]}")
            _cleanup(tmp_path)
            return False
        os.replace(tmp_path, poster_path)
        return True
    except subprocess.TimeoutExpired:
        logger.warning(f"ffmpeg 抽取视频封面超时 {video_path}")
        _cleanup(tmp_path)
        return False
    except Exception as e:
        logger.warning(f"ffmpeg 抽取视频封面异常 {video_path}: {e}")
        _cleanup(tmp_path)
        return False


def encode_mp4(src_path: str, dst_path: str, crf: int = 23,
               timeout: int = 900) -> bool:
    """
    把视频转码成浏览器友好的 H.264 MP4（yuv420p + faststart，无音轨）。

    背景：部分环境（含某些机器的 Chrome）无法解码 VP9 WebM，会报
    MEDIA_ERR_DECODE（表现为"点开播放闪一下就停"），H.264 MP4 兼容性最好。

    Returns:
        成功返回 True；ffmpeg 不可用或转码失败返回 False。
    """
    info = detect()
    exe = info.get('path')
    if not exe:
        logger.warning(f"未检测到 ffmpeg，无法转码 {src_path}")
        return False

    os.makedirs(os.path.dirname(dst_path) or '.', exist_ok=True)
    cmd = [
        exe, '-y', '-loglevel', 'error',
        '-i', src_path,
        '-an',
        # H.264 要求宽高为偶数，奇数尺寸会导致编码失败
        '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2',
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(crf),
        '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart',
        dst_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, creationflags=_NO_WINDOW)
        if proc.returncode != 0 or not os.path.isfile(dst_path) \
                or os.path.getsize(dst_path) == 0:
            logger.warning(f"视频转码失败 {src_path}: "
                           f"{(proc.stderr or '').strip()[-300:]}")
            _cleanup(dst_path)
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning(f"视频转码超时 {src_path}")
        _cleanup(dst_path)
        return False
    except Exception as e:
        logger.warning(f"视频转码异常 {src_path}: {e}")
        _cleanup(dst_path)
        return False


def _cleanup(path: str) -> None:
    """清理临时文件（静默）"""
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def convert_ugoira(frames_dir: str, frames: List[Dict], out_path: str,
                   timeout: int = 600) -> Tuple[bool, str]:
    """
    把 ugoira 帧序列合成为 WebM 动图。

    Args:
        frames_dir: 帧图片所在目录
        frames: [{'file': '000000.jpg', 'delay': 70}, ...]（delay 单位毫秒）
        out_path: 输出 webm 路径

    Returns:
        (成功, 错误信息)
    """
    info = detect()
    exe = info.get('path')
    if not exe:
        return False, '未检测到 ffmpeg'
    if not frames:
        return False, 'ugoira 帧信息为空'

    # 生成 concat 清单：每帧 + 持续时长；最后一帧重复一次避免被丢弃
    list_path = os.path.join(frames_dir, '_ugoira_concat.txt')
    try:
        with open(list_path, 'w', encoding='utf-8') as f:
            for i, fr in enumerate(frames):
                name = str(fr.get('file', '')).replace("'", "'\\''")
                f.write(f"file '{name}'\n")
                if i < len(frames) - 1:
                    delay = max(20, int(fr.get('delay', 100) or 100))
                    f.write(f"duration {delay / 1000.0:.3f}\n")
            last = str(frames[-1].get('file', '')).replace("'", "'\\''")
            f.write(f"file '{last}'\n")
    except Exception as e:
        return False, f'生成帧清单失败: {e}'

    # 帧率模式与编码器组合，逐个尝试（兼容不同 ffmpeg 版本）
    sync_variants = [['-fps_mode', 'vfr'], ['-vsync', 'vfr'], []]
    encoders = [
        ['-c:v', 'libvpx-vp9', '-b:v', '0', '-crf', '32', '-pix_fmt', 'yuv420p'],
        ['-c:v', 'libvpx', '-b:v', '2M', '-pix_fmt', 'yuv420p'],
    ]

    last_err = ''
    for sync_opts in sync_variants:
        for enc in encoders:
            if os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            cmd = [exe, '-y', '-loglevel', 'error',
                   '-f', 'concat', '-safe', '0', '-i', list_path] \
                  + sync_opts + ['-an'] + enc + [out_path]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=timeout, creationflags=_NO_WINDOW)
                if proc.returncode == 0 and os.path.isfile(out_path) \
                        and os.path.getsize(out_path) > 0:
                    return True, ''
                last_err = (proc.stderr or '').strip()[-400:]
            except subprocess.TimeoutExpired:
                last_err = 'ffmpeg 转换超时'
            except Exception as e:
                last_err = str(e)
    return False, last_err or 'ffmpeg 转换失败'
