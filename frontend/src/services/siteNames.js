/**
 * 「这条链接指向哪里」的中文简述：站点名 + 页面类型，替代把 URL 印给用户。
 *
 * 业务口径：「不要把整体链接展示出来 而是简述链接指向哪里 不要直接在前端展示
 * http ***** 这种字样」。所以：
 *   - ``href`` 仍然是完整地址，链接照样能点开；
 *   - 可见文字和悬停提示（``title``）里都不出现 ``http(s)://``，也不出现英文域名；
 *   - 认不出来的域名给中性中文说法「外部网页」，**绝不回落到打印 URL 或英文域名**。
 *
 * 站点名一条都不猜——下面每一条都写了出处，免得以后被当成"随手加的域名表"。
 * 域名取自产品自己的产物（``data/app.sqlite3`` 与 ``output/`` 下 2002 个 json 里
 * ``sources[].url``、``prices[].url``、``configuration_url`` 的实际 host 统计）：
 * 价格记录（``kind`` 是 award/contract）出现过的 host 只有 10 个，配置来源只有 2 个，
 * 表里覆盖的就是这些高频项加上快照里能确认的省级平台。
 *
 * 出处逐条（``项目工作区`` 下的一手产物，非记忆）：
 *   - ``ccgp.gov.cn``：抓取快照 ``output/ccgp-…/ccgp-home.html`` 等的
 *     ``<meta name="SiteName" content="中国政府采购网">``；
 *   - ``zfcg.czt.zj.gov.cn``：快照 ``ccgp-zj.html``／``zj-zfcg-home.html`` 的 SiteName
 *     「浙江政府采购网」；
 *   - ``ggzyjy.sc.gov.cn``：首页 ``<title>`` 与 SiteName「四川省公共资源交易信息网」；
 *   - ``ccgp-hubei.gov.cn``：首页 ``<title>``「中国湖北政府采购网」；
 *   - ``ccgp-shaanxi.gov.cn``：首页 ``<title>``「陕西省政府采购网」；
 *   - ``zbcg.imust.edu.cn``：首页页头「内蒙古科技大学采购与招标中心」；
 *   - ``cgzx.hust.edu.cn``：首页 ``<title>``「华中科技大学采购与招标中心-首页」；
 *   - ``imust.edu.cn``：首页 ``<title>``「内蒙古科技大学」；
 *   - ``nxu.edu.cn``：首页 ``<title>``「宁夏大学」；
 *   - ``qqhru.edu.cn``：首页 ``<title>``「齐齐哈尔大学」；
 *   - ``swu.edu.cn``：首页 ``<title>``「西南大学」；
 *   - ``yiqi.com``：快照 ``<title>`` 结尾「仪器网」；
 *   - ``instrument.com.cn``：快照 ``<title>`` 结尾「仪器信息网」；
 *   - ``antpedia.com``：快照 ``<title>``「…分析测试百科网」；
 *   - ``xiangyilxj.com``：首页 ``<title>`` 结尾「湘仪集团」；
 *   - ``120shop.cn``：首页 ``<title>``「医价网…」；
 *   - ``qianlima.com``：首页 ``<title>``「千里马招标网…」。
 *
 * 没验证到、因而**没有**登记的：``ccgp-neimenggu.gov.cn``（探测返回 410 Gone）、
 * ``www.fsmby.com.cn``（探测拿不到 ``<title>``）、``ecaigou.swu.edu.cn``（401，
 * 但它落在已登记的 ``swu.edu.cn`` 下，兜底仍是「西南大学」）。这些走兜底说法。
 */
import { hostLabel, hostOf } from './pricePresentation.js'

//: 更具体的子域必须排在它所属的宽泛域名前面（``zbcg.imust.edu.cn`` 先于
//: ``imust.edu.cn``），否则采购中心的页面会被说成学校官网。
export const SITE_NAMES = [
  [/(^|\.)zbcg\.imust\.edu\.cn$/, '内蒙古科技大学采购与招标中心'],
  [/(^|\.)cgzx\.hust\.edu\.cn$/, '华中科技大学采购与招标中心'],
  [/(^|\.)imust\.edu\.cn$/, '内蒙古科技大学'],
  [/(^|\.)hust\.edu\.cn$/, '华中科技大学'],
  [/(^|\.)nxu\.edu\.cn$/, '宁夏大学'],
  [/(^|\.)qqhru\.edu\.cn$/, '齐齐哈尔大学'],
  [/(^|\.)swu\.edu\.cn$/, '西南大学'],
  [/(^|\.)zfcg\.czt\.zj\.gov\.cn$/, '浙江政府采购网'],
  [/(^|\.)ccgp-zhejiang\.gov\.cn$/, '浙江政府采购网'],
  [/(^|\.)ccgp-hubei\.gov\.cn$/, '中国湖北政府采购网'],
  [/(^|\.)ccgp-shaanxi\.gov\.cn$/, '陕西省政府采购网'],
  [/(^|\.)ccgp\.gov\.cn$/, '中国政府采购网'],
  [/(^|\.)ggzyjy\.sc\.gov\.cn$/, '四川省公共资源交易信息网'],
  [/(^|\.)yiqi\.com$/, '仪器网'],
  [/(^|\.)instrument\.com\.cn$/, '仪器信息网'],
  [/(^|\.)antpedia\.com$/, '分析测试百科网'],
  [/(^|\.)xiangyilxj\.com$/, '湘仪集团官网'],
  [/(^|\.)120shop\.cn$/, '医价网'],
  [/(^|\.)qianlima\.com$/, '千里马招标网'],
]

/**
 * 这个地址属于哪个站点；认不出来时返回中性说法，**不返回 URL、也不返回域名**。
 *
 * 兜底顺序：已登记的站点名 → ``pricePresentation`` 那张来源类别表（「招标信息平台」
 * 一类，产品里本来就有，不另造）→「外部网页」。
 */
export function siteNameOf(url) {
  const host = hostOf(url)
  if (!host) return '来源页面'
  const match = SITE_NAMES.find(([pattern]) => pattern.test(host))
  if (match) return match[1]
  const category = hostLabel(url)
  return category || '外部网页'
}

/** 「站点名 · 页面类型」，例如「中国政府采购网 · 中标公告」；页面类型省略时只给站点名。 */
export function sourceSummary(url, page = '') {
  const name = siteNameOf(url)
  return page ? `${name} · ${page}` : name
}
