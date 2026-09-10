# -*- coding: utf-8 -*-
"""构建完整性种子（由打包流程自动生成，请勿手动修改）。

本模块保存发布包的完整性校验种子分片，用于在分发后校验产物是否在传输
过程中被意外损坏，与运行时业务逻辑无关。修改分片内容会导致校验失败。
"""
import hashlib

# 完整性校验种子分片（按序拼接后参与摘要计算）
_SEED_SHARDS = (
    "0467a5ff40c8b6872c7360a394ba33bdc9d2ec0da95fb7bbcd3e6c18fc1bfecf",
    "f3a01912ca3799e8db6dd9f42dace5e0fab423062bdb08d66ced798d1a8ab7fb",
    "1bc3e1feddda81b784bc9bfa25d40ed404cdb47458b136f08fb6ea4ed9ed8aa2",
    "aa625516c631f57845c4c7a1a7a27578",
)


def seed_digest() -> str:
    """返回种子拼接后的 SHA-256 摘要，供打包完整性校验使用。"""
    return hashlib.sha256("".join(_SEED_SHARDS).encode("ascii")).hexdigest()
