"""
semantic_layer.py —— 语义层：域 → 表 → 指标三层元数据 + 反向匹配

对应 L3 学的：
  - 三层索引：Domain / Table / Metric
  - 反向匹配：用户问题 → 关键词 → 命中哪层 → 选出相关表/指标
  - "不用向量召回"：纯关键词/元数据匹配

数据基于 dbt jaffle-shop 真实数据（六张表：订单/明细/客户/商品/门店/供应）。
域设计对齐 dbt 的模型分层：交易域（订单+明细）、客户域、商品域、门店域。
"""
from dataclasses import dataclass, field


# ─── 数据结构 ────────────────────────────────────────────────

@dataclass
class Domain:
    """数据域：业务领域的抽象（如"交易域"）"""
    key: str          # 机器名：trade
    name: str         # 中文名：交易域
    description: str  # 描述：订单、支付相关数据
    keywords: list[str]  # 匹配关键词：用户问题命中任一即认为相关


@dataclass
class Table:
    """物理表：数据库里真实存在的表"""
    name: str              # 表名：orders
    description: str       # 业务描述：订单表
    domain_key: str        # 属于哪个域
    columns: list[str]     # 列名
    is_visible: bool       # 是否对用户可见（门禁 1）
    keywords: list[str]    # 匹配关键词（独立维护，不靠描述分词）


@dataclass
class Metric:
    """指标口径：业务定义的计算逻辑（如"销售额"）"""
    key: str            # 机器名：sales
    name: str           # 中文名：销售额
    description: str    # 描述：所有订单的总金额
    table: str          # 来自哪张表
    sql_expression: str # 计算表达式：SUM(order_total)
    time_field: str     # 时间字段：ordered_at
    filters: str        # 过滤条件（jaffle 无状态，留空）
    aliases: list[str] = field(default_factory=list)  # 别名：用户可能说的其他说法


# ─── 元数据（对应项目的 domain_info / table_info / metric_info）───

DOMAINS = [
    Domain(key="trade",    name="交易域", description="订单、金额、销售相关数据",
           keywords=["订单", "金额", "销售", "收入", "交易", "卖"]),
    Domain(key="customer", name="客户域", description="客户、用户相关信息",
           keywords=["客户", "用户", "买家", "会员"]),
    Domain(key="goods",    name="商品域", description="商品、品类、价格相关信息",
           keywords=["商品", "品类", "价格", "产品", "SKU", "sku"]),
    Domain(key="store",    name="门店域", description="门店、店铺相关信息",
           keywords=["门店", "店铺", "店"]),
]

TABLES = [
    Table(
        name="orders",
        description="订单表，记录每一笔交易（客户、时间、门店、金额）",
        domain_key="trade",
        columns=["id", "customer_id", "ordered_at", "store_id",
                 "subtotal", "tax_paid", "order_total"],
        is_visible=True,
        keywords=["订单", "销售额", "销售", "金额", "收入", "营收", "客单价", "交易"],
    ),
    Table(
        name="items",
        description="订单明细表，记录每个订单买了哪些商品（SKU）",
        domain_key="trade",
        columns=["id", "order_id", "sku"],
        is_visible=True,
        keywords=["明细", "买了", "sku", "SKU", "商品", "销量", "卖"],
    ),
    Table(
        name="customers",
        description="客户表，记录客户姓名",
        domain_key="customer",
        columns=["id", "name"],
        is_visible=True,
        keywords=["客户", "用户", "买家", "顾客"],
    ),
    Table(
        name="products",
        description="商品表，记录商品（SKU、品类、单价）",
        domain_key="goods",
        columns=["sku", "name", "type", "price", "description"],
        is_visible=True,
        keywords=["商品", "品类", "价格", "多少钱", "产品", "sku", "SKU"],
    ),
    Table(
        name="stores",
        description="门店表，记录门店（开业时间、税率）",
        domain_key="store",
        columns=["id", "name", "opened_at", "tax_rate"],
        is_visible=True,
        keywords=["门店", "店铺", "店"],
    ),
    Table(
        name="supplies",
        description="供应品表，记录食材等供应品",
        domain_key="goods",
        columns=["id", "name", "cost", "perishable", "sku"],
        is_visible=False,  # 门禁 1：供应品对用户隐藏（内部数据）
        keywords=["供应", "食材", "成本"],
    ),
]

METRICS = {
    "sales": Metric(
        key="sales",
        name="销售额",
        description="所有订单的总金额（订单总额 order_total 之和，含税）",
        table="orders",
        sql_expression="SUM(order_total)",
        time_field="ordered_at",
        filters="",
        aliases=["营收", "收入", "营业额", "GMV", "卖了多少钱", "成交额"],
    ),
    "order_count": Metric(
        key="order_count",
        name="订单量",
        description="订单的笔数",
        table="orders",
        sql_expression="COUNT(*)",
        time_field="ordered_at",
        filters="",
        aliases=["订单数", "多少订单", "单量", "下单数"],
    ),
    "avg_order_value": Metric(
        key="avg_order_value",
        name="客单价",
        description="每笔订单的平均金额（销售额 ÷ 订单量）",
        table="orders",
        sql_expression="SUM(order_total) / COUNT(*)",
        time_field="ordered_at",
        filters="",
        aliases=["平均客单价", "每单金额", "平均每单多少钱"],
    ),
    "active_customer_count": Metric(
        key="active_customer_count",
        name="活跃客户数",
        description="下过订单的去重客户数",
        table="orders",
        sql_expression="COUNT(DISTINCT customer_id)",
        time_field="ordered_at",
        filters="",
        aliases=["下单客户数", "有多少客户在买", "活跃客户", "买家人数"],
    ),
    "items_sold": Metric(
        key="items_sold",
        name="销量",
        description="卖出的商品件数（订单明细行数，每个 SKU 一行）",
        table="items",
        sql_expression="COUNT(items.id)",
        time_field="",
        filters="",
        aliases=["卖了多少件", "销售件数", "售出多少", "商品销量", "卖", "销量"],
    ),
}


# ─── 反向匹配算法 ────────────────────────────────────────────

def match_domains(question: str) -> list[Domain]:
    """
    反向匹配：用户问题 → 哪些域相关？
    做法：问题里命中任一域关键词即算相关。
    """
    hits = []
    for d in DOMAINS:
        if any(kw in question for kw in d.keywords):
            hits.append(d)
    return hits if hits else DOMAINS  # 没命中就返回全部（兜底）


def match_tables(question: str, domains: list[Domain],
                 metrics: list[Metric] | None = None) -> list[Table]:
    """
    反向匹配：用户问题 + 域 + 指标 → 哪些表相关？
    两种来源合并：
      1. 描述关键词命中（"门店" → stores）
      2. 指标依赖的表（"销售额"命中 sales → 必须带出 orders）
    门禁：is_visible 必须为 True。
    """
    domain_keys = {d.key for d in domains}
    keyword_hits = []
    for t in TABLES:
        if not t.is_visible:
            continue  # 门禁 1：不可见的表直接跳过
        if t.domain_key not in domain_keys:
            continue  # 不在用户问的域里
        if any(kw in question for kw in t.keywords):
            keyword_hits.append(t)

    # 指标依赖的表（指标是业务问题的锚点：问"销售额"就必须有 orders 表）
    metric_tables = []
    for m in (metrics or []):
        for t in TABLES:
            if t.name == m.table and t.is_visible and t not in metric_tables:
                metric_tables.append(t)

    merged = keyword_hits + [t for t in metric_tables if t not in keyword_hits]
    # 兜底：一个都没命中 → 返回域内全部可见表
    return merged if merged else [t for t in TABLES
                                  if t.is_visible and t.domain_key in domain_keys]


def match_metrics(question: str, tables: list[Table] | None = None) -> list[Metric]:
    """
    反向匹配：用户问题 → 哪些指标相关？
    全范围匹配（不看表），命中就带出对应表；没命中返回空，
    让表关键词命中来兜底（避免"客单价"这种反直觉匹配落空）。
    """
    hits = []
    for m in METRICS.values():
        all_keywords = [m.name] + m.aliases
        if any(kw in question for kw in all_keywords):
            hits.append(m)
    return hits


def build_context(question: str) -> dict:
    """
    一键调用：用户问题 → 汇总所有相关元数据
    这就是给 LLM 的"上下文"——告诉它有哪些表、哪些指标可用。
    """
    domains = match_domains(question)
    metrics = match_metrics(question)          # 先指标：业务问题的锚点
    tables = match_tables(question, domains, metrics)  # 后表：关键词 + 指标依赖
    return {
        "domains": [{"key": d.key, "name": d.name, "description": d.description} for d in domains],
        "tables": [{"name": t.name, "description": t.description, "columns": t.columns} for t in tables],
        "metrics": [{"key": m.key, "name": m.name, "sql_expression": m.sql_expression,
                     "table": m.table, "time_field": m.time_field, "filters": m.filters}
                    for m in metrics],
    }


# ─── 自测 ────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    for q in ["各门店的销售额是多少？", "最近30天订单量怎么样", "客单价是多少",
              "哪个商品卖得最好？", "有多少客户在买东西？"]:
        ctx = build_context(q)
        print(f"{q!r:28} → tables={[t['name'] for t in ctx['tables']]} "
              f"metrics={[m['key'] for m in ctx['metrics']]}")
