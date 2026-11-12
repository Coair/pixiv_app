# Pixiv Downloader - 使用说明

## 功能概述

基于数据库的 Pixiv 图片下载管理工具，提供**命令行**和**Web 界面**两种使用方式：

- **Web 界面（推荐）** - 本地启动后浏览器访问，支持图片浏览/标签筛选/作者管理/收藏/下载队列/配置等
- **数据库管理** - SQLite 数据库管理下载记录
- **增量同步** - 扫描本地文件并按目录变更增量同步（秒级）
- **增量下载** - 自动跳过已下载的文件
- **多种下载方式** - 支持 URL、作者 ID、作品 ID、书签收藏
- **AI 批量过滤** - 自动识别并跳过疑似 AI 批量上传的作品

## 文件结构

```
pixiv_app/
├── app.py                       # Web 界面（Flask）
├── main.py                      # CLI 主程序入口
├── cli.py                       # 命令行界面
├── config.py                    # 配置模块
├── database.py                  # 数据库模块
├── api.py                       # Pixiv API 请求模块
├── scanner.py                   # 文件扫描同步模块
├── downloader.py                # 下载管理模块
├── cookie_util.py               # Cookie 解析工具
├── templates/                   # Web 页面模板
├── cookies_www.pixiv.net.txt    # Cookie 文件（自动加载）
├── pixiv_downloader.db          # 数据库文件（自动创建）
├── config.example.json          # 配置示例
└── README.md                    # 本文件
```

## Web 界面（推荐）

启动本地 Web 服务：

```bash
python app.py
```

然后浏览器访问 **http://127.0.0.1:5000** 即可。

### 页面功能

| 页面 | 说明 |
|------|------|
| 仪表盘 `/` | 统计概览、最近下载、被过滤作品（可一键重下）、系统状态、同步历史 |
| 图片浏览 `/images` | 图片瀑布流；顶部标签栏**多选联动**（选中标签后只显示能同时命中的其余标签），支持作者/关键字过滤、复制原图 |
| 作者列表 `/artists` | 按作者查看作品与标签，支持搜索 |
| 下载管理 `/download` | 作品/作者/增量/收藏/URL/标签补全任务；队列暂停/恢复/取消/**失败一键重试** |
| 收藏作品 `/bookmarks` | 浏览已同步的 Pixiv 收藏 |
| 本地同步 `/sync` | **增量扫描**（只扫有变化的作者目录）+ 全量扫描 + 同步历史 + 完整性验证修复 |
| 系统配置 `/config` | Cookie 检测/一键导入、下载路径、并发等参数 |

### 局域网访问

默认监听 `0.0.0.0`，同一局域网设备可访问 `http://<你的局域网IP>:5000`。
手机访问时自动切换移动端布局（底部 Tab 导航）。首次局域网使用如被防火墙拦截，请在 Windows 防火墙放行 5000 端口。

### 复制图片优化

- **📋 复制图片**：直接以原始格式/原质量复制（不再经转码压缩），本机与局域网均可用
- **📁 复制文件**：仅本机访问时显示，把文件本体放入系统剪贴板，可直接粘贴到文件夹

## 快速开始

### 1. 配置 Cookie

将 Cookie 保存为 `cookies_www.pixiv.net.txt` 文件，放在程序根目录。

**获取方法：**

1. 在浏览器中登录 [Pixiv](https://www.pixiv.net)
2. 安装 **EditThisCookie** 或 **Cookie-Editor** 扩展
3. 导出 Cookie 为 Netscape 格式
4. 保存为 `cookies_www.pixiv.net.txt`

**或手动获取：**

1. 登录 Pixiv，按 `F12` 打开开发者工具
2. 切换到 **Network** 标签
3. 刷新页面，任意请求 → 查看 **Request Headers** → 复制 `Cookie` 值
4. 保存到 `cookies_www.pixiv.net.txt`

> **重要**：工具会自动检测并加载根目录下的 `cookies_www.pixiv.net.txt`，**下载命令无需使用 `-c` 参数**！

### 2. 基本命令

```bash
# 查看数据库统计
python main.py stats

# 查看作者列表
python main.py artists

# 同步本地文件到数据库（一次性写入）
python main.py sync

# 验证文件完整性
python main.py verify
```

### 3. 下载命令

```bash
# 根据 URL 下载（Cookie自动加载）
python main.py download-url https://www.pixiv.net/artworks/12345678

# 根据作品 ID 下载（Cookie自动加载）
python main.py download-work 12345678

# 下载作者作品（最近 10 个）
python main.py download-artist 9645567 --limit 10

# 下载作者全部作品
python main.py download-artist 9645567 --limit 0

# 强制重新下载（覆盖已有文件）
python main.py download-work 12345678 --force
python main.py download-artist 9645567 --force
```

> **注意**：如果 `cookies_www.pixiv.net.txt` 不存在，可使用 `-c` 参数手动指定：
> ```bash
> python main.py -c /path/to/cookie.txt download-work 12345678
> ```

## 命令详解

| 命令 | 说明 | 需要 Cookie |
|------|------|-------------|
| `stats` | 显示数据库统计 | 否 |
| `artists` | 列出所有作者 | 否 |
| `verify` | 验证文件完整性 | 否 |
| `sync` | 同步本地文件 | 否 |
| `download-url` | 根据 URL 下载 | 是 |
| `download-work` | 根据作品 ID 下载 | 是 |
| `download-artist` | 下载作者作品 | 是 |

## Cookie 配置

### 默认加载

工具会**自动检测并加载**根目录下的 Cookie 文件：

```
cookies_www.pixiv.net.txt  ← 优先使用
cookie.txt
cookies.txt
```

### 手动指定

如果 Cookie 文件不在默认位置，可用 `-c` 参数指定：

```bash
# 指定 Cookie 文件
python main.py -c /path/to/cookie.txt download-work 12345678

# 直接指定 Cookie 字符串
python main.py -c "PHPSESSID=xxx; device_token=yyy" download-work 12345678
```

### Cookie 格式

文件必须为 **Netscape 格式**（curl/wget 兼容）：

```
# Netscape HTTP Cookie File
.pixiv.net	TRUE	/	TRUE	1735689600	PHPSESSID	abc123...
www.pixiv.net	FALSE	/	FALSE	1735689600	device_token	xyz789...
```

## 扫描同步说明

### 一次性写入

`sync` 命令会：
1. 扫描所有本地文件
2. **扫描完成后一次性写入数据库**
3. 不会在扫描过程中频繁写入

这样设计的原因是：
- **扫描本地**不会因中断导致数据不一致（没有下载流程）
- **下载时**需要分批处理，避免网络中断导致大量重试

### 扫描结果

```
发现 72 个作者目录，开始扫描...
进度: 72/72 (100%)
扫描完成，共 6566 个文件，正在写入数据库...
已写入 6566 条图片记录
```

## 目录结构

下载的文件保存在配置的 `download_path` 下：

```
F:\00download\pixiv\
├── 9645567_HuuOliv/
│   ├── 128264150_p0.png
│   ├── 128264150_p1.png
│   └── ...
├── 101003177_芥芥子子子/
│   └── ...
└── ...
```

**命名规范：**
- 作者目录：`{user_id}_{username}/`
- 图片文件：`{work_id}_p{page}.{ext}`

## 配置说明

编辑 `config.example.json` 并保存为 `config.json`：

```json
{
    "download_path": "F:\\00download\\pixiv",
    "db_path": "pixiv_downloader.db",
    "cookie": "",
    "delay": 2.0,
    "timeout": 30
}
```

或使用命令保存配置：

```bash
python main.py save-config
```

## 常见问题

### Q: Cookie 过期了怎么办？

重新导出 Cookie 并替换 `cookies_www.pixiv.net.txt` 文件。

### Q: 下载失败？

1. 检查 Cookie 是否过期
2. 降低下载延迟（修改 `delay` 配置）
3. 查看错误信息，使用 `--force` 重新下载

### Q: 扫描很慢？

工具已优化为一次性写入，扫描本身很快。如果文件很多，写入会稍慢但只需一次。

### Q: 数据库文件在哪里？

默认在程序根目录的 `pixiv_downloader.db`，可用 `config.json` 修改。

## 注意事项

1. 请遵守 Pixiv 服务条款
2. 请勿频繁请求，建议保持默认延迟设置
3. Cookie 会过期，如下载失败请更新 Cookie
4. 定期运行 `sync` 保持数据库与实际文件同步
