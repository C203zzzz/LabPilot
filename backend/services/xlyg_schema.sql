-- 小零易购（gdegp.gds.edu.cn:8093）成交明细本地库
--
-- 这个文件是**唯一**的建表来源：``backend/services/xlyg_local_db.py`` 的
-- ``connect()`` 直接读它执行，测试与一键重建脚本也读同一份。
--
-- 库里只放**公开的成交事实**：品牌、型号、数量、成交单价、成交金额、成交日期、
-- 采购单位、供货单位、订单号、详情页 URL、原文摘录。
-- **个人信息（联系人／电话／地址／收货人／开票信息）一格都不进这个库**——
-- 落盘前先跑产品的脱敏钩子，再跑 ``xlyg_local_db.pii_residue`` 闸门。

CREATE TABLE IF NOT EXISTS xlyg_order (
    order_no        TEXT PRIMARY KEY,   -- 列表页「订单编号」列，DD/CM 打头
    bill_guid       TEXT NOT NULL,      -- 列表行 code 属性，详情页 URL 的参数
    code_type       TEXT,               -- 列表行 codeType：0/1=订单，3/4=反拍竞价
    code2           TEXT,               -- 列表行 code2：0=物品，1=服务（服务类不抓）
    template        TEXT,               -- 详情页模板文件名
    order_mode      TEXT,               -- 页面面包屑：直购／议价／竞价／反拍
    project_name    TEXT,               -- 列表页「项目名称」
    purchaser       TEXT,               -- 列表页「采购单位」（= 采购人）
    supplier        TEXT,               -- 列表页「成交单位」
    supplier_detail TEXT,               -- 详情页「供应商／中标供应商」
    state           TEXT,               -- 列表页「状态」列原文
    deal_date       TEXT,               -- 成交日期 YYYY-MM-DD（列表页「成交时间」）
    deal_time       TEXT,               -- 成交时间 YYYY-MM-DD HH:MM:SS
    award_total     REAL,               -- 页面自己印的整单成交金额
    detail_url      TEXT,
    source_keywords TEXT,               -- 搜到这一单的品类词，逗号分隔
    fetch_status    TEXT,               -- ok / no_price / no_table / skipped / error …
    skip_reason     TEXT
);

CREATE INDEX IF NOT EXISTS ix_xlyg_order_date ON xlyg_order(deal_date);
CREATE INDEX IF NOT EXISTS ix_xlyg_order_status ON xlyg_order(fetch_status);

CREATE TABLE IF NOT EXISTS xlyg_item (
    order_no       TEXT NOT NULL,       -- 一条商品行 = 一条成交明细
    line_no        INTEGER NOT NULL,
    goods_name     TEXT,                -- 页面「商品名称」
    product_name   TEXT,                -- 页面「产品名称」（品目）
    brand          TEXT,                -- 页面「品牌」
    model          TEXT,                -- 页面「型号规格」，**完整型号**
    quantity       REAL,                -- 页面「数量」
    unit_price     REAL,                -- 成交单价（元）
    amount         REAL,                -- unit_price × quantity
    price_column   TEXT,                -- 单价读自哪一列：协议价格（元）／金额（元）／中标价
    price_kind     TEXT,                -- negotiated / list_price_direct / auction_award
    category       TEXT,                -- 仪器类别
    category_source TEXT,               -- item_text（按商品文字判） / probe_keyword（按检索词）
    deal_date      TEXT,
    purchaser      TEXT,
    supplier       TEXT,
    detail_url     TEXT,
    excerpt        TEXT,                -- 原文摘录：页面商品表那一行的逐字内容
    table_header   TEXT,                -- 该表印着的列名（让摘录能对着列读）
    price_excerpt  TEXT,                -- 送进 validate_price 的那一行
    verified       INTEGER NOT NULL DEFAULT 0,
    verify_reasons TEXT,                -- 没过闸门的原因，; 分隔
    source_keywords TEXT,
    order_total_consistent INTEGER,     -- Σ单价×数量 与页面整单金额自洽
    validate_price INTEGER NOT NULL DEFAULT 0,  -- 产品 validate_price 是否收下
    validate_note  TEXT,                -- 被哪一条判据拒的
    PRIMARY KEY (order_no, line_no)
);

CREATE INDEX IF NOT EXISTS ix_xlyg_item_model ON xlyg_item(model);
CREATE INDEX IF NOT EXISTS ix_xlyg_item_brand ON xlyg_item(brand);
CREATE INDEX IF NOT EXISTS ix_xlyg_item_category ON xlyg_item(category);
CREATE INDEX IF NOT EXISTS ix_xlyg_item_date ON xlyg_item(deal_date);

-- 缺口：每一个"没抓成"或"抓了但认不出"的单都记一条，绝不静默丢。
CREATE TABLE IF NOT EXISTS xlyg_gap (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT,
    kind     TEXT,                      -- unknown_template / no_table / no_price / 抓取失败种类
    detail   TEXT
);

CREATE TABLE IF NOT EXISTS xlyg_meta (
    key   TEXT PRIMARY KEY,             -- window_start / window_end / built_at / keywords / funnel
    value TEXT
);
