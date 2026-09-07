#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JSON to M3U Converter — 广西移动 IPTV 频道列表转换工具

从运营商 EPG API 抓取频道 JSON，生成播放器友好的 M3U 播放列表。

设计要点（相对旧版）：
  * 首行输出标准 ``#EXTM3U``（不带属性），最大化播放器兼容性；
    EPG 地址写入注释行，可用 --tvg-in-header 恢复旧式写法。
  * 文件头 ``# Total channels:`` 统计的是 **有效频道数**（过滤后）。
  * 输出不含任何时间戳 → 相同 API 数据生成字节级一致的文件，
    配合 git diff 即可判断频道列表是否发生“实质变化”，
    避免定时任务产生无意义的提交与部署。
  * tvg-id 使用针对 EPG 源归一化后的频道名（uuid 与 EPG 频道 ID 完全
    不匹配，实测匹配数为 0；归一化频道名可命中 130+ 条）。
  * 过滤坏数据：usable != 1、空/非法播放地址、路径含空段（如
    ``.../224//index.m3u8``，流 ID 缺失必然无法播放）、坏 logo（"http"）。
  * 网络请求带重试与退避，支持 gzip 与多字符集解码。
  * 有效频道数低于 --min-channels 时判定为上游数据异常，拒绝覆盖
    现有播放列表并以非零码退出（保护定时任务不产出“空列表”）。
  * 原子写入：先写临时文件再 os.replace，避免半截文件。

仅依赖 Python 3.8+ 标准库。
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

# --------------------------------------------------------------------------
# 默认配置
# --------------------------------------------------------------------------
DEFAULT_CHANNELS_URL = (
    "http://gxtvepg.taipan.jda.bcs.ottcn.com:8080/ysten-lvoms-epg/epg/"
    "getChannels.shtml?deviceGroupId=4747&districtCode=450000"
)
DEFAULT_EPG_URL = "https://down.nigx.cn/epg.112114.xyz/pp.xml.gz"
DEFAULT_OUTPUT = "channels.m3u"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 播放地址允许的协议（含组播 udp/rtp，VLC、DIYP、TiviMate 等支持）
PLAY_SCHEMES = {"http", "https", "rtmp", "rtsp", "rtp", "udp", "mms"}

log = logging.getLogger("json_to_m3u")


class FetchError(RuntimeError):
    """抓取或解析 API 数据失败。"""


# --------------------------------------------------------------------------
# 文本清洗 / 校验
# --------------------------------------------------------------------------
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPACES_RE = re.compile(r"\s+")


def clean_text(value: Any) -> str:
    """把任意 JSON 值转为单行安全文本：去控制字符、合并空白、双引号转单引号。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value)
    text = _CONTROL_CHARS_RE.sub(" ", text)
    text = _SPACES_RE.sub(" ", text).strip()
    return text.replace('"', "'")


def is_valid_image_url(url: str) -> bool:
    """logo / channelIcon 有效性：必须是带主机与路径的 http(s) 地址。

    API 数据中存在 ``"logo": "http"`` 这类残缺值（实测 127 条），必须过滤。
    """
    if not url:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc) and bool(parts.path)


def classify_play_url(url: str) -> str:
    """判定播放地址：'ok' 可用 / 'empty' 为空 / 'broken' 残缺 / 'scheme' 协议不支持。"""
    url = (url or "").strip()
    if not url:
        return "empty"
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "broken"
    scheme = parts.scheme.lower()
    if scheme not in PLAY_SCHEMES:
        return "scheme"
    if not parts.netloc and not parts.path:
        return "broken"
    if scheme in ("http", "https"):
        # 路径中出现空段（如 .../224//index.m3u8）说明上游缺失流 ID，必然播放失败
        if "//" in parts.path:
            return "broken"
    return "ok"


# --------------------------------------------------------------------------
# EPG ID 归一化（离线规则，已对照目标 EPG 源实际频道 ID 验证）
# --------------------------------------------------------------------------
_CODEC_PREFIX_RE = re.compile(r"^(?:H\.?\s?26[45]|HEVC|AVC)[\s\-_—]*", re.IGNORECASE)
_INTL_TV_RE = re.compile(r"^中国国际电视台\s*(.{1,8}?)\s*频道?$")
_LANG_MAP = {"西班牙语": "西语", "阿拉伯语": "阿语", "俄语": "俄语", "法语": "法语", "英语": "英语"}
_BRAND_RE = re.compile(r"^(CCTV|CGTN|CETV)[\s\-_—]+", re.IGNORECASE)
_LOCAL_PREFIX_RE = re.compile(r"^(?:广西|GX)[\s\-_—]+", re.IGNORECASE)
# 中文画质词（可带码率）：高清8M / 标清4M / 超高清 / 蓝光
_QUALITY_CN_RE = re.compile(r"[\s\-_—]*(?:标清|高清|超高清|超清|蓝光)\s*\d*(?:\.\d+)?\s*[MK]?$", re.IGNORECASE)
# 纯码率后缀：2.5M / 4M；前面紧邻字母数字时不剥离（保护 "CCTV4K" 这类 EPG ID）
_BITRATE_RE = re.compile(r"(?<![A-Za-z0-9])[\s\-_—]*\d+(?:\.\d+)?\s*[MK]$", re.IGNORECASE)
# 英文画质后缀：带分隔符时直接去掉（"CCTV16 4K"→"CCTV16"）；
# 不带分隔符时要求前一字符不是字母数字，避免误伤 "CCTV4K" 这类本身是 EPG ID 的名称
_QUALITY_EN_RE = re.compile(r"(?:[\s\-_—]+|(?<![A-Za-z0-9]))(?:HD|SD|FHD|UHD|4K|8K)$", re.IGNORECASE)
_CETV_TAO_RE = re.compile(r"^(中国教育[0-9一二三四五六]+)套$")
_TRAIL_SEPARATORS = " \t-_—"


def normalize_epg_id(name: str) -> str:
    """把频道显示名归一化为 EPG 频道 ID 的最佳猜测。

    例：'H265-CCTV-2高清4M' → 'CCTV2'；'兵团卫视标清4M' → '兵团卫视'；
        '中国国际电视台俄语频道' → 'CGTN俄语'；'中国教育2套标清4M' → '中国教育2台'。
    规则全部离线、确定性，不匹配任何 EPG 时也仅是无害的 tvg-id。
    """
    s = clean_text(name)
    if not s:
        return ""
    s = _CODEC_PREFIX_RE.sub("", s).strip(_TRAIL_SEPARATORS)
    m = _INTL_TV_RE.match(s)
    if m:
        lang = _LANG_MAP.get(m.group(1), m.group(1))
        return "CGTN" + lang
    s = _BRAND_RE.sub(lambda mm: mm.group(1).upper(), s)
    s = _LOCAL_PREFIX_RE.sub("", s)
    for _ in range(3):  # 最多迭代去除多重后缀，如 "超高清 4K"
        t = _QUALITY_CN_RE.sub("", s)
        t = _BITRATE_RE.sub("", t)
        t = _QUALITY_EN_RE.sub("", t)
        t = t.strip(_TRAIL_SEPARATORS)
        if t == s:
            break
        s = t
    s = _CETV_TAO_RE.sub(r"\1台", s)
    return s.strip(_TRAIL_SEPARATORS)


# --------------------------------------------------------------------------
# 网络抓取
# --------------------------------------------------------------------------
def decode_payload(raw: bytes, charset: Optional[str]) -> str:
    """按 声明字符集 → utf-8 → gb18030 的顺序严格解码，全部失败才降级 replace。"""
    candidates: List[str] = []
    if charset:
        candidates.append(charset)
    candidates.extend(("utf-8", "gb18030"))  # gb18030 兼容 gbk/gb2312
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_json(url: str, timeout: float = 30, retries: int = 3, retry_delay: float = 5.0) -> Any:
    """抓取并解析 JSON，带重试与指数退避；全部失败抛 FetchError。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "gzip",
        "Referer": urllib.parse.urlsplit(url).scheme + "://" + (urllib.parse.urlsplit(url).netloc or "") + "/",
    }
    last_error: Optional[BaseException] = None
    for attempt in range(1, retries + 1):
        try:
            log.info("正在获取数据(第 %d/%d 次): %s", attempt, retries, url)
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                charset = response.headers.get_content_charset()
                encoding_header = (response.headers.get("Content-Encoding") or "").lower()
            if encoding_header == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            data = json.loads(decode_payload(raw, charset))
            log.info("数据获取成功，共 %d 字节", len(raw))
            return data
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # URLError 含 HTTPError；ValueError 含 JSONDecodeError / UnicodeDecodeError
            last_error = exc
            detail = getattr(exc, "reason", exc)
            code = getattr(exc, "code", None)
            log.warning("获取失败%s: %s", f"(HTTP {code})" if code else "", detail)
            if attempt < retries:
                delay = retry_delay * attempt
                log.info("%.0f 秒后重试…", delay)
                time.sleep(delay)
    raise FetchError(f"连续 {retries} 次获取失败: {last_error}")


def extract_channel_list(data: Any) -> List[Any]:
    """容忍 API 直接返回列表，或包裹在常见键名下的字典。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("channels", "data", "list", "result", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    raise FetchError("JSON 数据不是频道列表格式（也不是含常见列表键的对象）")


# --------------------------------------------------------------------------
# M3U 生成
# --------------------------------------------------------------------------
def sort_key(item: Dict[str, Any]) -> float:
    """按频道号 no 排序，缺失/非法的排在最后。"""
    no = item.get("no")
    if no is None:
        return math.inf
    try:
        return int(no)
    except (TypeError, ValueError):
        try:
            return float(no)
        except (TypeError, ValueError):
            return math.inf


def is_usable(item: Dict[str, Any]) -> bool:
    """usable 字段缺失时视为可用；存在时必须为 1/'1'/True。"""
    usable = item.get("usable")
    if usable is None:
        return True
    if isinstance(usable, bool):
        return usable
    return str(usable).strip() == "1"


def build_m3u(
    channels: Iterable[Any],
    epg_url: str = DEFAULT_EPG_URL,
    tvg_in_header: bool = False,
) -> Tuple[List[str], Dict[str, int]]:
    """把频道 JSON 列表转换为 M3U 行。返回 (lines, stats)。

    输出是确定性的：不含时间戳，相同输入 → 相同输出。
    """
    stats: Dict[str, int] = {
        "fetched": 0, "malformed": 0, "unusable": 0, "no_name": 0,
        "empty_url": 0, "bad_scheme": 0, "broken_url": 0, "duplicate": 0, "valid": 0,
    }
    records: List[Dict[str, str]] = []
    seen: set = set()

    for item in channels:
        stats["fetched"] += 1
        if not isinstance(item, dict):
            stats["malformed"] += 1
            continue
        if not is_usable(item):
            stats["unusable"] += 1
            continue

        name = clean_text(item.get("channelName"))
        if not name:
            stats["no_name"] += 1
            continue

        play_url = clean_text(item.get("livePlayUrl"))
        verdict = classify_play_url(play_url)
        if verdict == "empty":
            stats["empty_url"] += 1
            continue
        if verdict == "scheme":
            stats["bad_scheme"] += 1
            log.warning("跳过(协议不支持): %s -> %s", name, play_url)
            continue
        if verdict == "broken":
            stats["broken_url"] += 1
            log.warning("跳过(播放地址残缺): %s -> %s", name, play_url)
            continue

        dedupe_key = (name, play_url)
        if dedupe_key in seen:
            stats["duplicate"] += 1
            continue
        seen.add(dedupe_key)

        # logo 优先，channelIcon 兜底；"http" 等残缺值已被 is_valid_image_url 拦截
        logo = clean_text(item.get("logo"))
        if not is_valid_image_url(logo):
            logo = clean_text(item.get("channelIcon"))
            if not is_valid_image_url(logo):
                logo = ""

        tvg_id = normalize_epg_id(name) or clean_text(item.get("uuid") or item.get("urlid"))
        chno = clean_text(item.get("no"))

        records.append({
            "name": name,
            "url": play_url,
            "logo": logo,
            "tvg_id": tvg_id,
            "chno": chno,
            "sort": sort_key(item),
        })

    records.sort(key=lambda r: r["sort"])  # 稳定排序，保证确定性
    stats["valid"] = len(records)

    # ---- 文件头 ----
    if tvg_in_header:
        lines = ['#EXTM3U url-tvg="%s"' % epg_url]
    else:
        # 标准写法：首行只有 #EXTM3U，兼容性最好；EPG 地址放注释行
        lines = ["#EXTM3U", '# url-tvg="%s"' % epg_url]
    # 注意：这里统计的是过滤后的有效频道数，且不写入更新时间戳
    lines.append("# Total channels: %d" % stats["valid"])

    # ---- 频道条目 ----
    for rec in records:
        attrs = ['#EXTINF:-1 tvg-id="%s" tvg-name="%s"' % (rec["tvg_id"], rec["name"])]
        if rec["logo"]:
            attrs.append('tvg-logo="%s"' % rec["logo"])
        if rec["chno"]:
            attrs.append('tvg-chno="%s"' % rec["chno"])
        lines.append(" ".join(attrs) + "," + rec["name"])
        lines.append(rec["url"])

    return lines, stats


def render_m3u(lines: List[str]) -> str:
    return "\n".join(lines) + "\n"


def write_atomic(path: str, content: str) -> None:
    """先写同目录临时文件再原子替换，避免生成半截文件。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".channels-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取运营商频道 JSON 并生成 M3U 播放列表（输出确定性、无时间戳）",
    )
    parser.add_argument("output_pos", nargs="?", default=None,
                        help="输出文件路径（位置参数，兼容旧用法；推荐用 -o）")
    parser.add_argument("-o", "--output", default=None, help="输出 M3U 文件路径，默认 %s" % DEFAULT_OUTPUT)
    parser.add_argument("--url", default=DEFAULT_CHANNELS_URL, help="频道 JSON API 地址")
    parser.add_argument("--epg-url", default=DEFAULT_EPG_URL, help="写入播放列表的 EPG 地址")
    parser.add_argument("--timeout", type=float, default=30, help="单次请求超时秒数，默认 30")
    parser.add_argument("--retries", type=int, default=3, help="请求重试次数，默认 3")
    parser.add_argument("--retry-delay", type=float, default=5.0, help="重试基础间隔秒数（线性递增），默认 5")
    parser.add_argument("--min-channels", type=int, default=20,
                        help="有效频道数低于该值视为上游数据异常，拒绝写文件并报错退出，默认 20")
    parser.add_argument("--tvg-in-header", action="store_true",
                        help="把 url-tvg 属性写回 #EXTM3U 首行（旧式写法，部分播放器需要）")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出警告与错误")
    args = parser.parse_args(argv)
    args.output = args.output or args.output_pos or DEFAULT_OUTPUT
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )

    try:
        data = fetch_json(args.url, timeout=args.timeout,
                          retries=args.retries, retry_delay=args.retry_delay)
        channels = extract_channel_list(data)
    except FetchError as exc:
        log.error("%s", exc)
        return 1

    lines, stats = build_m3u(channels, epg_url=args.epg_url, tvg_in_header=args.tvg_in_header)

    # 注意：logging 的 %-格式化要么全用位置参数，要么全用命名参数+单个 dict，不可混用
    log.info("抓取 %(fetched)d 条 | 有效 %(valid)d 条 | 跳过: 不可用 %(unusable)d, "
             "无名 %(no_name)d, 空地址 %(empty_url)d, 协议不支持 %(bad_scheme)d, "
             "残缺地址 %(broken_url)d, 非对象 %(malformed)d, 重复 %(duplicate)d", stats)

    if stats["valid"] < args.min_channels:
        log.error("有效频道仅 %d 个，低于安全阈值 %d，疑似上游数据异常；"
                  "保留现有文件不覆盖。", stats["valid"], args.min_channels)
        return 2

    try:
        write_atomic(args.output, render_m3u(lines))
    except OSError as exc:
        log.error("写入文件失败: %s", exc)
        return 1

    log.info("转换成功！已生成: %s（有效频道 %d 个，EPG: %s）",
             args.output, stats["valid"], args.epg_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
