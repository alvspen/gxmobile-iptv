# 广西移动 IPTV — M3U 自动更新

从运营商 EPG API 定时抓取频道列表，生成高兼容性、确定性的 M3U 播放列表，并通过
GitHub Actions 每 6 小时自动更新；**仅在频道列表发生实质变化时才提交与部署**。

## 文件说明

| 文件 | 作用 |
|---|---|
| `json_to_m3u.py` | 转换脚本（仅 Python 3.8+ 标准库，无第三方依赖） |
| `.github/workflows/update-m3u.yml` | 定时抓取 + 变化检测 + 提交/部署工作流 |
| `tests/test_json_to_m3u.py` | 29 个单元/端到端测试（含本地 HTTP 服务器模拟上游） |
| `channels.m3u` | 生成的播放列表（由工作流自动维护） |

## 用法

```bash
# 默认：抓取真实 API，生成 channels.m3u
python3 json_to_m3u.py

# 常用参数
python3 json_to_m3u.py -o out/tv.m3u \
    --url 'http://gxtvepg.../getChannels.shtml?deviceGroupId=4747&districtCode=450000' \
    --epg-url 'https://down.nigx.cn/epg.112114.xyz/pp.xml.gz' \
    --timeout 30 --retries 3 --retry-delay 5 \
    --min-channels 20

# 运行测试
python3 -m unittest discover -s tests -v
```

退出码：`0` 成功；`1` 抓取/写入失败；`2` 有效频道数低于 `--min-channels`
安全阈值（疑似上游数据异常，**不覆盖现有文件**，工作流随之失败，不会提交坏数据）。

## 相对旧版的改进（均已实测验证）

### 兼容性
- **首行输出纯 `#EXTM3U`**（不带 `url-tvg` 属性），符合 HLS 规范，兼容最严格的
  解析器；EPG 地址写入第 2 行注释。个别需要旧式写法的播放器可用
  `--tvg-in-header` 恢复 `#EXTM3U url-tvg="..."`。
- 属性值清洗：去除控制字符/换行、合并空白、`"` 转 `'`，避免畸形条目。

### 头部统计 = 有效频道数
- `# Total channels:` 现在统计**过滤后的有效频道数**（旧版统计的是过滤前的原始
  条数，两者相差 24）。当前实测：抓取 303 → 有效 279。

### Bug 修复 / 数据清洗
- **过滤 22 条必死频道**：播放地址为 `.../224//index.m3u8`（流 ID 缺失，路径含
  空段），旧版照单全收。
- **过滤残缺 logo**：API 中 127 条 `"logo":"http"` / `"HTTP"`，现用 URL 结构校验
  （协议+主机+路径）拦截，`channelIcon` 兜底。
- **去重**：同名同地址条目只保留一条。
- **EPG 匹配率 0 → 143/279**：旧版 `tvg-id` 用 `uuid`，与 EPG 源频道 ID 完全对不
  上（实测命中 0）；现按离线规则归一化频道名（`H265-CCTV-2高清4M`→`CCTV2`、
  `兵团卫视标清4M`→`兵团卫视`、`中国国际电视台俄语频道`→`CGTN俄语` 等）。
- 修复日志格式化混用位置/命名参数导致的 `--- Logging error ---`。

### 健壮性（面向无人值守定时任务）
- 网络请求重试 + 线性退避（默认 3 次），支持 gzip 响应与 utf-8/gb18030 解码。
- `--min-channels` 安全阈值：上游返回异常少量数据时拒绝覆盖、非零退出。
- 原子写入（临时文件 + `os.replace`），强制 LF 换行。
- 兼容 API 返回 `{“data”: [...]}` 等包裹结构。

### 确定性输出（避免无意义提交的关键）
- **不含任何时间戳**：旧版 `# Last updated:` 每次运行都变，必然产生提交。
- 相同 API 数据 → 字节级一致的文件（已实测两次运行 `cmp` 相同），因此
  `git diff` 为空 ⇔ 频道列表无实质变化。

## GitHub Actions 工作流

- **触发**：`cron: '0 */6 * * *'`（UTC 00/06/12/18 = 北京时间 08/14/20/02，高峰期
  可能延迟几分钟）；支持手动 `workflow_dispatch`；脚本/工作流变更 push 后立即重跑。
- **流程**：checkout → Python 3.12 → 跑测试 → 生成 M3U → `git diff --quiet` 判断
  实质变化 → 有变化才 commit（信息含有效频道数与增删行统计，如
  `update: channels.m3u (279 channels) [+3 -3]`）→ push（带 rebase 防竞态）→
  部署 GitHub Pages。
- **防呆**：抓取失败或触发 `--min-channels` 阈值时步骤直接失败，不提交不部署；
  `concurrency` 防止两次运行交叠。

### 部署说明（二选一）
1. Pages 源设为 **main 分支根目录**：push 后 GitHub 自动重建，可删除工作流中
   `Deploy to GitHub Pages` 两步。
2. Pages 源设为 **gh-pages 分支**：保留现有 `peaceiris/actions-gh-pages` 步骤即可
   （仅在频道有实质变化时执行，避免无意义部署）。

## 注意事项

- 播放地址为运营商内网 CDN（`cdnrrs.gx.chinamobile.com`）与组播 `udp://`，需在
  广西移动网络环境内播放。
- 旧版兼容 Python 2.7 的代码已移除：Python 2 已 EOL 多年，GitHub Actions 也不
  再提供；现仅支持 Python 3.8+。
