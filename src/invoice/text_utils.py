"""
文本处理工具
"""
import re
from typing import List


def clean_garbled_chars(text: str) -> str:
    """
    清理乱码字符（用于单元格文本清理）
    """
    if not text:
        return text
    # 移除常见的乱码字符
    return text.replace('\uFFFD', '').strip()


def reconstruct_text_from_words(words: List[dict]) -> str:
    """
    从单词列表重建文本（按Y-X排序）

    Args:
        words: 单词列表，每个单词包含 'text', 'x0', 'x1', 'top' 等字段

    Returns:
        重建后的文本，按行排列
    """
    if not words:
        return ""

    # 按Y坐标分组（同一行的单词）
    lines_dict = {}
    y_tolerance = 3.0

    for word in words:
        # 使用容差将相近Y坐标的单词归为同一行
        y_key = round(word['top'] / y_tolerance) * y_tolerance
        if y_key not in lines_dict:
            lines_dict[y_key] = []
        lines_dict[y_key].append(word)

    # 按Y坐标排序（从上到下，top值越小越靠上）
    # 在pdfplumber中，top值越大表示越靠下，所以应该从小到大排序
    sorted_lines = sorted(lines_dict.items(), key=lambda x: x[0], reverse=False)

    # 每行内按X坐标排序（从左到右）
    result_lines = []
    for y, line_words in sorted_lines:
        sorted_words = sorted(line_words, key=lambda w: w['x0'])
        line_text = ' '.join([w['text'] for w in sorted_words])
        result_lines.append(line_text)

    return '\n'.join(result_lines)


def normalize_text_whitespace(text: str) -> str:
    """
    规范化文本空白字符

    清理换行符及其周围的空格，将多个连续空格合并为单个空格

    Args:
        text: 待处理的文本

    Returns:
        规范化后的文本
    """
    if not text:
        return text

    # 去掉换行符及其周围的空格
    text = re.sub(r'\s*\n\s*', '', text)
    # 将多个连续空格合并为一个
    text = re.sub(r'\s+', ' ', text)

    return text.strip()
