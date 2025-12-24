"""
文本处理工具
"""
import re
from typing import List


def clean_text(text: str) -> str:
    """
    清理提取的文本，移除乱码和无效字符
    
    保留：
    - ASCII 可打印字符（32-126）
    - 中文字符（CJK统一汉字、扩展A、符号和标点）
    - 全角字符
    """
    if not text:
        return text

    # 移除替换字符（U+FFFD，通常表示编码错误）
    text = text.replace('\uFFFD', '')

    result = []
    for char in text:
        # 保留换行符、制表符等
        if char in ['\n', '\t', '\r']:
            result.append(char)
            continue

        # 保留 ASCII 可打印字符
        if 32 <= ord(char) < 127:
            result.append(char)
            continue

        # 保留中文字符和常见标点
        code = ord(char)
        if (0x4E00 <= code <= 0x9FFF or  # CJK统一汉字
            0x3400 <= code <= 0x4DBF or  # CJK扩展A
            0x3000 <= code <= 0x303F or  # CJK符号和标点
            0xFF00 <= code <= 0xFFEF):  # 全角字符
            result.append(char)
            continue

        # 保留其他有效的 Unicode 字符（排除乱码）
        # 乱码字符范围：0xFFF0-0xFFFF, 0xD800-0xDFFF
        if (0x80 <= code and
            not (0xFFF0 <= code <= 0xFFFF) and
            not (0xD800 <= code <= 0xDFFF)):
            result.append(char)

    return ''.join(result)


def is_all_text_valid(text: str) -> bool:
    """
    检查提取的文本是否有效（不是主要是控制字符）
    """
    if not text:
        return False

    printable_count = 0
    total_chars = 0

    for char in text:
        total_chars += 1
        code = ord(char)
        # 可打印字符：ASCII 可打印字符（32-126）或 Unicode 字符（>= 0x80）
        if (32 <= code < 127) or (code >= 0x80):
            printable_count += 1

    # 如果可打印字符占比小于 30%，认为文本无效
    if total_chars > 0:
        return (printable_count / total_chars) >= 0.3

    return False


def is_contain_garbled_text(text: str) -> bool:
    """
    检测文本是否是乱码
    
    乱码特征：包含大量特殊符号（如 #, $, %, &, *, +, -, /, <, =, > 等），
    但没有中文字符，且不包含常见的发票关键词
    """
    if not text:
        return False

    # 检查是否包含中文字符
    has_chinese = bool(re.search(r'[\u4e00-\u9fa5]', text))

    # 检查是否包含常见的发票关键词
    invoice_keywords = ['发票', '号码', '日期', '购买方', '项目', '名称', '数量', '单价', '金额', '税率', '税额', '合计']
    has_invoice_keywords = any(keyword in text for keyword in invoice_keywords)

    # 统计特殊符号的数量
    special_chars = '#$%&*+-/<=>'
    special_char_count = sum(1 for c in text if c in special_chars)
    total_chars = len(text)

    # 如果包含中文字符或发票关键词，认为不是乱码
    if has_chinese or has_invoice_keywords:
        return False

    # 如果特殊符号占比超过 20%，且没有中文字符和关键词，可能是乱码
    if total_chars > 0 and (special_char_count / total_chars) > 0.2:
        return True

    return False


def clean_garbled_chars(text: str) -> str:
    """
    清理乱码字符（用于单元格文本清理）
    """
    if not text:
        return text
    # 移除常见的乱码字符
    return text.replace('\uFFFD', '').strip()


def reorganize_text(text: str) -> str:
    """
    智能重组提取的文本
    
    pdfplumber 提取的文本可能不完整，需要合并相关行
    更好地识别哪些行应该合并（特别是项目明细行）
    """
    if not text:
        return text

    lines = text.split('\n')
    reorganized_lines = []
    current_line = []

    # 用于跟踪是否在处理明细行区域
    in_detail_section = False

    # 表头关键词
    basic_info_keywords = ['发票号码', '开票日期', '购买方', '销售方']
    table_keywords = ['项目名称', '规格型号', '单位', '数量', '单价', '金额', '税率', '税额']
    other_keywords = ['合计', '价税合计', '备注', '开票人']
    header_keywords = basic_info_keywords + table_keywords + other_keywords

    for line in lines:
        line = line.strip()
        if not line:
            # 空行：如果当前行有内容，保存并重置
            if current_line:
                reorganized_lines.append(' '.join(current_line))
                current_line = []
            continue

        # 检查是否是表头行（包含"备注"关键词的表头）
        is_remark_header = ('备注' in line and
                           any(kw in line for kw in ['项目名称', '规格型号', '单位', '数量']))

        # 检查是否是备注行
        is_remark_line = False
        if not is_remark_header:
            # 包含"备注"关键词，但不是表头
            has_remark_keyword = (re.search(r'备注', line) and
                                 not line.startswith('免税') and
                                 line != '免税')
            if has_remark_keyword and not line.startswith('备注'):
                is_remark_line = True
            elif in_detail_section:
                # 在明细行区域，只有日期范围格式才识别为备注
                is_date_range = re.match(r'^\d{4}\.\d{1,2}\.\d{1,2}-\d{4}\.\d{1,2}\.\d{1,2}$', line)
                if is_date_range and not re.search(r'\*[^*]+\*[^*]+', line):
                    is_remark_line = True

        if is_remark_line:
            # 备注行单独处理
            if current_line:
                reorganized_lines.append(' '.join(current_line))
                current_line = []
            # 移除"备注"关键词，只保留备注内容
            remark_content = re.sub(r'^备注[：:]?\s*', '', line).strip()
            if not remark_content:
                remark_content = line
            reorganized_lines.append(f'【备注】{remark_content}')
            continue

        # 检查是否进入明细行区域（包含项目名称模式）
        if re.search(r'\*[^*]+\*[^*]+', line):
            in_detail_section = True

        # 判断是否是新的逻辑行开始
        is_new_line = False

        # 检查是否以关键词开头
        for keyword in header_keywords:
            if line.startswith(keyword):
                is_new_line = True
                # 如果是发票基本信息关键词，确保当前行被保存
                if keyword in basic_info_keywords:
                    if current_line:
                        reorganized_lines.append(' '.join(current_line))
                        current_line = []
                break

        # 如果匹配项目名称模式（*项目名称*费用名称），是新行
        if re.match(r'^\*[^*]+\*[^*]+', line):
            is_new_line = True

        # 如果以日期格式开头（开票日期），是新行
        if re.match(r'^\d{4}年', line):
            is_new_line = True

        # 如果以长数字开头（发票号码），是新行
        if re.match(r'^\d{8,}', line):
            is_new_line = True

        # 如果以¥开头（金额），可能是新行（合计行）
        if line.startswith('¥') and '合计' in line:
            is_new_line = True

        # 关键改进：如果当前行只包含数据或字段值，且上一行包含项目名称，应该合并
        if not is_new_line and current_line:
            current_line_str = ' '.join(current_line)
            # 如果当前累积的行包含项目名称模式，且新行只包含数据或字段值，合并
            if re.search(r'\*[^*]+\*[^*]+', current_line_str):
                # 检查新行是否是备注（只有日期范围格式才识别为备注）
                is_current_line_remark = re.match(r'^\d{4}\.\d{1,2}\.\d{1,2}-\d{4}\.\d{1,2}\.\d{1,2}$', line)
                if not is_current_line_remark:
                    # 不是备注，应该合并到当前行
                    current_line.append(line)
                    continue

        if is_new_line and current_line:
            # 保存当前行，开始新行
            reorganized_lines.append(' '.join(current_line))
            current_line = []

        # 添加到当前行
        current_line.append(line)

    # 添加最后一行
    if current_line:
        reorganized_lines.append(' '.join(current_line))

    return '\n'.join(reorganized_lines)


