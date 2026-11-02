import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """轻量 .env 加载：{BASE_DIR}/.env 存在则读入 os.environ（真实环境变量优先）。"""
    p = os.path.join(BASE_DIR, '.env')
    if not os.path.isfile(p):
        return
    with open(p, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


_load_dotenv()


# ---------- 同步/落盘位置（均可经环境变量或 .env 覆盖，见 .env.example） ----------

# 数据根目录：SQLite（core/notes/photos/drive.db）+ 附件
DATA_DIR = os.environ.get('DATA_DIR', os.path.join(BASE_DIR, 'data'))
CORE_DB = os.path.join(DATA_DIR, 'core.db')

# 笔记附件目录（沿用 mock-server 布局 {user_id}/{file_id}.bin）
ATTACH_DIR = os.environ.get('ATTACH_DIR', os.path.join(DATA_DIR, 'files'))
# 笔记 txt 冷备目录
BACKUP_DIR = os.environ.get('BACKUP_DIR', os.path.join(BASE_DIR, 'backup'))

# 相册文件根目录（机械盘多盘拓扑：可指向 D:/photos 等大容量盘）
# 首版缺省落到 {DATA_DIR}/photos，便于开箱即用
PHOTOS_DIR = os.environ.get('PHOTOS_DIR', os.path.join(DATA_DIR, 'photos'))

# 单用户常量：真实值在迁移脚本执行后写入 core.db app_config（single_user_id/name），
# 此处为全新安装的默认兜底
SINGLE_USER_ID = os.environ.get('SINGLE_USER_ID', '1')
SINGLE_USER_NAME = os.environ.get('SINGLE_USER_NAME', 'user')

# 设备配对
PAIR_CODE_TTL = 600          # 配对码有效期（秒）
PAIR_CODE_PATTERN = '4+4'    # AB3K-D9QX 格式
PAIR_CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'  # 去易混字符 I/L/O/0/1

# 管理后台
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')
ADMIN_TOKEN_TTL = 7200

# 孤儿设备清理阈值：last_active 超过该天数未活动即提示回收
DEVICE_ORPHAN_DAYS = 30

# 云端回收站（SPEC-CLOUD-RECYCLE-RECOVERY.md §1.1）
TRASH_RETAIN_DAYS = 30            # 软删保留天数，超期自动物理清除
TRASH_SWEEP_INTERVAL = 86400      # 回收站超期清理扫描间隔（秒，默认每天）

# 分片上传会话（SPEC-UPLOAD-TRANSPORT.md §3.5）：孤儿会话 TTL 后由后台回收
UPLOAD_SESSION_TTL = 24 * 3600    # 未 complete 会话保留时长（秒）
UPLOAD_SWEEP_INTERVAL = 3600      # 上传会话清理扫描间隔（秒）

# 通用协议
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# notes（沿用 mock-server）
COLORS = ['blue', 'green', 'yellow', 'orange', 'red', 'gray']
SHARE_DEFAULT_LOGO_TEXT = '分享来自 Open Note'
SHARE_DEFAULT_WATERMARK = '备忘录'
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}

# 压缩图规格（两阶段上传）
THUMB_MAX_EDGE = 1280
THUMB_QUALITY = 70
THUMB_MAX_BYTES = 200 * 1024

# photos 插件（分片上传）
PHOTO_MAX_CHUNK_SIZE = 4 * 1024 * 1024       # 单片上限 4MB
PHOTO_MAX_TOTAL_SIZE = 8 * 1024 * 1024 * 1024  # 单文件上限 8GB
PHOTO_CHUNK_SIZE = 4 * 1024 * 1024           # 客户端分片基准 4MB
DISK_FREE_THRESHOLD = 10 * 1024 * 1024 * 1024  # 磁盘满保护：低于 10GB 暂停原图上传

# drive 插件
DRIVE_ENABLED_DEFAULT = '0'                  # 云盘默认关闭，管理后台显式开启
# 签名临时 URL 密钥（ARCHITECTURE §6.8）：留空则首次使用自动生成并持久化到 core.db；泄露可换环境变量轮换
SIGN_SECRET = os.environ.get('SIGN_SECRET', '')
SIGN_URL_TTL = 600                            # 签名 URL 默认有效期（秒）
# 显式共享映射 {share名: 本地路径}：测试/开发兜底；生产经 DRIVE_SHARES_JSON 环境变量配置
_drive_shares_env = os.environ.get('DRIVE_SHARES_JSON', '')
DRIVE_SHARES = json.loads(_drive_shares_env) if _drive_shares_env else {}
# 备份目录默认隐藏（photos 落盘目录）
DRIVE_HIDDEN_DIRS = ('云备份', 'photos')

# 插件启用列表（注册表从 config 读取）
ENABLED_PLUGINS = ['notes', 'photos', 'drive']

# 笔记前端静态资源（同源托管，UA 分发）
STATIC_DESKTOP = os.path.join(BASE_DIR, 'static', 'desktop')
STATIC_MOBILE = os.path.join(BASE_DIR, 'static', 'mobile')
MOBILE_UA_KEYWORDS = ['mobile', 'android', 'iphone', 'ipad', 'webos', 'blackberry', 'windows phone']

# 云服务 Web 前端（SPEC-WEB-FRONTEND.md）：/portal /web/album /web/drive
STATIC_WEB = os.path.join(BASE_DIR, 'static', 'web')
PORTAL_APPS_JSON = os.path.join(BASE_DIR, 'web', 'portal_apps.json')

# 服务端内置占位图
PLACEHOLDER_IMAGE_PATH = os.path.join(BASE_DIR, 'static', 'placeholder.webp')

# Flask
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB
