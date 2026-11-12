# -*- coding: utf-8 -*-
"""
Cookie 解析工具
支持从浏览器插件（如 "Get cookies.txt" / "EditThisCookie"）导出的 Netscape 格式 Cookie 文件
"""

import time
from typing import Dict, Tuple

# 只导入 pixiv 相关域名的 cookie
PIXIV_DOMAINS = ('pixiv.net', 'www.pixiv.net', 'i.pximg.net')

# pixiv 登录所需的关键 cookie 键
KEY_COOKIE_NAMES = ('PHPSESSID', 'p_ab_id', 'a_type', 'b_type', 'c_type')


def parse_cookie_file(file_path: str) -> Tuple[str, Dict]:
    """
    解析 Netscape 格式 Cookie 文件。

    返回: (cookie_string, stats)
      - cookie_string: 可直接用于 requests 的 Cookie 字符串（仅保留 pixiv 域名）
      - stats: {
            'total':   文件中的 cookie 总数,
            'used':    被采用的 cookie 数量（pixiv 域名且未过期）,
            'skipped': 跳过的数量（非 pixiv 域名）,
            'expired': 已过期的数量,
            'domains': {域名: 数量},
            'keys':    ['PHPSESSID', ...]  被采用的关键 cookie 键,
        }
    """
    now = time.time()
    cookies = []          # 最终采用的 cookie 键值对
    seen = set()          # 去重：同一域名+name 只保留最后一个
    total = 0
    skipped = 0
    expired = 0
    domains: Dict[str, int] = {}
    keys = []

    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            # 跳过注释和空行
            if not line or line.startswith('#') or line.startswith('HTTPOnly'):
                continue

            # Netscape 格式: domain  tailmatch  path  secure  expires  name  value
            parts = line.split('\t')
            if len(parts) < 7:
                continue

            total += 1
            domain = parts[0].lstrip('.').lower()
            secure = parts[3]
            expires_raw = parts[4]
            name = parts[5]
            value = '\t'.join(parts[6:])  # value 可能含制表符

            # 域名过滤：只保留 pixiv 相关
            if not any(domain == d or domain.endswith('.' + d) for d in PIXIV_DOMAINS):
                skipped += 1
                continue

            domains[domain] = domains.get(domain, 0) + 1

            # 过期检查（0 表示会话 cookie，永不过期）
            if expires_raw.isdigit():
                exp = int(expires_raw)
                if 0 < exp < now:
                    expired += 1
                    continue

            # 去重：同一域名+name 保留最后一个
            key = f"{domain}\x00{name}"
            if key in seen:
                continue
            seen.add(key)

            cookies.append(f"{name}={value}")
            if name in KEY_COOKIE_NAMES:
                keys.append(name)

    stats = {
        'total': total,
        'used': len(cookies),
        'skipped': skipped,
        'expired': expired,
        'domains': domains,
        'keys': list(dict.fromkeys(keys)),  # 去重保序
    }

    return '; '.join(cookies), stats


def parse_netscape_cookies(file_path: str) -> str:
    """解析 Netscape 格式 Cookie 文件，返回 Cookie 字符串（兼容旧接口）"""
    cookie_str, _ = parse_cookie_file(file_path)
    return cookie_str


def load_cookie_from_file(file_path: str) -> str:
    """加载 Cookie 文件并返回 Cookie 字符串（兼容旧接口）"""
    return parse_netscape_cookies(file_path)
