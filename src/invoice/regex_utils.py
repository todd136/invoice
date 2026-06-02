"""
正则表达式工具类
统一的正则常量，避免重复编译
"""
import re


# 发票号码正则
INVOICE_CODE_REGEX = re.compile(r'发票(?:号码|编码)?[：:\s]*(\d{12,20})')
INVOICE_CODE_REGEX_LOOSE = re.compile(r'(?s)发票(?:号码|编码)?[^0-9]*(\d{12,20})')

# 日期正则
DATE_REGEX = re.compile(r'开票日期[：:\s]*(\d{4}年\d{1,2}月\d{1,2}日)')
DATE_REGEX_LOOSE = re.compile(r'\d{4}\s*年\s*\d{1,2}\s*[月⽉]\s*\d{1,2}\s*[日⽇]')

# 项目名称模式：*项目名称*费用名称
PROJECT_NAME_REGEX = re.compile(r'\*[^*]+\*[^*]+')

# 项目名称须出现在文本开头（避免规格如 18mm*5y*2.5mm 误匹配）
PROJECT_NAME_AT_START_REGEX = re.compile(r'^\*([^*]+)\*[^*]+')


def has_project_name_at_start(text: str) -> bool:
    """判断 *类别*商品名 是否出现在文本开头（类别须含中文，排除 18mm*5y*2.5mm 等规格）"""
    compact = (text or '').strip().replace(' ', '')
    m = PROJECT_NAME_AT_START_REGEX.match(compact)
    if not m:
        return False
    return bool(re.search(r'[\u4e00-\u9fa5]', m.group(1)))

# 税号模式（包含允许空格的版本，便于从原始文本提取）
TAX_ID_PATTERNS = [
    re.compile(r'统一社会信用代码[：:\s]*([A-Za-z0-9]{15,20})'),
    re.compile(r'纳税人识别号[：:\s]*([A-Za-z0-9]{15,20})'),
    re.compile(r'(?:统一社会信用代码|纳税人识别号)[：:\s]*([A-Za-z0-9]{15,20})'),
    re.compile(r'统一社会信用代码/纳税人识别号[：:\s]*([A-Za-z0-9]{15,20})'),
    re.compile(r'统一社会信用代码[：:\s]*([A-Za-z0-9\s]{15,25})'),
    re.compile(r'纳税人识别号[：:\s]*([A-Za-z0-9\s]{15,25})'),
]

# 通用全局匹配
TAX_ID_GLOBAL_REGEX = re.compile(r'[A-Za-z0-9]{15,20}')
LONG_NUMBER_REGEX = re.compile(r'\d{12,20}')
ROOM_NUMBER_REGEX = re.compile(r'\b\d+-\d+(?:-\d+)?\b')
ROOM_ALPHA_REGEX = re.compile(r'\b[A-Za-z]?\d{3,6}\b')
COMPANY_NAME_REGEX = re.compile(r'([\u4e00-\u9fa5]+(?:（[^）]+）)?[\u4e00-\u9fa5]*(?:公司|企业|有限|股份|集团|个体工商户))')


def normalize_alpha_num(s: str) -> str:
    """
    去除空白和常见分隔符，仅保留字母数字
    """
    if not s:
        return s
    s = s.replace(' ', '').replace('　', '').replace('-', '').replace(':', '').replace('：', '')
    return ''.join(c for c in s if c.isalnum())


