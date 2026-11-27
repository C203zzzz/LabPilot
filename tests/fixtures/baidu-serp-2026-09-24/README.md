# 百度 SERP 实测夹具（抓取时间 2026-09-24 18:47 +08:00）

`organic-card.html` 是**一条结果卡片的逐字原文**，从下面这次真实请求的响应里切出来，没有改写、没有补标签。

| 项 | 值 |
|---|---|
| 请求 | `https://www.baidu.com/s?wd=%E5%BA%93%E8%BD%A6%E5%B8%82%E5%86%9C%E4%B8%9A%E5%86%9C%E6%9D%91%E5%B1%80+%E7%9F%A5%E6%A5%9A+%E7%94%9F%E7%89%A9%E5%AE%89%E5%85%A8%E6%9F%9C+%E4%B8%AD%E6%A0%87&rn=20` |
| 查询词 | `库车市农业农村局 知楚 生物安全柜 中标` |
| HTTP | 200，1 103 453 字节，1.36 s |
| 有机结果卡片 | **19** 条（`div.c-container.result` 且带 `mu`） |
| 其中 `.gov.cn` / `.edu.cn` | **15** 条 |
| 本文件 | 19 条里的第 1 条（`www.xjkc.gov.cn` 那篇成交公告） |

## 它证明什么

站点给有机结果包了 `/link?url=...` 跳转壳：本次响应里 `/link?url=` 出现 **115** 次，而**每一条**有机卡片的 `href` 都是这个壳（解析真实地址要每条多花一次请求）。
同时，卡片自身的 `mu` 属性就是**真实目标 URL**：19 条有机结果全部带 `mu`，无需再发请求。

`BaiduSearchProvider._parse_baidu_results` 因此读 `mu` 而不是 `href`，并且：

- 跳过 `*.baidu.com` 的自有卡片（热搜、推荐位、占位符）——它们复用同一个 `c-container` 容器，也带 `mu`；
- `mu` 不是 `http(s)` 绝对地址的一律丢弃（相对 `/link?url=` 卡片）；
- 只认同时带 `c-container` 与 `result` 两个 class 的容器（`result-op` 单类卡片不算有机结果）。

## 复现（离线，不联网）

```powershell
项目工作区.venv\Scripts\python.exe -m pytest -q tests\test_baidu_provider.py
```

其中 `test_a_verbatim_card_from_the_live_serp_yields_its_real_url` 直接读本文件，断言解析出的 URL、标题前缀与摘要来源。

## 注意

- 这是 2026-09-24 的页面。百度改版后 `mu` 可能消失，那时解析会返回空列表而不是返回壳地址——**空列表是刻意的**：把 `www.baidu.com/link?url=...` 当结果返回，会让上层按域名筛 `.gov.cn` 时永远筛不到，变成一种静默失败。真出现这种情况要重测结构，不要退回 `href`。
- 本文件用 UTF-8 读取（`errors="replace"`）后落盘，页面本身的编码就是 UTF-8。
