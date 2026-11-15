# -*- coding: utf-8 -*-
"""
Pixiv Downloader - Flask Web 界面
提供可视化的 Pixiv 图片下载管理界面
支持任务队列、暂停/取消/恢复、断线重连恢复进度
"""
import os
import sys
import json
import gzip
import time
import queue
import subprocess
import threading
import logging
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List

from flask import (
    Flask, render_template, jsonify, request, Response,
    send_from_directory, stream_with_context
)
import urllib.parse

# 添加当前目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows 控制台 GBK 下，打印含 emoji 的目录/文件名会崩溃，统一 UTF-8 容错
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from config import get_config, Config
from database import Database
from api import PixivAPI
from scanner import FileScanner
from downloader import Downloader
from cookie_util import parse_cookie_file
import ffmpeg_util

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.after_request
def _gzip_response(resp):
    """对文本文档/JSON 响应做 gzip 压缩（图片等已压缩资源跳过；SSE 不处理）"""
    try:
        if resp.direct_passthrough or not (200 <= resp.status_code < 300):
            return resp
        if resp.headers.get('Content-Encoding'):
            return resp
        if 'gzip' not in request.headers.get('Accept-Encoding', '').lower():
            return resp
        if resp.mimetype not in ('application/json', 'text/html', 'text/css',
                                 'application/javascript', 'text/javascript', 'text/plain'):
            return resp
        data = resp.get_data()
        if len(data) < 1024:
            return resp
        compressed = gzip.compress(data, compresslevel=6)
        if len(compressed) >= len(data):
            return resp
        resp.set_data(compressed)
        resp.headers['Content-Encoding'] = 'gzip'
        resp.headers['Content-Length'] = str(len(compressed))
        resp.headers['Vary'] = 'Accept-Encoding'
    except Exception:
        pass
    return resp


# 全局服务实例
db = None
api_client = None
downloader = None
scanner = None

# SSE 事件队列
sse_queues = {}

# 最大日志保留条数
MAX_LOG_LINES = 200


_services_lock = threading.RLock()


def init_services():
    """初始化所有服务"""
    global db, api_client, downloader, scanner
    config = get_config()
    db = Database(config.db_path)
    api_client = PixivAPI(config.cookie, config.user_agent)
    downloader = Downloader(db, api_client, config.download_path)
    scanner = FileScanner(db, config.download_path)


def get_or_init_services():
    """获取或初始化服务（加锁，避免多线程并发重复初始化）"""
    global db
    if db is None:
        with _services_lock:
            if db is None:
                init_services()
    return db, api_client, downloader, scanner


def reload_services():
    """配置变更后重建所有服务实例"""
    global db, api_client, downloader, scanner
    with _services_lock:
        db = None
        api_client = None
        downloader = None
        scanner = None
        init_services()


def emit_sse(task_id, event_type, data):
    """向指定任务发送 SSE 事件（队列可能已随任务清理，做安全取值）"""
    q = sse_queues.get(task_id)
    if q is None:
        return
    try:
        q.put({
            'event': event_type,
            'data': json.dumps(data, ensure_ascii=False)
        })
    except Exception:
        pass


# ============ 任务状态枚举 ============

class TaskStatus(Enum):
    PENDING = "pending"       # 排队中
    RUNNING = "running"       # 进行中
    PAUSED = "paused"         # 已暂停
    COMPLETED = "completed"   # 已完成
    FAILED = "failed"         # 失败
    CANCELLED = "cancelled"   # 已取消


# ============ 任务管理器 ============

class TaskInfo:
    """单个任务的详细信息"""
    def __init__(self, task_id: str, task_type: str, params: dict):
        self.task_id = task_id
        self.task_type = task_type      # work / artist / artist-new / bookmarks / url / sync
        self.params = params            # 原始请求参数
        self.status = TaskStatus.PENDING
        self.progress_current = 0
        self.progress_total = 0
        self.progress_message = ""
        self.progress_work_id = ""
        self.logs: List[dict] = []      # [{'time': str, 'type': str, 'message': str}]
        self.result: Optional[dict] = None
        self.extra_info: dict = {}      # 附加信息（如 artist_info, works_loaded 等）
        self.created_at = datetime.now().isoformat()
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self._thread: Optional[threading.Thread] = None

    def add_log(self, msg_type: str, message: str):
        """添加日志（保留最近 MAX_LOG_LINES 条）"""
        self.logs.append({
            'time': datetime.now().strftime('%H:%M:%S'),
            'type': msg_type,
            'message': message
        })
        if len(self.logs) > MAX_LOG_LINES:
            self.logs = self.logs[-MAX_LOG_LINES:]

    def to_dict(self) -> dict:
        """序列化为前端需要的格式"""
        d = {
            'task_id': self.task_id,
            'task_type': self.task_type,
            'status': self.status.value,
            'progress': {
                'current': self.progress_current,
                'total': self.progress_total,
                'message': self.progress_message,
                'work_id': self.progress_work_id,
            },
            'extra_info': self.extra_info,
            'result': self.result,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
        }
        return d

    def to_status_dict(self) -> dict:
        """返回给 /api/task/<id>/status 的完整状态（含日志）"""
        d = self.to_dict()
        d['logs'] = self.logs
        d['params'] = self.params
        return d


class TaskManager:
    """统一的任务管理器：队列、调度、状态追踪（支持持久化到 SQLite）"""

    def __init__(self):
        self._lock = threading.Lock()
        self._queue: List[TaskInfo] = []          # 排队中的任务
        self._current_task: Optional[TaskInfo] = None
        # 正在被线程执行的任务：暂停时 _current_task 会被清空，但执行线程仍在跑，
        # 因此单独记录，用于调度互斥和取消时通知下载线程
        self._active_task: Optional[TaskInfo] = None
        # 执行线程实际使用的下载器实例（reload_services 会重建全局 downloader，
        # 必须用任务自己那份实例，否则取消/暂停标记设到了新对象上，线程收不到）
        self._active_dl = None
        self._task_registry: Dict[str, TaskInfo] = {}  # 所有任务（包括已完成的，保留一段时间）
        self._scheduler_thread: Optional[threading.Thread] = None
        self._running = False
        self._paused = False                      # 全局暂停标记：True 时调度器不再取新任务
        self._completed_cleanup_sec = 300  # 已完成任务保留 5 分钟
        self._db = None  # Database 实例，start() 时注入

    def start(self, db_instance=None):
        """启动调度线程，注入数据库实例并从 DB 恢复未完成任务"""
        if db_instance is not None:
            self._db = db_instance
        found_tasks = False
        if not self._running and not self._task_registry:
            found_tasks = self._restore_tasks()
        self._running = True
        self._ensure_scheduler()
        return found_tasks

    def _ensure_scheduler(self):
        """确保调度线程存活（线程意外退出时自动重建，避免任务永久卡在 pending）"""
        with self._lock:
            if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
                return
            self._running = True
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop, daemon=True, name='task-scheduler')
            self._scheduler_thread.start()
            logger.info('[TaskManager] 调度线程已启动')

    def _restore_tasks(self):
        """从数据库恢复未完成的任务（pending/running/paused）"""
        if not self._db:
            return
        try:
            persisted = self._db.get_persisted_tasks()
            for p in persisted:
                if p['status'] in ('completed', 'failed', 'cancelled'):
                    continue
                task_id = p['task_id']
                task_type = p['task_type']
                params = p.get('params', {})
                # 恢复 TaskInfo
                task = TaskInfo(task_id, task_type, params)
                task.progress_current = p.get('progress_current', 0)
                task.progress_total = p.get('progress_total', 0)
                task.progress_message = p.get('progress_message', '')
                task.progress_work_id = p.get('progress_work_id', '')
                task.extra_info = p.get('extra_info', {})
                task.created_at = p.get('created_at', task.created_at)
                task.started_at = p.get('started_at')
                task.finished_at = p.get('finished_at')

                status = p['status']
                if status == 'running':
                    # 重启前正在执行的任务 → 重新排队（当作 pending 处理）
                    task.status = TaskStatus.PENDING
                    task.add_log('info', '🔄 任务从断点恢复（重启前正在执行）')
                    self._queue.append(task)
                    # 同步更新 DB 状态
                    self._db.update_task_status(task_id, 'pending')
                elif status == 'paused':
                    # 暂停的任务保持暂停状态，并设置全局暂停标记
                    task.status = TaskStatus.PAUSED
                    task.add_log('info', '⏸ 任务保持暂停（重启前已暂停）')
                    self._paused = True
                elif status == 'pending':
                    task.status = TaskStatus.PENDING
                    task.add_log('info', '📋 任务恢复排队（重启前排队中）')
                    self._queue.append(task)
                else:
                    continue

                self._task_registry[task_id] = task

            if self._task_registry:
                logger.info(f"[TaskManager] 从数据库恢复了 {len(self._task_registry)} 个未完成任务")

        except Exception as e:
            logger.error(f"[TaskManager] 恢复任务失败: {e}")
        return bool(self._task_registry)

    def _heal_stuck_state(self):
        """
        自愈：修复会导致队列永久卡死的残留状态（必须在持有 _lock 时调用）。

        1. _active_task 已是终态却未清空 → 清空，否则调度器永远不取新任务
        2. _paused 为 True 但已没有任何暂停中的任务 → 解除全局暂停
        """
        dl = self._active_dl or downloader
        if self._active_task is not None and self._active_task.status in (
            TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED
        ):
            self._active_task = None
            self._active_dl = None
        if self._active_task is None and self._current_task is not None and \
                self._current_task.status in (
                    TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED):
            self._current_task = None

        if self._paused:
            has_paused = any(
                t.status == TaskStatus.PAUSED for t in self._task_registry.values())
            if not has_paused:
                logger.warning('[TaskManager] 检测到残留的全局暂停标记（无暂停任务），已自动解除')
                self._paused = False
                if dl:
                    dl.paused = False

    def _scheduler_loop(self):
        """调度循环：从队列取任务执行（支持全局暂停）"""
        while self._running:
            task = None
            with self._lock:
                self._heal_stuck_state()
                # 有任务正在执行时不取新任务（_active_task 覆盖暂停后线程仍在跑的情况）
                if not self._paused and self._active_task is None and self._queue:
                    # 跳过队列中已取消/已完成的任务（清理残余）
                    while self._queue:
                        candidate = self._queue[0]
                        if candidate.status in (TaskStatus.CANCELLED, TaskStatus.COMPLETED, TaskStatus.FAILED):
                            self._queue.pop(0)
                            continue
                        break
                    if self._queue:
                        task = self._queue.pop(0)
                        task.status = TaskStatus.RUNNING
                        task.started_at = datetime.now().isoformat()
                        self._current_task = task
                        self._active_task = task

            if task:
                self._persist_task_status(task)  # 标记为 running
                try:
                    self._execute_task(task)
                except Exception as e:
                    logger.error(f"任务执行异常 {task.task_id}: {e}")
                    with self._lock:
                        if task.status not in (TaskStatus.CANCELLED, TaskStatus.COMPLETED, TaskStatus.FAILED):
                            task.status = TaskStatus.FAILED
                            task.add_log('error', f'执行异常: {str(e)}')
                        task.finished_at = datetime.now().isoformat()
                        self._persist_task_status(task)
                        if self._current_task == task:
                            self._current_task = None
                finally:
                    # 执行线程结束后必须释放，否则后续任务永远排不上
                    with self._lock:
                        if self._active_task is task:
                            self._active_task = None
                            self._active_dl = None
                        if self._current_task is task:
                            self._current_task = None

            # 清理过期的已完成任务
            self._cleanup_completed()
            time.sleep(0.5)

    def _download_work_list(self, task, dl, work_ids, force=False):
        """
        公共方法：遍历 work_ids 列表逐一下载。
        供 artist 和 url(artist) 分支复用，消除 ~40 行重复的遍历逻辑。
        """
        stats = {'downloaded_works': 0, 'skipped_works': 0, 'failed_works': 0, 'total_images': 0}

        # 批量AI检测（下载前对全量作品做一次检测）
        batch_filtered = dl._detect_batch_ai(work_ids)
        if batch_filtered:
            task.add_log('info', f'批量检测发现 {len(batch_filtered)} 个疑似AI批量作品，将自动跳过')

        # force 模式预取作品信息与图片数，避免循环内逐作品查库/发 API
        prefetched_works = {}
        prefetched_counts = {}
        if force and work_ids:
            num_ids = [int(w) for w in work_ids]
            prefetched_works = dl.db.get_works_by_ids(num_ids)
            prefetched_counts = dl.db.get_work_image_counts(num_ids)

        for i, wid in enumerate(work_ids):
            if dl.cancelled:
                task.add_log('info', '任务已取消')
                break
            if dl.paused:
                task.add_log('info', '任务已暂停')
                if not dl.check_pause():
                    task.add_log('info', '任务已取消')
                    break
                task.add_log('info', '任务已恢复')

            task.progress_work_id = str(wid)
            task.progress_current = i + 1
            task.progress_total = len(work_ids)
            task.progress_message = f'{i + 1}/{len(work_ids)}'
            emit_sse(task.task_id, 'progress', {
                'work_id': str(wid),
                'current': i + 1,
                'total': len(work_ids),
                'message': f'{i + 1}/{len(work_ids)}',
            })

            # force 模式下的已存在检查（用预取数据，0 次查库/API）
            if force:
                work = prefetched_works.get(int(wid))
                if work and work.page_count and \
                        prefetched_counts.get(int(wid), 0) >= work.page_count:
                    stats['skipped_works'] += 1
                    task.add_log('info', f'[{i+1}/{len(work_ids)}] 作品 {wid} 已存在，跳过')
                    continue

            result = dl.download_single_work(wid, skip_existing=not force, batch_filtered=batch_filtered)
            if result.downloaded > 0:
                stats['downloaded_works'] += 1
                stats['total_images'] += result.downloaded
                task.add_log('success', f'[{i+1}/{len(work_ids)}] 作品 {wid} 下载成功 ({result.downloaded}张)')
            elif result.skipped > 0:
                stats['skipped_works'] += 1
                task.add_log('info', f'[{i+1}/{len(work_ids)}] 作品 {wid} 跳过')
            elif result.error_message and '已过滤' in str(result.error_message):
                # 被AI过滤跳过，不算失败
                stats['skipped_works'] += 1
                task.add_log('info', f'[{i+1}/{len(work_ids)}] 作品 {wid} {result.error_message}')
            else:
                stats['failed_works'] += 1
                task.add_log('error', f'[{i+1}/{len(work_ids)}] 作品 {wid} 失败: {result.error_message or "未知错误"}')
            dl.api.wait()

        return stats

    def _execute_task(self, task: TaskInfo):
        """执行具体任务"""
        _, api_obj, dl, scan_obj = get_or_init_services()
        # 记录本执行线程使用的下载器实例，暂停/取消时对同一实例下标记
        self._active_dl = dl
        logger.info(f'[task] {task.task_id} 开始执行 (dl={id(dl)})')

        # 重置 downloader 控制标记
        dl.reset_control_flags()

        def progress_cb(wid, cur, total, msg):
            task.progress_work_id = str(wid) if wid else ''
            task.progress_current = cur
            task.progress_total = total
            task.progress_message = msg
            emit_sse(task.task_id, 'progress', {
                'work_id': task.progress_work_id,
                'current': cur,
                'total': total,
                'message': msg,
            })
            task.add_log('progress', f'[{cur}/{total}] {msg}')

        task_type = task.task_type
        params = task.params

        try:
            if task_type == 'work':
                # 单个作品下载：用户手动指定ID，跳过AI过滤
                result = dl.download_single_work(
                    params['work_id'],
                    progress_callback=progress_cb,
                    skip_existing=not params.get('force', False),
                    skip_ai_filter=True
                )
                task.result = {
                    'work_id': params['work_id'],
                    'downloaded': result.downloaded,
                    'skipped': result.skipped,
                    'failed': result.failed,
                    'error_message': result.error_message,
                }
                emit_sse(task.task_id, 'complete', task.result)

                if dl.cancelled:
                    task.status = TaskStatus.CANCELLED
                    task.add_log('info', '任务已取消')
                elif not result.success:
                    task.status = TaskStatus.FAILED
                    task.add_log('error', f'下载失败: {result.error_message or "未知错误"}')
                elif result.downloaded == 0 and result.failed == 0:
                    task.status = TaskStatus.COMPLETED
                    task.add_log('info', f'下载完成: 全部跳过 ({result.skipped} 个文件已存在)')
                else:
                    task.status = TaskStatus.COMPLETED
                    task.add_log('success', f'下载完成: 成功{result.downloaded}, 跳过{result.skipped}, 失败{result.failed}')

            elif task_type == 'artist':
                artist_id = params['artist_id']
                limit = params.get('limit')
                force = params.get('force', False)

                task.add_log('info', '获取作者信息...')
                artist_info = api_obj.get_artist_info(str(artist_id))
                if not artist_info:
                    task.add_log('error', '无法获取作者信息')
                    task.status = TaskStatus.FAILED
                    emit_sse(task.task_id, 'error', {'error': '无法获取作者信息'})
                    return

                artist_name = artist_info.get('name', f'artist_{artist_id}')
                task.extra_info = {'artist_id': artist_id, 'artist_name': artist_name}
                emit_sse(task.task_id, 'artist_info', task.extra_info)

                task.add_log('info', '获取作品列表...')
                work_ids = api_obj.get_artist_all_works(str(artist_id))
                if not work_ids:
                    task.add_log('error', '作者没有作品或获取失败')
                    task.status = TaskStatus.FAILED
                    emit_sse(task.task_id, 'error', {'error': '作者没有作品或获取失败'})
                    return

                total_works = len(work_ids)
                if limit:
                    work_ids = work_ids[:limit]

                task.extra_info['total'] = total_works
                task.extra_info['to_download'] = len(work_ids)
                emit_sse(task.task_id, 'works_loaded', {
                    'total': total_works,
                    'to_download': len(work_ids),
                })

                stats = self._download_work_list(task, dl, work_ids, force=force)

                task.result = stats
                emit_sse(task.task_id, 'complete', stats)
                task.add_log('success', f'全部完成: 成功{stats["downloaded_works"]}, 跳过{stats["skipped_works"]}, 失败{stats["failed_works"]}')
                if dl.cancelled:
                    task.status = TaskStatus.CANCELLED
                else:
                    task.status = TaskStatus.COMPLETED

            elif task_type == 'artist-new':
                artist_id = params['artist_id']
                force = params.get('force', False)

                stats = dl.download_artist_new_works(
                    artist_id,
                    progress_callback=progress_cb,
                    skip_existing=not force
                )
                task.extra_info = {
                    'total_works': stats['total_works'],
                    'new_works': stats['new_works'],
                }
                emit_sse(task.task_id, 'incremental_info', task.extra_info)
                task.result = {
                    'downloaded_works': stats['downloaded_works'],
                    'skipped_works': stats['skipped_works'],
                    'failed_works': stats['failed_works'],
                    'total_images': stats['total_images'],
                }
                emit_sse(task.task_id, 'complete', task.result)
                task.add_log('success', f'增量下载完成: 成功{stats["downloaded_works"]}, 跳过{stats["skipped_works"]}, 失败{stats["failed_works"]}')
                if dl.cancelled:
                    task.status = TaskStatus.CANCELLED
                else:
                    task.status = TaskStatus.COMPLETED

            elif task_type == 'bookmarks':
                rest = params.get('rest', 'show')
                limit = params.get('limit')
                max_pages = params.get('max_pages')
                force = params.get('force', False)

                stats = dl.download_bookmarks(
                    rest=rest,
                    limit=limit,
                    max_pages=max_pages,
                    progress_callback=progress_cb,
                    skip_existing=not force
                )
                task.extra_info = {
                    'rest_type': stats['rest_type'],
                    'total_bookmarks': stats['total_bookmarks'],
                }
                emit_sse(task.task_id, 'bookmark_info', task.extra_info)
                task.result = {
                    'downloaded_works': stats['downloaded_works'],
                    'skipped_works': stats['skipped_works'],
                    'failed_works': stats['failed_works'],
                    'total_images': stats['total_images'],
                }
                emit_sse(task.task_id, 'complete', task.result)
                task.add_log('success', f'收藏下载完成: 成功{stats["downloaded_works"]}, 跳过{stats["skipped_works"]}, 失败{stats["failed_works"]}')
                if dl.cancelled:
                    task.status = TaskStatus.CANCELLED
                else:
                    task.status = TaskStatus.COMPLETED

            elif task_type == 'url':
                url = params['url']
                url_type, target_id = api_obj.parse_url(url)

                if url_type == 'work':
                    task.extra_info = {'type': 'work', 'id': target_id}
                    emit_sse(task.task_id, 'url_parsed', task.extra_info)
                    # URL下载作品：用户手动粘贴链接，跳过AI过滤
                    result = dl.download_single_work(target_id, progress_callback=progress_cb, skip_ai_filter=True)
                    task.result = {
                        'work_id': target_id,
                        'downloaded': result.downloaded,
                        'skipped': result.skipped,
                        'failed': result.failed,
                        'error_message': result.error_message,
                    }
                    emit_sse(task.task_id, 'complete', task.result)
                    if not result.success:
                        task.add_log('error', f'下载失败: {result.error_message or "未知错误"}')
                    elif result.downloaded == 0 and result.failed == 0:
                        task.add_log('info', f'下载完成: 全部跳过 ({result.skipped} 个文件已存在)')
                    else:
                        task.add_log('success', f'下载完成: 成功{result.downloaded}, 跳过{result.skipped}, 失败{result.failed}')

                elif url_type == 'artist':
                    task.extra_info = {'type': 'artist', 'id': target_id}
                    emit_sse(task.task_id, 'url_parsed', task.extra_info)
                    artist_info = api_obj.get_artist_info(target_id)
                    if not artist_info:
                        task.add_log('error', '无法获取作者信息')
                        task.status = TaskStatus.FAILED
                        emit_sse(task.task_id, 'error', {'error': '无法获取作者信息'})
                        return
                    work_ids = api_obj.get_artist_all_works(target_id)
                    task.extra_info['total'] = len(work_ids)
                    emit_sse(task.task_id, 'works_loaded', {'total': len(work_ids)})

                    stats = self._download_work_list(task, dl, work_ids, force=False)

                    task.result = stats
                    emit_sse(task.task_id, 'complete', stats)
                else:
                    task.add_log('error', '无法识别的URL')
                    task.status = TaskStatus.FAILED
                    emit_sse(task.task_id, 'error', {'error': '无法识别的URL'})
                    return

                if dl.cancelled:
                    task.status = TaskStatus.CANCELLED
                elif url_type == 'work':
                    # work 分支已在上面单独设置了 task.add_log，这里根据 result 设置状态
                    if not result.success:
                        task.status = TaskStatus.FAILED
                    else:
                        task.status = TaskStatus.COMPLETED
                else:
                    task.status = TaskStatus.COMPLETED

            elif task_type == 'tags':
                # 标签补全任务：为缺少 tag 的作品从 Pixiv API 获取标签
                task.add_log('info', '正在查询缺少标签的作品...')
                total_without_tags = db.get_work_count_without_tags()
                task.add_log('info', f'共 {total_without_tags} 个作品缺少标签')

                if total_without_tags == 0:
                    task.add_log('success', '所有作品已有标签，无需补全')
                    task.result = {'synced': 0, 'failed': 0, 'total_remaining': 0}
                    emit_sse(task.task_id, 'complete', task.result)
                    task.status = TaskStatus.COMPLETED
                    return

                synced = 0
                failed = 0
                batch_size = 100  # 每批获取100个作品ID
                delay = params.get('delay', 1.5)  # API请求间隔

                offset = 0
                while True:
                    if dl.cancelled:
                        task.add_log('info', '任务已取消')
                        break
                    if dl.paused:
                        task.add_log('info', '任务已暂停')
                        if not dl.check_pause():
                            task.add_log('info', '任务已取消')
                            break
                        task.add_log('info', '任务已恢复')

                    # 每次重新查询缺少 tag 的作品（支持断点续传）
                    work_ids = db.get_works_without_tags(limit=batch_size, offset=0)
                    if not work_ids:
                        break

                    for i, wid in enumerate(work_ids):
                        if dl.cancelled:
                            break
                        if dl.paused:
                            task.add_log('info', '任务已暂停')
                            if not dl.check_pause():
                                task.add_log('info', '任务已取消')
                                break
                            task.add_log('info', '任务已恢复')

                        task.progress_work_id = str(wid)
                        task.progress_current = synced + failed + 1
                        task.progress_total = total_without_tags
                        task.progress_message = f'标签补全: {synced + failed + 1}/{total_without_tags}'
                        emit_sse(task.task_id, 'progress', {
                            'work_id': str(wid),
                            'current': task.progress_current,
                            'total': task.progress_total,
                            'message': task.progress_message,
                        })

                        max_retries = 3
                        for retry in range(max_retries):
                            try:
                                info, status_code = api_obj.get_work_info(str(wid))
                                if info and info.get('tags'):
                                    db.set_work_tags(wid, info['tags'])
                                    synced += 1
                                    task.add_log('success', f'[{synced+failed}/{total_without_tags}] 作品 {wid} 获取 {len(info["tags"])} 个标签')
                                    # 如果之前被标记为 deleted，自动恢复
                                    if db.is_work_deleted(wid):
                                        db.mark_work_restored(wid)
                                        task.add_log('info', f'作品 {wid} 已恢复（之前被标记为已删除）')
                                    break
                                elif status_code == 404:
                                    # 404: 作品在 Pixiv 上已被删除
                                    if retry < max_retries - 1:
                                        task.add_log('warn', f'作品 {wid} 返回404，重试 {retry + 2}/{max_retries}...')
                                        time.sleep(delay)
                                    else:
                                        db.mark_work_deleted(wid)
                                        failed += 1
                                        task.add_log('warn', f'[{synced+failed}/{total_without_tags}] 作品 {wid} 已从 Pixiv 删除，标记并跳过')
                                        break
                                else:
                                    # 其他失败（无tags、网络错误等）
                                    if retry < max_retries - 1:
                                        task.add_log('warn', f'作品 {wid} 获取失败，重试 {retry + 2}/{max_retries}...')
                                        time.sleep(delay)
                                    else:
                                        failed += 1
                                        task.add_log('warn', f'[{synced+failed}/{total_without_tags}] 作品 {wid} 获取失败（已重试{max_retries}次）')
                                        break
                            except Exception as e:
                                if retry < max_retries - 1:
                                    task.add_log('warn', f'作品 {wid} 异常，重试 {retry + 2}/{max_retries}: {str(e)}')
                                    time.sleep(delay)
                                else:
                                    failed += 1
                                    task.add_log('error', f'[{synced+failed}/{total_without_tags}] 作品 {wid} 出错: {str(e)}')
                                    break

                        time.sleep(delay)

                remaining = db.get_work_count_without_tags()
                task.result = {
                    'synced': synced,
                    'failed': failed,
                    'total_remaining': remaining,
                }
                emit_sse(task.task_id, 'complete', task.result)
                if synced > 0:
                    task.add_log('success', f'标签补全完成: 成功 {synced}, 失败 {failed}, 剩余 {remaining}')
                if dl.cancelled:
                    task.status = TaskStatus.CANCELLED
                    task.add_log('info', '任务已取消（已获取的标签已保存）')
                else:
                    task.status = TaskStatus.COMPLETED

            elif task_type == 'bookmark-refill':
                work_ids = params.get('work_ids') or []
                total = len(work_ids)
                stats = {'downloaded_works': 0, 'skipped_works': 0, 'failed_works': 0, 'total_images': 0}
                task.progress_total = total
                for i, wid in enumerate(work_ids):
                    if dl.cancelled:
                        break
                    if not dl.check_pause():
                        break
                    progress_cb(wid, i + 1, total, f'补全 {i + 1}/{total}')
                    result = dl.download_single_work(str(wid), skip_existing=True, skip_ai_filter=True)
                    if result.downloaded > 0:
                        stats['downloaded_works'] += 1
                        stats['total_images'] += result.downloaded
                    elif result.skipped > 0:
                        stats['skipped_works'] += 1
                    else:
                        stats['failed_works'] += 1
                    dl.api.wait()
                task.result = stats
                emit_sse(task.task_id, 'complete', stats)
                task.add_log('success',
                    f'收藏补全完成: 成功{stats["downloaded_works"]}, 跳过{stats["skipped_works"]}, 失败{stats["failed_works"]}')
                if dl.cancelled:
                    task.status = TaskStatus.CANCELLED
                else:
                    task.status = TaskStatus.COMPLETED

            elif task_type == 'sync':
                is_incremental = (task.params.get('mode') or 'incremental') != 'full'

                def sync_progress_cb(wid, cur, total, msg):
                    emit_sse(task.task_id, 'progress', {
                        'current': cur,
                        'total': total,
                        'message': msg,
                    })
                    task.add_log('progress', f'[{cur}/{total}] {msg}')

                result = scan_obj.scan_directory(
                    progress_callback=sync_progress_cb,
                    incremental=is_incremental
                )
                task.result = {
                    'total_dirs': result.total_dirs,
                    'total_files': result.total_files,
                    'scanned_dirs': result.scanned_dirs,
                    'skipped_dirs': result.skipped_dirs,
                    'incremental': result.incremental,
                    'new_artists': result.new_artists,
                    'new_works': result.new_works,
                    'new_images': result.new_images,
                    'updated_images': result.updated_images,
                    'errors': result.errors,
                    'error_messages': result.error_messages[:20],
                }
                emit_sse(task.task_id, 'complete', task.result)
                if is_incremental:
                    task.add_log('success',
                        f'增量同步完成: 扫描{result.scanned_dirs}个目录, 跳过{result.skipped_dirs}个(无变化), '
                        f'{result.new_images} 新图片')
                else:
                    task.add_log('success', f'全量同步完成: {result.new_images} 新图片, {result.new_works} 新作品, {result.new_artists} 新作者')
                task.status = TaskStatus.COMPLETED

        except Exception as e:
            logger.error(f"任务执行异常 {task.task_id}: {e}")
            task.add_log('error', str(e))
            emit_sse(task.task_id, 'error', {'error': str(e)})
            if task.status not in (TaskStatus.CANCELLED, TaskStatus.COMPLETED, TaskStatus.FAILED):
                task.status = TaskStatus.FAILED

        finally:
            task.finished_at = datetime.now().isoformat()
            self._persist_task_status(task)
            with self._lock:
                if self._current_task == task:
                    self._current_task = None
                # 如果任务在暂停状态下被完成/取消/失败，清理全局暂停标记
                if self._paused and task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED):
                    # 检查是否还有其他暂停的任务
                    has_other_paused = any(
                        t.status == TaskStatus.PAUSED
                        for t in self._task_registry.values()
                        if t.task_id != task.task_id
                    )
                    if not has_other_paused:
                        self._paused = False

    def submit(self, task_type: str, params: dict) -> str:
        """提交任务到队列，返回 task_id（同时持久化到 DB）"""
        task_id = params.get('task_id', f'{task_type}_{int(time.time())}')
        task = TaskInfo(task_id, task_type, params)
        task.add_log('info', f'任务已提交 ({task_type})')
        with self._lock:
            self._task_registry[task_id] = task
            self._queue.append(task)
        # 持久化到数据库
        if self._db:
            try:
                self._db.save_task(task_id, task_type, params, 'pending')
            except Exception as e:
                logger.error(f"[TaskManager] 持久化任务失败 {task_id}: {e}")
        # 调度线程意外退出时重建，避免新任务永远停在"排队中"
        self._ensure_scheduler()
        return task_id

    def _drop_persisted_task(self, task_id: str):
        """删除持久化记录 + 清理内存中的终态任务（重试/清空共用）"""
        if self._db:
            try:
                self._db.delete_task(task_id)
            except Exception as e:
                logger.error(f"[TaskManager] 删除任务记录失败 {task_id}: {e}")
        with self._lock:
            t = self._task_registry.get(task_id)
            if t is not None and t.status in (
                    TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                del self._task_registry[task_id]
                sse_queues.pop(task_id, None)

    def retry_task(self, task_id: str) -> Optional[str]:
        """重试单个失败任务：重建并重新排队，同时删除旧的失败记录"""
        if not self._db:
            return None
        p = self._db.get_persisted_task(task_id)
        if not p or p.get('status') != 'failed':
            return None
        params = dict(p.get('params') or {})
        params.pop('task_id', None)
        new_id = self.submit(p['task_type'], params)
        # 旧失败记录必须删掉：否则"失败任务"计数永远不减，还会被再次重试
        self._drop_persisted_task(task_id)
        return new_id

    def retry_all_failed(self) -> int:
        """重试持久化表中所有失败任务，返回重试数量（旧的失败记录会被清理）"""
        if not self._db:
            return 0
        failed = self._db.get_persisted_tasks(status='failed')
        count = 0
        for p in failed:
            params = dict(p.get('params') or {})
            params.pop('task_id', None)
            try:
                self.submit(p['task_type'], params)
                self._drop_persisted_task(p['task_id'])
                count += 1
            except Exception as e:
                logger.error(f"[TaskManager] 重试任务失败 {p['task_id']}: {e}")
        return count

    def clear_history(self, scope: str = 'history') -> int:
        """
        清空任务记录（DB + 内存）。

        Args:
            scope: history=已取消/已完成（默认）, failed=失败记录, all=全部终态任务

        Returns:
            删除的记录数
        """
        if not self._db:
            return 0
        if scope == 'failed':
            removed = self._db.clear_failed_task_history()
            statuses = (TaskStatus.FAILED,)
        elif scope == 'all':
            removed = self._db.clear_terminal_task_history(include_failed=True)
            statuses = (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED)
        else:
            removed = self._db.clear_terminal_task_history()
            statuses = (TaskStatus.COMPLETED, TaskStatus.CANCELLED)

        # 同步清理内存，避免页面仍能从注册表读到旧任务
        # （跳过仍在执行的线程，否则任务对象被删后无法再通知它停止）
        with self._lock:
            for tid, t in list(self._task_registry.items()):
                if t.status in statuses and t is not self._active_task:
                    del self._task_registry[tid]
                    sse_queues.pop(tid, None)
        return removed

    def pause(self, task_id: str) -> bool:
        """暂停任务：设置全局暂停标记，停止所有任务（同时更新 DB）"""
        dl = self._active_dl or downloader
        with self._lock:
            task = self._task_registry.get(task_id)
            if task is None:
                return False

            # 设置全局暂停标记（整个队列暂停）
            self._paused = True

            if task.status == TaskStatus.RUNNING:
                # 正在运行的任务：标记为暂停，从 _current_task 移除（让调度循环能继续）
                task.status = TaskStatus.PAUSED
                task.add_log('info', '⏸ 暂停中...（当前图片下载完成后暂停）')
                emit_sse(task_id, 'status_change', {'status': 'paused'})
                if dl:
                    dl.paused = True
                self._current_task = None
                self._persist_task_status(task)
                return True
            elif task.status == TaskStatus.PENDING:
                # 排队中的任务：直接标记为暂停
                task.status = TaskStatus.PAUSED
                task.add_log('info', '⏸ 任务已暂停（排队中）')
                emit_sse(task_id, 'status_change', {'status': 'paused'})
                # 从队列中移除（避免 _queue 中残留暂停任务）
                if task in self._queue:
                    self._queue.remove(task)
                self._persist_task_status(task)
                return True
            return False

    def resume(self, task_id: str) -> bool:
        """恢复暂停的任务：取消全局暂停标记，将暂停的任务放回队列（同时更新 DB）"""
        dl = self._active_dl or downloader
        with self._lock:
            task = self._task_registry.get(task_id)
            if task is None:
                return False
            if task.status == TaskStatus.PAUSED:
                # 取消全局暂停标记
                self._paused = False

                # 恢复下载器
                if dl:
                    dl.paused = False

                if self._active_task is task:
                    # 执行线程还活着（阻塞在 check_pause），直接让它继续，
                    # 不能重新入队，否则同一任务会被执行两次
                    task.status = TaskStatus.RUNNING
                    task.add_log('info', '▶ 任务已恢复')
                    emit_sse(task_id, 'status_change', {'status': 'running'})
                else:
                    # 将当前暂停的任务放回队列头（恢复时优先执行）
                    task.status = TaskStatus.PENDING
                    task.add_log('info', '▶ 任务已恢复，等待执行')
                    emit_sse(task_id, 'status_change', {'status': 'pending'})
                    if task not in self._queue:
                        self._queue.insert(0, task)  # 插入队头，优先执行

                # 同时将其他暂停的任务也恢复为 pending 并放入队列
                for t in self._task_registry.values():
                    if t.status == TaskStatus.PAUSED and t != task:
                        t.status = TaskStatus.PENDING
                        t.add_log('info', '▶ 任务已恢复，等待执行')
                        emit_sse(t.task_id, 'status_change', {'status': 'pending'})
                        if t not in self._queue:
                            self._queue.append(t)

                self._persist_task_status(task)
                return True
            return False

    def cancel(self, task_id: str) -> bool:
        """取消任务：清理所有相关引用，自动取消全局暂停（同时更新 DB）"""
        dl = self._active_dl or downloader
        with self._lock:
            task = self._task_registry.get(task_id)
            if task is None:
                return False

            if task.status in (TaskStatus.PENDING, TaskStatus.PAUSED, TaskStatus.RUNNING):
                # 该任务的执行线程是否还活着（暂停时 _current_task 会清空，但线程仍在跑）
                was_active = self._active_task is task

                task.status = TaskStatus.CANCELLED
                task.add_log('info', '⏹ 取消中...' if was_active else '任务已取消')
                if not was_active:
                    task.finished_at = datetime.now().isoformat()
                emit_sse(task_id, 'status_change', {'status': 'cancelled'})
                emit_sse(task_id, 'complete', {'cancelled': True})

                # 从队列移除
                if task in self._queue:
                    self._queue.remove(task)
                # 清理 _current_task 引用（如果暂停时未正确清空）
                if self._current_task == task:
                    self._current_task = None

                # 关键：通知正在执行的下载线程。否则线程会一直卡在 check_pause()
                # 里等待，任务显示已取消但实际仍在跑/永不退出
                if was_active and dl:
                    dl.cancelled = True
                    dl.paused = False
                    logger.info(f'[cancel] {task_id} 已通知执行线程 '
                                f'(dl={id(dl)}, cancelled={dl.cancelled})')

                # 如果取消了唯一的暂停任务，解除全局暂停
                if self._paused:
                    paused_count = sum(
                        1 for t in self._task_registry.values()
                        if t.status == TaskStatus.PAUSED and t.task_id != task_id
                    )
                    if paused_count == 0:
                        self._paused = False
                        if dl:
                            dl.paused = False

                self._persist_task_status(task)
                return True
            return False

    def get_active_tasks(self) -> List[dict]:
        """获取所有活跃任务（排队中、运行中、暂停），去重"""
        with self._lock:
            seen_ids = set()
            tasks = []

            # 1. 当前正在执行的任务
            if self._current_task and self._current_task.status in (
                TaskStatus.RUNNING, TaskStatus.PAUSED
            ):
                tasks.append(self._current_task.to_dict())
                seen_ids.add(self._current_task.task_id)

            # 2. 队列中排队的任务
            for t in self._queue:
                if t.task_id not in seen_ids and t.status in (TaskStatus.PENDING, TaskStatus.PAUSED):
                    tasks.append(t.to_dict())
                    seen_ids.add(t.task_id)

            # 3. 兜底：registry 中其他活跃但不在上述集合中的任务
            for t in self._task_registry.values():
                if t.task_id not in seen_ids and t.status in (
                    TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED
                ):
                    tasks.append(t.to_dict())
                    seen_ids.add(t.task_id)

            return tasks

    def get_task_status(self, task_id: str) -> Optional[dict]:
        """获取任务完整状态（含日志）"""
        with self._lock:
            task = self._task_registry.get(task_id)
            if task:
                return task.to_status_dict()
            return None

    def _cleanup_completed(self):
        """清理过期的已完成/已取消任务（内存 + DB）"""
        with self._lock:
            now = datetime.now()
            to_remove = []
            for tid, task in self._task_registry.items():
                if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                    if task.finished_at:
                        finished = datetime.fromisoformat(task.finished_at)
                        if (now - finished).total_seconds() > self._completed_cleanup_sec:
                            to_remove.append(tid)
            for tid in to_remove:
                del self._task_registry[tid]
                if tid in sse_queues:
                    del sse_queues[tid]
                # 从 DB 中删除
                if self._db:
                    try:
                        self._db.delete_task(tid)
                    except Exception:
                        pass

    def _persist_task_status(self, task: TaskInfo):
        """将任务状态同步到数据库"""
        if not self._db:
            return
        try:
            self._db.update_task_status(
                task.task_id,
                task.status.value,
                progress_current=task.progress_current,
                progress_total=task.progress_total,
                progress_message=task.progress_message,
                progress_work_id=task.progress_work_id,
                extra_info=task.extra_info,
                result=task.result,
            )
        except Exception as e:
            logger.error(f"[TaskManager] 同步任务状态到DB失败 {task.task_id}: {e}")


# 全局任务管理器
task_manager = TaskManager()
# 注意：task_manager.start() 需要在 init_services() 之后调用（有 DB 实例才能恢复任务）
# 延迟到 main() 中初始化


# ============ 页面路由 ============

@app.route('/')
def index():
    """仪表盘首页"""
    return render_template('index.html')


@app.route('/artists')
def artists_page():
    """作者列表页"""
    return render_template('artists.html')


@app.route('/download')
def download_page():
    """下载管理页"""
    return render_template('download.html')


@app.route('/sync')
def sync_page():
    """本地同步页"""
    return render_template('sync.html')


@app.route('/images')
def images_page():
    """图片浏览页"""
    return render_template('images.html')


@app.route('/bookmarks')
def bookmarks_page():
    """收藏作品页"""
    return render_template('bookmarks.html')


@app.route('/config')
def config_page():
    """配置管理页"""
    return render_template('config.html')


# ============ 静态文件服务（本地图片缩略图） ============

def _is_path_within_download(abs_path: str) -> bool:
    """校验绝对路径是否位于下载目录内（防 'download_path' 前缀兄弟目录穿越）"""
    abs_download = os.path.abspath(get_config().download_path)
    try:
        return os.path.commonpath([os.path.abspath(abs_path), abs_download]) == abs_download
    except ValueError:
        return False


@app.route('/thumbnail/<path:filepath>')
def serve_thumbnail(filepath):
    """提供本地图片缩略图（Pillow 缩放到 240px 宽 JPEG，大幅减少传输量）"""
    decoded_path = urllib.parse.unquote(filepath)
    abs_path = os.path.abspath(decoded_path)
    if not _is_path_within_download(abs_path):
        return 'Forbidden', 403
    if not os.path.exists(abs_path):
        return 'Not Found', 404

    # 视频（ugoira 合成的 webm）：用同名 .jpg 封面生成缩略图
    ext = os.path.splitext(abs_path)[1].lower()
    source_path = abs_path
    if ext in ('.webm', '.mp4', '.mkv', '.m4v'):
        poster = abs_path + '.jpg'
        if not os.path.isfile(poster):
            # 缺少封面（如非本工具下载的视频）：用 ffmpeg 抽首帧自动生成
            from ffmpeg_util import extract_poster
            if not extract_poster(abs_path, poster):
                return 'Not Found', 404
        source_path = poster

    # 检查是否已有缓存缩略图（下载目录 .thumb 下，按文件路径 hash 缓存）
    import hashlib
    thumb_dir = os.path.join(os.path.abspath(get_config().download_path), '.thumb')
    file_hash = hashlib.md5(abs_path.encode()).hexdigest()
    thumb_path = os.path.join(thumb_dir, f'{file_hash}.jpg')

    if os.path.exists(thumb_path):
        return send_from_directory(thumb_dir, f'{file_hash}.jpg', mimetype='image/jpeg', max_age=86400)

    # 生成缩略图并缓存
    try:
        os.makedirs(thumb_dir, exist_ok=True)
        from PIL import Image
        try:
            with Image.open(source_path) as img:
                img.load()  # 强制加载全部数据，触发 truncated 等异常
                # 转为 RGB（避免 RGBA 在 JPEG 中报错）
                if img.mode in ('RGBA', 'P', 'LA'):
                    img = img.convert('RGBA')
                    background = Image.new('RGBA', img.size, (30, 30, 35, 255))
                    img = Image.alpha_composite(background, img)
                    img = img.convert('RGB')
                elif img.mode != 'RGB':
                    img = img.convert('RGB')

                # 缩放到 240px 宽，保持比例
                w, h = img.size
                new_w = 240
                new_h = int(h * new_w / w)
                img = img.resize((new_w, new_h), Image.LANCZOS)
                img.save(thumb_path, 'JPEG', quality=70, optimize=True)
        except (OSError, IOError) as img_err:
            # 图片文件损坏（truncated、格式异常等），记录警告并返回占位
            logger.warning(f'图片文件损坏，跳过缩略图生成 {abs_path}: {img_err}')
            return 'Broken Image', 415

        return send_from_directory(thumb_dir, f'{file_hash}.jpg', mimetype='image/jpeg', max_age=86400)
    except Exception as e:
        logger.warning(f'缩略图生成失败 {abs_path}: {e}')
        return 'Thumbnail Error', 500


@app.route('/original/<path:filepath>')
def serve_original(filepath):
    """提供原图（不压缩，用于大图查看器）"""
    decoded_path = urllib.parse.unquote(filepath)
    abs_path = os.path.abspath(decoded_path)
    if not _is_path_within_download(abs_path):
        return 'Forbidden', 403
    if not os.path.exists(abs_path):
        return 'Not Found', 404
    directory = os.path.dirname(abs_path)
    filename = os.path.basename(abs_path)
    return send_from_directory(directory, filename)


# 视频转码缓存锁：同一文件的并发请求只转码一次，避免重复占用 CPU
_video_locks: Dict[str, threading.Lock] = {}
_video_locks_guard = threading.Lock()


def _video_lock(key: str) -> threading.Lock:
    with _video_locks_guard:
        lock = _video_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _video_locks[key] = lock
        return lock


def _playable_mp4(abs_path: str) -> Optional[str]:
    """
    返回可被浏览器播放的 MP4 路径（非 MP4 容器按需用 ffmpeg 转码并缓存在 .video 目录）。

    背景：部分环境无法解码 VP9 WebM（Chrome 报 MEDIA_ERR_DECODE），
    表现为"点开大图后播放闪一下就停"。统一转 H.264 MP4 兼容性最好。
    返回 None 表示转码失败。
    """
    import hashlib
    ext = os.path.splitext(abs_path)[1].lower()
    if ext == '.mp4':
        return abs_path

    try:
        stat = os.stat(abs_path)
    except OSError:
        return None
    # 缓存键包含文件大小与 mtime：重新下载/替换文件后自动失效
    key = hashlib.md5(
        f'{abs_path}|{stat.st_size}|{int(stat.st_mtime)}'.encode()).hexdigest()
    cache_dir = os.path.join(os.path.abspath(get_config().download_path), '.video')
    mp4_path = os.path.join(cache_dir, f'{key}.mp4')

    if os.path.isfile(mp4_path) and os.path.getsize(mp4_path) > 0:
        return mp4_path

    with _video_lock(key):
        if os.path.isfile(mp4_path) and os.path.getsize(mp4_path) > 0:
            return mp4_path
        from ffmpeg_util import encode_mp4
        tmp_path = mp4_path + '.tmp.mp4'
        logger.info(f'转码视频用于预览: {abs_path}')
        if not encode_mp4(abs_path, tmp_path):
            return None
        try:
            os.replace(tmp_path, mp4_path)
        except OSError as e:
            logger.warning(f'写入视频缓存失败 {mp4_path}: {e}')
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return None
        return mp4_path


@app.route('/video/<path:filepath>')
def serve_video(filepath):
    """提供可直接播放的视频（非 MP4 自动转码为 H.264 MP4 并缓存）"""
    decoded_path = urllib.parse.unquote(filepath)
    abs_path = os.path.abspath(decoded_path)
    if not _is_path_within_download(abs_path):
        return 'Forbidden', 403
    if not os.path.exists(abs_path):
        return 'Not Found', 404

    playable = _playable_mp4(abs_path)
    if not playable:
        return 'Video Not Playable', 415
    return send_from_directory(os.path.dirname(playable), os.path.basename(playable),
                               mimetype='video/mp4', conditional=True)



def is_local_request() -> bool:
    """判断请求是否来自本机（回环地址）"""
    remote = (request.remote_addr or '').strip()
    return remote in ('127.0.0.1', '::1', 'localhost', '::ffff:127.0.0.1')


@app.route('/api/image/copy-file', methods=['POST'])
def api_image_copy_file():
    """本地环境：复制图片文件本体到系统剪贴板（可粘贴为文件）
    仅本机访问时可用；用系统剪贴板 API 放置文件（CF_HDROP），保留原始格式与质量
    """
    try:
        # 仅本机访问可用（局域网/手机无法操作 PC 剪贴板）
        if not is_local_request():
            return jsonify({'success': False, 'error': '复制文件功能仅在本机访问时可用'}), 403

        data = request.get_json(force=True)
        file_path = (data.get('file_path') or '').strip()
        if not file_path:
            return jsonify({'success': False, 'error': '缺少 file_path'})

        config = get_config()
        abs_download = os.path.abspath(config.download_path)

        # 路径解析 + 安全校验（确保在 download_path 内）
        if not os.path.isabs(file_path):
            abs_path = os.path.normpath(os.path.join(abs_download, file_path))
        else:
            abs_path = os.path.abspath(file_path)

        if not abs_path.startswith(abs_download):
            return jsonify({'success': False, 'error': '路径越权'}), 403
        if not os.path.exists(abs_path):
            return jsonify({'success': False, 'error': '文件不存在'}), 404

        if sys.platform == 'win32':
            # Windows：PowerShell 复制文件到剪贴板（CF_HDROP）
            safe_path = abs_path.replace("'", "''")
            cmd = f"Set-Clipboard -Path '{safe_path}'"
            subprocess.run(
                ['powershell', '-NoProfile', '-NonInteractive',
                 '-WindowStyle', 'Hidden', '-Command', cmd],
                check=True, timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            )
        elif sys.platform == 'darwin':
            subprocess.run(
                ['osascript', '-e',
                 f'set the clipboard to (POSIX file "{abs_path}")'],
                check=True, timeout=10
            )
        else:
            # Linux：xclip 复制文件 URI
            subprocess.run(
                ['xclip', '-selection', 'clipboard', '-t', 'text/uri-list', abs_path],
                check=True, timeout=10
            )

        return jsonify({'success': True, 'message': '已复制文件本体，可直接粘贴到文件夹或聊天窗口'})

    except subprocess.CalledProcessError as e:
        logger.error(f"复制文件到剪贴板失败: {e}")
        return jsonify({'success': False, 'error': '复制文件失败（剪贴板被占用或权限不足）'})
    except FileNotFoundError:
        return jsonify({'success': False, 'error': '当前系统缺少剪贴板工具，请使用「复制图片」'})
    except Exception as e:
        logger.error(f"复制文件失败: {e}")
        return jsonify({'success': False, 'error': str(e)})


# ============ API 路由 ============

@app.route('/api/stats')
def api_stats():
    """获取统计信息"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        stats = db_obj.get_stats()
        return jsonify({'success': True, 'data': stats})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/dashboard')
def api_dashboard():
    """获取仪表盘聚合数据（统计+作者更新状态+最近活动）"""
    try:
        db_obj, api_obj, _, _ = get_or_init_services()
        stats = db_obj.get_stats()

        # 最近下载记录：先在子查询中聚合排序取前 30，再补作者信息与封面
        # （避免对全量作品执行相关子查询、也避免序列化前端用不到的 author 全量列表）
        with db_obj.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT w.id as work_id, w.title, w.page_count,
                       a.id as artist_id, a.name as artist_name, a.account as artist_account,
                       t.downloaded_pages, t.latest_downloaded_at,
                       (SELECT im2.file_path FROM images im2
                        WHERE im2.work_id = w.id AND im2.is_downloaded = 1
                        ORDER BY im2.page LIMIT 1) as first_image_path
                FROM (
                    SELECT i.work_id, COUNT(i.id) as downloaded_pages,
                           MAX(i.downloaded_at) as latest_downloaded_at
                    FROM images i
                    WHERE i.is_downloaded = 1
                    GROUP BY i.work_id
                    ORDER BY latest_downloaded_at DESC
                    LIMIT 100
                ) t
                JOIN works w ON w.id = t.work_id
                JOIN artists a ON w.artist_id = a.id
                ORDER BY t.latest_downloaded_at DESC
                LIMIT 30
            ''')
            recent_downloads = [_serialize_datetime_fields(dict(row)) for row in cursor.fetchall()]

        # 过滤统计
        filter_stats = db_obj.get_filter_stats()

        # Cookie 状态（是否有设置 + 是否最近遇到 403）
        config = get_config()
        cookie_status = {
            'has': bool(config.cookie),
            'invalid': bool(getattr(api_obj, 'cookie_invalid', False)),
        }

        return jsonify({
            'success': True,
            'data': {
                'stats': stats,
                'recent_downloads': recent_downloads,
                'filter_stats': filter_stats,
                'cookie_status': cookie_status,
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/artists')
def api_artists():
    """获取作者列表（含作品数、收藏数、标签数）"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        artists = db_obj.get_all_artists(with_work_count=True)
        
        # 批量获取所有作者的标签数量
        artist_ids = [a['id'] for a in artists]
        tag_counts = {}
        if artist_ids:
            with db_obj.get_connection() as conn:
                cursor = conn.cursor()
                placeholders = ','.join(['?'] * len(artist_ids))
                cursor.execute(f'''
                    SELECT w.artist_id, COUNT(DISTINCT wt.tag) as tag_count
                    FROM work_tags wt
                    JOIN works w ON wt.work_id = w.id
                    WHERE w.artist_id IN ({placeholders})
                    GROUP BY w.artist_id
                ''', artist_ids)
                for row in cursor.fetchall():
                    tag_counts[row['artist_id']] = row['tag_count']
        
        result = []
        for a in artists:
            item = _serialize_datetime_fields(dict(a))
            item['tag_count'] = tag_counts.get(a['id'], 0)
            result.append(item)
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


def _safe_datetime(val):
    """安全转换日期时间为字符串"""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def _serialize_datetime_fields(d: dict) -> dict:
    """将字典中的 datetime 字段原位转为 ISO 字符串（用于 JSON 响应）"""
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


@app.route('/api/artist/<int:artist_id>/works')
def api_artist_works(artist_id):
    """获取作者的作品列表"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        works = db_obj.get_artist_works(artist_id)
        images_map = db_obj.get_images_for_works([w.id for w in works])
        result = []
        for w in works:
            item = {
                'id': w.id,
                'artist_id': w.artist_id,
                'title': w.title,
                'page_count': w.page_count,
            }
            images = images_map.get(w.id, [])
            # 只返回卡片渲染所需的聚合信息，避免传输完整图片明细（可减小约 80% 响应体）
            item['image_count'] = len(images)
            item['downloaded_count'] = sum(1 for img in images if img.is_downloaded)
            item['first_image_path'] = next((img.file_path for img in images if img.file_path), None)
            result.append(item)
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/artist/<int:artist_id>/info')
def api_artist_info(artist_id):
    """获取作者详情（从API获取）"""
    try:
        _, api_obj, _, _ = get_or_init_services()
        info = api_obj.get_artist_info(str(artist_id))
        if info:
            return jsonify({'success': True, 'data': info})
        return jsonify({'success': False, 'error': '无法获取作者信息'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/work/<int:work_id>/info')
def api_work_info(work_id):
    """获取作品详情（从API获取）"""
    try:
        _, api_obj, _, _ = get_or_init_services()
        info, status_code = api_obj.get_work_info(str(work_id))
        if info:
            return jsonify({'success': True, 'data': info})
        if status_code == 404:
            return jsonify({'success': False, 'error': '作品不存在（可能已被删除）', 'status_code': 404})
        return jsonify({'success': False, 'error': '无法获取作品信息'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/work/<int:work_id>/images')
def api_work_images(work_id):
    """获取作品的所有图片详情（从数据库）"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        work = db_obj.get_work(work_id)
        if not work:
            return jsonify({'success': False, 'error': '作品不存在'})

        images = db_obj.get_work_images(work_id)
        result = {
            'work_id': work.id,
            'title': work.title,
            'artist_id': work.artist_id,
            'page_count': work.page_count,
            'width': work.width,
            'height': work.height,
            'created_at': _safe_datetime(work.created_at),
            'updated_at': _safe_datetime(work.updated_at),
            'images': [{
                'id': img.id,
                'page': img.page,
                'filename': img.filename,
                'file_path': img.file_path,
                'file_size': img.file_size,
                'is_downloaded': img.is_downloaded,
            } for img in images],
        }
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/work/<int:work_id>/detail')
def api_work_detail(work_id):
    """获取作品完整详情（数据库+关联作者信息）"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        work = db_obj.get_work(work_id)
        if not work:
            return jsonify({'success': False, 'error': '作品不存在'})

        artist = db_obj.get_artist(work.artist_id)
        images = db_obj.get_work_images(work_id)

        # 获取标签
        tags = db_obj.get_work_tags(work_id)
        
        result = {
            'work_id': work.id,
            'title': work.title,
            'description': work.description,
            'page_count': work.page_count,
            'width': work.width,
            'height': work.height,
            'tags': tags,
            'created_at': _safe_datetime(work.created_at),
            'updated_at': _safe_datetime(work.updated_at),
            'artist': {
                'id': artist.id,
                'name': artist.name,
                'account': artist.account,
            } if artist else None,
            'images': [{
                'id': img.id,
                'page': img.page,
                'filename': img.filename,
                'file_path': img.file_path,
                'file_size': img.file_size,
                'is_downloaded': img.is_downloaded,
                'downloaded_at': _safe_datetime(img.downloaded_at),
            } for img in images],
        }
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/verify')
def api_verify():
    """验证数据库完整性（7类异常检查）"""
    try:
        _, _, _, scan_obj = get_or_init_services()
        result = scan_obj.verify_database_integrity()
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/verify/fix', methods=['POST'])
def api_verify_fix():
    """
    执行数据库修复操作
    请求体: {
        "actions": {
            "delete_missing_file_ids": [1, 2, 3],    # 删除缺失文件的images记录
            "reimport_orphan_files": [...],            # 重新扫描入库孤儿文件
            "delete_orphan_work_ids": [101, 102],     # 删除孤儿作品
            "delete_orphan_artist_ids": [201, 202],   # 删除孤儿作者
            "delete_broken_image_fk_ids": [301],       # 删除外键断裂的images
            "delete_broken_work_fk_ids": [401],        # 删除外键断裂的works
            "fix_page_counts": [{"work_id": 1, "actual_pages": 5}, ...],  # 修正页数
            "remove_empty_dirs": [{...}, ...]          # 清除空目录（同时清理孤儿作者）
        }
    }
    """
    try:
        db_obj, _, _, _ = get_or_init_services()
        data = request.get_json() or {}
        actions = data.get('actions', {})

        results = {}

        # 1. 删除缺失文件的 images 记录
        ids = actions.get('delete_missing_file_ids', [])
        if ids:
            n = db_obj.fix_delete_missing_file_records(ids)
            results['deleted_missing_file_records'] = n
            logger.info(f"[修复] 删除 {n} 条缺失文件的images记录")

        # 2. 重新导入孤儿文件
        files = actions.get('reimport_orphan_files', [])
        if files:
            n = db_obj.fix_reimport_orphan_files(files)
            results['reimported_orphan_files'] = n
            logger.info(f"[修复] 重新导入 {n} 个孤儿文件")

        # 3. 删除孤儿作品
        ids = actions.get('delete_orphan_work_ids', [])
        if ids:
            n = db_obj.fix_delete_orphan_works(ids)
            results['deleted_orphan_works'] = n
            logger.info(f"[修复] 删除 {n} 个孤儿作品")

        # 4. 删除孤儿作者
        ids = actions.get('delete_orphan_artist_ids', [])
        if ids:
            n = db_obj.fix_delete_orphan_artists(ids)
            results['deleted_orphan_artists'] = n
            logger.info(f"[修复] 删除 {n} 个孤儿作者")

        # 5. 修复外键断裂的 images（补全 works + artists）
        ids = actions.get('delete_broken_image_fk_ids', [])
        if ids:
            n = db_obj.fix_delete_broken_image_fk(ids)
            results['deleted_broken_image_fk'] = n
            logger.info(f"[修复] 补全 {n} 条外键断裂的images关联")

        # 6. 删除外键断裂的 works
        ids = actions.get('delete_broken_work_fk_ids', [])
        if ids:
            n = db_obj.fix_delete_broken_work_fk(ids)
            results['deleted_broken_work_fk'] = n
            logger.info(f"[修复] 删除 {n} 条外键断裂的works记录")

        # 7. 修正页数不匹配
        corrections = actions.get('fix_page_counts', [])
        if corrections:
            n = db_obj.fix_page_count_mismatch(corrections)
            results['fixed_page_counts'] = n
            logger.info(f"[修复] 修正 {n} 个作品的页数")

        # 8. 清除空目录（磁盘 + 孤儿作者）
        dirs = actions.get('remove_empty_dirs', [])
        if dirs:
            n = db_obj.fix_remove_empty_dirs(dirs)
            results['removed_empty_dirs'] = n
            logger.info(f"[修复] 删除 {n} 个空目录")

        total = sum(v for v in results.values() if isinstance(v, int))
        return jsonify({
            'success': True,
            'message': f'修复完成，共处理 {total} 项',
            'data': results
        })
    except Exception as e:
        logger.error(f"修复数据库失败: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/verify/fix-all-page-counts', methods=['POST'])
def api_verify_fix_all_page_counts():
    """
    一键修复所有页数不匹配：将 works.page_count 更新为实际 images 数量。
    同时返回 expected > actual 的作品列表，供前端触发增量下载。
    """
    try:
        db_obj, _, _, _ = get_or_init_services()

        # 重新扫描获取所有 works 的实际 images 数量
        with db_obj.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT w.id, w.artist_id, w.title, w.page_count,
                       a.name as artist_name
                FROM works w
                LEFT JOIN artists a ON w.artist_id = a.id
            ''')
            all_works = [dict(row) for row in cursor.fetchall()]

            # 获取每个作品的 images 数量
            cursor.execute('''
                SELECT work_id, COUNT(*) as cnt
                FROM images
                GROUP BY work_id
            ''')
            page_counts = {row['work_id']: row['cnt'] for row in cursor.fetchall()}

        mismatches = []
        need_download = []  # expected > actual，缺失页面，需要重新下载

        for w in all_works:
            actual = page_counts.get(w['id'], 0)
            expected = w['page_count'] or 0
            if actual > 0 and expected > 0 and actual != expected:
                mismatches.append({
                    'work_id': w['id'],
                    'actual_pages': actual,
                })
                if expected > actual:
                    need_download.append({
                        'work_id': w['id'],
                        'artist_id': w['artist_id'],
                        'artist_name': w.get('artist_name', ''),
                        'title': w.get('title', ''),
                        'expected_pages': expected,
                        'actual_pages': actual,
                        'missing_pages': expected - actual,
                    })

        # 修复所有页数不匹配
        fixed_count = db_obj.fix_page_count_mismatch(mismatches) if mismatches else 0

        return jsonify({
            'success': True,
            'message': f'修复 {fixed_count} 个页数不匹配，其中 {len(need_download)} 个作品可能缺失页面',
            'data': {
                'fixed': fixed_count,
                'need_download': need_download,
            }
        })
    except Exception as e:
        logger.error(f"一键修复页数不匹配失败: {e}")
        return jsonify({'success': False, 'error': str(e)})


def _coerce_config_value(field_name: str, value):
    """按 pydantic 模型字段类型转换表单值（避免手写类型映射表，字段类型始终与模型同步）"""
    annotation = Config.model_fields[field_name].annotation
    if annotation is bool:
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 'yes', 'on')
        return bool(value)
    if annotation is int:
        return int(value)
    if annotation is float:
        return float(value)
    return value


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    """获取或更新配置"""
    if request.method == 'GET':
        config = get_config()
        data = config.model_dump()
        data['has_cookie'] = bool(config.cookie)
        return jsonify({'success': True, 'data': data})

    # POST - 更新配置
    try:
        data = request.get_json()
        config = get_config()

        for field_name in Config.model_fields:
            if field_name not in data:
                continue
            value = data[field_name]
            if value is None or (isinstance(value, str) and value == '' and field_name != 'cookie'):
                continue  # 空字符串不更新（cookie 除外，允许清空）
            if field_name == 'cookie' and not value:
                config.cookie = ''
                continue
            setattr(config, field_name, _coerce_config_value(field_name, value))

        config.save()

        # 重新初始化服务
        reload_services()
        ffmpeg_util.reset_cache()   # ffmpeg_path 可能变化，重新检测

        return jsonify({'success': True, 'message': '配置已保存'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/config/cookie-file', methods=['POST'])
def api_load_cookie_file():
    """加载 Cookie 文件（Netscape 格式）"""
    try:
        data = request.get_json()
        file_path = (data.get('file_path') or '').strip()

        if not file_path:
            return jsonify({'success': False, 'error': '请输入 Cookie 文件路径'})

        if not os.path.exists(file_path):
            return jsonify({'success': False, 'error': f'文件不存在: {file_path}'})

        cookie_str, stats = parse_cookie_file(file_path)

        if not cookie_str:
            return jsonify({'success': False, 'error': '文件中未找到有效的 Pixiv Cookie（请确认导出的是 pixiv.net 的 Cookie）'})

        config = get_config()
        config.cookie = cookie_str
        config.save()

        # 重新初始化
        reload_services()

        return jsonify({
            'success': True,
            'message': f'Cookie 已导入（{stats["used"]} 个）',
            'data': {'cookie': cookie_str, 'stats': stats}
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/config/check-default-cookie')
def api_check_default_cookie():
    """检查项目目录下是否存在默认 Cookie 文件"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    default_files = [
        "cookies_www.pixiv.net.txt",
        "cookie.txt",
        "cookies.txt",
        "cookies.txt.txt",
    ]
    files = []
    for f in default_files:
        path = os.path.join(base_dir, f)
        if os.path.isfile(path):
            files.append(path)

    return jsonify({'success': True, 'data': {'found': bool(files), 'files': files}})


# ============ SSE 实时进度 ============

@app.route('/api/stream/<task_id>')
def stream_progress(task_id):
    """SSE 实时进度推送"""
    q = queue.Queue()
    sse_queues[task_id] = q

    def generate():
        try:
            yield f"event: connected\ndata: {json.dumps({'task_id': task_id})}\n\n"

            while True:
                try:
                    msg = q.get(timeout=30)
                    yield f"event: {msg['event']}\ndata: {msg['data']}\n\n"

                    if msg['event'] == 'complete' or msg['event'] == 'error':
                        break
                except queue.Empty:
                    yield f"event: heartbeat\ndata: {json.dumps({})}\n\n"
        except GeneratorExit:
            pass
        finally:
            if task_id in sse_queues:
                del sse_queues[task_id]

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


# ============ 任务管理 API ============

@app.route('/api/tasks/active')
def api_tasks_active():
    """获取所有活跃任务 + 可重试的失败任务数（合并为一次请求，减少轮询开销）"""
    failed_count = 0
    try:
        db_obj, _, _, _ = get_or_init_services()
        failed_count = len(db_obj.get_persisted_tasks(status='failed'))
    except Exception:
        pass
    return jsonify({
        'success': True,
        'data': task_manager.get_active_tasks(),
        'failed_count': failed_count,
    })


@app.route('/api/task/<task_id>/status')
def api_task_status(task_id):
    """获取任务完整状态（含日志历史）"""
    status = task_manager.get_task_status(task_id)
    if status:
        return jsonify({'success': True, 'data': status})
    return jsonify({'success': False, 'error': '任务不存在或已过期'})


@app.route('/api/task/<task_id>/pause', methods=['POST'])
def api_task_pause(task_id):
    """暂停任务"""
    ok = task_manager.pause(task_id)
    return jsonify({'success': ok, 'message': '已暂停' if ok else '无法暂停'})


@app.route('/api/task/<task_id>/resume', methods=['POST'])
def api_task_resume(task_id):
    """恢复任务"""
    ok = task_manager.resume(task_id)
    return jsonify({'success': ok, 'message': '已恢复' if ok else '无法恢复'})


@app.route('/api/task/<task_id>/cancel', methods=['POST'])
def api_task_cancel(task_id):
    """取消任务"""
    ok = task_manager.cancel(task_id)
    return jsonify({'success': ok, 'message': '已取消' if ok else '无法取消'})


@app.route('/api/task/<task_id>/retry', methods=['POST'])
def api_task_retry(task_id):
    """重试单个失败任务"""
    new_id = task_manager.retry_task(task_id)
    if new_id:
        return jsonify({'success': True, 'task_id': new_id, 'message': '已重新加入队列'})
    return jsonify({'success': False, 'error': '任务不存在或不是失败状态'})


@app.route('/api/tasks/failed')
def api_tasks_failed():
    """获取可重试的失败任务列表（仅真正 failed，不含已取消/已完成）"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        failed = db_obj.get_persisted_tasks(status='failed')
        return jsonify({'success': True, 'data': {'failed': failed}})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/tasks/retry-failed', methods=['POST'])
def api_tasks_retry_failed():
    """一键重试所有失败任务（仅真正 failed）"""
    count = task_manager.retry_all_failed()
    return jsonify({'success': True, 'retried': count, 'message': f'已重新加入 {count} 个失败任务'})


@app.route('/api/tasks/clear-history', methods=['POST'])
def api_tasks_clear_history():
    """
    清空任务记录。

    body: {"scope": "history" | "failed" | "all"}
      - history（默认）：已取消/已完成
      - failed：失败记录
      - all：全部终态记录
    """
    try:
        payload = request.get_json(silent=True) or {}
        scope = (payload.get('scope') or 'history').lower()
        if scope not in ('history', 'failed', 'all'):
            return jsonify({'success': False, 'error': 'scope 参数无效'})
        removed = task_manager.clear_history(scope)
        label = {'history': '历史任务记录', 'failed': '失败任务记录',
                 'all': '历史与失败任务记录'}[scope]
        return jsonify({'success': True, 'removed': removed,
                        'message': f'已清空 {removed} 条{label}'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ============ 下载操作（提交到队列） ============

@app.route('/api/download/work', methods=['POST'])
def api_download_work():
    """下载单个作品（提交到队列）"""
    data = request.get_json()
    work_id = data.get('work_id', '')
    if not work_id:
        return jsonify({'success': False, 'error': '请输入作品ID'})
    task_id = data.get('task_id', f'dl_work_{int(time.time())}')
    params = {
        'task_id': task_id,
        'work_id': work_id,
        'force': data.get('force', False),
    }
    submitted_id = task_manager.submit('work', params)
    return jsonify({'success': True, 'task_id': submitted_id})


@app.route('/api/download/artist', methods=['POST'])
def api_download_artist():
    """下载作者作品（提交到队列）"""
    data = request.get_json()
    artist_id = data.get('artist_id', '')
    if not artist_id:
        return jsonify({'success': False, 'error': '请输入作者ID'})
    limit = data.get('limit')
    if limit == 0 or limit is None or limit == '':
        limit = None
    else:
        limit = int(limit)
    task_id = data.get('task_id', f'dl_artist_{int(time.time())}')
    params = {
        'task_id': task_id,
        'artist_id': artist_id,
        'limit': limit,
        'force': data.get('force', False),
    }
    submitted_id = task_manager.submit('artist', params)
    return jsonify({'success': True, 'task_id': submitted_id})


@app.route('/api/download/url', methods=['POST'])
def api_download_url():
    """根据URL下载（提交到队列）"""
    data = request.get_json()
    url = data.get('url', '')
    if not url:
        return jsonify({'success': False, 'error': '请输入URL'})
    task_id = data.get('task_id', f'dl_url_{int(time.time())}')
    params = {
        'task_id': task_id,
        'url': url,
    }
    submitted_id = task_manager.submit('url', params)
    return jsonify({'success': True, 'task_id': submitted_id})


@app.route('/api/download/artist-new', methods=['POST'])
def api_download_artist_new():
    """增量下载作者新作品（提交到队列）"""
    data = request.get_json()
    artist_id = data.get('artist_id', '')
    if not artist_id:
        return jsonify({'success': False, 'error': '请输入作者ID'})
    task_id = data.get('task_id', f'dl_artist_new_{int(time.time())}')
    params = {
        'task_id': task_id,
        'artist_id': artist_id,
        'force': data.get('force', False),
    }
    submitted_id = task_manager.submit('artist-new', params)
    return jsonify({'success': True, 'task_id': submitted_id})


@app.route('/api/download/bookmarks', methods=['POST'])
def api_download_bookmarks():
    """下载收藏作品（提交到队列）"""
    data = request.get_json()
    rest = data.get('rest', 'show')
    limit = data.get('limit')
    max_pages = data.get('max_pages')
    if limit == 0 or limit is None or limit == '':
        limit = None
    else:
        limit = int(limit)
    if max_pages == 0 or max_pages is None or max_pages == '':
        max_pages = None
    else:
        max_pages = int(max_pages)
    task_id = data.get('task_id', f'dl_bookmark_{int(time.time())}')
    params = {
        'task_id': task_id,
        'rest': rest,
        'limit': limit,
        'max_pages': max_pages,
        'force': data.get('force', False),
    }
    submitted_id = task_manager.submit('bookmarks', params)
    return jsonify({'success': True, 'task_id': submitted_id})


# ============ 标签补全操作 ============

@app.route('/api/download/tags', methods=['POST'])
def api_download_tags():
    """标签补全：为缺少 tag 的作品从 Pixiv API 获取标签（提交到队列）"""
    data = request.get_json() or {}
    task_id = data.get('task_id', f'dl_tags_{int(time.time())}')
    params = {
        'task_id': task_id,
        'delay': float(data.get('delay', 1.5)),
    }
    submitted_id = task_manager.submit('tags', params)
    return jsonify({'success': True, 'task_id': submitted_id})


# ============ 同步操作 ============

@app.route('/api/sync', methods=['POST'])
def api_sync():
    """扫描本地文件并同步到数据库（提交到队列）
    mode: incremental=增量扫描(默认), full=全量扫描
    """
    data = request.get_json() or {}
    task_id = data.get('task_id', f'sync_{int(time.time())}')
    mode = (data.get('mode') or 'incremental').strip().lower()
    if mode not in ('incremental', 'full'):
        mode = 'incremental'
    params = {'task_id': task_id, 'mode': mode}
    submitted_id = task_manager.submit('sync', params)
    return jsonify({'success': True, 'task_id': submitted_id})


@app.route('/api/sync/history')
def api_sync_history():
    """获取同步/扫描历史记录"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        records = db_obj.get_sync_records(limit=30)
        return jsonify({'success': True, 'data': records})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ============ 维护操作 ============

@app.route('/api/maintenance/cleanup-thumbs', methods=['POST'])
def api_cleanup_thumbs():
    """清理失效的缩略图缓存（源文件已不存在的 .thumb 文件）"""
    try:
        download_path = os.path.abspath(get_config().download_path)
        thumb_dir = os.path.join(download_path, '.thumb')
        if not os.path.isdir(thumb_dir):
            return jsonify({'success': True, 'removed': 0, 'message': '没有缩略图缓存'})

        removed = 0
        for name in os.listdir(thumb_dir):
            if not name.endswith('.jpg'):
                continue
            thumb_path = os.path.join(thumb_dir, name)
            try:
                os.remove(thumb_path)
                removed += 1
            except Exception:
                pass
        return jsonify({'success': True, 'removed': removed, 'message': f'已清理 {removed} 个缩略图缓存'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/system/ffmpeg')
def api_system_ffmpeg():
    """检测 ffmpeg（ugoira 动图功能依赖）"""
    try:
        info = ffmpeg_util.detect(force=True)
        return jsonify({'success': True, 'data': info})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ============ 删除（作者 / 作品） ============

def _remove_local_image_files(paths: List[str]) -> int:
    """删除本地图片文件及其缩略图缓存（仅限下载目录内）"""
    import hashlib
    thumb_dir = os.path.join(os.path.abspath(get_config().download_path), '.thumb')
    removed = 0
    for p in paths or []:
        if not _is_path_within_download(p):
            continue
        try:
            if os.path.isfile(p):
                os.remove(p)
                removed += 1
        except OSError as e:
            logger.warning(f"删除文件失败 {p}: {e}")
        # 动图视频：同时删除同名封面
        if os.path.splitext(p)[1].lower() in ('.webm', '.mp4', '.mkv', '.m4v'):
            poster = p + '.jpg'
            try:
                if os.path.isfile(poster):
                    os.remove(poster)
            except OSError:
                pass
        # 同步删除对应缩略图缓存
        if os.path.isdir(thumb_dir):
            thumb_path = os.path.join(thumb_dir, hashlib.md5(p.encode()).hexdigest() + '.jpg')
            try:
                if os.path.isfile(thumb_path):
                    os.remove(thumb_path)
            except OSError:
                pass
    return removed


def _remove_artist_directory(artist_id: int) -> int:
    """删除作者目录（download_path 下形如 {artist_id}_* 的目录）"""
    import shutil
    download = os.path.abspath(get_config().download_path)
    removed = 0
    try:
        for name in os.listdir(download):
            if name.startswith(f'{artist_id}_'):
                full = os.path.join(download, name)
                if os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
                    removed += 1
    except OSError as e:
        logger.warning(f"删除作者目录失败 {artist_id}: {e}")
    return removed


@app.route('/api/work/<int:work_id>/delete', methods=['POST'])
def api_delete_work(work_id):
    """删除单个作品（数据库 + 本地文件 + 缩略图）"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        images_deleted, paths = db_obj.delete_work_with_files(work_id)
        files_removed = _remove_local_image_files(paths)
        return jsonify({
            'success': True,
            'message': f'已删除作品 {work_id}',
            'data': {'images_deleted': images_deleted, 'files_removed': files_removed}
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/artist/<int:artist_id>/delete', methods=['POST'])
def api_delete_artist(artist_id):
    """删除作者及其全部作品（数据库 + 本地目录 + 缩略图）"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        images_deleted, works_deleted, paths = db_obj.delete_artist_with_files(artist_id)
        files_removed = _remove_local_image_files(paths)
        dirs_removed = _remove_artist_directory(artist_id)
        return jsonify({
            'success': True,
            'message': f'已删除作者 {artist_id}',
            'data': {
                'images_deleted': images_deleted,
                'works_deleted': works_deleted,
                'files_removed': files_removed,
                'dirs_removed': dirs_removed,
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ============ 过滤记录 ============

@app.route('/api/filter/records')
def api_filter_records():
    """获取过滤记录列表"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        records = db_obj.get_filter_records(limit=50)
        return jsonify({'success': True, 'data': records})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/filter/stats')
def api_filter_stats():
    """获取过滤统计"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        stats = db_obj.get_filter_stats()
        return jsonify({'success': True, 'data': stats})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ============ 收藏(书签) ============

@app.route('/api/bookmarks')
def api_bookmarks():
    """获取收藏作品列表（分页）- 不在此获取在线缩略图，前端自行处理"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        rest_type = request.args.get('rest_type') or None
        status = request.args.get('status', 'active')
        limit = request.args.get('limit', 200, type=int)
        offset = request.args.get('offset', 0, type=int)
        data = db_obj.get_bookmarks_with_detail(rest_type=rest_type, status=status, limit=limit, offset=offset)
        total = db_obj.get_bookmark_count(rest_type=rest_type, status=status)
        return jsonify({'success': True, 'data': data, 'total': total})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/bookmarks/refill', methods=['POST'])
def api_bookmarks_refill():
    """批量补全收藏作品（下载并写入作品元数据/标签/图片）"""
    try:
        data = request.get_json() or {}
        raw_ids = data.get('work_ids') or []
        ids = [int(x) for x in raw_ids if str(x).isdigit()]
        if not ids:
            return jsonify({'success': False, 'error': '没有需要补全的作品'})
        task_id = data.get('task_id') or f'bm_refill_{int(time.time())}'
        submitted = task_manager.submit('bookmark-refill', {'task_id': task_id, 'work_ids': ids})
        return jsonify({'success': True, 'task_id': submitted, 'count': len(ids)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/bookmarks/sync', methods=['POST'])
def api_bookmarks_sync():
    """同步收藏列表（只同步不下载）"""
    try:
        db_obj, api_obj, _, _ = get_or_init_services()
        data = request.get_json() or {}
        rest = data.get('rest', 'show')
        limit = data.get('limit')
        max_pages = data.get('max_pages')
        if limit == 0 or limit is None or limit == '':
            limit = None
        else:
            limit = int(limit)
        if max_pages == 0 or max_pages is None or max_pages == '':
            max_pages = None
        else:
            max_pages = int(max_pages)
        
        work_ids = api_obj.get_bookmarks(rest=rest, limit=limit, max_pages=max_pages)
        if not work_ids:
            return jsonify({'success': True, 'added': 0, 'restored': 0, 'removed': 0, 'total': 0, 'message': '没有收藏作品'})
        
        result = db_obj.sync_bookmarks([int(wid) for wid in work_ids], rest_type=rest)
        return jsonify({
            'success': True,
            'added': result['added'],
            'restored': result['restored'],
            'removed': result['removed'],
            'total': len(work_ids),
            'message': f'同步完成: 共 {len(work_ids)} 个收藏，新增 {result["added"]}，恢复 {result["restored"]}，取消 {result["removed"]}'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/bookmarks/stats')
def api_bookmarks_stats():
    """获取收藏统计"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        show_count = db_obj.get_bookmark_count(rest_type='show')
        hide_count = db_obj.get_bookmark_count(rest_type='hide')
        return jsonify({
            'success': True,
            'data': {
                'show': show_count,
                'hide': hide_count,
                'total': show_count + hide_count,
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/bookmarks/check/<int:work_id>')
def api_bookmarks_check(work_id):
    """检查作品是否已收藏"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        is_bm = db_obj.is_bookmarked(work_id)
        return jsonify({'success': True, 'is_bookmarked': is_bm})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/bookmarks/batch-check', methods=['POST'])
def api_bookmarks_batch_check():
    """批量检查作品是否已收藏"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        data = request.get_json() or {}
        work_ids = data.get('work_ids', [])
        if not work_ids:
            return jsonify({'success': True, 'data': {}})
        bm_set = db_obj.get_bookmarked_ids_set()
        result = {str(wid): (wid in bm_set) for wid in work_ids}
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ============ 图片浏览相关路由 ============

# 筛选元数据缓存：标签联动 / 作者列表在翻页时结果不变，避免重复全量聚合（约 70-90ms/次）
_filter_meta_cache = {}
_FILTER_META_TTL = 20  # 秒


def _cached_filter_tags(db_obj, tags, artist_id, search):
    key = ('tags', tuple(tags), artist_id, search)
    now = time.time()
    hit = _filter_meta_cache.get(key)
    if hit and now - hit[0] < _FILTER_META_TTL:
        return hit[1]
    value = db_obj.get_filter_tags(tags=tags, artist_id=artist_id, search=search, limit=200)
    if len(_filter_meta_cache) > 200:
        _filter_meta_cache.clear()
    _filter_meta_cache[key] = (now, value)
    return value


def _cached_filter_authors(db_obj):
    key = ('authors',)
    now = time.time()
    hit = _filter_meta_cache.get(key)
    if hit and now - hit[0] < _FILTER_META_TTL:
        return hit[1]
    with db_obj.get_connection() as conn:
        rows = conn.execute('''
            SELECT a.id, a.name, a.account, COUNT(DISTINCT i.id) as img_count
            FROM artists a
            JOIN works w ON w.artist_id = a.id
            JOIN images i ON i.work_id = w.id AND i.is_downloaded = 1
            GROUP BY a.id
            ORDER BY img_count DESC
            LIMIT 200
        ''').fetchall()
    value = [dict(r) for r in rows]
    if len(_filter_meta_cache) > 200:
        _filter_meta_cache.clear()
    _filter_meta_cache[key] = (now, value)
    return value


@app.route('/api/images/browse')
def api_images_browse():
    """分页浏览图片（支持标签/作者/搜索过滤 + 侧边栏筛选面板数据）"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 40, type=int)
        tags_str = request.args.get('tags', '').strip()
        tags = [t.strip() for t in tags_str.split(',') if t.strip()] if tags_str else []
        artist_id = request.args.get('artist_id', 0, type=int)
        search = request.args.get('search', '').strip()

        # 限制 per_page 范围
        per_page = max(12, min(per_page, 100))

        result = db_obj.browse_images(
            page=page, per_page=per_page,
            tags=tags, artist_id=artist_id, search=search
        )

        # 联动标签 + 作者列表（带短 TTL 缓存，翻页时无需重复聚合）
        all_tags = _cached_filter_tags(db_obj, tags, artist_id, search)
        filter_authors = _cached_filter_authors(db_obj)

        return jsonify({
            'success': True,
            'data': {
                **result,
                'filter_tags': all_tags,
                'filter_authors': filter_authors,
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ============ 标签相关路由 ============

@app.route('/api/tags')
def api_tags():
    """获取所有标签列表（按作品数量降序）"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        tags = db_obj.get_all_tags()
        return jsonify({'success': True, 'data': tags})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/tags/status')
def api_tags_status():
    """获取标签补全状态（缺少标签的作品数等）"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        without_tags = db_obj.get_work_count_without_tags()
        total_works = db_obj.get_stats().get('works', 0)
        return jsonify({
            'success': True,
            'data': {
                'without_tags': without_tags,
                'total_works': total_works,
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/tags/sync', methods=['POST'])
def api_tags_sync():
    """
    批量同步已有作品的 tags（从 Pixiv API 拉取）
    请求体: { limit: 每次同步数量, max_total: 最多同步总数, delay: API请求间隔 }
    """
    try:
        _, api_obj, _, _ = get_or_init_services()
        db_obj, _, _, _ = get_or_init_services()
        data = request.get_json() or {}
        limit = data.get('limit', 5)
        max_total = data.get('max_total', 50)
        delay = data.get('delay', 2.0)
        
        # 获取没有 tags 的作品
        total_without_tags = db_obj.get_work_count_without_tags()
        work_ids = db_obj.get_works_without_tags(limit=min(limit, max_total))
        
        if not work_ids:
            return jsonify({
                'success': True,
                'data': {'synced': 0, 'total_remaining': 0, 'message': '所有作品已有标签'}
            })
        
        synced = 0
        failed = 0
        deleted = 0
        for wid in work_ids:
            try:
                info, status_code = api_obj.get_work_info(str(wid))
                if info and info.get('tags'):
                    db_obj.set_work_tags(wid, info['tags'])
                    synced += 1
                    if db_obj.is_work_deleted(wid):
                        db_obj.mark_work_restored(wid)
                elif status_code == 404:
                    db_obj.mark_work_deleted(wid)
                    deleted += 1
                else:
                    failed += 1
                time.sleep(delay)
            except Exception as e:
                logger.warning(f'同步作品 {wid} 标签失败: {e}')
                failed += 1
        
        remaining = total_without_tags - synced - failed - deleted
        return jsonify({
            'success': True,
            'data': {
                'synced': synced,
                'failed': failed,
                'deleted': deleted,
                'total_remaining': max(0, remaining),
                'total_without_tags': total_without_tags
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/tags/artists')
def api_tags_artists():
    """根据标签查询作者列表"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        tag = request.args.get('tag', '').strip()
        if not tag:
            return jsonify({'success': False, 'error': '缺少 tag 参数'})
        artists = db_obj.get_artists_by_tag(tag)
        return jsonify({'success': True, 'data': artists})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/artist/<int:artist_id>/tags')
def api_artist_tags(artist_id):
    """获取指定作者的标签统计"""
    try:
        db_obj, _, _, _ = get_or_init_services()
        tags = db_obj.get_artist_tags(artist_id)
        return jsonify({'success': True, 'data': tags})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ============ 重启路由 ============

_restart_args = None  # 保存启动参数用于重启


@app.route('/api/system/restart', methods=['POST'])
def api_system_restart():
    """重启 Flask 服务"""
    import signal

    def shutdown_server():
        """延迟关闭，给前端返回响应的时间"""
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=shutdown_server, daemon=True).start()
    return jsonify({'success': True, 'message': 'Flask 正在重启...'})


# ============ 启动 ============

def main():
    """启动 Flask 应用"""
    global _restart_args
    import argparse
    parser = argparse.ArgumentParser(description='Pixiv Downloader Web UI')
    parser.add_argument('-H', '--host', default='0.0.0.0', help='监听地址')
    parser.add_argument('-p', '--port', type=int, default=5000, help='监听端口')
    parser.add_argument('-d', '--debug', action='store_true', help='调试模式（启用后修改代码自动热重载）')
    parser.add_argument('--no-reload', action='store_true', help='debug模式下也禁止自动重载')
    args = parser.parse_args()
    _restart_args = args

    use_reloader = args.debug and not args.no_reload

    # 初始化服务（必须在 task_manager.start() 之前，因为需要 DB 实例）
    init_services()
    # 启动任务管理器（含从 DB 恢复未完成任务）
    task_manager.start(db_instance=db)

    print(f"""
╔══════════════════════════════════════════╗
║     Pixiv Downloader - Web 界面          ║
║                                          ║
║  地址: http://{args.host}:{args.port}              ║
║                                          ║
║  按 Ctrl+C 退出                          ║
║  修改代码保存后自动重启（debug模式）     ║
║  或访问 /api/system/restart 手动重启     ║
╚══════════════════════════════════════════╝
    """)

    app.run(
        host=args.host,
        port=args.port,
        debug=args.debug,
        threaded=True,
        use_reloader=use_reloader
    )


if __name__ == '__main__':
    main()
