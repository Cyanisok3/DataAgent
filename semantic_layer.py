"""
semantic_layer.py —— 语义层：域 → 表 → 指标三层元数据 + 反向匹配

对应 L3 学的：
  - 三层索引：Domain / Table / Metric
  - 反向匹配：用户问题 → 关键词 → 命中哪层 → 选出相关表/指标
  - "不用向量召回"：纯关键词/元数据匹配

这层是项目最有技术含量的部分，也是你简历的核心。
"""
from dataclasses import dataclass, field


# ─── 数据结构 ────────────────────────────────────────────────

@dataclass
class Domain:
    """数据域：业务领域的抽象（如"交易域"）"""
    key: str          # 机器名：trade
    name: str         # 中文名：交易域
    description: str  # 描述：订单、支付相关数据


@dataclass
class Table:
    """物理表：数据库里真实存在的表"""
    name: str              # 表名：orders
    description: str       # 业务描述：订单表
    domain_key: str        # 属于哪个域
    columns: list[str]     # 列名
    is_visible: bool       # 是否对用户可见（门禁 1）


@dataclass
class Metric:
    """指标口径：业务定义的计算逻辑（如"销售额"）"""
    key: str            # 机器名：sales
    name: str           # 中文名：销售额
    description: str    # 描述：已支付订单的总金额
    table: str          # 来自哪张表
    sql_expression: str # 计算表达式：SUM(paid_amount)
    time_field: str     # 时间字段：settle_time
    filters: str        # 过滤条件：status='paid' AND is_test=0
    aliases: list[str] = field(default_factory=list)  # 别名：用户可能说的其他说法


# ─── 元数据（对应项目的 domain_info / table_info / metric_info）───

DOMAINS = [
    Domain(key="trade", name="交易域", description="订单、支付、退款相关数据"),
    Domain(key="user",  name="用户域", description="用户注册、会员等级、行为数据"),
    Domain(key="goods", name="商品域", description="商品信息、库存、品类数据"),
]

TABLES = [
    Table(
        name="orders",
        description="订单表，记录每一笔交易",
        domain_key="trade",
        columns=["order_id", "user_id", "paid_amount", "status", "is_test", "settle_time", "region"],
        is_visible=True,
    ),
    Table(
        name="users",
        description="用户表，记录注册用户信息",
        domain_key="user",
        columns=["user_id", "register_time", "level", "city"],
        is_visible=True,
    ),
    Table(
        name="goods",
        description="商品表，记录商品信息",
        domain_key="goods",
        columns=["goods_id", "name", "category", "price", "stock"],
        is_visible=True,
    ),
]

METRICS = {
    "sales": Metric(
        key="sales",
        name="销售额",
        description="已支付订单的总金额（排除测试单）",
        table="orders",
        sql_expression="SUM(paid_amount)",
        time_field="settle_time",
        filters="status='paid' AND is_test=0",
        aliases=["营收", "收入", "卖了多少钱"],
    ),
    "order_count": Metric(
        key="order_count",
        name="订单量",
        description="已支付订单的笔数",
        table="orders",
        sql_expression="COUNT(*)",
        time_field="settle_time",
        filters="status='paid' AND is_test=0",
        aliases=["订单数", "多少订单", "订单笔数"],
    ),
}


# ─── 反向匹配算法 ────────────────────────────────────────────

def match_domains(question: str) -> list[Domain]:
    """
    反向匹配：用户问题 → 哪些域相关？
    做法：在域的 description 里找关键词命中。
    """
    hits = []
    for d in DOMAINS:
        # 简单关键词匹配：问题里包含域描述里的某个词就算命中
        # （真实项目会做分词、同义词、拼音等，这里最小实现）
        keywords = d.description.replace("，", " ").replace("、", " ").split()
        if any(kw in question for kw in keywords):
            hits.append(d)
    return hits if hits else DOMAINS  # 没命中就返回全部（兜底）


def match_tables(question: str, domains: list[Domain]) -> list[Table]:
    """
    反向匹配：用户问题 + 域 → 哪些表相关？
    做法：先按域过滤，再在表的 description 里找关键词。
    门禁：is_visible 必须为 True。
    """
    domain_keys = {d.key for d in domains}
    hits = []
    for t in TABLES:
        if not t.is_visible:
            continue  # 门禁 1：不可见的表直接跳过
        if t.domain_key not in domain_keys:
            continue  # 不在用户问的域里
        # 在表描述里找关键词
        if any(kw in question for kw in t.description.replace("，", " ").split()):
            hits.append(t)
    return hits if hits else [t for t in TABLES if t.is_visible and t.domain_key in domain_keys]


def match_metrics(question: str, tables: list[Table]) -> list[Metric]:
    """
    反向匹配：用户问题 + 表 → 哪些指标相关？
    做法：在指标的 name/description 里找关键词。
    """
    table_names = {t.name for t in tables}
    hits = []
    for m in METRICS.values():
        if m.table not in table_names:
            continue
        # 在指标名、别名、描述里找关键词
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
    tables = match_tables(question, domains)
    metrics = match_metrics(question, tables)
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

    print("=== 问：'各区域的销售额是多少？' ===")
    ctx = build_context("各区域的销售额是多少？")
    print(json.dumps(ctx, ensure_ascii=False, indent=2))

    print("\n=== 问：'最近有多少订单？' ===")
    ctx = build_context("最近有多少订单？")
    print(json.dumps(ctx, ensure_ascii=False, indent=2))
