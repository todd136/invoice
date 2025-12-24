"""
发票数据模型定义
"""
from dataclasses import dataclass
from typing import List


@dataclass
class LineItem:
    """商品明细项"""
    item_name: str = ""  # 项目名称
    spec: str = ""  # 规格
    unit: str = ""  # 单位
    quantity: str = ""  # 数量
    price: str = ""  # 单价
    amount: str = ""  # 金额
    tax_rate: str = ""  # 税率
    tax_amount: str = ""  # 税额


@dataclass
class Invoice:
    """发票结构"""
    name: str = ""  # 发票文件名
    code: str = ""  # 发票编码
    date: str = ""  # 日期
    buyer: str = ""  # 购买方
    buyer_tax_id: str = ""  # 购买方统一社会信用代码/纳税人识别号
    invoice_type: str = ""  # 发票类型（普通/增值税专用）
    items: List[LineItem] = None  # 商品明细列表

    def __post_init__(self):
        if self.items is None:
            self.items = []


