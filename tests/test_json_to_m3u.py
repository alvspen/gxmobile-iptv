# -*- coding: utf-8 -*-
"""json_to_m3u 的单元测试与端到端测试（仅标准库）。

运行方式（仓库根目录）：
    python -m unittest discover -s tests -v
"""

import gzip
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json_to_m3u as jtm  # noqa: E402


# --------------------------------------------------------------------------
# 纯函数测试
# --------------------------------------------------------------------------
class TestCleanText(unittest.TestCase):
    def test_basics(self):
        self.assertEqual(jtm.clean_text(None), "")
        self.assertEqual(jtm.clean_text(500), "500")
        self.assertEqual(jtm.clean_text("  广西 卫视\r\n"), "广西 卫视")

    def test_quotes_and_control_chars(self):
        self.assertEqual(jtm.clean_text('a"b'), "a'b")
        self.assertEqual(jtm.clean_text("a\x00b\x1fc"), "a b c")


class TestImageAndPlayUrl(unittest.TestCase):
    def test_bad_logos_from_real_api(self):
        # 真实 API 中出现的残缺值
        for bad in ("", "http", "HTTP", "https", "http://", "ftp://a/b.png"):
            self.assertFalse(jtm.is_valid_image_url(bad), bad)

    def test_good_logos(self):
        good = "http://images.center.bcs.ottcn.com:8080/images/x/BTWSTB0126.png"
        self.assertTrue(jtm.is_valid_image_url(good))

    def test_play_url_classification(self):
        self.assertEqual(jtm.classify_play_url(""), "empty")
        self.assertEqual(jtm.classify_play_url("   "), "empty")
        self.assertEqual(
            jtm.classify_play_url("http://cdn/PLTV/77777777/224//index.m3u8?servicetype=1"),
            "broken")
        self.assertEqual(jtm.classify_play_url("file:///etc/passwd"), "scheme")
        self.assertEqual(
            jtm.classify_play_url("http://cdn/PLTV/77777777/224/3221225795/index.m3u8?servicetype=1"),
            "ok")
        self.assertEqual(jtm.classify_play_url("udp://@239.0.1.2:5140"), "ok")
        self.assertEqual(jtm.classify_play_url("rtsp://a/b"), "ok")


class TestNormalizeEpgId(unittest.TestCase):
    CASES = {
        "兵团卫视标清4M": "兵团卫视",
        "安徽卫视高清8M": "安徽卫视",
        "CCTV-13超高清": "CCTV13",
        "H265-CCTV-2高清4M": "CCTV2",
        "CCTV-16 4K": "CCTV16",
        "CCTV4K": "CCTV4K",          # EPG 中本身就叫 CCTV4K，不能误删
        "CCTV5+高清": "CCTV5+",
        "北京卫视4K": "北京卫视",
        "三沙卫视2.5M": "三沙卫视",
        "广西-动作电影": "动作电影",
        "广西综艺旅游": "广西综艺旅游",  # 无分隔符不误删
        "中国国际电视台俄语频道": "CGTN俄语",
        "中国国际电视台西班牙语频道": "CGTN西语",
        "中国教育2套标清4M": "中国教育2台",
        "慢享邕城日出日落": "慢享邕城日出日落",
    }

    def test_cases(self):
        for raw, expected in self.CASES.items():
            self.assertEqual(jtm.normalize_epg_id(raw), expected, raw)

    def test_deterministic(self):
        for raw in self.CASES:
            self.assertEqual(jtm.normalize_epg_id(raw), jtm.normalize_epg_id(raw))


class TestSortAndUsable(unittest.TestCase):
    def test_sort_key(self):
        self.assertEqual(jtm.sort_key({"no": 5}), 5)
        self.assertEqual(jtm.sort_key({"no": "7"}), 7)
        self.assertEqual(jtm.sort_key({"no": None}), float("inf"))
        self.assertEqual(jtm.sort_key({}), float("inf"))
        self.assertEqual(jtm.sort_key({"no": "abc"}), float("inf"))

    def test_usable(self):
        self.assertTrue(jtm.is_usable({"usable": 1}))
        self.assertTrue(jtm.is_usable({"usable": "1"}))
        self.assertTrue(jtm.is_usable({}))            # 缺失视为可用
        self.assertFalse(jtm.is_usable({"usable": 0}))
        self.assertFalse(jtm.is_usable({"usable": "0"}))


# --------------------------------------------------------------------------
# build_m3u 测试
# --------------------------------------------------------------------------
def make_channel(no, name, url, usable=1, logo="http", icon="", uuid=None):
    return {
        "uuid": uuid or ("id-%d" % no), "channelName": name, "logo": logo,
        "channelIcon": icon, "no": no, "urlid": "u%d" % no,
        "livePlayUrl": url, "coverImgUrl": "", "usable": usable, "onplay": "",
    }


GOOD = "http://cdn.example.com/PLTV/1/224/32212/index.m3u8?servicetype=1"
GOOD2 = "http://cdn.example.com/PLTV/1/224/32213/index.m3u8?servicetype=1"
BROKEN = "http://cdn.example.com/PLTV/1/224//index.m3u8?servicetype=1"


class TestBuildM3u(unittest.TestCase):
    def test_header_and_counts(self):
        channels = [
            make_channel(2, "频道B", GOOD2),
            make_channel(1, "频道A", GOOD, usable=0),          # 不可用
            make_channel(3, "频道C", BROKEN),                  # 残缺地址
            make_channel(4, "", GOOD),                         # 无名称
            make_channel(5, "频道D", ""),                      # 空地址
            make_channel(6, "频道E", GOOD2, logo="http",       # 坏 logo → icon 兜底
                         icon="https://img.example.com/e.png"),
            make_channel(7, "频道E", GOOD2),                   # 与上一条重复(name+url)
        ]
        lines, stats = jtm.build_m3u(channels, epg_url="https://epg.example/x.xml.gz")
        # 首行必须是纯 #EXTM3U（兼容性要求）
        self.assertEqual(lines[0], "#EXTM3U")
        self.assertEqual(lines[1], '# url-tvg="https://epg.example/x.xml.gz"')
        # 头部统计 = 有效频道数：B、E(icon) = 2 个有效
        self.assertEqual(stats["valid"], 2)
        self.assertEqual(lines[2], "# Total channels: 2")
        self.assertEqual(stats["unusable"], 1)
        self.assertEqual(stats["broken_url"], 1)
        self.assertEqual(stats["no_name"], 1)
        self.assertEqual(stats["empty_url"], 1)
        self.assertEqual(stats["duplicate"], 1)

        extinf = [l for l in lines if l.startswith("#EXTINF")]
        self.assertEqual(len(extinf), stats["valid"])
        # 残缺 logo "http" 不得出现在输出中；icon 兜底生效
        self.assertNotIn('tvg-logo="http"', "\n".join(lines))
        self.assertIn('tvg-logo="https://img.example.com/e.png"', "\n".join(lines))
        # 每个 EXTINF 下一行必须是播放地址
        for i, l in enumerate(lines):
            if l.startswith("#EXTINF"):
                self.assertTrue(lines[i + 1].startswith(("http", "udp", "rtsp")), lines[i + 1])

    def test_sorted_by_no(self):
        channels = [make_channel(n, "C%d" % n, GOOD.replace("32212", str(n)))
                    for n in (30, 10, 20)]
        lines, _ = jtm.build_m3u(channels)
        chnos = [l for l in lines if "tvg-chno" in l]
        self.assertIn('tvg-chno="10"', chnos[0])
        self.assertIn('tvg-chno="20"', chnos[1])
        self.assertIn('tvg-chno="30"', chnos[2])

    def test_deterministic_output(self):
        channels = [make_channel(n, "C%d" % n, GOOD.replace("32212", str(n)))
                    for n in (3, 1, 2)]
        a, _ = jtm.build_m3u(channels)
        b, _ = jtm.build_m3u(channels)
        self.assertEqual(jtm.render_m3u(a), jtm.render_m3u(b))
        # 输出中不得含任何时间戳痕迹
        text = jtm.render_m3u(a)
        self.assertNotIn("Last updated", text)
        self.assertNotIn("UTC", text)

    def test_quote_escaping(self):
        channels = [make_channel(1, '恶意"频道"名', GOOD)]
        lines, stats = jtm.build_m3u(channels)
        self.assertEqual(stats["valid"], 1)
        self.assertIn('tvg-name="恶意\'频道\'名"', lines[3])

    def test_tvg_in_header_option(self):
        lines, _ = jtm.build_m3u([make_channel(1, "A", GOOD)],
                                 epg_url="https://e/x.gz", tvg_in_header=True)
        self.assertEqual(lines[0], '#EXTM3U url-tvg="https://e/x.gz"')

    def test_malformed_entries(self):
        lines, stats = jtm.build_m3u(["not-a-dict", 42, None, make_channel(1, "A", GOOD)])
        self.assertEqual(stats["malformed"], 3)
        self.assertEqual(stats["valid"], 1)

    def test_udp_kept(self):
        lines, stats = jtm.build_m3u([make_channel(1, "组播频道", "udp://@239.0.1.2:5140")])
        self.assertEqual(stats["valid"], 1)
        self.assertIn("udp://@239.0.1.2:5140", lines)


# --------------------------------------------------------------------------
# 端到端：本地 HTTP 服务器 + fetch_json + main()
# --------------------------------------------------------------------------
FIXTURE = [
    {"uuid": "SD-x", "channelName": "兵团卫视标清4M", "logo": "http",
     "channelIcon": "http://img/x.png", "no": 500, "urlid": "SD-x",
     "livePlayUrl": "http://cdn/PLTV/1/224/101/index.m3u8", "usable": 1},
    {"uuid": "g", "channelName": "广西综艺旅游", "logo": "http", "no": 368,
     "urlid": "g", "livePlayUrl": "udp://@239.0.1.2:5140", "usable": 1},
    {"uuid": "b", "channelName": "坏频道", "no": 900, "urlid": "b",
     "livePlayUrl": "http://cdn/PLTV/1/224//index.m3u8", "usable": 1},
]


class _Handler(BaseHTTPRequestHandler):
    """按路径分发：/ok /gzip /gbk /flaky(首次500) /badjson /empty"""
    flaky_hits = 0

    def log_message(self, *args):  # 静默
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        payload = json.dumps(FIXTURE).encode("utf-8")
        if path == "/ok":
            self._send(200, payload)
        elif path == "/gzip":
            self._send(200, gzip.compress(payload), extra={"Content-Encoding": "gzip"})
        elif path == "/gbk":
            self._send(200, json.dumps(FIXTURE, ensure_ascii=False).encode("gbk"),
                       ctype="application/json; charset=gbk")
        elif path == "/flaky":
            _Handler.flaky_hits += 1
            if _Handler.flaky_hits == 1:
                self._send(500, b"oops", ctype="text/plain")
            else:
                self._send(200, payload)
        elif path == "/badjson":
            self._send(200, b"{not json", ctype="text/plain")
        elif path == "/wrongshape":
            self._send(200, json.dumps({"msg": "no list here"}).encode())
        elif path == "/empty":
            self._send(200, json.dumps([]).encode())
        else:
            self._send(404, b"")


class ServerTestCase(unittest.TestCase):
    server = None
    thread = None

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()


class TestFetchJson(ServerTestCase):
    def fetch(self, path, **kw):
        kw.setdefault("timeout", 5)
        kw.setdefault("retries", 2)
        kw.setdefault("retry_delay", 0.01)
        return jtm.fetch_json(self.base + path, **kw)

    def test_ok(self):
        data = self.fetch("/ok")
        self.assertEqual(len(data), 3)

    def test_gzip(self):
        self.assertEqual(len(self.fetch("/gzip")), 3)

    def test_gbk(self):
        data = self.fetch("/gbk")
        self.assertEqual(data[0]["channelName"], "兵团卫视标清4M")

    def test_retry_on_500(self):
        _Handler.flaky_hits = 0
        data = self.fetch("/flaky", retries=3)
        self.assertEqual(len(data), 3)
        self.assertEqual(_Handler.flaky_hits, 2)

    def test_badjson_raises(self):
        with self.assertRaises(jtm.FetchError):
            self.fetch("/badjson", retries=1)

    def test_404_raises(self):
        with self.assertRaises(jtm.FetchError):
            self.fetch("/nonexistent", retries=1)

    def test_extract_channel_list(self):
        with self.assertRaises(jtm.FetchError):
            jtm.extract_channel_list({"msg": "x"})
        self.assertEqual(jtm.extract_channel_list({"data": [1, 2]}), [1, 2])


class TestMainEndToEnd(ServerTestCase):
    def run_main(self, path, extra=()):
        """运行 main() 并捕获全部日志到 self.last_logs（避免污染测试输出，
        且规避 basicConfig 只在首次调用绑定 stderr 的问题）。"""
        import contextlib
        import io
        import logging
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "channels.m3u")
            argv = [
                "--url", self.base + path, "-o", out,
                "--retries", "1", "--timeout", "5", *extra,
            ]
            if "--min-channels" not in extra:
                argv += ["--min-channels", "1"]  # 测试夹具很小，避免触发默认安全阈值
            root = logging.getLogger()
            saved_handlers = list(root.handlers)
            for h in saved_handlers:
                root.removeHandler(h)
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    code = jtm.main(argv)  # basicConfig 会绑定当前(重定向后的) stderr
            finally:
                self.last_logs = err.getvalue()
                for h in list(root.handlers):
                    root.removeHandler(h)
                for h in saved_handlers:
                    root.addHandler(h)
            content = None
            if os.path.exists(out):
                with open(out, encoding="utf-8") as fh:
                    content = fh.read()
            return code, out, content

    def test_success(self):
        code, out, content = self.run_main("/ok")
        self.assertEqual(code, 0)
        lines = content.splitlines()
        self.assertEqual(lines[0], "#EXTM3U")
        # 3 条中 1 条残缺地址被过滤 → 有效 2
        self.assertEqual(lines[2], "# Total channels: 2")
        self.assertEqual(content.count("#EXTINF"), 2)
        self.assertIn("兵团卫视", content)
        self.assertIn("udp://@239.0.1.2:5140", content)
        self.assertNotIn("//index.m3u8", content)

    def test_min_channels_guard(self):
        code, out, content = self.run_main("/ok", extra=("--min-channels", "100"))
        self.assertEqual(code, 2)
        self.assertIsNone(content)  # 不得写出文件

    def test_empty_list_guard(self):
        code, _, content = self.run_main("/empty")
        self.assertEqual(code, 2)
        self.assertIsNone(content)

    def test_wrong_shape(self):
        code, _, content = self.run_main("/wrongshape")
        self.assertEqual(code, 1)
        self.assertIsNone(content)

    def test_deterministic_across_runs(self):
        _, _, c1 = self.run_main("/ok")
        _, _, c2 = self.run_main("/ok")
        self.assertEqual(c1, c2)

    def test_no_logging_format_errors(self):
        # 回归测试：汇总日志曾因混用位置参数与命名参数触发 "--- Logging error ---"
        code, _, _ = self.run_main("/ok")
        self.assertEqual(code, 0)
        self.assertNotIn("Logging error", self.last_logs)
        self.assertIn("有效", self.last_logs)
        self.assertIn("转换成功", self.last_logs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
