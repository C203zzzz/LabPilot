# CCGP 正文索引实测夹具（抓取时间 2026-09-23 12:04–12:44 +08:00）

这些文件是 `设计说明` 里每一个数字的**原始响应**。它们只被保存、被离线重读，没有在离线校验里发过任何请求。

站点：`http://search.ccgp.gov.cn/bxsearch`（列表页）与 `http://www.ccgp.gov.cn/cggg/...`（详情页）。
抓取参数与节流记录见 `requests.jsonl` 与 `jobs/`。

## 先看哪三个文件

| 文件 | 它证明什么 |
|---|---|
| `list-bio-searchtype2.html` | 站点自己的权威总数：`共找到 <span>1526</span> 条内容`，模式词 `全文检索` |
| `list-bio-searchtype1.html` | 同一关键词的标题模式：`标题检索`、`178` 条 —— 两套模式的分界 |
| `requests.jsonl` | 186 次请求的完整原始记录（URL／时间／字节／共找到／页数／窗口／行／高亮／错误） |

## 字段在哪（别把行数当总数）

- **权威总数**：`共找到 <span style="color:#c00000">N</span> 条内容`（记录数）。同一段文字还给出 `全文检索`/`标题检索` 和 `查询日期从 X 到 Y`。
- **页数**：`Pager({ size: N, current: M })` 里的 `size` 是**页数**，每页 20 行。`list-bio-searchtype2.html` 是 77 页 ≈ 1526 条。
- **命中词字面**：行内 `<font color=red>…</font>`，是**该行正文里出现的关键词字面**，不是索引词边界。
- 结果行容器：`<ul class="vT-srch-result-list-bid">` 下的 `<li>`；行内 `<strong>` 是公告类型/品目，`<span>` 里含发布时间、采购人、代理机构、省份。

## 型号分词（每个查询都有对应文件）

`86L578S`=2、`86L578`=1（命中**另一篇**公告的 `MD-86L578`）、`578S`=0、`DW-86L578S`=2、`DW 86L578S`=2、
`DW-86`=0、`DW-86L578`=0、`DW86L578S`=0、`DW 578S`=0、`TBSPX`=1、`TBSPX-150BIII`=0、
`TBSPX 150BIII`=0、`150BIII`=0、`BIII`=2、`TBSPX BIII`=0、`TBSPX 150`=0、`TB101`=1、`TB101-2DB`=0、
`TBSYG-A2-6`=0、`TBMF-7-12P`=0、`ZQLY-180`=1、`ZQLY-18`=0、`ZQLY`=124、`DW`=920。

详情页字面：`detail-guangdong-qinggong-TBSPX-150BIII.html` 里有 `<td>TBSPX-150BIII</td>`（ASCII 0x2D），
`detail-zhujiang-DW-86L578S.html` 里有 `<td>DW-86L578S</td>`，`detail-chengde-MD-86L578.html` 里有 `MD-86L578`。

## 覆盖与时间窗

- `list-bio-bidSort1-central.html`=208 与 `list-bio-bidSort2-local.html`=1318，两者与未过滤的 1526 精确相等。
- 12 个公告类型（`bidType=1..12`）的计数相加 = 1526；其中资格预审/邀请公告对 `生物安全柜` 为 0，
  用 `采购` 复测得到 161 / 61（`list-caigou-bidType5-prequal.html`、`list-caigou-bidType6-invite.html`）。
- 时间边界：`list-bio-window-366d.html`=1531（如实）、`list-bio-window-370d.html`=921（静默变小）、
  `list-bio-window-730d-zero.html`=0、`list-bio-window-2023-09-to-2024-09.html`=1333（结束日期正好 730 天前，如实）、
  `list-bio-window-2022-09-rejected.html`=0 且窗口被回显成 `2026-09-23..2023-09-23`、
  `list-bio-window-2020H1-clamped.html` / `-2021-clamped` / `-2019-clamped` 的窗口都被折叠成 `2026-09-23..2026-09-23`。
- 宽窗掉命中的直接证据：`86L578S` 在 365 天窗下 2 条（`list-86L578S.html`），在 400 天窗下 1 条
  （见 `requests.jsonl` 里 `w8_loss_check_86L578S`）。

## 附件

- `list-*.html`（43 个列表页）里 **一个附件链接都没有**。
- 详情页三种形态：`detail-guangdong-qinggong-*.html`（4 个 `bizDownload` 空 href + 4 条 gdgpo 直链）、
  `detail-zhujiang-*.html`（5 + 5，含 `报价明细附件.zip`）、`detail-chengde-*.html`（1 条直链，无 `bizDownload`）、
  `detail-zygg-central-empty-href.html`（2 个 `bizDownload` 空 href，**页面里没有任何直链**）。
- `js-detailaddon-attachment-url-template.js`（从 `http://www.ccgp.gov.cn/js/detailaddon.js` 抓的 130 KB 脚本）里有把空
  href 还原成下载地址的代码：`href = "//download.ccgp.gov.cn/oss/download?uuid=" + id`。

## 复现

离线（不联网，逐项对照文档里的数字，不一致就 FAIL）：

```powershell
项目工作区.venv\Scripts\python.exe scripts\ccgp_verify_fixtures.py
```

联网重测（**会向站点发请求**，必须带节流；`jobs/` 里的文件可直接当 `--job` 用）：

```powershell
项目工作区.venv\Scripts\python.exe scripts\ccgp_probe.py `
  --out tmp/ccgp --job tests/fixtures/ccgp-index-study-2026-09-23/jobs/job3.jsonl `
  --sleep 9 --batch 12 --batch-pause 240 --retry 1
```

> `jobs/job_burst.jsonl` 是 40 个**故意连发**的请求（`--sleep 0.7 --batch 0`），实测会在第 31 次触发站点的
> 「频繁访问」页。不要顺手重跑它；它存在只是为了说明那个阈值是怎么测出来的。
