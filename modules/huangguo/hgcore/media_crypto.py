# -*- coding: utf-8 -*-
"""媒体解密：站点封面的加密数据还原（多站点、多算法）。

不同「黄果」站点使用不同的封面加密方式，由站点注册表的 cover_crypt 指定：

  - huangguoai.com -> AES-128-CBC（前端 crypto-worker.js / CryptoJS）
        mode    = AES-128-CBC
        padding = NoPadding
        key     = charCode 序列还原 -> "f5d965df75336270"
        iv      = charCode 序列还原 -> "97b60394abc2fbe1"

  - huangguo.me    -> XOR（前端 static/common/imgDecypt.js 的 imgEncrypt）
        仅前 100 字节与密钥 "2019ysapp7527" 循环异或（异或自反，加解密同一函数）

统一入口 decrypt_cover() 会依据当前生效站点自动选择算法。
"""

from typing import Optional

from Crypto.Cipher import AES

# 配置以 "字符码_字符码_..." 形式硬编码在前端 bundle 中
_OBF_KEY = "102_53_100_57_54_53_100_102_55_53_51_51_54_50_55_48"
_OBF_IV = "57_55_98_54_48_51_57_52_97_98_99_50_102_98_101_49"


def _deobfuscate(obf: str) -> bytes:
    """把 '102_53_100_...' 还原为原始字节串。"""
    return "".join(chr(int(p)) for p in obf.split("_")).encode("ascii")


KEY = _deobfuscate(_OBF_KEY)
IV = _deobfuscate(_OBF_IV)

assert len(KEY) == 16 and len(IV) == 16, "AES-128 key/iv 必须为 16 字节"


def decrypt_image(data: bytes) -> Optional[bytes]:
    """解密封面数据，返回 JPEG 明文；输入异常时返回 None。

    - NoPadding 模式：服务器端把明文补齐到 16 倍数，解密结果尾部
      可能有多余字节，这里按 JPEG 结束标记 (FFD9) 截断。
    - 非加密数据（已是图片）原样返回，调用方无需预判。
    """
    if not data:
        return None

    # 已是明文图片：直接返回
    if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
        return data

    if len(data) % 16 != 0:
        return None

    try:
        cipher = AES.new(KEY, AES.MODE_CBC, IV)
        plain = cipher.decrypt(data)
    except ValueError:
        return None

    # JPEG: 截到最后一个 FFD9
    if plain[:3] == b"\xff\xd8\xff":
        end = plain.rfind(b"\xff\xd9")
        if end != -1:
            plain = plain[: end + 2]
        return plain

    # PNG: 按长度字段截断较复杂，直接返回全量
    if plain[:8] == b"\x89PNG\r\n\x1a\n":
        return plain

    return None


# ---------------------------------------------------------------- XOR（huangguo.me）

# 来自 huangguo.me 前端 static/common/imgDecypt.js：imgEncrypt 中的 KEY
_XOR_KEY = b"2019ysapp7527"
_XOR_LIMIT = 100     # 仅前 100 字节参与异或


def decrypt_image_xor(data: bytes) -> Optional[bytes]:
    """还原 huangguo.me 的 XOR 封面：前 100 字节与密钥循环异或。

    XOR 自反，加密与解密是同一运算。已是明文图片时原样返回。
    """
    if not data:
        return None
    if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    if len(data) < 4:
        return None

    buf = bytearray(data)
    n = min(_XOR_LIMIT, len(buf))
    klen = len(_XOR_KEY)
    for i in range(n):
        buf[i] ^= _XOR_KEY[i % klen]

    out = bytes(buf)
    # 校验还原结果确为常见图片格式
    if (out[:3] == b"\xff\xd8\xff" or out[:8] == b"\x89PNG\r\n\x1a\n"
            or out[:4] == b"GIF8"
            or (out[:4] == b"RIFF" and out[8:12] == b"WEBP")):
        return out
    return None


# ---------------------------------------------------------------- 统一入口

def decrypt_cover(data: bytes, mode: str = None) -> Optional[bytes]:
    """按算法模式解密封面。

    mode: "aes" / "xor" / "" 或 None（None 时读取当前生效站点配置）。
    """
    if mode is None:
        try:
            from . import config
            mode = config.ACTIVE_SITE.get("cover_crypt", "aes")
        except Exception:                      # noqa: BLE001
            mode = "aes"

    if mode == "xor":
        return decrypt_image_xor(data)
    if not mode:
        return data
    return decrypt_image(data)
