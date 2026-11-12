# -*- coding: utf-8 -*-
"""
Pixiv Downloader - 下载管理模块
处理图片下载和任务管理
"""
import os
import re
import time
import shutil
import zipfile
import tempfile
from typing import Dict, List, Callable, Tuple
from datetime import datetime
from dataclasses import dataclass
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

import ffmpeg_util

from database import Database
from api import PixivAPI
from config import get_config

logger = logging.getLogger(__name__)


@dataclass
class DownloadResult:
    """下载结果"""
    success: bool
    work_id: int
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    error_message: str = None


class Downloader:
    """下载管理类"""
    
    def __init__(
        self, 
        db: Database, 
        api: PixivAPI = None,
        download_path: str = None
    ):
        self.db = db
        self.api = api or PixivAPI()
        self.download_path = download_path or get_config().download_path
        self.config = get_config()
        
        # 暂停/取消控制
        self._paused = False
        self._cancelled = False
        
        # 确保下载目录存在
        if not os.path.exists(self.download_path):
            os.makedirs(self.download_path)
    
    @property
    def paused(self) -> bool:
        return self._paused
    
    @paused.setter
    def paused(self, value: bool):
        self._paused = value
    
    @property
    def cancelled(self) -> bool:
        return self._cancelled
    
    @cancelled.setter
    def cancelled(self, value: bool):
        self._cancelled = value
    
    def reset_control_flags(self):
        """重置暂停/取消标记（新任务开始时调用）"""
        self._paused = False
        self._cancelled = False
    
    def check_pause(self):
        """检查是否被暂停，如果暂停则阻塞等待恢复或被取消"""
        while self._paused and not self._cancelled:
            time.sleep(0.5)
        return not self._cancelled  # True 表示可以继续，False 表示被取消了
    
    def get_artist_dir(self, artist_id: int, artist_name: str) -> str:
        """获取作者目录路径"""
        # 清理目录名中的非法字符
        safe_name = re.sub(r'[<>:"/\\|?*]', '', artist_name)
        return os.path.join(self.download_path, f"{artist_id}_{safe_name}")
    
    def ensure_artist_dir(self, artist_id: int, artist_name: str) -> str:
        """确保作者目录存在"""
        artist_dir = self.get_artist_dir(artist_id, artist_name)
        if not os.path.exists(artist_dir):
            os.makedirs(artist_dir)
        return artist_dir
    
    def get_save_path(self, artist_dir: str, image_url: str, work_id: int, page: int) -> Tuple[str, str]:
        """
        获取保存路径
        
        Returns:
            (file_path, filename)
        """
        # 从URL提取文件扩展名
        parsed = re.search(r'\.(\w+)(?:\?.*)?$', image_url)
        ext = parsed.group(1) if parsed else 'png'
        
        filename = f"{work_id}_p{page}.{ext}"
        file_path = os.path.join(artist_dir, filename)
        
        return file_path, filename
    
    def download_image(
        self, 
        image_url: str, 
        file_path: str,
        work_id: int = None,
        page: int = 0
    ) -> Tuple[bool, str]:
        """
        下载单张图片
        
        Returns:
            (success, error_message)
        """
        # 检查文件是否已存在
        if os.path.exists(file_path):
            logger.debug(f"文件已存在，跳过: {file_path}")
            return True, None
        
        # 确保目录存在
        file_dir = os.path.dirname(file_path)
        if not os.path.exists(file_dir):
            os.makedirs(file_dir)
        
        # 准备请求头
        headers = {
            'Referer': 'https://www.pixiv.net/',
            'User-Agent': self.api.user_agent,
        }
        
        last_error = None
        for attempt in range(max(1, int(self.config.max_retries or 1))):
            try:
                response = self.api.session.get(
                    image_url, 
                    headers=headers, 
                    stream=True,
                    timeout=self.config.timeout
                )
                response.raise_for_status()
                
                # 保存文件
                with open(file_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=self.config.chunk_size):
                        if chunk:
                            f.write(chunk)
                
                logger.info(f"下载成功: {os.path.basename(file_path)}")
                return True, None
                
            except Exception as e:
                last_error = e
                logger.warning(f"下载失败 (尝试 {attempt + 1}/{self.config.max_retries}): {e}")
                if attempt < self.config.max_retries - 1:
                    time.sleep(self.config.retry_delay)
        
        # 所有重试都失败了
        error_msg = f"下载失败: {last_error}" if last_error else "下载失败"
        logger.error(error_msg)
        
        # 删除可能创建的不完整文件
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass
        
        return False, error_msg
    
    def download_single_work(
        self, 
        work_id: str,
        progress_callback: Callable = None,
        skip_existing: bool = True,
        batch_filtered: Dict[str, str] = None,
        skip_ai_filter: bool = False
    ) -> DownloadResult:
        """
        下载单个作品（逐个处理作品流程）
        
        Args:
            work_id: 作品ID
            progress_callback: 进度回调 callback(work_id, current, total, message)
            skip_existing: 是否跳过已存在的文件
            batch_filtered: 批量AI检测结果 {work_id: reason}
            skip_ai_filter: 是否跳过AI过滤（收藏下载时使用）
        
        Returns:
            DownloadResult: 下载结果
        """
        result = DownloadResult(success=False, work_id=int(work_id))
        
        try:
            # 1. 获取作品信息
            if progress_callback:
                progress_callback(work_id, 0, 3, "获取作品信息...")
            
            work_info, status_code = self.api.get_work_info(work_id)
            if not work_info:
                if status_code == 404:
                    result.error_message = "作品已被删除（Pixiv 上不存在）"
                else:
                    result.error_message = "无法获取作品信息"
                return result
            
            # 1.5 过滤检查（在下载前）
            should_skip = False
            skip_reason = ''
            if not skip_ai_filter:
                should_skip, skip_reason = self.filter_and_record(work_id, work_info, batch_filtered)
            if should_skip:
                result.success = True  # 不算失败
                result.error_message = f"已过滤: {skip_reason}"
                logger.info(f"作品 {work_id} 被过滤跳过: {skip_reason}")
                return result
            
            artist_id = work_info.get('userId', '')
            artist_name = work_info.get('userName', f"artist_{artist_id}")
            page_count = work_info.get('pageCount', 1)
            
            # 2. 保存/更新作品到数据库（始终刷新元数据，修复历史遗留的占位标题/缺失信息）
            self.db.add_or_update_work(
                work_id=int(work_id),
                artist_id=int(artist_id),
                title=work_info.get('title', ''),
                description=work_info.get('description', ''),
                page_count=page_count,
                width=work_info.get('width'),
                height=work_info.get('height'),
                created_at=work_info.get('createDate')
            )
            # 保存 tags（下载时同步获取并存入数据库）
            tags = work_info.get('tags', [])
            if tags:
                self.db.set_work_tags(int(work_id), tags)
            # 如果之前被标记为已删除，自动恢复
            if self.db.is_work_deleted(int(work_id)):
                self.db.mark_work_restored(int(work_id))
                logger.info(f"作品 {work_id} 已恢复（之前被标记为已删除）")
            
            # 确保作者目录存在
            artist_dir = self.ensure_artist_dir(int(artist_id), artist_name)
            
            # 确保作者在数据库中
            self.db.add_or_update_artist(int(artist_id), artist_name)

            # 2.5 ugoira（动图）走独立流程：zip → 帧 → ffmpeg 合成 webm
            if int(work_info.get('illustType') or 0) == 2:
                return self._download_ugoira_work(
                    work_id, artist_id, artist_name, artist_dir,
                    work_info, result, progress_callback, skip_existing
                )

            # 3. 获取下载链接
            if progress_callback:
                progress_callback(work_id, 1, 3, "获取下载链接...")
            
            image_info_list = self.api.get_work_download_urls(work_id)
            if not image_info_list:
                result.error_message = "无法获取下载链接"
                return result
            
            # 4. 下载每个页面（并发）
            if progress_callback:
                progress_callback(work_id, 2, 3, f"开始下载 {len(image_info_list)} 张图片...")

            # 下载前检查取消/暂停
            if self._cancelled:
                result.error_message = "任务已取消"
                return result
            if not self.check_pause():
                result.error_message = "任务已取消"
                return result

            # 过滤出需要下载的页（已存在的跳过）
            to_download = []
            for img_info in image_info_list:
                image_url = img_info['url']
                page = img_info['page']
                file_path, filename = self.get_save_path(artist_dir, image_url, int(work_id), page)
                if skip_existing and os.path.exists(file_path):
                    result.skipped += 1
                    logger.debug(f"跳过已存在: {filename}")
                    continue
                to_download.append((page, filename, file_path, image_url))

            if to_download:
                max_workers = max(1, int(getattr(self.config, 'download_concurrency', 4) or 4))

                def _download_one(item):
                    page, filename, file_path, image_url = item
                    success, error = self.download_image(image_url, file_path, work_id, page)
                    file_size = os.path.getsize(file_path) if success and os.path.exists(file_path) else None
                    return page, filename, file_path, image_url, file_size, success, error

                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = [pool.submit(_download_one, item) for item in to_download]
                    for fut in as_completed(futures):
                        try:
                            page, filename, file_path, image_url, file_size, success, error = fut.result()
                        except Exception as ex:
                            result.failed += 1
                            if not result.error_message:
                                result.error_message = str(ex)
                            continue
                        if success:
                            result.downloaded += 1
                            self.db.add_image(
                                work_id=int(work_id),
                                page=page,
                                filename=filename,
                                file_path=file_path,
                                file_size=file_size,
                                original_url=image_url,
                                is_downloaded=True,
                                downloaded_at=datetime.now()
                            )
                        else:
                            result.failed += 1
                            if not result.error_message:
                                result.error_message = error
            
            if progress_callback:
                progress_callback(work_id, 3, 3, f"完成: 下载{result.downloaded}, 跳过{result.skipped}, 失败{result.failed}")
            
            result.success = result.failed == 0
            
        except Exception as e:
            result.error_message = str(e)
            logger.error(f"下载作品失败 {work_id}: {e}")
        
        return result

    def _download_ugoira_work(self, work_id, artist_id, artist_name, artist_dir,
                              work_info, result, progress_callback, skip_existing):
        """
        下载 ugoira 动图：
        1. 取 ugoira_meta（zip 地址 + 每帧延时）
        2. 下载 zip 并解压帧
        3. ffmpeg 合成 {work_id}_p0.webm，首帧另存 {work_id}_p0.webm.jpg 作封面
        4. 写入 images 表（page=0）
        未检测到 ffmpeg 时跳过，不影响其他图片。
        """
        work_id = int(work_id)

        if not ffmpeg_util.is_available():
            result.success = True          # 视为正常跳过，不算失败
            result.skipped += 1
            result.error_message = '未检测到 ffmpeg，已跳过 ugoira 动图'
            logger.warning(f"作品 {work_id} 是 ugoira，但未检测到 ffmpeg，已跳过")
            if progress_callback:
                progress_callback(work_id, 3, 3, '未检测到 ffmpeg，已跳过动图')
            return result

        video_path = os.path.join(artist_dir, f'{work_id}_p0.webm')
        poster_path = video_path + '.jpg'

        # 已存在：跳过，并确保数据库有记录
        if skip_existing and os.path.exists(video_path):
            result.skipped += 1
            existing = {im.file_path for im in self.db.get_work_images(work_id)}
            if video_path not in existing:
                self.db.add_image(
                    work_id=work_id, page=0,
                    filename=os.path.basename(video_path),
                    file_path=video_path,
                    file_size=os.path.getsize(video_path),
                    original_url='',
                    is_downloaded=True,
                    downloaded_at=datetime.now()
                )
            if progress_callback:
                progress_callback(work_id, 3, 3, '动图已存在，跳过')
            return result

        if self._cancelled:
            result.error_message = '任务已取消'
            return result
        if not self.check_pause():
            result.error_message = '任务已取消'
            return result

        if progress_callback:
            progress_callback(work_id, 1, 3, '获取动图元数据...')
        meta = self.api.get_ugoira_meta(str(work_id))
        if not meta or not meta.get('src') or not meta.get('frames'):
            result.failed += 1
            result.error_message = '无法获取 ugoira 元数据'
            return result

        frames = meta['frames']
        tmp_dir = tempfile.mkdtemp(prefix=f'ugoira_{work_id}_')
        try:
            zip_path = os.path.join(tmp_dir, f'{work_id}.zip')
            if progress_callback:
                progress_callback(work_id, 2, 3, '下载动图压缩包...')
            ok, err = self.download_image(meta['src'], zip_path, work_id, 0)
            if not ok:
                result.failed += 1
                result.error_message = f'动图压缩包下载失败: {err}'
                return result

            frames_dir = os.path.join(tmp_dir, 'frames')
            os.makedirs(frames_dir, exist_ok=True)
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    ffmpeg_util.safe_extract(zf, frames_dir)
            except zipfile.BadZipFile:
                result.failed += 1
                result.error_message = '动图压缩包损坏'
                return result

            if progress_callback:
                progress_callback(work_id, 2, 3, f'合成动图（{len(frames)} 帧）...')
            ok, err = ffmpeg_util.convert_ugoira(frames_dir, frames, video_path)
            if not ok:
                result.failed += 1
                result.error_message = f'动图合成失败: {err}'
                logger.error(f"ugoira 合成失败 {work_id}: {err}")
                return result

            # 首帧作为封面（缩略图/列表用）
            first_frame = os.path.join(frames_dir, frames[0].get('file', ''))
            if os.path.isfile(first_frame):
                ffmpeg_util.make_poster(first_frame, poster_path)

            file_size = os.path.getsize(video_path) if os.path.exists(video_path) else None
            self.db.add_image(
                work_id=work_id, page=0,
                filename=os.path.basename(video_path),
                file_path=video_path,
                file_size=file_size,
                original_url=meta.get('src', ''),
                is_downloaded=True,
                downloaded_at=datetime.now()
            )
            result.downloaded += 1
            result.success = True
            if progress_callback:
                progress_callback(work_id, 3, 3, '动图下载完成')
            return result

        except Exception as e:
            result.failed += 1
            result.error_message = f'动图处理失败: {e}'
            logger.error(f"ugoira 处理失败 {work_id}: {e}")
            return result
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def download_artist_works(
        self,
        artist_id: str,
        limit: int = None,
        progress_callback: Callable = None,
        skip_existing: bool = True
    ) -> Dict:
        """
        下载指定作者的作品（增量下载，逐个处理）
        
        Args:
            artist_id: 作者ID
            limit: 下载数量限制（从最新开始），None表示全部
            progress_callback: 进度回调
            skip_existing: 是否跳过已存在的文件
        
        Returns:
            统计结果
        """
        stats = {
            'total_works': 0,
            'downloaded_works': 0,
            'skipped_works': 0,
            'failed_works': 0,
            'total_images': 0,
        }
        
        try:
            # 1. 获取作者信息
            print("[1/3] 获取作者信息...")
            
            artist_info = self.api.get_artist_info(artist_id)
            if not artist_info:
                logger.error(f"无法获取作者信息: {artist_id}")
                return stats
            
            artist_name = artist_info.get('name', f"artist_{artist_id}")
            self.db.add_or_update_artist(int(artist_id), artist_name)
            
            # 2. 获取所有作品ID
            print("[2/3] 获取作品列表...")
            
            work_ids = self.api.get_artist_all_works(artist_id)
            if not work_ids:
                logger.warning(f"作者没有作品或获取失败: {artist_id}")
                return stats
            
            stats['total_works'] = len(work_ids)
            print(f"共 {len(work_ids)} 个作品")
            
            # 3. 如果需要限制数量
            if limit:
                work_ids = work_ids[:limit]
            
            # 3.5 批量AI检测（下载前对全量作品做一次检测）
            batch_filtered = self._detect_batch_ai(work_ids)
            if batch_filtered:
                logger.info(f"批量检测发现 {len(batch_filtered)} 个疑似AI批量作品")

            # 4. 逐个处理作品
            print(f"[3/3] 开始下载...")
            # 预取作品信息与图片数，避免循环内逐作品查库（N+1）
            prefetched_works = {}
            prefetched_counts = {}
            if skip_existing:
                num_ids = [int(w) for w in work_ids]
                prefetched_works = self.db.get_works_by_ids(num_ids)
                prefetched_counts = self.db.get_work_image_counts(num_ids)

            for i, work_id in enumerate(work_ids):
                # 检查取消/暂停
                if self._cancelled:
                    break
                if not self.check_pause():
                    break
                
                if progress_callback:
                    progress_callback(work_id, i + 1, len(work_ids), f"{i + 1}/{len(work_ids)}")
                
                # 检查作品是否已完全下载（用预取数据，0 次循环内查库）
                if skip_existing:
                    work = prefetched_works.get(int(work_id))
                    if work and work.page_count and \
                            prefetched_counts.get(int(work_id), 0) >= work.page_count:
                        stats['skipped_works'] += 1
                        continue
                
                # 下载作品
                result = self.download_single_work(work_id, skip_existing=skip_existing, batch_filtered=batch_filtered)
                
                if result.downloaded > 0:
                    stats['downloaded_works'] += 1
                    stats['total_images'] += result.downloaded
                elif result.skipped > 0:
                    stats['skipped_works'] += 1
                else:
                    stats['failed_works'] += 1
                
                # API请求限流（保持较长延迟）
                self.api.wait()
                
        except Exception as e:
            logger.error(f"下载作者作品失败 {artist_id}: {e}")
        
        return stats
    
    def download_by_url(
        self,
        url: str,
        progress_callback: Callable = None
    ) -> Tuple[bool, str]:
        """
        根据URL下载（自动识别作品或作者）
        
        Returns:
            (success, message)
        """
        url_type, target_id = self.api.parse_url(url)
        
        if url_type == 'work':
            result = self.download_single_work(target_id, progress_callback)
            if result.success:
                return True, f"作品 {target_id} 下载完成"
            else:
                return False, result.error_message or "下载失败"
        
        elif url_type == 'artist':
            stats = self.download_artist_works(target_id, progress_callback=progress_callback)
            return True, f"作者 {target_id} 下载完成: {stats['downloaded_works']} 个作品"
        
        else:
            return False, "无法识别的URL"
    
    def download_artist_new_works(
        self,
        artist_id: str,
        progress_callback: Callable = None,
        skip_existing: bool = True
    ) -> Dict:
        """
        增量下载作者的新作品（只下载数据库中还没有的）
        
        Args:
            artist_id: 作者ID
            progress_callback: 进度回调
            skip_existing: 是否跳过已存在的文件
        
        Returns:
            统计结果
        """
        stats = {
            'total_works': 0,
            'new_works': 0,
            'downloaded_works': 0,
            'skipped_works': 0,
            'failed_works': 0,
            'total_images': 0,
        }
        
        try:
            # 1. 获取作者信息
            if progress_callback:
                progress_callback(None, 0, 4, "获取作者信息...")
            
            artist_info = self.api.get_artist_info(artist_id)
            if not artist_info:
                logger.error(f"无法获取作者信息: {artist_id}")
                return stats
            
            artist_name = artist_info.get('name', f"artist_{artist_id}")
            self.db.add_or_update_artist(int(artist_id), artist_name)
            
            # 2. 获取数据库中已有的作品ID
            if progress_callback:
                progress_callback(None, 1, 4, "查询已有作品...")
            
            existing_works = self.db.get_artist_works(int(artist_id))
            existing_ids = {w.id for w in existing_works}
            
            # 3. 获取全量作品列表并过滤出新作品
            if progress_callback:
                progress_callback(None, 2, 4, "获取作品列表并对比...")
            
            new_work_ids = self.api.get_artist_new_works(artist_id, existing_ids)
            
            stats['total_works'] = len(existing_ids) + len(new_work_ids)
            stats['new_works'] = len(new_work_ids)
            
            if not new_work_ids:
                if progress_callback:
                    progress_callback(None, 4, 4, "没有新作品，已是最新")
                return stats
            
            # 3.5 批量AI检测
            batch_filtered = self._detect_batch_ai(new_work_ids)

            # 4. 逐个下载新作品
            if progress_callback:
                progress_callback(None, 3, 4, f"发现 {len(new_work_ids)} 个新作品，开始下载...")
            
            for i, work_id in enumerate(new_work_ids):
                # 检查取消/暂停
                if self._cancelled:
                    break
                if not self.check_pause():
                    break
                    
                if progress_callback:
                    progress_callback(work_id, i + 1, len(new_work_ids), f"{i + 1}/{len(new_work_ids)}")
                
                result = self.download_single_work(work_id, skip_existing=skip_existing, batch_filtered=batch_filtered)
                
                if result.downloaded > 0:
                    stats['downloaded_works'] += 1
                    stats['total_images'] += result.downloaded
                elif result.skipped > 0:
                    stats['skipped_works'] += 1
                else:
                    stats['failed_works'] += 1
                
                self.api.wait()
            
            if progress_callback:
                progress_callback(None, 4, 4, "增量下载完成")
                
        except Exception as e:
            logger.error(f"增量下载作者作品失败 {artist_id}: {e}")
        
        return stats

    def download_bookmarks(
        self,
        rest: str = 'show',
        limit: int = None,
        max_pages: int = None,
        progress_callback: Callable = None,
        skip_existing: bool = True
    ) -> Dict:
        """
        下载收藏(书签)作品
        
        Args:
            rest: 'show'=公开收藏, 'hide'=私密收藏
            limit: 最大作品数量
            max_pages: 最大翻页数
            progress_callback: 进度回调
            skip_existing: 是否跳过已存在的文件
        
        Returns:
            统计结果
        """
        stats = {
            'rest_type': '公开收藏' if rest == 'show' else '私密收藏',
            'total_bookmarks': 0,
            'downloaded_works': 0,
            'skipped_works': 0,
            'failed_works': 0,
            'total_images': 0,
        }
        
        # 是否跳过AI过滤（收藏下载默认跳过）
        skip_filter = getattr(self.config, 'bookmark_skip_filter', True)
        
        try:
            # 1. 获取收藏作品列表
            if progress_callback:
                progress_callback(None, 0, 3, "获取收藏列表...")
            
            work_ids = self.api.get_bookmarks(rest=rest, limit=limit, max_pages=max_pages)
            
            if not work_ids:
                if progress_callback:
                    progress_callback(None, 3, 3, "没有收藏作品")
                return stats
            
            stats['total_bookmarks'] = len(work_ids)
            
            # 1.5 批量AI检测（收藏下载可配置跳过）
            if skip_filter:
                batch_filtered = {}
                logger.info("[收藏下载] 已跳过AI批量过滤（bookmark_skip_filter=True）")
            else:
                batch_filtered = self._detect_batch_ai(work_ids)
            
            # 1.6 同步收藏列表到本地数据库
            self.db.sync_bookmarks([int(wid) for wid in work_ids], rest_type=rest)

            # 2. 逐个下载（增量补充模式）
            if progress_callback:
                progress_callback(None, 1, 3, f"共 {len(work_ids)} 个收藏作品，开始下载...")

            # 预取作品信息与图片数，避免循环内逐作品查库（N+1）
            prefetched_works = {}
            prefetched_counts = {}
            if skip_existing:
                num_ids = [int(w) for w in work_ids]
                prefetched_works = self.db.get_works_by_ids(num_ids)
                prefetched_counts = self.db.get_work_image_counts(num_ids)

            for i, work_id in enumerate(work_ids):
                # 检查取消/暂停
                if self._cancelled:
                    break
                if not self.check_pause():
                    break
                    
                if progress_callback:
                    progress_callback(work_id, i + 1, len(work_ids), f"{i + 1}/{len(work_ids)}")
                
                # 增量补充：检查是否已有作品记录，对比页数（用预取数据，0 次循环内查库）
                if skip_existing:
                    work = prefetched_works.get(int(work_id))
                    if work and work.page_count:
                        expected = work.page_count
                        existing_count = prefetched_counts.get(int(work_id), 0)
                        if existing_count >= expected:
                            stats['skipped_works'] += 1
                            continue
                        # 有部分缺失 -> 继续下载（增量补充）
                        if existing_count > 0:
                            logger.info(f"[收藏下载] 作品 {work_id} 已有 {existing_count}/{expected} 页，增量补充中...")
                
                result = self.download_single_work(
                    work_id,
                    skip_existing=skip_existing,
                    batch_filtered=batch_filtered,
                    skip_ai_filter=skip_filter
                )
                
                if result.downloaded > 0:
                    stats['downloaded_works'] += 1
                    stats['total_images'] += result.downloaded
                elif result.skipped > 0:
                    stats['skipped_works'] += 1
                else:
                    stats['failed_works'] += 1
                
                self.api.wait()
            
            if progress_callback:
                progress_callback(None, 3, 3, "收藏下载完成")
                
        except Exception as e:
            logger.error(f"下载收藏作品失败: {e}")
        
        return stats

    # ============ AI批量作品过滤 ============

    def _should_filter_work(self, work_info: Dict, artist_id: int = None) -> Tuple[bool, str]:
        """
        判断是否应该过滤（跳过）这个作品

        Args:
            work_info: 作品信息（来自 API get_work_info）
            artist_id: 作者ID（可选，用于批量检测）

        Returns:
            (是否过滤, 过滤原因)
        """
        cfg = self.config

        # 检查过滤是否启用
        if not getattr(cfg, 'filter_enabled', False):
            return False, ''

        page_count = work_info.get('pageCount', 1)
        work_id = work_info.get('id', '')

        # 规则1: 页数超过阈值
        max_pages = getattr(cfg, 'filter_max_pages', 30)
        if max_pages > 0 and page_count > max_pages:
            reason = f'页数过多 ({page_count}页 > {max_pages}页阈值)'
            logger.info(f'[过滤] 作品 {work_id} - {reason}')
            return True, reason

        # 规则2: 作品描述/标题包含明显AI标记
        title = (work_info.get('title', '') or '').lower()
        description = (work_info.get('description', '') or '').lower()
        ai_keywords = [
            'ai生成', 'ai绘图', 'aiイラスト', 'ai art',
            'stablediffusion', 'stable diffusion', 'novelai',
            'midjourney', 'dall-e', 'dalle',
            '生成ai', 'ai-generated', 'aiillust',
        ]
        for kw in ai_keywords:
            if kw in title or kw in description:
                # AI标记 + 页数较多时才过滤（避免误杀单张AI图）
                if page_count >= 5:
                    reason = f'AI标记"{kw}" + 页数{page_count}页'
                    logger.info(f'[过滤] 作品 {work_id} - {reason}')
                    return True, reason

        return False, ''

    def _detect_batch_ai(self, work_ids: List[str]) -> Dict[str, str]:
        """
        检测同作者批量AI生成

        获取作者所有作品的日期分布，如果同一天上传超过阈值且平均页数高，
        则标记整批作品。

        Args:
            work_ids: 待下载的作品ID列表

        Returns:
            {work_id: reason} 需要过滤的作品及原因
        """
        cfg = self.config
        if not getattr(cfg, 'filter_enabled', False):
            return {}

        batch_threshold = getattr(cfg, 'filter_batch_same_day', 5)
        avg_pages_threshold = getattr(cfg, 'filter_batch_avg_pages', 10)

        # 按作者分组
        artist_works: Dict[str, List[Dict]] = defaultdict(list)

        # 一次性批量取作品信息（避免逐作品查库的 N+1）
        works_map = self.db.get_works_by_ids([int(w) for w in work_ids])
        for wid in work_ids:
            work = works_map.get(int(wid))
            if work and work.artist_id:
                artist_works[str(work.artist_id)].append({
                    'work_id': wid,
                    'page_count': work.page_count or 1,
                    'created_at': work.created_at,
                })

        filtered = {}
        for artist_id, works in artist_works.items():
            if len(works) < batch_threshold:
                continue

            # 按日期分组
            date_groups: Dict[str, List[Dict]] = defaultdict(list)
            for w in works:
                if w['created_at']:
                    date_str = w['created_at'][:10] if isinstance(w['created_at'], str) else w['created_at'].strftime('%Y-%m-%d')
                    date_groups[date_str].append(w)

            # 检查是否有同一天大量上传的
            for date_str, day_works in date_groups.items():
                if len(day_works) >= batch_threshold:
                    # 计算平均页数
                    avg_pages = sum(w['page_count'] for w in day_works) / len(day_works)
                    if avg_pages >= avg_pages_threshold:
                        for w in day_works:
                            reason = f'疑似批量AI: 作者{artist_id}在{date_str}上传{len(day_works)}个作品，平均{avg_pages:.0f}页'
                            filtered[str(w['work_id'])] = reason
                        logger.info(f'[批量检测] 作者 {artist_id} 在 {date_str} 疑似批量AI ({len(day_works)}个作品，均{avg_pages:.0f}页)')

        return filtered

    def filter_and_record(self, work_id: str, work_info: Dict, batch_filtered: Dict[str, str] = None) -> Tuple[bool, str]:
        """
        综合过滤判断并记录

        Args:
            work_id: 作品ID
            work_info: 作品详细信息
            batch_filtered: 批量检测结果

        Returns:
            (should_skip, reason)
        """
        # 检查批量检测结果
        if batch_filtered and work_id in batch_filtered:
            reason = batch_filtered[work_id]
            self.db.add_filter_record(
                work_id=int(work_id),
                filter_reason=reason,
                artist_id=work_info.get('userId'),
                artist_name=work_info.get('userName'),
                title=work_info.get('title'),
                page_count=work_info.get('pageCount'),
            )
            return True, reason

        # 检查单作品规则
        should_filter, reason = self._should_filter_work(work_info)
        if should_filter:
            self.db.add_filter_record(
                work_id=int(work_id),
                filter_reason=reason,
                artist_id=work_info.get('userId'),
                artist_name=work_info.get('userName'),
                title=work_info.get('title'),
                page_count=work_info.get('pageCount'),
            )
            return True, reason

        return False, ''
