# -*- coding: utf-8 -*-
"""
Pixiv Downloader - 配置模块
基于 pydantic BaseModel，自动序列化/反序列化，消除字段硬编码重复。
"""
import os
import json
from typing import Optional

from pydantic import BaseModel


class Config(BaseModel):
    """应用程序配置（pydantic 模型，自动序列化所有字段）"""

    # 下载路径
    download_path: str = r"F:\00download\pixiv"

    # 数据库路径
    db_path: str = "pixiv_downloader.db"

    # Pixiv认证
    cookie: str = ""
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    # 下载设置
    # 防反爬间隔：默认 0（不限流）。需要时在「系统配置」页调大即可。
    delay: float = 0.0            # API 请求间隔(秒)，0=无间隔
    download_delay: float = 0.0   # 图片下载间隔(秒)，0=无间隔
    timeout: int = 30             # 请求超时(秒)
    chunk_size: int = 8192        # 下载块大小

    # 并发设置
    max_retries: int = 3          # 最大重试次数
    retry_delay: float = 5.0      # 重试延迟(秒)
    download_concurrency: int = 4  # 图片并发下载数（同一作品的多页并行下载）

    # ugoira 动图（需要 ffmpeg）
    ffmpeg_path: str = ""         # ffmpeg 可执行文件路径，留空自动检测

    # API设置
    api_base_url: str = "https://www.pixiv.net/ajax"

    # AI批量作品过滤设置
    filter_enabled: bool = True            # 是否启用过滤
    filter_max_pages: int = 30             # 页数超过此值则跳过（0=不限制）
    filter_batch_same_day: int = 5         # 同作者同一天上传超过此数量则怀疑批量
    filter_batch_avg_pages: int = 10       # 批量检测时平均页数超过此值才触发

    # 收藏下载设置
    bookmark_skip_filter: bool = True      # 收藏作品下载时是否跳过AI过滤

    @classmethod
    def load(cls, config_file: str = "config.json") -> "Config":
        """从 JSON 文件加载配置，缺失字段使用默认值"""
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return cls(**data)
            except Exception as e:
                print(f"加载配置文件失败: {e}")
        return cls()

    def save(self, config_file: str = "config.json"):
        """保存配置到 JSON 文件（自动遍历所有 model_fields）"""
        try:
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(self.model_dump(), f, ensure_ascii=False, indent=2)
            print(f"配置已保存到: {config_file}")
        except Exception as e:
            print(f"保存配置文件失败: {e}")


# 全局配置实例
_config: Optional[Config] = None


def get_config() -> Config:
    """获取全局配置实例"""
    global _config
    if _config is None:
        _config = Config.load()
    return _config


def init_config(
    cookie: str = None,
    download_path: str = None,
    **kwargs
) -> Config:
    """初始化配置"""
    global _config
    _config = Config.load()

    if cookie:
        _config.cookie = cookie
    if download_path:
        _config.download_path = download_path

    for key, value in kwargs.items():
        if value is not None and key in Config.model_fields:
            setattr(_config, key, value)

    return _config
