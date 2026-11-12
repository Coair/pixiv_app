# -*- coding: utf-8 -*-
"""
Pixiv Downloader - CLI命令行界面
"""
import sys
import os
import argparse
import logging
from typing import Optional

from config import get_config, init_config
from database import Database
from api import PixivAPI
from scanner import FileScanner
from downloader import Downloader
from cookie_util import load_cookie_from_file


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class CLI:
    """命令行界面"""
    
    def __init__(self):
        self.db: Optional[Database] = None
        self.api: Optional[PixivAPI] = None
        self.downloader: Optional[Downloader] = None
        self.scanner: Optional[FileScanner] = None
    
    def init_services(self, cookie: str = None, download_path: str = None):
        """初始化服务"""
        config = init_config(
            cookie=cookie,
            download_path=download_path
        )
        
        self.db = Database(config.db_path)
        self.api = PixivAPI(config.cookie, config.user_agent)
        self.downloader = Downloader(self.db, self.api, config.download_path)
        self.scanner = FileScanner(self.db, config.download_path)
    
    def progress_callback(self, work_id, current, total, message):
        """进度回调"""
        if work_id:
            # 作品级别进度：显示简洁格式
            print(f"[{current}/{total}] 作品 {work_id}: {message}")
    
    def cmd_sync(self, args):
        """同步命令 - 扫描现有文件到数据库"""
        incremental = getattr(args, 'incremental', False)
        mode_label = '增量' if incremental else '全量'
        print("=" * 50)
        print(f"开始{mode_label}扫描本地文件...")
        print(f"目录: {self.scanner.download_path}")
        print("=" * 50)
        
        result = self.scanner.scan_directory(
            progress_callback=self.progress_callback,
            incremental=incremental
        )
        
        print("\n" + "=" * 50)
        print("扫描完成！")
        print(f"  作者目录: {result.total_dirs}")
        if incremental:
            print(f"  扫描目录: {result.scanned_dirs}  跳过(无变化): {result.skipped_dirs}")
        print(f"  文件总数: {result.total_files}")
        print(f"  新增作者: {result.new_artists}")
        print(f"  新增作品: {result.new_works}")
        print(f"  新增图片: {result.new_images}")
        print(f"  更新图片: {result.updated_images}")
        print(f"  错误数: {result.errors}")
        
        if result.error_messages:
            print("\n错误详情:")
            for msg in result.error_messages[:10]:
                print(f"  - {msg}")
            if len(result.error_messages) > 10:
                print(f"  ... 还有 {len(result.error_messages) - 10} 个错误")
        
        return 0
    
    def cmd_stats(self, args):
        """统计命令 - 显示数据库统计"""
        stats = self.db.get_stats()
        
        print("=" * 50)
        print("数据库统计")
        print("=" * 50)
        print(f"  作者数: {stats['artists']}")
        print(f"  作品数: {stats['works']}")
        print(f"  图片数: {stats['images']}")
        print(f"  总大小: {stats['total_size_mb']} MB")
        print(f"  待下载任务: {stats['pending_tasks']}")
        print(f"  失败任务: {stats['failed_tasks']}")
        
        return 0
    
    def cmd_artists(self, args):
        """作者列表命令"""
        artists = self.db.get_all_artists(with_work_count=True)
        
        if not artists:
            print("没有记录任何作者")
            return 0
        
        print(f"{'ID':<15} {'名称':<30} {'作品数':<10}")
        print("-" * 55)
        
        for artist in artists:
            print(f"{artist['id']:<15} {artist['name']:<30} {artist['work_count']:<10}")
        
        return 0
    
    def cmd_download_url(self, args):
        """下载命令 - 根据URL下载"""
        print(f"开始下载: {args.url}")
        
        success, message = self.downloader.download_by_url(
            args.url, 
            progress_callback=self.progress_callback
        )
        
        print(f"\n结果: {message}")
        return 0 if success else 1
    
    def cmd_download_work(self, args):
        """下载命令 - 根据作品ID下载"""
        print(f"开始下载作品: {args.work_id}")
        
        result = self.downloader.download_single_work(
            args.work_id,
            progress_callback=self.progress_callback,
            skip_existing=not args.force
        )
        
        print(f"\n结果:")
        print(f"  成功: {result.downloaded}")
        print(f"  跳过: {result.skipped}")
        print(f"  失败: {result.failed}")
        
        if result.error_message:
            print(f"  错误: {result.error_message}")
        
        return 0 if result.success else 1
    
    def cmd_download_artist(self, args):
        """下载命令 - 根据作者ID下载"""
        limit = args.limit if args.limit > 0 else None
        limit_text = f"最近 {args.limit} 个" if limit else "全部"
        print(f"开始下载作者 {args.artist_id} 的作品 ({limit_text})...")
        
        stats = self.downloader.download_artist_works(
            args.artist_id,
            limit=limit,
            progress_callback=self.progress_callback,
            skip_existing=not args.force
        )
        
        print(f"\n结果:")
        print(f"  总作品数: {stats['total_works']}")
        print(f"  下载作品: {stats['downloaded_works']}")
        print(f"  跳过作品: {stats['skipped_works']}")
        print(f"  失败作品: {stats['failed_works']}")
        print(f"  下载图片: {stats['total_images']}")
        
        return 0 if stats['failed_works'] == 0 else 1
    
    def cmd_verify(self, args):
        """验证命令 - 验证数据库完整性"""
        print("开始验证数据库完整性...")
        
        result = self.scanner.verify_database_integrity()
        
        print(f"\n检查完成:")
        print(f"  数据库图片: {result['total_images_db']}")
        print(f"  磁盘文件: {result['total_files_disk']}")
        print(f"  数据库作品: {result['total_works_db']}")
        print(f"  数据库作者: {result['total_artists_db']}")
        print(f"  问题数: {len(result['issues'])}")
        
        if result['issues']:
            print("\n问题列表:")
            for issue in result['issues'][:10]:
                print(f"  - {issue}")
        
        return 0 if result['is_healthy'] else 1
    
    def cmd_save_config(self, args):
        """保存配置命令"""
        config = get_config()
        config.save()
        print(f"配置已保存")
        return 0
    
    def run(self, args):
        """运行命令"""
        # 设置日志级别
        if args.verbose:
            logging.getLogger().setLevel(logging.DEBUG)
        
        # 需要Cookie的命令
        cookie_required_commands = {
            'download-url', 'download-work', 'download-artist'
        }
        
        # 处理 Cookie 参数
        cookie = args.cookie
        if cookie and os.path.isfile(cookie):
            # 如果是文件路径，加载 Cookie 文件
            cookie = load_cookie_from_file(cookie)
        
        # 检查Cookie是否需要（包括检查默认文件）
        if args.command in cookie_required_commands:
            config = get_config()
            has_cookie = bool(config.cookie or cookie)
            
            # 检查默认Cookie文件
            if not has_cookie:
                default_files = [
                    "cookies_www.pixiv.net.txt",
                    "cookie.txt",
                    "cookies.txt",
                ]
                for f in default_files:
                    if os.path.exists(f):
                        cookie = load_cookie_from_file(f)
                        has_cookie = True
                        print(f"[配置] 已自动加载Cookie文件: {f}")
                        break
            
            if not has_cookie:
                print("错误: 请先设置Cookie")
                print("提示: 将Cookie保存为 cookies_www.pixiv.net.txt 自动加载")
                print("      或使用 -c 参数指定 Cookie")
                return 1
        
        # 初始化服务
        self.init_services(
            cookie=cookie,
            download_path=args.download_path
        )
        
        # 执行命令
        commands = {
            'sync': self.cmd_sync,
            'stats': self.cmd_stats,
            'artists': self.cmd_artists,
            'download-url': self.cmd_download_url,
            'download-work': self.cmd_download_work,
            'download-artist': self.cmd_download_artist,
            'verify': self.cmd_verify,
            'save-config': self.cmd_save_config,
        }
        
        command_func = commands.get(args.command)
        if command_func:
            return command_func(args)
        else:
            print("请指定命令")
            return 1


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='Pixiv Downloader - Pixiv图片下载管理工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 初始化/同步本地文件
  %(prog)s sync

  # 查看统计
  %(prog)s stats

  # 查看作者列表
  %(prog)s artists

  # 下载单个作品
  %(prog)s download-work 12345678

  # 下载作者作品（最近10个）
  %(prog)s download-artist 12345678 --limit 10

  # 下载作者全部作品
  %(prog)s download-artist 12345678 --limit 0

  # 根据URL下载
  %(prog)s download-url https://www.pixiv.net/artworks/12345678

  # 验证数据库
  %(prog)s verify
        """
    )
    
    # 全局参数
    parser.add_argument('-c', '--cookie', help='Pixiv Cookie', default=None)
    parser.add_argument('-p', '--path', dest='download_path', help='下载路径', default=None)
    parser.add_argument('-v', '--verbose', action='store_true', help='详细输出')
    
    # 子命令
    subparsers = parser.add_subparsers(dest='command', help='可用命令')
    
    # sync 命令
    sync_parser = subparsers.add_parser('sync', help='扫描本地文件并同步到数据库')
    sync_parser.add_argument('--incremental', action='store_true',
                             help='增量扫描：仅扫描有变动的作者目录')
    
    # stats 命令
    stats_parser = subparsers.add_parser('stats', help='显示数据库统计信息')
    
    # artists 命令
    artists_parser = subparsers.add_parser('artists', help='列出所有作者')
    
    # download-url 命令
    download_url_parser = subparsers.add_parser('download-url', help='根据URL下载')
    download_url_parser.add_argument('url', help='Pixiv作品或作者URL')
    
    # download-work 命令
    work_parser = subparsers.add_parser('download-work', help='根据作品ID下载')
    work_parser.add_argument('work_id', help='作品ID')
    work_parser.add_argument('-f', '--force', action='store_true', help='强制重新下载（覆盖）')
    
    # download-artist 命令
    artist_parser = subparsers.add_parser('download-artist', help='根据作者ID下载')
    artist_parser.add_argument('artist_id', help='作者ID')
    artist_parser.add_argument('-l', '--limit', type=int, default=10, help='下载数量限制（0=全部）')
    artist_parser.add_argument('-f', '--force', action='store_true', help='强制重新下载（覆盖）')
    
    # verify 命令
    verify_parser = subparsers.add_parser('verify', help='验证数据库完整性')
    
    # save-config 命令
    save_parser = subparsers.add_parser('save-config', help='保存配置到文件')
    
    # 解析参数
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    # 创建CLI并运行
    cli = CLI()
    return cli.run(args)


if __name__ == '__main__':
    sys.exit(main())
