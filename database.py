# -*- coding: utf-8 -*-
"""
Pixiv Downloader - 数据库模块
"""
import sqlite3
import os
import re
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass
from contextlib import contextmanager

from config import get_config

logger = logging.getLogger(__name__)


@dataclass
class Artist:
    """作者数据类"""
    id: int
    name: str
    account: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class Work:
    """作品数据类"""
    id: int
    artist_id: int
    title: Optional[str] = None
    description: Optional[str] = None
    page_count: int = 1
    width: Optional[int] = None
    height: Optional[int] = None
    tags: Optional[List[str]] = None  # 运行时填充，不直接对应数据库字段
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class Image:
    """图片数据类"""
    id: int
    work_id: int
    page: int
    filename: str
    file_path: str
    file_size: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    original_url: Optional[str] = None
    is_downloaded: bool = True
    downloaded_at: Optional[datetime] = None


class Database:
    """数据库管理类"""
    
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = get_config().db_path
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self.init_db()
    
    @contextmanager
    def get_connection(self):
        """获取数据库连接的上下文管理器（WAL 模式，读写并发更友好）"""
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            # WAL：后台写入时前台查询不被阻塞；NORMAL 同步在本地场景兼顾安全与性能
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            conn.execute('PRAGMA busy_timeout=5000')
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    def init_db(self):
        """初始化数据库表"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 作者表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS artists (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    account TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 作品表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS works (
                    id INTEGER PRIMARY KEY,
                    artist_id INTEGER NOT NULL,
                    title TEXT,
                    description TEXT,
                    page_count INTEGER DEFAULT 1,
                    width INTEGER,
                    height INTEGER,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    deleted INTEGER DEFAULT 0,
                    deleted_at TIMESTAMP,
                    FOREIGN KEY (artist_id) REFERENCES artists(id)
                )
            ''')
            
            # 图片表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_id INTEGER NOT NULL,
                    page INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    file_path TEXT NOT NULL UNIQUE,
                    file_size INTEGER,
                    width INTEGER,
                    height INTEGER,
                    original_url TEXT,
                    is_downloaded INTEGER DEFAULT 1,
                    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (work_id) REFERENCES works(id)
                )
            ''')
            
            # 同步记录表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sync_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sync_type TEXT NOT NULL,
                    target_path TEXT,
                    items_found INTEGER DEFAULT 0,
                    items_added INTEGER DEFAULT 0,
                    items_updated INTEGER DEFAULT 0,
                    items_removed INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # 作者目录扫描状态表（用于增量扫描判断：只扫描有变化的目录）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS scan_dir_state (
                    dir_name TEXT PRIMARY KEY,
                    mtime REAL NOT NULL DEFAULT 0,
                    file_count INTEGER NOT NULL DEFAULT 0,
                    last_scan_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # 作品标签表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS work_tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_id INTEGER NOT NULL,
                    tag TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (work_id) REFERENCES works(id),
                    UNIQUE(work_id, tag)
                )
            ''')

            # 过滤记录表（记录被跳过的作品）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS filter_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_id INTEGER NOT NULL,
                    artist_id INTEGER,
                    artist_name TEXT,
                    title TEXT,
                    page_count INTEGER,
                    filter_reason TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 收藏(书签)表：记录从Pixiv同步的收藏作品
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS bookmarks (
                    work_id INTEGER PRIMARY KEY,
                    rest_type TEXT NOT NULL DEFAULT 'show',
                    status TEXT NOT NULL DEFAULT 'active',
                    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (work_id) REFERENCES works(id)
                )
            ''')
            # 迁移：为旧数据添加 status 字段（如果不存在）
            try:
                cursor.execute("ALTER TABLE bookmarks ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            except Exception:
                pass  # 字段已存在则忽略
            
            # 任务队列持久化表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS task_queue (
                    task_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    params TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    progress_current INTEGER DEFAULT 0,
                    progress_total INTEGER DEFAULT 0,
                    progress_message TEXT DEFAULT '',
                    progress_work_id TEXT DEFAULT '',
                    extra_info TEXT DEFAULT '{}',
                    result TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP
                )
            ''')

            # 创建索引
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_works_artist ON works(artist_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_works_deleted ON works(deleted)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_images_work ON images(work_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_images_work_dl ON images(work_id, is_downloaded)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_images_path ON images(file_path)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_work_tags_tag ON work_tags(tag)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_filter_records_work ON filter_records(work_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_bookmarks_status ON bookmarks(status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_task_queue_status ON task_queue(status)')
            
            conn.commit()

            # 迁移：为已有 works 表添加 deleted 字段（如果不存在）
            self._migrate_add_deleted_column(conn)
    
    def _migrate_add_deleted_column(self, conn):
        """迁移：为 works 表添加 deleted 和 deleted_at 字段（兼容旧数据库）"""
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT deleted FROM works LIMIT 1")
        except sqlite3.OperationalError:
            cursor.execute("ALTER TABLE works ADD COLUMN deleted INTEGER DEFAULT 0")
            cursor.execute("ALTER TABLE works ADD COLUMN deleted_at TIMESTAMP")
            conn.commit()
            logger.info("数据库迁移: works 表已添加 deleted/deleted_at 字段")
    
    # ============ 作者操作 ============
    
    def add_or_update_artist(self, artist_id: int, name: str, account: str = None) -> Artist:
        """添加或更新作者"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO artists (id, name, account, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    account = excluded.account,
                    updated_at = CURRENT_TIMESTAMP
            ''', (artist_id, name, account))
            return Artist(id=artist_id, name=name, account=account)
    
    def get_artist(self, artist_id: int) -> Optional[Artist]:
        """获取作者信息"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM artists WHERE id = ?', (artist_id,))
            row = cursor.fetchone()
            if row:
                return Artist(**dict(row))
            return None
    
    def get_all_artists(self, with_work_count: bool = False) -> List[Dict]:
        """获取所有作者，可选择是否包含作品数量"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if with_work_count:
                cursor.execute('''
                    SELECT a.*, COUNT(w.id) as work_count,
                           COUNT(b.work_id) as bookmark_count
                    FROM artists a 
                    LEFT JOIN works w ON a.id = w.artist_id 
                    LEFT JOIN bookmarks b ON w.id = b.work_id
                    GROUP BY a.id 
                    ORDER BY a.name
                ''')
                return [dict(row) for row in cursor.fetchall()]
            else:
                cursor.execute('SELECT * FROM artists ORDER BY name')
                return [Artist(**dict(row)) for row in cursor.fetchall()]
    
    # ============ 作品操作 ============
    
    def add_or_update_work(self, work_id: int, artist_id: int, **kwargs) -> Work:
        """添加或更新作品"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            fields = ['id', 'artist_id']
            values = [work_id, artist_id]
            
            for key, value in kwargs.items():
                if key in ['title', 'description', 'page_count', 'width', 'height']:
                    fields.append(key)
                    values.append(value)
            
            set_clause = ', '.join([f'{f} = excluded.{f}' for f in fields[2:]]) if len(fields) > 2 else ''
            
            sql = f'''
                INSERT INTO works ({', '.join(fields)})
                VALUES ({', '.join(['?'] * len(values))})
                ON CONFLICT(id) DO UPDATE SET
                    {set_clause},
                    updated_at = CURRENT_TIMESTAMP
            '''
            cursor.execute(sql, values)
            conn.commit()
            
            return self.get_work(work_id)
    
    def get_work(self, work_id: int) -> Optional[Work]:
        """获取作品信息"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id, artist_id, title, description, page_count, width, height, '
                'created_at, updated_at FROM works WHERE id = ?',
                (work_id,)
            )
            row = cursor.fetchone()
            if row:
                return Work(**dict(row))
            return None
    
    def get_artist_works(self, artist_id: int) -> List[Work]:
        """获取作者的所有作品"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id, artist_id, title, description, page_count, width, height, '
                'created_at, updated_at FROM works WHERE artist_id = ? ORDER BY id DESC',
                (artist_id,)
            )
            return [Work(**dict(row)) for row in cursor.fetchall()]
    
    # ============ 标签操作 ============
    
    def set_work_tags(self, work_id: int, tags: List[str]):
        """设置作品标签（先删后插）"""
        if not tags:
            return
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # 先删除旧标签
            cursor.execute('DELETE FROM work_tags WHERE work_id = ?', (work_id,))
            # 批量插入新标签
            cursor.executemany(
                'INSERT OR IGNORE INTO work_tags (work_id, tag) VALUES (?, ?)',
                [(work_id, tag) for tag in tags]
            )
    
    def get_work_tags(self, work_id: int) -> List[str]:
        """获取作品标签"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT tag FROM work_tags WHERE work_id = ? ORDER BY id', (work_id,))
            return [row['tag'] for row in cursor.fetchall()]
    
    def get_all_tags(self, limit: int = 500) -> List[Dict]:
        """获取所有标签及其作品数量（按数量降序）"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT tag, COUNT(work_id) as work_count
                FROM work_tags
                GROUP BY tag
                ORDER BY work_count DESC
                LIMIT ?
            ''', (limit,))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_filter_tags(self, tags: list = None, artist_id: int = 0,
                        search: str = '', limit: int = 200) -> List[Dict]:
        """获取标签列表（联动筛选），仅返回当前过滤条件下存在的标签及其作品数
        
        - 选中 A 标签后，只剩同时拥有 A 的其他标签
        - 选中 A+B 后，只剩同时拥有 A 和 B 的其他标签
        - 作者/搜索过滤也参与联动
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            where_sql, params = self._build_image_filter_where(tags, artist_id, search)

            cursor.execute(f'''
                SELECT wt.tag, COUNT(DISTINCT wt.work_id) as work_count
                FROM work_tags wt
                WHERE wt.work_id IN (
                    SELECT DISTINCT i.work_id
                    FROM images i
                    JOIN works w ON i.work_id = w.id
                    JOIN artists a ON w.artist_id = a.id
                    WHERE {where_sql}
                )
                GROUP BY wt.tag
                ORDER BY work_count DESC
                LIMIT ?
            ''', params + [limit])

            return [dict(row) for row in cursor.fetchall()]
    
    def get_artist_tags(self, artist_id: int, limit: int = 100) -> List[Dict]:
        """获取指定作者的所有标签及其作品数量"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT wt.tag, COUNT(wt.work_id) as work_count
                FROM work_tags wt
                JOIN works w ON wt.work_id = w.id
                WHERE w.artist_id = ?
                GROUP BY wt.tag
                ORDER BY work_count DESC
                LIMIT ?
            ''', (artist_id, limit))
            return [dict(row) for row in cursor.fetchall()]
    
    def batch_get_work_tags(self, work_ids: List[int]) -> Dict[int, List[str]]:
        """批量获取多个作品的标签"""
        if not work_ids:
            return {}
        with self.get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ','.join(['?'] * len(work_ids))
            cursor.execute(
                f'SELECT work_id, tag FROM work_tags WHERE work_id IN ({placeholders}) ORDER BY id',
                work_ids
            )
            result = {}
            for row in cursor.fetchall():
                wid = row['work_id']
                if wid not in result:
                    result[wid] = []
                result[wid].append(row['tag'])
            return result
    
    def get_artists_by_tag(self, tag: str) -> List[Dict]:
        """根据标签查询拥有该标签作品的作者（含作品数）"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT a.*, COUNT(DISTINCT wt.work_id) as tag_work_count
                FROM work_tags wt
                JOIN works w ON wt.work_id = w.id
                JOIN artists a ON w.artist_id = a.id
                WHERE wt.tag = ?
                GROUP BY a.id
                ORDER BY tag_work_count DESC
            ''', (tag,))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_work_count_without_tags(self) -> int:
        """获取没有标签的作品数量（跳过已删除的作品）"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COUNT(*) as cnt FROM works w
                WHERE NOT EXISTS (SELECT 1 FROM work_tags wt WHERE wt.work_id = w.id)
                AND w.deleted = 0
            ''')
            row = cursor.fetchone()
            return row['cnt'] if row else 0
    
    def get_works_without_tags(self, limit: int = 100, offset: int = 0) -> List[int]:
        """获取没有标签的作品ID列表（跳过已删除的作品）"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT w.id FROM works w
                WHERE NOT EXISTS (SELECT 1 FROM work_tags wt WHERE wt.work_id = w.id)
                AND w.deleted = 0
                ORDER BY w.id DESC
                LIMIT ? OFFSET ?
            ''', (limit, offset))
            return [row['id'] for row in cursor.fetchall()]
    
    def mark_work_deleted(self, work_id: int):
        """标记作品为已删除（Pixiv 上已不存在）"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE works SET deleted = 1, deleted_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (work_id,))
            logger.info(f"作品 {work_id} 已标记为已删除")
    
    def mark_work_restored(self, work_id: int):
        """清除作品的已删除标记（Pixiv 上恢复了）"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE works SET deleted = 0, deleted_at = NULL
                WHERE id = ?
            ''', (work_id,))
            logger.info(f"作品 {work_id} 已恢复（清除已删除标记）")
    
    def is_work_deleted(self, work_id: int) -> bool:
        """检查作品是否被标记为已删除"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT deleted FROM works WHERE id = ?', (work_id,))
            row = cursor.fetchone()
            return row['deleted'] == 1 if row else False
    
    # ============ 图片操作 ============
    
    def add_image(self, work_id: int, page: int, filename: str, file_path: str, **kwargs) -> Image:
        """添加图片记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            fields = ['work_id', 'page', 'filename', 'file_path']
            values = [work_id, page, filename, file_path]
            
            for key, value in kwargs.items():
                if key in ['file_size', 'width', 'height', 'original_url', 'is_downloaded', 'downloaded_at']:
                    fields.append(key)
                    values.append(value)
            
            # 确保 downloaded_at 有默认值
            if 'downloaded_at' not in kwargs:
                fields.append('downloaded_at')
                values.append(datetime.now())
            
            sql = f'''
                INSERT INTO images ({', '.join(fields)})
                VALUES ({', '.join(['?'] * len(values))})
                ON CONFLICT(file_path) DO UPDATE SET
                    work_id = excluded.work_id,
                    page = excluded.page,
                    filename = excluded.filename,
                    file_size = COALESCE(excluded.file_size, images.file_size),
                    is_downloaded = 1
            '''
            cursor.execute(sql, values)
            conn.commit()
            
            cursor.execute('SELECT * FROM images WHERE file_path = ?', (file_path,))
            return Image(**dict(cursor.fetchone()))
    
    def add_works_batch(self, works: List[Tuple[int, int, int]]) -> int:
        """
        批量补建作品记录（本地扫描用）：
        只补缺失的作品，已存在的保留真实标题/页数等元数据（后续下载会覆盖占位信息）。
        works: [(work_id, artist_id, page_count), ...]
        返回：新建的作品数
        """
        if not works:
            return 0

        with self.get_connection() as conn:
            cursor = conn.cursor()

            ids = [w[0] for w in works]
            existing = set()
            chunk = 900
            for i in range(0, len(ids), chunk):
                part = ids[i:i + chunk]
                ph = ','.join(['?'] * len(part))
                cursor.execute(f'SELECT id FROM works WHERE id IN ({ph})', part)
                existing.update(r['id'] for r in cursor.fetchall())

            to_insert = [w for w in works if w[0] not in existing]
            if to_insert:
                cursor.executemany(
                    '''INSERT OR IGNORE INTO works (id, artist_id, title, page_count)
                       VALUES (?, ?, '', ?)''',
                    to_insert
                )
            conn.commit()
            return len(to_insert)

    def add_images_batch(self, images: List[Tuple]) -> int:
        """
        批量添加图片记录（大幅提升扫描性能）
        
        Args:
            images: 图片信息列表，每项为 (work_id, page, filename, file_path, file_size)
        
        Returns:
            本次真正新增的记录数（已存在的只更新字段，不计入新增）
        """
        if not images:
            return 0
        
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 先统计哪些是真正新增的（用于同步结果展示"新增 N 条"）
            paths = [img[3] for img in images]
            existing = set()
            chunk = 900
            for i in range(0, len(paths), chunk):
                part = paths[i:i + chunk]
                ph = ','.join(['?'] * len(part))
                cursor.execute(f'SELECT file_path FROM images WHERE file_path IN ({ph})', part)
                existing.update(r['file_path'] for r in cursor.fetchall())
            inserted = sum(1 for p in paths if p not in existing)

            sql = '''
                INSERT INTO images (work_id, page, filename, file_path, file_size, is_downloaded, downloaded_at)
                VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(file_path) DO UPDATE SET
                    work_id = excluded.work_id,
                    page = excluded.page,
                    filename = excluded.filename,
                    file_size = COALESCE(excluded.file_size, images.file_size),
                    is_downloaded = 1
            '''
            
            cursor.executemany(sql, images)
            conn.commit()
            return inserted
    
    def get_work_images(self, work_id: int) -> List[Image]:
        """获取作品的所有图片"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM images WHERE work_id = ? ORDER BY page', (work_id,))
            return [Image(**dict(row)) for row in cursor.fetchall()]

    def get_works_by_ids(self, work_ids: List[int]) -> Dict[int, Work]:
        """批量获取作品 {work_id: Work}（避免逐作品查询）"""
        if not work_ids:
            return {}
        result: Dict[int, Work] = {}
        chunk_size = 900
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for i in range(0, len(work_ids), chunk_size):
                chunk = work_ids[i:i + chunk_size]
                placeholders = ','.join(['?'] * len(chunk))
                cursor.execute(
                    f'SELECT id, artist_id, title, description, page_count, width, height, '
                    f'created_at, updated_at FROM works WHERE id IN ({placeholders})',
                    chunk
                )
                for row in cursor.fetchall():
                    work = Work(**dict(row))
                    result[work.id] = work
        return result

    def get_work_image_counts(self, work_ids: List[int]) -> Dict[int, int]:
        """批量获取各作品的图片数量 {work_id: count}"""
        if not work_ids:
            return {}
        result: Dict[int, int] = {}
        chunk_size = 900
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for i in range(0, len(work_ids), chunk_size):
                chunk = work_ids[i:i + chunk_size]
                placeholders = ','.join(['?'] * len(chunk))
                cursor.execute(
                    f'SELECT work_id, COUNT(*) as c FROM images '
                    f'WHERE work_id IN ({placeholders}) GROUP BY work_id',
                    chunk
                )
                for row in cursor.fetchall():
                    result[row['work_id']] = row['c']
        return result

    def get_images_for_works(self, work_ids: List[int]) -> Dict[int, List[Image]]:
        """
        批量获取多个作品的图片，返回 {work_id: [Image, ...]}。
        避免"逐作品查询"的 N+1 问题（一次连接、分批 IN 查询）。
        """
        if not work_ids:
            return {}
        result: Dict[int, List[Image]] = {wid: [] for wid in work_ids}
        chunk_size = 900  # 规避 SQLite 变量数上限
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for i in range(0, len(work_ids), chunk_size):
                chunk = work_ids[i:i + chunk_size]
                placeholders = ','.join(['?'] * len(chunk))
                cursor.execute(
                    f'SELECT * FROM images WHERE work_id IN ({placeholders}) '
                    f'ORDER BY work_id, page',
                    chunk
                )
                for row in cursor.fetchall():
                    img = Image(**dict(row))
                    result.setdefault(img.work_id, []).append(img)
        return result
    
    def get_all_images(self) -> List[Image]:
        """获取所有图片"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM images ORDER BY work_id, page')
            return [Image(**dict(row)) for row in cursor.fetchall()]
    
    @staticmethod
    def _build_image_filter_where(tags: list, artist_id: int, search: str):
        """
        构建图片浏览/标签联动的通用过滤条件。
        上下文：三表连接 images i / works w / artists a。
        返回: (where_sql, params)
        """
        where_parts = ['i.is_downloaded = 1']
        params = []

        if tags:
            for t in tags:
                where_parts.append('''i.work_id IN (
                    SELECT wt.work_id FROM work_tags wt WHERE wt.tag = ?
                )''')
                params.append(t)

        if artist_id:
            where_parts.append('w.artist_id = ?')
            params.append(artist_id)

        if search:
            where_parts.append('''(w.title LIKE ? OR a.name LIKE ? OR a.account LIKE ?
                OR i.work_id IN (SELECT wt2.work_id FROM work_tags wt2 WHERE wt2.tag LIKE ?))''')
            like = f'%{search}%'
            params.extend([like, like, like, like])

        where_sql = ' AND '.join(where_parts) if where_parts else '1=1'
        return where_sql, params

    def browse_images(self, page: int = 1, per_page: int = 40, tags: list = None,
                      artist_id: int = 0, search: str = '') -> Dict:
        """
        分页浏览图片（支持标签/作者/搜索过滤）
        tags: 标签列表，多个标签为 AND 逻辑（作品必须同时拥有所有标签）
        返回: { items: [...], total: int, page: int, per_page: int, total_pages: int }
        每张图片附带作品标题、作者名、标签列表
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            where_sql, params = self._build_image_filter_where(tags, artist_id, search)

            # 总数
            cursor.execute(f'''
                SELECT COUNT(*) as cnt
                FROM images i
                JOIN works w ON i.work_id = w.id
                JOIN artists a ON w.artist_id = a.id
                WHERE {where_sql}
            ''', params)
            total = cursor.fetchone()['cnt']

            total_pages = max(1, (total + per_page - 1) // per_page)
            offset = (page - 1) * per_page

            # 分页数据
            cursor.execute(f'''
                SELECT i.*, w.title as work_title, w.page_count,
                       a.id as artist_id, a.name as artist_name, a.account as artist_account
                FROM images i
                JOIN works w ON i.work_id = w.id
                JOIN artists a ON w.artist_id = a.id
                WHERE {where_sql}
                ORDER BY i.downloaded_at DESC, i.work_id DESC, i.page
                LIMIT ? OFFSET ?
            ''', params + [per_page, offset])
            rows = cursor.fetchall()

            items = []
            work_ids = list(set(row['work_id'] for row in rows))
            tags_map = self.batch_get_work_tags(work_ids) if work_ids else {}

            for row in rows:
                d = dict(row)
                wid = d['work_id']
                d['tags'] = tags_map.get(wid, [])
                items.append(d)

            return {
                'items': items,
                'total': total,
                'page': page,
                'per_page': per_page,
                'total_pages': total_pages,
            }
    
    # ============ 同步记录 ============
    
    def add_sync_record(self, sync_type: str, target_path: str = None, **kwargs) -> int:
        """添加同步记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO sync_records (sync_type, target_path, items_found, items_added, items_updated, items_removed)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (sync_type, target_path, 
                  kwargs.get('items_found', 0),
                  kwargs.get('items_added', 0),
                  kwargs.get('items_updated', 0),
                  kwargs.get('items_removed', 0)))
            conn.commit()
            return cursor.lastrowid

    def get_sync_records(self, limit: int = 20) -> List[Dict]:
        """获取最近的同步/扫描历史记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, sync_type, target_path, items_found, items_added,
                       items_updated, items_removed, created_at
                FROM sync_records ORDER BY id DESC LIMIT ?
            ''', (limit,))
            return [dict(row) for row in cursor.fetchall()]

    # ============ 作者目录扫描状态（增量扫描） ============

    def get_scan_dir_states(self) -> Dict[str, Dict]:
        """获取所有作者目录的扫描状态 {dir_name: {mtime, file_count}}"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT dir_name, mtime, file_count FROM scan_dir_state')
            return {row['dir_name']: {'mtime': row['mtime'], 'file_count': row['file_count']}
                    for row in cursor.fetchall()}

    def save_scan_dir_states(self, states: Dict[str, Dict]):
        """批量保存/更新作者目录扫描状态（增量扫描后调用）"""
        if not states:
            return
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for dir_name, st in states.items():
                cursor.execute('''
                    INSERT OR REPLACE INTO scan_dir_state (dir_name, mtime, file_count, last_scan_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ''', (dir_name, st.get('mtime', 0), st.get('file_count', 0)))
            conn.commit()
    
    # ============ 统计 ============
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            stats = {}
            
            # 统计作者数
            cursor.execute('SELECT COUNT(*) as count FROM artists')
            stats['artists'] = cursor.fetchone()['count']
            
            # 统计作品数
            cursor.execute('SELECT COUNT(*) as count FROM works')
            stats['works'] = cursor.fetchone()['count']
            
            # 统计图片数
            cursor.execute('SELECT COUNT(*) as count FROM images')
            stats['images'] = cursor.fetchone()['count']
            
            # 统计总大小
            cursor.execute('SELECT SUM(file_size) as total FROM images WHERE file_size IS NOT NULL')
            total = cursor.fetchone()['total']
            stats['total_size'] = total or 0
            stats['total_size_mb'] = round(stats['total_size'] / (1024 * 1024), 2)
            
            # 统计任务数（来自 task_queue 持久化表）
            cursor.execute("SELECT COUNT(*) as count FROM task_queue WHERE status IN ('pending', 'running', 'paused')")
            stats['pending_tasks'] = cursor.fetchone()['count']
            # 失败任务仅统计真正 failed（可重试）；cancelled/已完成单独归为历史，不混入"失败"
            cursor.execute("SELECT COUNT(*) as count FROM task_queue WHERE status = 'failed'")
            stats['failed_tasks'] = cursor.fetchone()['count']
            cursor.execute("SELECT COUNT(*) as count FROM task_queue WHERE status IN ('cancelled', 'completed')")
            stats['history_tasks'] = cursor.fetchone()['count']
            
            return stats
    
    # ============ 过滤记录 ============

    def add_filter_record(self, work_id: int, filter_reason: str, **kwargs):
        """添加过滤记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO filter_records 
                (work_id, artist_id, artist_name, title, page_count, filter_reason)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                work_id,
                kwargs.get('artist_id'),
                kwargs.get('artist_name'),
                kwargs.get('title'),
                kwargs.get('page_count'),
                filter_reason
            ))
            conn.commit()

    def get_filter_stats(self) -> Dict[str, Any]:
        """获取过滤统计"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) as total FROM filter_records')
            total = cursor.fetchone()['total']

            cursor.execute('''
                SELECT filter_reason, COUNT(*) as count 
                FROM filter_records 
                GROUP BY filter_reason
            ''')
            by_reason = {row['filter_reason']: row['count'] for row in cursor.fetchall()}

            return {'total_filtered': total, 'by_reason': by_reason}

    def get_filter_records(self, limit: int = 50) -> List[Dict]:
        """获取过滤记录列表"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM filter_records 
                ORDER BY created_at DESC 
                LIMIT ?
            ''', (limit,))
            return [dict(row) for row in cursor.fetchall()]

    # ============ 收藏(书签)操作 ============

    def sync_bookmarks(self, work_ids: List[int], rest_type: str = 'show') -> Dict[str, int]:
        """
        同步收藏列表到本地数据库（增量对比模式）。
        - Pixiv 上仍收藏的 → status='active'（新插入或从 removed 恢复）
        - Pixiv 上已取消的 → status='removed'（保留记录，不删文件）
        
        Args:
            work_ids: Pixiv 上当前收藏的作品ID列表
            rest_type: 'show'=公开收藏, 'hide'=私密收藏
        
        Returns:
            {'added': N, 'restored': N, 'removed': N}
        """
        added = 0
        restored = 0
        removed = 0
        work_id_set = set(work_ids)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 1. 获取该类型下所有已有的 work_id 及状态
            cursor.execute(
                'SELECT work_id, status FROM bookmarks WHERE rest_type = ?',
                (rest_type,)
            )
            existing = {row['work_id']: row['status'] for row in cursor.fetchall()}
            existing_ids = set(existing.keys())
            
            # 2. Pixiv 上仍收藏的：插入新记录 或 恢复 removed → active
            for wid in work_ids:
                if wid not in existing_ids:
                    # 新收藏
                    cursor.execute(
                        "INSERT INTO bookmarks (work_id, rest_type, status, synced_at) VALUES (?, ?, 'active', CURRENT_TIMESTAMP)",
                        (wid, rest_type)
                    )
                    added += 1
                elif existing[wid] == 'removed':
                    # 之前取消收藏，现在又重新收藏了 → 恢复
                    cursor.execute(
                        "UPDATE bookmarks SET status = 'active', synced_at = CURRENT_TIMESTAMP WHERE work_id = ? AND rest_type = ?",
                        (wid, rest_type)
                    )
                    restored += 1
            
            # 3. 本地有但 Pixiv 上已取消的 → 标记为 removed（保留记录）
            to_remove = existing_ids - work_id_set
            if to_remove:
                cursor.executemany(
                    "UPDATE bookmarks SET status = 'removed' WHERE work_id = ? AND rest_type = ?",
                    [(wid, rest_type) for wid in to_remove]
                )
                removed = len(to_remove)
        
        logger.info(
            f"[收藏同步] {rest_type}: 共 {len(work_ids)} 个, "
            f"新增 {added}, 恢复 {restored}, 取消 {removed}"
        )
        return {'added': added, 'restored': restored, 'removed': removed}

    def get_bookmarked_work_ids(self, rest_type: str = None) -> List[int]:
        """获取活跃收藏的作品ID列表"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if rest_type:
                cursor.execute(
                    'SELECT work_id FROM bookmarks WHERE rest_type = ? AND status = "active" ORDER BY work_id DESC',
                    (rest_type,)
                )
            else:
                cursor.execute("SELECT work_id FROM bookmarks WHERE status = 'active' ORDER BY work_id DESC")
            return [row['work_id'] for row in cursor.fetchall()]

    def get_bookmarks_with_detail(
        self, rest_type: str = None, status: str = 'active',
        limit: int = 200, offset: int = 0
    ) -> List[Dict]:
        """
        获取收藏作品详情（含作者信息和下载状态）
        
        Args:
            rest_type: 'show'/'hide'/None=全部
            status: 'active'/'removed'/'all'/None=全部
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            conditions = []
            params = []
            if rest_type:
                conditions.append('b.rest_type = ?')
                params.append(rest_type)
            if status and status != 'all':
                conditions.append('b.status = ?')
                params.append(status)
            
            where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
            
            cursor.execute(f'''
                SELECT b.work_id, b.rest_type, b.status, b.synced_at,
                       w.title, w.page_count, w.width, w.height, w.created_at,
                       a.id as artist_id, a.name as artist_name,
                       COUNT(i.id) as downloaded_pages
                FROM bookmarks b
                LEFT JOIN works w ON b.work_id = w.id
                LEFT JOIN artists a ON w.artist_id = a.id
                LEFT JOIN images i ON b.work_id = i.work_id
                {where}
                GROUP BY b.work_id
                ORDER BY b.synced_at DESC
                LIMIT ? OFFSET ?
            ''', params + [limit, offset])
            
            rows = cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                # 仅当作品元数据存在（page_count>0）且页数齐全才算"已下载"；
                # works 缺失或标题为占位符的视为"待补全"，避免误判为已完成
                expected = d['page_count'] or 0
                d['is_complete'] = expected > 0 and (d['downloaded_pages'] or 0) >= expected
                title = d.get('title')
                is_placeholder_title = not title or (isinstance(title, str) and title.strip() in ('', '(待补全)'))
                d['needs_refill'] = expected <= 0 or (d['downloaded_pages'] or 0) < expected or is_placeholder_title
                result.append(d)

            # 批量获取所有收藏的第一张图片路径（一次查询，避免每行一次 N+1 连接）
            first_paths = {}
            work_ids = [d['work_id'] for d in result if d['work_id'] is not None]
            if work_ids:
                placeholders = ','.join(['?'] * len(work_ids))
                cursor.execute(f'''
                    SELECT work_id, file_path FROM images
                    WHERE work_id IN ({placeholders}) AND is_downloaded = 1
                    ORDER BY work_id, page
                ''', work_ids)
                for r in cursor.fetchall():
                    if r['work_id'] not in first_paths:
                        first_paths[r['work_id']] = r['file_path']

            for d in result:
                d['first_image_path'] = first_paths.get(d['work_id'])

            return result

    def get_bookmark_count(self, rest_type: str = None, status: str = 'active') -> int:
        """获取收藏总数"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            conditions = []
            params = []
            if rest_type:
                conditions.append('rest_type = ?')
                params.append(rest_type)
            if status and status != 'all':
                conditions.append('status = ?')
                params.append(status)
            where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
            cursor.execute(f'SELECT COUNT(*) as cnt FROM bookmarks {where}', params)
            return cursor.fetchone()['cnt']

    def is_bookmarked(self, work_id: int) -> bool:
        """检查作品是否处于活跃收藏状态"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM bookmarks WHERE work_id = ? AND status = 'active'", (work_id,))
            return cursor.fetchone() is not None

    def get_bookmarked_ids_set(self) -> set:
        """获取所有已收藏作品ID的集合（用于快速查找）"""
        return set(self.get_bookmarked_work_ids())

    # ============ 数据库修复操作 ============

    def fix_delete_missing_file_records(self, image_ids: List[int]) -> int:
        """删除缺失文件的 images 记录（只删数据库，不删磁盘）"""
        if not image_ids:
            return 0
        with self.get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ','.join(['?'] * len(image_ids))
            cursor.execute(f'DELETE FROM images WHERE id IN ({placeholders})', image_ids)
            return cursor.rowcount

    def fix_reimport_orphan_files(self, files: List[Dict]) -> int:
        """
        把孤儿文件（磁盘有但数据库无）重新扫描入库。
        同时自动补全 works 和 artists 记录，避免产生"图片外键断裂"。
        
        从 file_path 解析目录名提取 artist_id 和 artist_name，
        从 filename 解析 work_id。
        """
        import re as _re
        DIR_PATTERN = _re.compile(r'^(\d+)_(.+)$')
        
        if not files:
            return 0
        
        # 1. 收集所有需要补全的 artist 和 work
        artists_to_upsert = {}   # artist_id -> (name, account)
        works_to_upsert = {}     # work_id -> artist_id
        images_batch = []
        
        for f in files:
            file_path = f.get('file_path', '')
            work_id = f.get('work_id')
            filename = f.get('filename', '')
            
            images_batch.append((
                work_id, f['page'], filename,
                file_path, f.get('file_size', 0)
            ))
            
            # 从 file_path 中提取 artist_id
            # 路径格式: download_path/{artist_id}_{artist_name}/{work_id}_p{page}.{ext}
            dir_path = os.path.dirname(file_path)
            dir_name = os.path.basename(dir_path)
            dir_match = DIR_PATTERN.match(dir_name)
            if dir_match:
                artist_id = int(dir_match.group(1))
                artist_name = dir_match.group(2)
                artists_to_upsert[artist_id] = (artist_name, None)
                works_to_upsert[work_id] = artist_id
        
        # 2. 创建/更新 artists
        if artists_to_upsert:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.executemany('''
                    INSERT INTO artists (id, name, account, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET
                        name = COALESCE(NULLIF(excluded.name, ''), artists.name),
                        updated_at = CURRENT_TIMESTAMP
                ''', [
                    (aid, name, account)
                    for aid, (name, account) in artists_to_upsert.items()
                ])
        
        # 3. 创建 works（如果不存在）
        if works_to_upsert:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.executemany('''
                    INSERT OR IGNORE INTO works (id, artist_id, title, page_count)
                    VALUES (?, ?, '(待补全)', 1)
                ''', [
                    (wid, aid) for wid, aid in works_to_upsert.items()
                ])
        
        # 4. 插入 images
        return self.add_images_batch(images_batch)

    def _delete_by_ids(self, table: str, ids: List[int]) -> int:
        """通用按 ID 列表删除记录"""
        if not ids:
            return 0
        with self.get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ','.join(['?'] * len(ids))
            cursor.execute(f'DELETE FROM {table} WHERE id IN ({placeholders})', ids)
            return cursor.rowcount

    def fix_delete_orphan_works(self, work_ids: List[int]) -> int:
        """删除孤儿作品记录（无images关联的works）"""
        return self._delete_by_ids('works', work_ids)

    def fix_delete_orphan_artists(self, artist_ids: List[int]) -> int:
        """删除孤儿作者记录（无works关联的artists）"""
        return self._delete_by_ids('artists', artist_ids)

    def fix_delete_broken_work_fk(self, work_ids: List[int]) -> int:
        """删除外键断裂的 works 记录（artist_id指向不存在的作者）"""
        return self._delete_by_ids('works', work_ids)

    def fix_delete_broken_image_fk(self, image_ids: List[int]) -> int:
        """修复外键断裂的 images：尝试补全 works + artists，补全不了的才删除"""
        if not image_ids:
            return 0
        import re as _re
        DIR_PATTERN = _re.compile(r'^(\d+)_(.+)$')
        
        # 先查询这些 images 的详细信息
        with self.get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ','.join(['?'] * len(image_ids))
            cursor.execute(
                f'SELECT id, work_id, file_path FROM images WHERE id IN ({placeholders})',
                image_ids
            )
            broken_images = [dict(row) for row in cursor.fetchall()]
        
        if not broken_images:
            return 0
        
        # 尝试补全 works + artists
        artists_to_upsert = {}
        works_to_upsert = {}
        fixed_count = 0
        
        for img in broken_images:
            file_path = img.get('file_path', '')
            work_id = img.get('work_id')
            dir_path = os.path.dirname(file_path)
            dir_name = os.path.basename(dir_path)
            dir_match = DIR_PATTERN.match(dir_name)
            if dir_match and work_id:
                artist_id = int(dir_match.group(1))
                artist_name = dir_match.group(2)
                artists_to_upsert[artist_id] = (artist_name, None)
                works_to_upsert[work_id] = artist_id
                fixed_count += 1
        
        # 补全 artists
        if artists_to_upsert:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.executemany('''
                    INSERT INTO artists (id, name, account, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET
                        name = COALESCE(NULLIF(excluded.name, ''), artists.name),
                        updated_at = CURRENT_TIMESTAMP
                ''', [
                    (aid, name, account)
                    for aid, (name, account) in artists_to_upsert.items()
                ])
        
        # 补全 works
        if works_to_upsert:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.executemany('''
                    INSERT OR IGNORE INTO works (id, artist_id, title, page_count)
                    VALUES (?, ?, '(待补全)', 1)
                ''', [
                    (wid, aid) for wid, aid in works_to_upsert.items()
                ])
        
        logger.info(f"[修复] 图片外键断裂: 补全了 {fixed_count} 条（works + artists），"
                    f"共 {len(broken_images)} 条异常")
        
        return fixed_count

    def fix_page_count_mismatch(self, corrections: List[Dict]) -> int:
        """修正页数不匹配：将 works.page_count 更新为实际 images 数量"""
        if not corrections:
            return 0
        count = 0
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for c in corrections:
                cursor.execute(
                    'UPDATE works SET page_count = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                    (c['actual_pages'], c['work_id'])
                )
                count += cursor.rowcount
        return count

    def fix_remove_empty_dirs(self, dirs: List[Dict]) -> int:
        """
        删除磁盘上的空目录，并清理数据库中的孤儿作者记录
        
        Args:
            dirs: 空目录列表，每项包含 {dir_path, user_id, ...}
        
        Returns:
            成功删除的目录数量
        """
        import shutil
        removed = 0
        orphan_artist_ids = []
        
        for d in dirs:
            dir_path = d.get('dir_path', '')
            user_id = d.get('user_id')
            
            # 删除磁盘上的空目录
            if dir_path and os.path.isdir(dir_path):
                try:
                    shutil.rmtree(dir_path)
                    removed += 1
                    logger.info(f"[清理] 已删除空目录: {dir_path}")
                except Exception as e:
                    logger.error(f"[清理] 删除目录失败 {dir_path}: {e}")
            
            # 收集需要清理的孤儿作者ID
            if user_id is not None:
                orphan_artist_ids.append(user_id)
        
        # 清理孤儿作者（仅删除那些确实没有任何作品的作者）
        if orphan_artist_ids:
            placeholders = ','.join(['?'] * len(orphan_artist_ids))
            with self.get_connection() as conn:
                cursor = conn.cursor()
                # 只删除没有任何works的作者
                cursor.execute(f'''
                    DELETE FROM artists 
                    WHERE id IN ({placeholders})
                    AND id NOT IN (SELECT DISTINCT artist_id FROM works)
                ''', orphan_artist_ids)
                deleted_artists = cursor.rowcount
                if deleted_artists > 0:
                    logger.info(f"[清理] 同时清理了 {deleted_artists} 个孤儿作者记录")
        
        return removed

    # ============ 任务队列持久化 ============

    def save_task(self, task_id: str, task_type: str, params: dict, status: str = 'pending'):
        """保存/更新任务到持久化表"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO task_queue (task_id, task_type, params, status, created_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(task_id) DO UPDATE SET
                    task_type = excluded.task_type,
                    params = excluded.params,
                    status = excluded.status
            ''', (task_id, task_type, json.dumps(params, ensure_ascii=False), status))

    def update_task_status(self, task_id: str, status: str, **kwargs):
        """更新任务状态和进度"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            fields = ['status = ?']
            values = [status]

            for key in ['progress_current', 'progress_total', 'progress_message',
                        'progress_work_id', 'extra_info', 'result']:
                if key in kwargs:
                    val = kwargs[key]
                    if isinstance(val, (dict, list)):
                        val = json.dumps(val, ensure_ascii=False)
                    fields.append(f'{key} = ?')
                    values.append(val)

            if status in ('completed', 'failed', 'cancelled'):
                fields.append('finished_at = CURRENT_TIMESTAMP')
            elif status == 'running' and 'started_at' not in kwargs:
                fields.append('started_at = CURRENT_TIMESTAMP')

            values.append(task_id)
            cursor.execute(
                f'UPDATE task_queue SET {", ".join(fields)} WHERE task_id = ?',
                values
            )

    def delete_task(self, task_id: str):
        """从持久化表删除任务"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM task_queue WHERE task_id = ?', (task_id,))

    def get_persisted_tasks(self, status: str = None) -> List[Dict]:
        """获取持久化表中的任务列表"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute(
                    'SELECT * FROM task_queue WHERE status = ? ORDER BY created_at ASC',
                    (status,)
                )
            else:
                cursor.execute('SELECT * FROM task_queue ORDER BY created_at ASC')
            rows = cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                # 解析 JSON 字段
                for field in ['params', 'extra_info', 'result']:
                    if d.get(field) and isinstance(d[field], str):
                        try:
                            d[field] = json.loads(d[field])
                        except (json.JSONDecodeError, TypeError):
                            pass
                result.append(d)
            return result

    def get_persisted_task(self, task_id: str) -> Optional[Dict]:
        """获取单个持久化任务"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM task_queue WHERE task_id = ?', (task_id,))
            row = cursor.fetchone()
            if row:
                d = dict(row)
                for field in ['params', 'extra_info', 'result']:
                    if d.get(field) and isinstance(d[field], str):
                        try:
                            d[field] = json.loads(d[field])
                        except (json.JSONDecodeError, TypeError):
                            pass
                return d
            return None

    def clear_terminal_task_history(self, include_failed: bool = False) -> int:
        """
        清空历史任务记录（已取消/已完成）。

        Args:
            include_failed: True 时连失败记录一起删除
        """
        statuses = ['cancelled', 'completed']
        if include_failed:
            statuses.append('failed')
        placeholders = ','.join('?' for _ in statuses)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f'DELETE FROM task_queue WHERE status IN ({placeholders})',
                statuses
            )
            conn.commit()
            return cursor.rowcount

    def clear_failed_task_history(self) -> int:
        """清空失败任务记录（可重试列表），返回删除条数"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM task_queue WHERE status = 'failed'")
            conn.commit()
            return cursor.rowcount

    # ============ 删除（作者/作品，含关联数据） ============

    def delete_work_with_files(self, work_id: int):
        """
        删除单个作品及其所有关联数据（图片、标签、收藏、过滤记录）。
        返回: (删除的图片记录数, 需从磁盘删除的本地文件路径列表)
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT file_path FROM images WHERE work_id = ?', (work_id,))
            paths = [r['file_path'] for r in cursor.fetchall() if r['file_path']]

            cursor.execute('DELETE FROM images WHERE work_id = ?', (work_id,))
            images_deleted = cursor.rowcount
            cursor.execute('DELETE FROM work_tags WHERE work_id = ?', (work_id,))
            cursor.execute('DELETE FROM bookmarks WHERE work_id = ?', (work_id,))
            cursor.execute('DELETE FROM filter_records WHERE work_id = ?', (work_id,))
            cursor.execute('DELETE FROM works WHERE id = ?', (work_id,))
            conn.commit()
            return images_deleted, paths

    def delete_artist_with_files(self, artist_id: int):
        """
        删除作者及其全部作品与关联数据。
        返回: (删除的图片记录数, 删除的作品数, 需从磁盘删除的本地文件路径列表)
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 先收集本地文件路径（供上层删除磁盘文件），再按子查询批量清理关联表
            cursor.execute('''
                SELECT i.file_path FROM images i
                JOIN works w ON i.work_id = w.id
                WHERE w.artist_id = ? AND i.file_path IS NOT NULL
            ''', (artist_id,))
            paths = [r['file_path'] for r in cursor.fetchall()]

            cursor.execute(
                'DELETE FROM images WHERE work_id IN (SELECT id FROM works WHERE artist_id = ?)',
                (artist_id,)
            )
            images_deleted = cursor.rowcount
            cursor.execute(
                'DELETE FROM work_tags WHERE work_id IN (SELECT id FROM works WHERE artist_id = ?)',
                (artist_id,)
            )
            cursor.execute(
                'DELETE FROM bookmarks WHERE work_id IN (SELECT id FROM works WHERE artist_id = ?)',
                (artist_id,)
            )
            cursor.execute(
                'DELETE FROM filter_records WHERE work_id IN (SELECT id FROM works WHERE artist_id = ?)',
                (artist_id,)
            )

            cursor.execute('DELETE FROM filter_records WHERE artist_id = ?', (artist_id,))
            cursor.execute('DELETE FROM works WHERE artist_id = ?', (artist_id,))
            works_deleted = cursor.rowcount
            cursor.execute('DELETE FROM artists WHERE id = ?', (artist_id,))
            # 清理该作者的目录扫描状态（目录名形如 "{artist_id}_用户名"）
            cursor.execute('DELETE FROM scan_dir_state WHERE dir_name LIKE ?', (f'{artist_id}_%',))
            conn.commit()
            return images_deleted, works_deleted, paths

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            self.conn = None
