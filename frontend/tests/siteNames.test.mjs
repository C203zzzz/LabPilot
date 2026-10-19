/**
 * 「这条链接指向哪里」——站点名简述。
 *
 * 用户口径：「不要把整体链接展示出来 而是简述链接指向哪里 不要直接在前端展示
 * http ***** 这种字样」。所以链接文字与悬停提示都要落到中文站点名，
 * 认不出来的域名给中性说法，**任何情况下都不能回落到打印完整 URL**。
 *
 * 表里登记的域名与站点名来自产品自己的一手产物（见 ``services/siteNames.js``
 * 头部逐条出处），不是凭记忆写的；这份测试同时钉住兜底行为。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {SITE_NAMES, siteNameOf, sourceSummary} from '../src/services/siteNames.js'

test('登记的域名给出中文站点名（含子域）', () => {
  assert.equal(siteNameOf('http://www.ccgp.gov.cn/cggg/dfgg/zbgg/202603/t20260301_10000001.htm'), '中国政府采购网')
  assert.equal(siteNameOf('http://download.ccgp.gov.cn/oss/download?uuid=x'), '中国政府采购网')
  assert.equal(siteNameOf('https://www.xiangyilxj.com/product/info_itemid_1001.html'), '湘仪集团官网')
  assert.equal(siteNameOf('https://www.yiqi.com/product/detail_1000004.html'), '仪器网')
  assert.equal(siteNameOf('https://ggzyjy.sc.gov.cn/a/b.html'), '四川省公共资源交易信息网')
  assert.equal(siteNameOf('https://ccgp-hubei.gov.cn/a.html'), '中国湖北政府采购网')
  assert.equal(siteNameOf('http://zfcg.czt.zj.gov.cn/luban/category'), '浙江政府采购网')
})

test('更具体的子域优先于它所属的宽泛域名', () => {
  // 采购中心的页面不能说成"学校官网"，反过来也一样。
  assert.equal(siteNameOf('https://zbcg.imust.edu.cn/info/1041/4479.htm'), '内蒙古科技大学采购与招标中心')
  assert.equal(siteNameOf('https://www.imust.edu.cn/'), '内蒙古科技大学')
  assert.equal(siteNameOf('http://cgzx.hust.edu.cn/'), '华中科技大学采购与招标中心')
  assert.equal(siteNameOf('https://emall.hust.edu.cn/'), '华中科技大学')
})

test('认不出来的域名给中性说法，不打印 URL', () => {
  const unknown = siteNameOf('https://www.example-shop.com.cn/product/1')
  assert.ok(!/https?:\/\//.test(unknown), unknown)
  assert.ok(!unknown.includes('/'), '兜底说法里带了路径：' + unknown)
  assert.ok(unknown.includes('www.example-shop.com.cn'), unknown)
  // 产品里已有的来源类别表也复用上了：招标信息平台的域名不必逐个登记。
  assert.ok(siteNameOf('https://www.unknown-bidding.com/x').length > 0)
  assert.equal(siteNameOf('https://www.qianlima.com/zbxx/1.html'), '千里马招标网')
})

test('空值与非法值不产生异常，也不产生 URL', () => {
  for (const value of ['', null, undefined, 'not-a-url', 'javascript:alert(1)']) {
    const name = siteNameOf(value)
    assert.ok(name.length > 0, '兜底说法为空')
    assert.ok(!/https?:\/\//.test(name), name)
  }
  assert.equal(siteNameOf(''), '来源页面')
})

test('sourceSummary 拼出「站点名 · 页面类型」', () => {
  assert.equal(sourceSummary('http://www.ccgp.gov.cn/a.htm', '中标公告'), '中国政府采购网 · 中标公告')
  assert.equal(sourceSummary('https://www.xiangyilxj.com/x.html', '产品页'), '湘仪集团官网 · 产品页')
  assert.equal(sourceSummary('http://www.ccgp.gov.cn/a.htm'), '中国政府采购网')
})

test('表里没有重复的站点名写法，也没有会被前一条吃掉的顺序问题', () => {
  // 顺序错了会让"华中科技大学采购与招标中心"永远命中不到（被 hust.edu.cn 先吃掉），
  // 这里逐条按表本身的顺序验证一遍：第一条命中的就是期望的那条。
  for (const [pattern, name] of SITE_NAMES) {
    const host = pattern.source.replace(/^\(\^\|\\\.\)/, '').replace(/\\\./g, '.').replace(/\$$/, '')
    assert.equal(siteNameOf(`https://${host}/`), name, `${host} 命中的不是它自己那条`)
  }
})
