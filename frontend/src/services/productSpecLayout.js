/**
 * 厂家产品页正文 → 「模块化网格」能吃的结构。
 *
 * 背景：``configuration`` 这一段是后端从厂家产品页正文里截下来的（网窗口
 * 2400 字，见 ``backend/services/product_configuration.py``），它不是干净的参数表，
 * 而是**整页正文**：前半段是参数，中间是同一张表按行展开重复一遍，尾部混进了
 * 页面导航（留言板 / 发送）、相关推荐（十几个别的产品名 + 裸 URL）。原来页面用
 * ``<pre>`` 原样铺出来，用户看到的就是一堆 ``键 | 值`` 加上两屏网页垃圾。
 *
 * 后端会去源头清洗这些噪声，但历史记录改不掉了，所以这一层必须自己扛：
 * 解析不出「键 | 值」的行不原样铺开，逐字重复的键值对只留一条。目标是"即使拿到
 * 的还是脏文本，用户看到的也是干净、能对齐的网格"。
 *
 * 认得出四类行（输入是真实排版的参数表原文）：
 *   1. 分组标题      ``技术参数`` / ``转子参数``——后面跟着结构化行，自己没有分隔符；
 *   2. 键值对行      ``最高转速 | 4500r/min``；
 *   3. 多列表格块    ``转子名称 | 转子号 | 离心力(xg) | 离心时间(s) | 用途`` 连着 6 行数据；
 *   4. 表格展开行    ``[表格按列展开 行3列2] XKA-2200 最高转速 4500r/min``——第 1 格是型号，
 *                    后面是「键 值」，与第 2 类逐字重复，靠已知键切回去做去重。
 *
 * 丢掉的东西一律记在 ``discarded`` 里，测试拿它证明"丢的是导航和重复行，不是参数"。
 */

const PIPE = /\s*[|｜]\s*/
const EXPANDED_ROW = /^\[表格按列展开\s*行(\d+)列(\d+)\]\s*([\s\S]*)$/
const URL_ONLY = /^[[(【]?\s*(?:https?:\/\/|www\.)\S*[\])】]?$/i

//: 厂家页正文里带进来的导航与推荐区锚点。命中任何一条，**它和它后面的内容**都不是
//: 这台机器的参数：页面尾部整块是「相关推荐 + 产品名 + 裸链接」。整段截断而不是逐行
//: 过滤，是因为产品名（``HT120高速台式离心机``）本身长得像参数行，只有"在推荐区里"
//: 这个位置信息才能把它和真参数区分开。
const TRAILING_NAV = [
  /^留言板$/, /^在线留言$/, /^留言$/, /^发送$/, /^提交$/, /^评论$/,
  /^相关推荐$/, /^热门推荐$/, /^推荐产品$/, /^相关产品$/, /^猜你喜欢$/,
  /^产品中心$/, /^产品分类$/, /^联系我们$/, /^返回列表$/, /^上一篇/, /^下一篇/, /^分享到/,
]

//: 单行噪声：就算没有命中整段截断（例如它出现在参数中间），也不当参数渲染。
const NOISE_WORD = /^(留言板|在线留言|发送|提交|评论|相关推荐|热门推荐|推荐产品|相关产品|猜你喜欢|产品中心|产品分类|联系我们|返回列表|上一条|下一条|分享|收藏|打印|关闭|首页|导航)$/

const NOTE_LINE = /^\*|^(注[:：]|备注[:：]|说明[:：])/

/** 比较用的归一化：厂家页里同一个词常差一个空格（``发 送``），不算两条。 */
const compact = value => String(value == null ? '' : value).replace(/\s+/g, '')

const sameText = (left, right) => compact(left) === compact(right)

/** 从 ``index`` 开始的第一个非空行，是不是结构化参数行的开头。 */
function structuredAt(lines, index) {
  for (let at = index; at < lines.length; at += 1) {
    const line = lines[at]
    if (!line) continue
    if (EXPANDED_ROW.test(line)) return true
    const cells = line.split(PIPE)
    if (cells.length === 2 && cells[0].trim() && cells[1].trim()) return true
    // 多列行只有在**后面还有同样列数的行**时才是表格（表头 + 数据），
    // 单独一行的多列表头没有值可对齐，不算结构化行。
    if (cells.length >= 3 && lines[at + 1] && lines[at + 1].split(PIPE).length === cells.length) return true
    return false
  }
  return false
}

/**
 * 分组标题：短、不带分隔符、不像句子，并且**后面真的跟着参数行**。
 * 「有参数跟着」这一条把 ``技术参数`` 和尾部产品名（``HT200高速台式离心机``）分开。
 */
function isGroupTitle(line, lines, index) {
  if (!line || line.length > 24) return false
  if (PIPE.test(line)) return false
  if (NOTE_LINE.test(line)) return false
  if (/[。；;，,]$/.test(line)) return false
  return structuredAt(lines, index)
}

/**
 * 表格展开行（``[表格按列展开 行3列2] XKA-2200 最高转速 4500r/min``）切回「键 / 值」。
 *
 * 不能按空格切：值的内部就有空格（``AC220V ±22V 50/60Hz``、``最低 ≤58dB(A)``）。
 * 用**上面已经解析出来的键**去原文里定位：命中的位置之后就是值，之前是型号等前缀。
 * 取位置最靠前的键，位置相同取最长的那个，避免短键名吃掉长键名。
 */
function splitByKnownKey(remainder, keys) {
  let best = null
  for (const key of new Set(keys)) {
    const at = remainder.indexOf(key)
    if (at < 0) continue
    if (!best || at < best.at || (at === best.at && key.length > best.key.length)) best = { key, at }
  }
  if (!best) return null
  const value = remainder.slice(best.at + best.key.length).trim()
  return value ? { key: best.key, value } : null
}

/**
 * 哪几列是**纯数值列**（供页面右对齐 + 等宽数字用）。
 *
 * 多列表格原来列与列之间没有边界：``淋巴球清洗用转子/HLA`` 后面紧跟着 ``1``，读起来
 * 就是 ``HLA1``——实际是两个字段（转子名称、转子号）。间隔由 CSS 给，但"数值列右对齐"
 * 这件事只能由数据决定：表头里的 ``离心力(xg)`` 带单位，值却是 ``2000xg``，靠表头猜
 * 会猜错。所以判据是**这一列每个非空格子都长得像"数字＋单位符号"**（``1``、``2000xg``、
 * ``180s``、``±30r/min``），中文列（``转子名称``、``用途``）自然落选。
 */
const NUMERIC_CELL = /^[±+\-−]?\s*\d[\d.,]*(?:\s*[a-zA-Z%/°·×]+)*$/

function numericColumns(table) {
  return table.headers.map((_, column) => {
    const cells = table.rows.map(row => String(row[column] == null ? '' : row[column]).trim()).filter(Boolean)
    return cells.length > 0 && cells.every(cell => NUMERIC_CELL.test(cell))
  })
}

/**
 * @param {string} text 厂家资料里的 ``configuration`` 原文（可能很脏）。
 * @returns {{groups: Array, notes: string[], discarded: string[], stats: object}}
 *   ``groups[].rows`` 是键值对，``groups[].table`` 是多列表格（``numeric`` 逐列标出
 *   是不是纯数值列）；``discarded`` 是被判定为导航区或逐字重复而没有渲染的原文行。
 */
export function productSpecLayout(text) {
  const source = String(text == null ? '' : text).replace(/\r\n?/g, '\n').split('\n').map(line => line.trim())
  const discarded = []
  const notes = []
  const groups = []
  const expanded = []
  let current = null

  const ensureGroup = () => {
    if (!current) {
      current = { title: '', rows: [], table: null }
      groups.push(current)
    }
    return current
  }
  /** 逐字重复（键和值都一样）的条目只留一条；返回值不同的同名键是真实信息，两者都留。 */
  const addRow = (target, key, value) => {
    if (target.rows.some(row => sameText(row.key, key) && sameText(row.value, value))) return false
    target.rows.push({ key, value })
    return true
  }

  const nav = source.findIndex(line => line && TRAILING_NAV.some(pattern => pattern.test(compact(line))))
  const lines = nav >= 0 ? source.slice(0, nav) : source
  if (nav >= 0) discarded.push(...source.slice(nav).filter(Boolean))

  let index = 0
  while (index < lines.length) {
    const line = lines[index]
    index += 1
    if (!line) continue
    if (URL_ONLY.test(line) || NOISE_WORD.test(compact(line))) { discarded.push(line); continue }

    const table = line.match(EXPANDED_ROW)
    if (table) { expanded.push({ line, columns: Number(table[2]), remainder: table[3].trim() }); continue }

    const cells = line.split(PIPE).map(cell => cell.trim())
    if (cells.length >= 3) {
      // 连续同样列数的行合起来才是一张表：第一行是表头。
      const block = [cells]
      while (index < lines.length && lines[index] && lines[index].split(PIPE).length === cells.length) {
        block.push(lines[index].split(PIPE).map(cell => cell.trim()))
        index += 1
      }
      if (block.length < 2) { discarded.push(line); continue }
      const target = ensureGroup()
      const rows = block.slice(1).filter(row => !(target.table || { rows: [] }).rows.some(existing => existing.every((cell, at) => sameText(cell, row[at]))))
      target.table = { headers: block[0], rows: [...((target.table || {}).rows || []), ...rows] }
      continue
    }
    if (cells.length === 2 && cells[0] && cells[1]) {
      if (!addRow(ensureGroup(), cells[0], cells[1])) discarded.push(line)
      continue
    }
    if (isGroupTitle(line, lines, index)) {
      current = { title: line, rows: [], table: null }
      groups.push(current)
      continue
    }
    if (NOTE_LINE.test(line)) { notes.push(line); continue }
    discarded.push(line)
  }

  // 展开行放在最后处理：这时"已知键"已经是全文的键，命中就说明它和上面某一条是同一份数据。
  const knownKeys = groups.flatMap(group => group.rows.map(row => row.key))
  for (const item of expanded) {
    if (item.columns === 2) {
      const split = splitByKnownKey(item.remainder, knownKeys)
      const owner = split && groups.find(group => group.rows.some(row => row.key === split.key))
      if (!split || !owner || !addRow(owner, split.key, split.value)) { discarded.push(item.line); continue }
      continue
    }
    // 多列表格展开行：段数对上当前表头才算数，否则不猜。
    const owner = groups.filter(group => group.table).pop()
    const cells = item.remainder.split(PIPE).map(cell => cell.trim())
    if (!owner || cells.length !== owner.table.headers.length) { discarded.push(item.line); continue }
    owner.table.rows.push(cells)
  }

  // 数值列的判断放在最后：多列表格还会被上面的「表格按列展开」行追加数据行，
  // 拿追加前的数据判列会漏掉后到的那几行。
  const rendered = groups
    .filter(group => group.title || group.rows.length || (group.table && group.table.rows.length))
    .map(group => (group.table ? {...group, table: {...group.table, numeric: numericColumns(group.table)}} : group))
  return {
    groups: rendered,
    notes,
    discarded,
    stats: {
      parameters: rendered.reduce((sum, group) => sum + group.rows.length, 0),
      tableRows: rendered.reduce((sum, group) => sum + ((group.table || {}).rows || []).length, 0),
      tableColumns: rendered.reduce((max, group) => Math.max(max, ((group.table || {}).headers || []).length), 0),
      notes: notes.length,
      discarded: discarded.length,
    },
  }
}
