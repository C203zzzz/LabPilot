/**
 * 「这条链接指向哪里」的中文简述：站点名 + 页面类型，替代把 URL 印给用户。
 *
 * 约定：
 *   - href 仍然是完整地址，链接照样能点开；
 *   - 可见文字和悬停提示里都不出现 http(s)://，也不出现英文域名；
 *   - 认不出来的域名给中性说法「来源类别（域名）」，绝不回落到打印完整 URL。
 *
 * 站点名取自各站首页的 SiteName 或 <title>，逐条核对过，不凭猜测；
 * 无法确认的站点不登记，走兜底说法。
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
 * 一类，产品里本来就有，不另造）→「外部页面（域名）」。
 */
export function siteNameOf(url) {
  const host = hostOf(url)
  if (!host) return '来源页面'
  const match = SITE_NAMES.find(([pattern]) => pattern.test(host))
  if (match) return match[1]
  const category = hostLabel(url)
  return `${category || '外部页面'}（${host}）`
}

/** 「站点名 · 页面类型」，例如「中国政府采购网 · 中标公告」；页面类型省略时只给站点名。 */
export function sourceSummary(url, page = '') {
  const name = siteNameOf(url)
  return page ? `${name} · ${page}` : name
}
