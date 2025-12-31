"""
数字处理工具
"""
import re
from typing import Optional


def is_tax_id(s: str) -> bool:
    """
    检查是否是统一社会信用代码（18位）
    
    自2015年"三证合一"改革后，新注册的企业、社会团体等均使用18位的"统一社会信用代码"作为税号
    结构：
      - 第1位：登记管理部门代码（9-工商；5-民政；1-机构编制等，最常见的是9）
      - 第2位：机构类别代码（1-企业；2-个体工商户；3-事业单位等）
      - 第3-8位：登记管理机关行政区划码（6位数字，对应注册地的省、市、区）
      - 第9-17位：主体标识码（9位，由阿拉伯数字或大写英文字母组成，即原有的"组织机构代码"）
        注意：可以仅由数字组成，也可以包含字母，但并非必须包含字母
        同时，不使用字母I、O、Z、S、V，以避免与数字混淆
      - 第18位：校验码（数字或字母，通过特定算法生成）
    """
    # 1. 长度必须是18位
    if len(s) != 18:
        return False

    # 2. 检查是否只包含字母和数字
    if not s.isalnum():
        return False

    # 3. 第1位：登记管理部门代码，必须是数字
    if not s[0].isdigit():
        return False

    # 4. 第2位：机构类别代码，必须是数字
    if not s[1].isdigit():
        return False

    # 5. 第3-8位：登记管理机关行政区划码，必须是6位数字
    if not s[2:8].isdigit():
        return False

    # 6. 第9-17位：主体标识码（9位），由阿拉伯数字或大写英文字母组成
    # 不使用字母I、O、Z、S、V
    invalid_letters = {'I', 'O', 'Z', 'S', 'V', 'i', 'o', 'z', 's', 'v'}
    for i in range(8, 17):
        if s[i] in invalid_letters:
            return False

    # 7. 第18位：校验码，可以是数字或字母（已在第2步检查过字符类型）
    if s[17] in invalid_letters:
        return False

    # 8. 整体必须包含字母（确保不是纯数字）
    # 统一社会信用代码必须包含至少一个字母（可能在主体标识码或校验码中）
    if s.isdigit():
        return False  # 纯数字不是有效的统一社会信用代码

    return True


def score_candidate(s: str) -> int:
    """
    简单评分：税号优先，其次长度18，其次长度
    """
    if is_tax_id(s):
        return 100
    if len(s) == 18:
        return 90
    return len(s)

def extract_number(s: str) -> Optional[str]:
    """从字符串中提取数字（包括小数）"""
    # 匹配数字，包括小数
    match = re.search(r'\d+\.?\d*', s)
    if match:
        return match.group()
    return None


