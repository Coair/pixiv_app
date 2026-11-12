# -*- coding: utf-8 -*-
"""
Pixiv Downloader - 文件扫描同步模块
扫描现有文件并与数据库同步
"""
import os
import re
import sys
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
import logging

from database import Database
from config import get_config

# Windows 控制台默认 GBK，打印含 emoji 的目录名会抛 UnicodeEncodeError，
# 导致扫描/下载任务中途崩溃。统一改为 UTF-8 容错输出。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

logger = logging.getLogger(__name__)


def _safe_print(*args, **kwargs):
    """编码容错打印：控制台无法编码 emoji 时，替换为占位符而不是抛异常中断任务"""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        enc = (getattr(sys.stdout, 'encoding', None) or 'utf-8')
        text = ' '.join(str(a) for a in args)
        try:
            print(text.encode(enc, 'replace').decode(enc, 'replace'), **kwargs)
        except Exception:
            pass
    except Exception:
        pass


@dataclass
class ScanResult:
    """扫描结果"""
    total_dirs: int = 0
    total_files: int = 0
    new_artists: int = 0
    new_works: int = 0
    new_images: int = 0
    updated_images: int = 0
    errors: int = 0
    scanned_dirs: int = 0    # 实际扫描的目录数（增量模式）
    skipped_dirs: int = 0    # 增量跳过（无变化）的目录数
    incremental: bool = False
    error_messages: List[str] = None
    
    def __post_init__(self):
        if self.error_messages is None:
            self.error_messages = []


class FileScanner:
    """文件扫描器 - 扫描现有文件并同步到数据库"""
    
    # 目录名正则: {user_id}_{username}
    DIR_PATTERN = re.compile(r'^(\d+)_(.+)$')
    
    # 文件名正则: {work_id}_p{page}.{ext}
    # 兼容 {work_id}_p{page}-{标题}.{ext}（其他下载工具的命名，标题后缀可选）
    # 包含 webm/mp4：ugoira 动图合成后的视频（封面为 xxx.webm.jpg，不符合该正则，不会重复入库）
    FILE_PATTERN = re.compile(
        r'^(\d+)_p(\d+)(?:-.+)?\.(png|jpg|jpeg|gif|webp|webm|mp4)$', re.IGNORECASE)
    
    def __init__(self, db: Database, download_path: str = None):
        self.db = db
        self.download_path = download_path or get_config().download_path
        self._all_images = []  # 收集所有图片，一次性写入
    
    def parse_directory_name(self, dir_name: str) -> Optional[Tuple[int, str]]:
        """
        解析目录名，返回 (user_id, username)
        目录名格式: {user_id}_{username}
        """
        match = self.DIR_PATTERN.match(dir_name)
        if match:
            return int(match.group(1)), match.group(2)
        return None
    
    def parse_filename(self, filename: str) -> Optional[Tuple[int, int, str]]:
        """
        解析文件名，返回 (work_id, page, extension)
        文件名格式: {work_id}_p{page}.{ext}
        """
        match = self.FILE_PATTERN.match(filename)
        if match:
            return int(match.group(1)), int(match.group(2)), match.group(3).lower()
        return None
    
    def walk_disk_files(self):
        """
        遍历下载目录下所有文件，生成 (file_path, work_id, page, ext, file_size, artist_dir_name) 元组。

        这是一个统一的磁盘遍历生成器，供 scan_directory() 和 verify_database_integrity()
        复用，消除重复的目录/文件遍历逻辑。
        """
        if not os.path.exists(self.download_path):
            return

        for dir_name in os.listdir(self.download_path):
            artist_dir = os.path.join(self.download_path, dir_name)
            if not os.path.isdir(artist_dir):
                continue
            for filename in os.listdir(artist_dir):
                file_path = os.path.join(artist_dir, filename)
                if not os.path.isfile(file_path):
                    continue
                file_info = self.parse_filename(filename)
                if not file_info:
                    continue
                work_id, page, ext = file_info
                try:
                    file_size = os.path.getsize(file_path)
                except Exception:
                    file_size = 0
                yield file_path, work_id, page, ext, file_size, dir_name

    def _get_dir_signature(self, dir_path: str) -> Tuple[float, int]:
        """
        获取目录签名 (最新 mtime, 文件数)，用于增量扫描判断目录是否有变化。
        同时用文件数兜底：即使 mtime 没变（如覆盖写入/保留时间戳的复制），文件数变化也能发现。
        """
        latest = 0.0
        count = 0
        try:
            latest = os.path.getmtime(dir_path)
        except Exception:
            pass
        try:
            for name in os.listdir(dir_path):
                fp = os.path.join(dir_path, name)
                try:
                    if os.path.isfile(fp):
                        count += 1
                        m = os.path.getmtime(fp)
                        if m > latest:
                            latest = m
                except Exception:
                    continue
        except Exception:
            pass
        return latest, count

    def scan_directory(self, progress_callback=None, verbose=False,
                       incremental: bool = True) -> ScanResult:
        """
        扫描下载目录，同步文件到数据库

        Args:
            progress_callback: 进度回调函数 callback(work_id, current, total, message)
            verbose: 是否打印详细信息
            incremental: True=增量扫描（仅扫描自上次扫描后目录有变化的作者目录）
                         False=全量扫描所有作者目录

        Returns:
            ScanResult: 扫描结果
        """
        result = ScanResult()
        self._all_images = []  # 重置收集列表

        if not os.path.exists(self.download_path):
            print(f"[ERROR] 下载目录不存在: {self.download_path}")
            result.error_messages.append(f"目录不存在: {self.download_path}")
            result.errors = 1
            return result

        # 获取所有子目录
        try:
            # 跳过隐藏目录（如缩略图缓存 .thumb），它们不是作者目录
            subdirs = [d for d in os.listdir(self.download_path)
                      if not d.startswith('.')
                      and os.path.isdir(os.path.join(self.download_path, d))]
        except Exception as e:
            print(f"[ERROR] 读取目录失败: {e}")
            result.error_messages.append(f"读取目录失败: {e}")
            result.errors = 1
            return result

        result.total_dirs = len(subdirs)
        total_work = len(subdirs)
        mode_label = '增量' if incremental else '全量'
        print(f"[{mode_label}扫描] 发现 {total_work} 个作者目录...")

        # 增量模式：读取上次扫描的目录状态
        dir_states = {}
        if incremental:
            try:
                dir_states = self.db.get_scan_dir_states()
            except Exception as e:
                logger.warning(f"读取扫描状态失败，回退为全量扫描: {e}")
                incremental = False

        result.incremental = incremental
        new_states = {}    # 本次实际扫描并需更新状态的目录
        seen_artists = set()
        # 本次扫描到的作品：work_id -> {artist_id, max_page}
        # 用于补建缺失的 works 记录（否则图片在库里但浏览页 JOIN 不到，显示不出来）
        works_map = {}

        for i, dir_name in enumerate(subdirs):
            if verbose or (i % 10 == 0) or i == total_work - 1:
                _safe_print(f"\r进度: {i+1}/{total_work} ({100*(i+1)//total_work}%) - {dir_name}", end='', flush=True)

            if progress_callback:
                progress_callback(None, i, total_work, f"扫描: {dir_name}")

            artist_dir = os.path.join(self.download_path, dir_name)

            # 增量判断：目录签名（mtime + 文件数）与上次一致才跳过
            latest_mtime, sig_count = 0.0, 0
            if incremental:
                latest_mtime, sig_count = self._get_dir_signature(artist_dir)
                prev = dir_states.get(dir_name)
                if (prev is not None
                        and latest_mtime <= prev.get('mtime', 0)
                        and sig_count == prev.get('file_count', -1)):
                    result.skipped_dirs += 1
                    if progress_callback:
                        progress_callback(None, i, total_work, f"跳过(无变化): {dir_name}")
                    continue

            # 解析目录名
            dir_info = self.parse_directory_name(dir_name)
            if not dir_info:
                if verbose:
                    _safe_print(f"\n[WARNING] 无法解析目录名: {dir_name}")
                result.error_messages.append(f"无法解析目录名: {dir_name}")
                result.errors += 1
                continue

            user_id, username = dir_info

            # 检查作者是否已存在并添加/更新（每个目录只处理一次）
            if user_id not in seen_artists:
                seen_artists.add(user_id)
                existing_artist = self.db.get_artist(user_id)
                try:
                    self.db.add_or_update_artist(user_id, username)
                    if not existing_artist:
                        result.new_artists += 1
                except Exception as e:
                    print(f"\n[ERROR] 添加作者失败 {user_id}: {e}")
                    result.error_messages.append(f"添加作者失败 {user_id}: {e}")
                    result.errors += 1
                    continue

            # 扫描该作者目录下的文件
            try:
                for filename in os.listdir(artist_dir):
                    file_path = os.path.join(artist_dir, filename)
                    if not os.path.isfile(file_path):
                        continue
                    file_info = self.parse_filename(filename)
                    if not file_info:
                        if verbose:
                            _safe_print(f"\n[WARNING] 无法解析文件名: {filename}")
                        continue
                    work_id, page, ext = file_info
                    result.total_files += 1
                    # 记录作品信息，稍后批量补建缺失的 works 记录
                    info = works_map.get(work_id)
                    if info is None:
                        works_map[work_id] = {'artist_id': user_id, 'max_page': page}
                    elif page > info['max_page']:
                        info['max_page'] = page
                    try:
                        file_size = os.path.getsize(file_path)
                        self._all_images.append((work_id, page, filename, file_path, file_size))
                    except Exception as e:
                        logger.error(f"获取文件大小失败 {file_path}: {e}")
                        result.errors += 1
            except Exception as e:
                _safe_print(f"\n[ERROR] 扫描作者目录失败 {dir_name}: {e}")
                result.error_messages.append(f"扫描目录失败 {dir_name}: {e}")
                result.errors += 1
                continue

            result.scanned_dirs += 1
            # 记录目录状态（全量/增量都记录，作为下次增量扫描的基线）
            if incremental:
                dir_mtime, dir_count = latest_mtime, sig_count
            else:
                dir_mtime, dir_count = self._get_dir_signature(artist_dir)
            new_states[dir_name] = {
                'mtime': dir_mtime,
                'file_count': dir_count,
            }

        print()  # 换行

        # 补建缺失的作品记录（扫描无法获取标题/作者名等，先占位，后续下载会覆盖）
        # 不补建的话，images 表有数据但浏览页 JOIN works 不到，会"同步了却看不到"
        if works_map:
            try:
                result.new_works = self.db.add_works_batch([
                    (wid, info['artist_id'], info['max_page'] + 1)
                    for wid, info in works_map.items()
                ])
                if result.new_works:
                    print(f"已补建 {result.new_works} 条作品记录")
            except Exception as e:
                logger.error(f"补建作品记录失败: {e}")
                result.errors += 1

        # 一次性写入所有图片到数据库
        if self._all_images:
            print(f"扫描完成，共 {result.total_files} 个文件，正在写入数据库...")
            try:
                inserted = self.db.add_images_batch(self._all_images)
                result.new_images = inserted
                print(f"已写入 {inserted} 条图片记录")
            except Exception as e:
                logger.error(f"写入数据库失败: {e}")
                result.errors += 1
        else:
            print("未发现新文件")

        # 保存目录扫描状态（供下次增量判断）
        if new_states:
            try:
                self.db.save_scan_dir_states(new_states)
            except Exception as e:
                logger.warning(f"保存扫描状态失败: {e}")

        # 记录同步结果
        try:
            self.db.add_sync_record(
                sync_type='incremental_scan' if incremental else 'full_scan',
                target_path=self.download_path,
                items_found=result.total_files,
                items_added=result.new_images,
                items_updated=result.updated_images
            )
        except Exception as e:
            print(f"[ERROR] 记录同步结果失败: {e}")

        if progress_callback:
            progress_callback(None, total_work, total_work, "扫描完成")

        return result
    
    def scan_empty_directories(self) -> List[Dict]:
        """
        扫描磁盘上所有空的作者目录
        
        Returns:
            空目录列表，每项包含: {dir_name, dir_path, user_id, username}
        """
        empty_dirs = []
        if not os.path.exists(self.download_path):
            return empty_dirs
        
        for dir_name in os.listdir(self.download_path):
            if dir_name.startswith('.'):
                continue  # 跳过隐藏目录（如 .thumb 缩略图缓存）
            dir_path = os.path.join(self.download_path, dir_name)
            if not os.path.isdir(dir_path):
                continue
            
            # 检查目录是否为空（不含任何文件）
            try:
                has_files = False
                for item in os.listdir(dir_path):
                    item_path = os.path.join(dir_path, item)
                    if os.path.isfile(item_path):
                        has_files = True
                        break
                
                if not has_files:
                    dir_info = self.parse_directory_name(dir_name)
                    if dir_info:
                        user_id, username = dir_info
                        empty_dirs.append({
                            'dir_name': dir_name,
                            'dir_path': dir_path,
                            'user_id': user_id,
                            'username': username
                        })
                    else:
                        # 不符合命名规范的目录，也列出
                        empty_dirs.append({
                            'dir_name': dir_name,
                            'dir_path': dir_path,
                            'user_id': None,
                            'username': None
                        })
            except Exception as e:
                logger.error(f"扫描目录 {dir_path} 失败: {e}")
        
        return empty_dirs

    def verify_database_integrity(self) -> Dict[str, any]:
        """
        验证数据库完整性（8类异常检查）
        
        检查项目：
        1. 缺失文件：images表有记录但磁盘文件不存在
        2. 孤儿文件：磁盘上有文件但images表没有记录
        3. 孤儿作品：works表有记录但没有任何关联的images
        4. 孤儿作者：artists表有记录但没有任何关联的works
        5. 图片外键断裂：images.work_id 在 works 表中不存在
        6. 作品外键断裂：works.artist_id 在 artists 表中不存在
        7. 页数不匹配：works.page_count 与实际 images 数量不一致
        8. 空目录：磁盘上作者目录为空（无任何文件）
        """
        result = {
            'total_images_db': 0,
            'total_works_db': 0,
            'total_artists_db': 0,
            'total_files_disk': 0,
            'issues': [],
            'is_healthy': True,
            # 8类异常数据，每项为列表
            'missing_files': [],        # 1. images有记录但文件缺失
            'orphan_files': [],         # 2. 磁盘有文件但images无记录
            'orphan_works': [],         # 3. works无images
            'orphan_artists': [],       # 4. artists无works
            'broken_image_fk': [],      # 5. images.work_id -> works 不存在
            'broken_work_fk': [],       # 6. works.artist_id -> artists 不存在
            'page_count_mismatch': [],  # 7. page_count与实际images数不一致
            'empty_directories': [],    # 8. 磁盘上空作者目录
        }

        # --- 预备：收集磁盘上所有文件（复用 walk_disk_files 生成器）---
        disk_files = {}  # file_path -> {work_id, page, filename, file_size}
        for file_path, work_id, page, ext, file_size, _dir_name in self.walk_disk_files():
            disk_files[file_path] = {
                'work_id': work_id,
                'page': page,
                'filename': os.path.basename(file_path),
                'file_path': file_path,
                'file_size': file_size,
            }
        result['total_files_disk'] = len(disk_files)

        # --- 获取数据库全量数据 ---
        all_images = self.db.get_all_images()
        result['total_images_db'] = len(all_images)

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM works')
            result['total_works_db'] = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM artists')
            result['total_artists_db'] = cursor.fetchone()[0]

        # 构建辅助集合
        db_file_paths = set()           # images表中的file_path
        db_work_ids = set()             # images表中出现过的work_id
        db_work_page_counts = {}        # work_id -> images数量（从images表统计）
        for img in all_images:
            db_file_paths.add(img.file_path)
            db_work_ids.add(img.work_id)
            db_work_page_counts[img.work_id] = db_work_page_counts.get(img.work_id, 0) + 1

        # --- 检查1: 缺失文件 (images有记录但文件不存在) ---
        for img in all_images:
            if img.file_path not in disk_files:
                result['missing_files'].append({
                    'id': img.id,
                    'work_id': img.work_id,
                    'file_path': img.file_path,
                    'filename': img.filename,
                    'page': img.page
                })
                result['issues'].append({
                    'type': 'missing_file',
                    'message': f"文件缺失: {img.file_path}"
                })

        # --- 检查2: 孤儿文件 (磁盘有文件但images无记录) ---
        for fp, info in disk_files.items():
            if fp not in db_file_paths:
                result['orphan_files'].append(info)
                result['issues'].append({
                    'type': 'orphan_file',
                    'message': f"磁盘有文件但数据库无记录: {fp}"
                })

        # --- 检查3-6: 外键和孤儿检查 ---
        with self.db.get_connection() as conn:
            cursor = conn.cursor()

            # 3. 孤儿作品：works表有记录但没有任何images
            cursor.execute('''
                SELECT w.id, w.artist_id, w.title, w.page_count, a.name as artist_name
                FROM works w
                LEFT JOIN artists a ON w.artist_id = a.id
                WHERE w.id NOT IN (SELECT DISTINCT work_id FROM images)
            ''')
            for row in cursor.fetchall():
                d = dict(row)
                result['orphan_works'].append(d)
                result['issues'].append({
                    'type': 'orphan_work',
                    'message': f"作品无图片: work_id={d['id']} title={d.get('title','')}"
                })

            # 4. 孤儿作者：artists有记录但没有任何works
            cursor.execute('''
                SELECT a.id, a.name, a.account
                FROM artists a
                WHERE a.id NOT IN (SELECT DISTINCT artist_id FROM works)
            ''')
            for row in cursor.fetchall():
                d = dict(row)
                result['orphan_artists'].append(d)
                result['issues'].append({
                    'type': 'orphan_artist',
                    'message': f"作者无作品: id={d['id']} name={d.get('name','')}"
                })

            # 5. 图片外键断裂：images.work_id 在 works 中不存在
            cursor.execute('''
                SELECT i.id, i.work_id, i.filename, i.file_path, i.page
                FROM images i
                WHERE i.work_id NOT IN (SELECT id FROM works)
            ''')
            for row in cursor.fetchall():
                d = dict(row)
                result['broken_image_fk'].append(d)
                result['issues'].append({
                    'type': 'broken_image_fk',
                    'message': f"图片指向不存在的作品: image_id={d['id']} work_id={d['work_id']} file={d.get('filename','')}"
                })

            # 6. 作品外键断裂：works.artist_id 在 artists 中不存在
            cursor.execute('''
                SELECT w.id, w.artist_id, w.title, w.page_count
                FROM works w
                WHERE w.artist_id NOT IN (SELECT id FROM artists)
            ''')
            for row in cursor.fetchall():
                d = dict(row)
                result['broken_work_fk'].append(d)
                result['issues'].append({
                    'type': 'broken_work_fk',
                    'message': f"作品指向不存在的作者: work_id={d['id']} artist_id={d['artist_id']}"
                })

            # 7. 页数不匹配：works.page_count 与实际 images 数量不一致
            cursor.execute('''
                SELECT w.id, w.artist_id, w.title, w.page_count,
                       a.name as artist_name
                FROM works w
                LEFT JOIN artists a ON w.artist_id = a.id
            ''')
            for row in cursor.fetchall():
                d = dict(row)
                work_id = d['id']
                actual_count = db_work_page_counts.get(work_id, 0)
                expected_count = d['page_count'] or 0
                if actual_count > 0 and expected_count > 0 and actual_count != expected_count:
                    result['page_count_mismatch'].append({
                        'work_id': work_id,
                        'artist_id': d['artist_id'],
                        'artist_name': d.get('artist_name', ''),
                        'title': d.get('title', ''),
                        'expected_pages': expected_count,
                        'actual_pages': actual_count
                    })
                    result['issues'].append({
                        'type': 'page_count_mismatch',
                        'message': f"页数不匹配: work_id={work_id} 应有{expected_count}页 实际{actual_count}页"
                    })

        # --- 检查8: 空目录 (磁盘上作者目录没有任何文件) ---
        empty_dirs = self.scan_empty_directories()
        for d in empty_dirs:
            result['empty_directories'].append(d)
            dir_label = f"{d['dir_name']}" + (f" (user_id={d['user_id']})" if d['user_id'] else " (命名不规范)")
            result['issues'].append({
                'type': 'empty_directory',
                'message': f"空目录: {dir_label}"
            })

        # 综合判断健康状态
        result['is_healthy'] = len(result['issues']) == 0

        return result
