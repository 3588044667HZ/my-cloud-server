"""缩略图生成（Pillow，复用 mock-server THUMB 规格：最长边 ≤1280、WEBP q70、目标 ≤200KB）。"""
import io
import os

from config import THUMB_MAX_BYTES, THUMB_MAX_EDGE, THUMB_QUALITY


def make_thumbnail_bytes(src_path):
    """从源图片生成压缩图字节流；失败返回 None。"""
    try:
        from PIL import Image
        with Image.open(src_path) as img:
            w, h = img.size
            scale = min(1.0, THUMB_MAX_EDGE / max(w, h))
            if scale < 1.0:
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            quality = THUMB_QUALITY
            buf = io.BytesIO()
            img.save(buf, 'WEBP', quality=quality)
            data = buf.getvalue()
            while len(data) > THUMB_MAX_BYTES and quality > 40:
                quality -= 10
                buf = io.BytesIO()
                img.save(buf, 'WEBP', quality=quality)
                data = buf.getvalue()
            return data
    except Exception:
        return None


def image_dimensions(src_path):
    """返回 (width, height)；无法解析返回 (0, 0)。"""
    try:
        from PIL import Image
        with Image.open(src_path) as img:
            return img.width, img.height
    except Exception:
        return 0, 0
