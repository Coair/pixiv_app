# -*- coding: utf-8 -*-
"""
Pixiv Downloader - API请求模块
处理与Pixiv API的交互
"""
import requests
import time
import re
from typing import Dict, List, Optional, Tuple
import logging

from config import get_config

logger = logging.getLogger(__name__)


class PixivAPI:
    """Pixiv API 请求类"""
    
    def __init__(self, cookie: str = None, user_agent: str = None):
        config = get_config()
        self.cookie = cookie or config.cookie
        self.user_agent = user_agent or config.user_agent
        self.timeout = config.timeout
        self.delay = config.delay
        self.download_delay = getattr(config, 'download_delay', 0.3)  # 下载延迟
        self.max_retries = max(1, int(getattr(config, 'max_retries', 3) or 1))
        self.retry_delay = float(getattr(config, 'retry_delay', 2.0) or 2.0)
        self._user_id: Optional[str] = None  # 缓存当前用户的 pixiv ID
        
        self.session = requests.Session()
        self.cookie_invalid = False  # 最近一次请求是否遇到 403（Cookie 可能失效）
        self._update_headers()
    
    def _update_headers(self):
        """更新请求头"""
        self.session.headers.update({
            'User-Agent': self.user_agent,
            'Cookie': self.cookie,
            'Referer': 'https://www.pixiv.net/',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        })
    
    def _request(self, method: str, url: str, **kwargs) -> Tuple[Optional[Dict], Optional[int]]:
        """
        发送HTTP请求
        
        Args:
            method: 请求方法 (GET, POST)
            url: 请求URL
            **kwargs: 其他请求参数
        
        Returns:
            (响应JSON数据, HTTP状态码)。成功时状态码为200，失败时状态码为实际错误码或None
        """
        last_status = None
        for attempt in range(self.max_retries):
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=self.timeout,
                    **kwargs
                )
                response.raise_for_status()

                # 尝试解析JSON
                content_type = response.headers.get('Content-Type', '')
                if 'application/json' in content_type:
                    return response.json(), response.status_code
                return {'text': response.text}, response.status_code

            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                last_status = status
                if status == 403:
                    self.cookie_invalid = True
                    logger.error("Cookie可能已过期或无效")
                    return None, status
                if status == 404:
                    logger.error(f"资源不存在: {url}")
                    return None, status
                # 限流 / 服务端错误：指数退避重试
                retryable = (status == 429) or (status is not None and status >= 500)
                if retryable and attempt < self.max_retries - 1:
                    wait = self.retry_delay * (attempt + 1)
                    logger.warning(f"HTTP {status}，{wait:.1f}s 后重试 ({attempt + 1}/{self.max_retries})")
                    time.sleep(wait)
                    continue
                logger.error(f"HTTP错误: {status} - {e}")
                return None, status

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < self.max_retries - 1:
                    wait = self.retry_delay * (attempt + 1)
                    logger.warning(
                        f"网络异常({type(e).__name__})，{wait:.1f}s 后重试 "
                        f"({attempt + 1}/{self.max_retries})"
                    )
                    time.sleep(wait)
                    continue
                logger.error(f"网络请求失败: {url} ({e})")
                return None, None

            except Exception as e:
                logger.error(f"请求失败: {e}")
                return None, None

        return None, last_status
    
    def get(self, url: str, **kwargs) -> Tuple[Optional[Dict], Optional[int]]:
        """发送GET请求，返回 (data, status_code)"""
        return self._request('GET', url, **kwargs)
    
    def get_artist_info(self, artist_id: str) -> Optional[Dict]:
        """
        获取作者信息
        
        Args:
            artist_id: 作者ID
        
        Returns:
            作者信息字典
        """
        url = f"https://www.pixiv.net/ajax/user/{artist_id}"
        params = {'lang': 'zh'}
        
        data, _status = self.get(url, params=params)
        if data and 'body' in data:
            body = data['body']
            return {
                'id': artist_id,
                'name': body.get('name', ''),
                'account': body.get('account', ''),
                'userId': body.get('userId', artist_id),
            }
        return None
    
    def get_artist_all_works(self, artist_id: str) -> List[str]:
        """
        获取作者的所有作品ID列表
        
        Args:
            artist_id: 作者ID
        
        Returns:
            作品ID列表（按ID降序，即从新到旧）
        """
        url = f"https://www.pixiv.net/ajax/user/{artist_id}/profile/all"
        params = {'lang': 'zh'}
        
        data, _status = self.get(url, params=params)
        if data and 'body' in data:
            works = data['body'].get('illusts', {})
            work_ids = list(works.keys())
            
            # 按ID降序排序（ID越大越新）
            work_ids.sort(key=int, reverse=True)
            return work_ids
        return []
    
    def get_work_info(self, work_id: str) -> Tuple[Optional[Dict], Optional[int]]:
        """
        获取作品详细信息
        
        Args:
            work_id: 作品ID
        
        Returns:
            (作品信息字典, HTTP状态码)。成功时 status 为 200，404 时 status 为 404
        """
        url = f"https://www.pixiv.net/ajax/illust/{work_id}"
        params = {'lang': 'zh'}
        
        data, status = self.get(url, params=params)
        if data and 'body' in data:
            body = data['body']
            return {
                'id': work_id,
                'title': body.get('title', ''),
                'description': body.get('description', ''),
                'userId': body.get('userId', ''),
                'userName': body.get('userName', ''),
                'pageCount': body.get('pageCount', 1),
                'width': body.get('width', 0),
                'height': body.get('height', 0),
                'tags': [tag.get('tag', '') for tag in body.get('tags', {}).get('tags', [])],
                'createDate': body.get('createDate', ''),
                'updateDate': body.get('updateDate', ''),
            }, status
        return None, status
    
    def get_work_download_urls(self, work_id: str) -> List[Dict]:
        """
        获取作品的所有图片下载URL

        Args:
            work_id: 作品ID

        Returns:
            图片信息列表，每个包含 url, thumb_url, page 等信息
        """
        url = f"https://www.pixiv.net/ajax/illust/{work_id}/pages"
        params = {'lang': 'zh'}
        
        data, _status = self.get(url, params=params)
        if data and 'body' in data:
            pages = []
            for i, page in enumerate(data['body']):
                urls = page.get('urls', {})
                pages.append({
                    'page': i,
                    'url': urls.get('original', ''),
                    'thumb_url': urls.get('thumb_mini', urls.get('thumb', '')),
                    'width': page.get('width', 0),
                    'height': page.get('height', 0),
                })
            return pages
        return []

    def get_work_id_from_url(self, url: str) -> Optional[str]:
        """
        从Pixiv URL提取作品ID
        
        支持的URL格式:
        - https://www.pixiv.net/artworks/12345678
        - https://www.pixiv.net/en/artworks/12345678
        - https://pixiv.net/artworks/12345678
        
        Args:
            url: Pixiv作品URL
        
        Returns:
            作品ID字符串，失败返回None
        """
        # 匹配 /artworks/{id} 或 /en/artworks/{id}
        patterns = [
            r'/artworks/(\d+)',
            r'/en/artworks/(\d+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        return None
    
    def get_artist_id_from_url(self, url: str) -> Optional[str]:
        """
        从Pixiv URL提取作者ID
        
        支持的URL格式:
        - https://www.pixiv.net/users/12345678
        - https://www.pixiv.net/en/users/12345678
        
        Args:
            url: Pixiv作者主页URL
        
        Returns:
            作者ID字符串，失败返回None
        """
        patterns = [
            r'/users/(\d+)',
            r'/en/users/(\d+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        return None
    
    def parse_url(self, url: str) -> tuple:
        """
        解析任意Pixiv URL
        
        Args:
            url: Pixiv URL
        
        Returns:
            (type, id): type为 'work' 或 'artist'，id为对应ID
        """
        # 尝试提取作品ID
        work_id = self.get_work_id_from_url(url)
        if work_id:
            return ('work', work_id)
        
        # 尝试提取作者ID
        artist_id = self.get_artist_id_from_url(url)
        if artist_id:
            return ('artist', artist_id)
        
        return (None, None)
    
    def get_artist_new_works(self, artist_id: str, existing_work_ids: set = None) -> List[str]:
        """
        获取作者的新作品ID列表（排除已存在的）
        
        Args:
            artist_id: 作者ID
            existing_work_ids: 数据库中已有的作品ID集合
        
        Returns:
            新作品ID列表（按ID降序，从新到旧）
        """
        all_ids = self.get_artist_all_works(artist_id)
        if not existing_work_ids:
            return all_ids
        
        existing = {int(wid) for wid in existing_work_ids}
        new_ids = [wid for wid in all_ids if int(wid) not in existing]
        return new_ids

    def _get_user_id(self) -> Optional[str]:
        """获取当前登录用户的 pixiv user_id（从首页解析，缓存结果）"""
        if self._user_id:
            return self._user_id
        
        try:
            data, _status = self._request('GET', 'https://www.pixiv.net/')
            if data and isinstance(data, dict):
                html = str(data)
                # 从页面中提取 userId
                match = re.search(r'"userId"\s*:\s*"(\d+)"', html)
                if match:
                    self._user_id = match.group(1)
                    logger.info(f"[API] 获取到当前用户 ID: {self._user_id}")
                    return self._user_id
        except Exception as e:
            logger.warning(f"[API] 获取 user_id 失败: {e}")
        
        # 回退：从 Cookie 的 PHPSESSID 中解析
        match = re.search(r'PHPSESSID=(\d+)_', self.cookie)
        if match:
            self._user_id = match.group(1)
            logger.info(f"[API] 从 Cookie 解析到用户 ID: {self._user_id}")
            return self._user_id
        
        logger.error("[API] 无法获取 user_id，收藏功能将无法使用")
        return None

    def get_bookmarks(self, rest: str = 'show', limit: int = None, 
                      max_pages: int = None) -> List[str]:
        """
        获取收藏(书签)作品ID列表
        
        Args:
            rest: 'show'=公开收藏, 'hide'=私密收藏
            limit: 最大作品数量，None=全部
            max_pages: 最大翻页数，None=全部
        
        Returns:
            作品ID列表
        """
        user_id = self._get_user_id()
        if not user_id:
            logger.error("[收藏] 无法获取 user_id，无法获取收藏列表")
            return []
        
        all_ids = []
        offset = 0
        page_size = 48  # pixiv 默认每页48个
        page = 0
        
        while True:
            if max_pages is not None and page >= max_pages:
                break
            
            # 新的 API 端点（需要 user_id）
            url = f"https://www.pixiv.net/ajax/user/{user_id}/illusts/bookmarks"
            params = {
                'tag': '',
                'offset': offset,
                'limit': page_size,
                'rest': rest,
                'lang': 'zh',
            }
            
            data, _status = self.get(url, params=params)
            if not data or 'body' not in data:
                break
            
            body = data['body']
            # 新 API 返回的是完整的作品对象数组，不是纯 ID 数组
            works = body.get('works', [])
            
            if not works:
                break
            
            for work_obj in works:
                all_ids.append(str(work_obj['id']))
                if limit and len(all_ids) >= limit:
                    break
            
            if limit and len(all_ids) >= limit:
                break
            
            offset += page_size
            page += 1
            self.wait()
        
        if limit:
            all_ids = all_ids[:limit]
        
        return all_ids

    def wait(self, delay: float = None):
        """等待（用于API请求限流）"""
        time.sleep(delay if delay is not None else self.delay)

    def download_wait(self):
        """等待（用于图片下载限流，比API请求延迟短）"""
        time.sleep(self.download_delay)
